# SPDX-License-Identifier: GPL-3.0-or-later
"""PhoneCam engine: the phone talks to a tiny web server inside Blender.

  phone (Safari / Chrome)  --https-->  camera server  (motion, zoom, buttons; preview JPEGs back)
  phone (first time)       --http--->  setup server   (one-time certificate install, then forwards to the camera)

Blender's main thread does all scene work in a timer (tick); the web threads only store phone input.
Recording keeps the raw phone stream and rebuilds a smooth camera path from it, so a slow scene never makes
a take stutter. Saving renders the take to an MP4 with the scene's own render engine.
"""
import bisect
import hmac
import json
import math
import os
import platform
import secrets
import shutil
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bpy
from mathutils import Quaternion, Vector

from . import netinfo, pki

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
CAM = "PhoneCam"
CA_LABEL = "PhoneCam on " + (platform.node().split(".")[0] or "this computer")

# iPhone-style lenses: 1x = 24 mm main camera, 0.5x ultra wide ... 10x
ZOOM_1X, ZOOM_MIN, ZOOM_MAX = 24.0, 12.0, 240.0
ZOOM_W = 13.0       # zoom smoothing (rad/s); the phone eases the zoom itself, this only irons out network steps
FOCUS_W = 8.0       # focus pull speed
CINE_FSTOP = 2.0    # Cinematic look: shallow depth of field
STAB_SIGMA = {"off": 0.0, "on": 0.07, "action": 0.22}  # seconds of hand-shake smoothing baked into takes
# Fisheye = barrel distortion; the SAME numbers warp the live preview (numpy) and the saved video (ffmpeg)
FISH_K1, FISH_K2 = 0.5, 0.25
VF_W, VF_H, VF_FPS = 480, 270, 30
FOCUS_SKIP = ("smoke", "dust", "fog", "haze", "glow", "beam", "flash", "spark", "volume", "puff")


def _ffmpeg():
    for p in (shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if p and os.path.exists(p):
            return p
    return None


def _fish_zoom(k1=FISH_K1, k2=FISH_K2, aspect=16 / 9):
    """Smallest zoom-in that leaves no empty corners after the barrel warp."""
    w, h = aspect, 1.0
    r0 = 0.5 * math.hypot(w, h)
    need = 1.0
    for i in range(101):
        t = i / 100
        for qx, qy in ((t * w / 2, h / 2), (w / 2, t * h / 2)):
            lo, hi = 1.0, 4.0
            for _ in range(40):
                z = (lo + hi) / 2
                px, py = qx / z, qy / z
                r2 = (px * px + py * py) / (r0 * r0)
                m = 1 + k1 * r2 + k2 * r2 * r2
                if abs(px * m) <= w / 2 + 1e-9 and abs(py * m) <= h / 2 + 1e-9:
                    hi = z
                else:
                    lo = z
            need = max(need, hi)
    return need


FISH_ZOOM = _fish_zoom()

S = {}          # shared state (web threads write phone input; the main thread does all Blender work)
CFG = {}        # settings snapshot taken at start
_srv = {}       # running servers


def _reset_state():
    S.clear()
    S.update(q=None, jx=0.0, jy=0.0, vz=0.0, lens=ZOOM_1X, lens_cur=ZOOM_1X, lens_vel=0.0, lens_rev=0, spot=0, eta=0,
             recenter=True, local_loc=Vector((0, 0, 1.5)), local_rot=Quaternion(), local_q=Quaternion(), yaw=0.0,
             mode="LIVE", shot="ALL", seen=0.0, take=0, frame=1, run=None, samples=[], raw=None, render=0.0, video="",
             msg="", jpg=None, reel=False, fx="normal", stab="on", focus=[0.5, 0.5], focus_d=5.0, focus_cur=5.0,
             focus_vel=0.0, shots=[], spots=[], rev="", fps=24.0, status="Starting…", ready=False, error="",
             phone_name="")


_reset_state()


def connected():
    return time.time() - S.get("seen", 0) < 2.0


# ---------------------------------------------------------------- settings & files
def data_dir():
    return CFG.get("data_dir") or os.path.join(os.path.expanduser("~"), ".phonecam")


def ip():
    """The address the phone should use: picked in the panel, or the best Wi-Fi/Ethernet address."""
    return CFG.get("ip") or netinfo.main_ip()


def camera_url():
    return f"https://{ip()}:{CFG['port']}/?k={CFG['token']}"


def setup_url():
    return f"http://{ip()}:{CFG['setup_port']}/?k={CFG['token']}"


def qr_path():
    return os.path.join(data_dir(), "connect_qr.png")


def write_qr():
    try:
        from .vendor import segno
        segno.make(setup_url(), error="m").save(qr_path(), scale=10, border=2, dark="#111114", light="#ffffff")
        S["qr_rev"] = S.get("qr_rev", 0) + 1
    except Exception as e:
        print("PhoneCam: QR failed:", e)


def _token():
    p = os.path.join(data_dir(), "pairing_key.txt")
    try:
        t = open(p).read().strip()
        if len(t) >= 10:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(9)
    os.makedirs(data_dir(), exist_ok=True)
    with open(p, "w") as f:
        f.write(t)
    return t


def ensure_cert():
    """(Re)issue the https certificate when this computer's addresses change. Returns True if renewed."""
    ips = ["127.0.0.1"] + netinfo.local_ips()
    names = netinfo.host_names()
    want = "|".join(ips + names)
    stamp = os.path.join(data_dir(), "server_sans.txt")
    try:
        if open(stamp).read() == want and os.path.exists(os.path.join(data_dir(), "server.pem")):
            return False
    except OSError:
        pass
    pki.issue_server_cert(data_dir(), CA_LABEL, ips, names)
    with open(stamp, "w") as f:
        f.write(want)
    return True


# ---------------------------------------------------------------- scene helpers
def scene():
    return bpy.context.scene


def cam_obj():
    o = bpy.data.objects.get(CAM)
    if o is None or o.type != "CAMERA":
        cd = bpy.data.cameras.get(CAM) or bpy.data.cameras.new(CAM)
        cd.lens = ZOOM_1X
        cd.clip_end = max(cd.clip_end, 1000.0)
        o = bpy.data.objects.new(CAM, cd)
        scene().collection.objects.link(o)
        src = scene().camera
        if src is not None and src != o:
            o.matrix_world = src.matrix_world.copy()
        else:
            o.location = (0.0, -8.0, 1.7)
            o.rotation_mode = "QUATERNION"
            o.rotation_quaternion = (Vector((0, 0, 1.0)) - o.location).to_track_quat("-Z", "Y")
    if o.name not in scene().objects:
        scene().collection.objects.link(o)
    return o


def ride_obj():
    o = getattr(scene(), "phonecam_ride", None)
    return o if o is not None and o.name in scene().objects else None


def heading(q):
    v = q @ Vector((0, 0, -1))
    return math.atan2(v.y, v.x)


def place_in_ride(cam, ride, q_local):
    mw = ride.matrix_world
    cam.location = mw @ S["local_loc"]
    cam.rotation_mode = "QUATERNION"
    cam.rotation_quaternion = mw.to_quaternion() @ q_local


def refresh_scene_lists():
    """Shots = timeline markers (or the whole timeline); camera spots = the scene's other cameras."""
    sc = scene()
    S["fps"] = sc.render.fps / (sc.render.fps_base or 1.0)
    marks = sorted((m for m in sc.timeline_markers if sc.frame_start <= m.frame <= sc.frame_end), key=lambda m: m.frame)
    shots = []
    if marks and marks[0].frame > sc.frame_start:
        shots.append(dict(id="0", name="Opening", start=sc.frame_start, end=marks[0].frame - 1))
    for i, m in enumerate(marks):
        end = marks[i + 1].frame - 1 if i + 1 < len(marks) else sc.frame_end
        if end >= m.frame:
            shots.append(dict(id=str(i + 1), name=m.name or f"Shot {i + 1}", start=m.frame, end=end))
    shots.append(dict(id="ALL", name="Whole timeline" if marks else "Timeline", start=sc.frame_start, end=sc.frame_end))
    home = S.get("home")
    spots = [dict(name="Start", loc=home[0], q=home[1], lens=home[2])] if home else []
    for o in sorted((o for o in sc.objects if o.type == "CAMERA" and o.name != CAM), key=lambda o: o.name):
        mw = o.matrix_world
        spots.append(dict(name=o.name, loc=tuple(mw.translation), q=tuple(mw.to_quaternion()),
                          lens=o.data.lens if o.data.type == "PERSP" else ZOOM_1X))
    rev = "|".join(f"{s['id']}:{s['name']}:{s['start']}-{s['end']}" for s in shots) + "#" + "|".join(s["name"] for s in spots)
    S["shots"], S["spots"] = shots, spots
    if rev != S["rev"]:
        S["rev"] = rev
    if not any(s["id"] == S["shot"] for s in shots):
        S["shot"] = shots[0]["id"]


def shot():
    for s in S["shots"]:
        if s["id"] == S["shot"]:
            return s
    return S["shots"][0] if S["shots"] else dict(id="ALL", name="Timeline", start=scene().frame_start, end=scene().frame_end)


def view3ds():
    out = []
    for win in bpy.context.window_manager.windows:
        for a in win.screen.areas:
            if a.type == "VIEW_3D":
                regs = [r for r in a.regions if r.type == "WINDOW"]
                if regs:
                    out.append((win, a, regs[0]))
    return sorted(out, key=lambda v: -v[1].width * v[1].height)


def view3d():
    v = view3ds()
    return v[0] if v else None


def look_through_phonecam():
    for win, a, r in view3ds()[:1]:
        sp = a.spaces.active
        sp.use_local_camera = True
        sp.camera = cam_obj()
        sp.region_3d.view_perspective = "CAMERA"


def stop_blender_playback():
    for w in bpy.context.window_manager.windows:
        if w.screen.is_animation_playing:
            v = view3d()
            if v:
                with bpy.context.temp_override(window=v[0], area=v[1], region=v[2]):
                    bpy.ops.screen.animation_cancel(restore_frame=False)
            return


# ---------------------------------------------------------------- smoothing
def crit_damp(x, v, tgt, w, dt):
    """Exact critically-damped spring step: stable for any frame time, never overshoots."""
    e = math.exp(-w * dt)
    d = x - tgt
    tmp = (v + w * d) * dt
    return tgt + (d + tmp) * e, (v - w * tmp) * e


def _pos(v, fallback):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return fallback
    return v if math.isfinite(v) and v > 0 else fallback


def smooth_zoom(dt):
    tgt = math.log(min(ZOOM_MAX, max(ZOOM_MIN, _pos(S["lens"], ZOOM_1X))))
    cur = _pos(S["lens_cur"], 0.0)
    if cur <= 0 or not math.isfinite(S["lens_vel"]):
        x, S["lens_vel"] = tgt, 0.0
    else:
        x = math.log(cur)
    x, S["lens_vel"] = crit_damp(x, S["lens_vel"], tgt, ZOOM_W, dt)
    S["lens_cur"] = math.exp(min(math.log(ZOOM_MAX), max(math.log(ZOOM_MIN), x)))
    return S["lens_cur"]


def apply_pose(dt):
    cam = cam_obj()
    if S["q"] is None:
        return
    qp = Quaternion(S["q"])
    cam.rotation_mode = "QUATERNION"
    ride = ride_obj()
    if ride:  # riding along: aim and walk relative to the vehicle
        if S["recenter"]:
            S["yaw"] = heading(S["local_rot"]) - heading(qp)
            S["recenter"] = False
        ql = Quaternion((0, 0, 1), S["yaw"]) @ qp
        S["local_q"] = ql
        h = heading(ql)
        fwd, right = Vector((math.cos(h), math.sin(h), 0)), Vector((math.sin(h), -math.cos(h), 0))
        S["local_loc"] += (fwd * S["jy"] + right * S["jx"]) * 1.2 * dt
        S["local_loc"].z = max(0.3, min(4.0, S["local_loc"].z + S["vz"] * 0.8 * dt))
        place_in_ride(cam, ride, ql)
    else:
        if S["recenter"]:  # keep the camera's heading; tilt and roll always follow the phone
            S["yaw"] = heading(cam.matrix_world.to_quaternion()) - heading(qp)
            S["recenter"] = False
        q = Quaternion((0, 0, 1), S["yaw"]) @ qp
        cam.rotation_quaternion = q
        h = heading(q)
        fwd, right = Vector((math.cos(h), math.sin(h), 0)), Vector((math.sin(h), -math.cos(h), 0))
        cam.location += (fwd * S["jy"] + right * S["jx"]) * CFG.get("walk_speed", 3.0) * dt
        cam.location.z += S["vz"] * 1.5 * dt
    cam.data.lens = smooth_zoom(dt)


def focus_ray():
    cam, sc = cam_obj(), scene()
    dg = bpy.context.evaluated_depsgraph_get()
    u, v = S["focus"]
    tr, br, bl, tl = cam.data.view_frame(scene=sc)
    p = tl.lerp(tr, u).lerp(bl.lerp(br, u), v)
    mw = cam.matrix_world
    origin = mw.translation.copy()
    d = (mw.to_3x3() @ p).normalized()
    fwd = (mw.to_3x3() @ Vector((0, 0, -1))).normalized()
    o = origin
    for _ in range(6):
        hit, loc, _n, _i, ob, _m = sc.ray_cast(dg, o, d, distance=2000)
        if not hit:
            return None
        if ob is not None and any(k in ob.name.lower() for k in FOCUS_SKIP):
            o = loc + d * 0.05
            continue
        return max(0.2, (loc - origin).dot(fwd))
    return None


def update_focus(dt, now):
    cam = cam_obj()
    cine = S["fx"] == "cinema"
    if cam.data.dof.use_dof != cine:
        cam.data.dof.use_dof = cine
        cam.data.dof.aperture_fstop = CINE_FSTOP
    if not cine:
        return
    if now - S.get("_af_t", 0) > 1 / 15:
        S["_af_t"] = now
        d = focus_ray()
        if d is not None:
            S["focus_d"] = d
    tgt = math.log(min(2000.0, max(0.2, _pos(S["focus_d"], 5.0))))
    cur = _pos(S["focus_cur"], 0.0)
    x = math.log(cur) if cur > 0 and math.isfinite(S["focus_vel"]) else tgt
    if cur <= 0:
        S["focus_vel"] = 0.0
    x, S["focus_vel"] = crit_damp(x, S["focus_vel"], tgt, FOCUS_W, dt)
    S["focus_cur"] = math.exp(min(math.log(2000.0), max(math.log(0.2), x)))
    cam.data.dof.focus_distance = S["focus_cur"]


# ---------------------------------------------------------------- spots, shots, format
def clear_take(cam):
    for id_ in (cam, cam.data):
        if id_.animation_data:
            id_.animation_data.action = None


def jump_to_spot(i):
    cam = cam_obj()
    spots = S["spots"]
    if not spots:
        return
    i = max(0, min(i, len(spots) - 1))
    sp = spots[i]
    lens = ZOOM_MIN if S["fx"] == "fisheye" else min(ZOOM_MAX, max(ZOOM_MIN, sp["lens"]))
    q = Quaternion(sp["q"])
    ride = ride_obj()
    if ride:
        inv = ride.matrix_world.inverted()
        S["local_loc"] = inv @ Vector(sp["loc"])
        S["local_rot"] = S["local_q"] = inv.to_quaternion() @ q
        place_in_ride(cam, ride, S["local_rot"])
    else:
        cam.location = sp["loc"]
        cam.rotation_mode = "QUATERNION"
        cam.rotation_quaternion = q
    S.update(spot=i, lens=float(lens), lens_cur=float(lens), lens_vel=0.0, lens_rev=S["lens_rev"] + 1, recenter=True)
    cam.data.lens = lens


def apply_format():
    """Wide (16:9) or vertical (9:16): Blender's camera frame, the preview and the saved video all follow."""
    on = S["reel"]
    S["_reel_on"] = on
    r = scene().render
    w, h = video_size(on)
    r.resolution_x, r.resolution_y, r.resolution_percentage = w, h, 100


def video_size(vertical):
    big, small = (1920, 1080) if CFG.get("res") == "1080" else (1280, 720)
    return (small, big) if vertical else (big, small)


def goto_shot_start():
    clear_take(cam_obj())
    scene().frame_set(shot()["start"])
    jump_to_spot(S["spot"] if S["spot"] < len(S["spots"]) else 0)


# ---------------------------------------------------------------- recording
def begin_run(mode):
    s = shot()
    stop_blender_playback()
    cam = cam_obj()
    clear_take(cam)
    scene().frame_set(s["start"])
    ride = ride_obj()
    S["rec0"] = dict(loc=(S["local_loc"].copy() if ride else cam.location.copy()),
                     q=(S["local_q"].copy() if ride else cam.rotation_quaternion.copy()),
                     local_rot=S["local_rot"].copy(), yaw=S["yaw"], recenter=bool(S["recenter"]), q_phone=S["q"],
                     lens=_pos(S["lens_cur"], ZOOM_1X), lens_vel=S["lens_vel"], lens_tgt=_pos(S["lens"], ZOOM_1X),
                     jx=S["jx"], jy=S["jy"], vz=S["vz"])
    S["raw"] = []
    S.update(mode=mode, run={"a": s["start"], "b": s["end"], "t0": time.time()}, samples=[], msg="", video="",
             take_ride=ride.name if ride else "", take_reel=bool(S.get("_reel_on")), take_fx=S["fx"], take_stab=S["stab"],
             take_shot=s["name"])


def capture_sample(t):
    cam = cam_obj()
    if S.get("take_ride"):
        S["samples"].append((t, S["local_loc"].copy(), S["local_q"].copy(), cam.data.lens, S["focus_cur"]))
    else:
        S["samples"].append((t, cam.location.copy(), cam.rotation_quaternion.copy(), cam.data.lens, S["focus_cur"]))


def rebuild_from_phone(n, fps):
    """Replay the recorded phone stream through the live-camera rules at 5x the frame rate -> one pose per frame."""
    R, r0 = list(S.get("raw") or []), S.get("rec0")
    if not r0 or len(R) < 8 or R[-1][0] < 0.3:
        return None
    ride = bool(S.get("take_ride"))
    times = [x[0] for x in R]
    qs, last = [], (Quaternion(r0["q_phone"]) if r0["q_phone"] else None)
    for x in R:
        if x[1] is not None:
            last = Quaternion(x[1])
        qs.append(last)
    for i in range(1, len(qs)):
        if qs[i] is not None and qs[i - 1] is not None and qs[i].dot(qs[i - 1]) < 0:
            qs[i] = -qs[i]
    pos, qcur, yaw, need = r0["loc"].copy(), r0["q"].copy(), r0["yaw"], r0["recenter"]
    lens, lv, tgt = max(ZOOM_MIN, r0["lens"]), r0["lens_vel"], r0["lens_tgt"]
    jx, jy, vz = r0["jx"], r0["jy"], r0["vz"]
    walk = CFG.get("walk_speed", 3.0)
    SUB = 5
    dt = 1.0 / (fps * SUB)
    L, Q, N = [], [], []
    j = 0
    for step in range(n * SUB + 1):
        t = step * dt
        while j < len(R) and R[j][0] <= t:
            _t, _q, jx, jy, vz, ln, rc = R[j]
            if ln is not None:
                tgt = ln
            if rc:
                need = True
            j += 1
        if j == 0:
            qp = Quaternion(r0["q_phone"]) if r0["q_phone"] else qs[0]
        elif j >= len(R):
            qp = qs[-1]
        else:
            t0_, t1_ = times[j - 1], times[j]
            u = 0.0 if t1_ <= t0_ else min(1.0, max(0.0, (t - t0_) / (t1_ - t0_)))
            a_, b_ = qs[j - 1], qs[j]
            qp = a_.slerp(b_, u) if a_ is not None and b_ is not None else (b_ or a_)
        if qp is not None:
            if need:
                yaw = heading(r0["local_rot"] if ride else qcur) - heading(qp)
                need = False
            qcur = Quaternion((0, 0, 1), yaw) @ qp
            h = heading(qcur)
            fwd, right = Vector((math.cos(h), math.sin(h), 0)), Vector((math.sin(h), -math.cos(h), 0))
            if ride:
                pos += (fwd * jy + right * jx) * 1.2 * dt
                pos.z = max(0.3, min(4.0, pos.z + vz * 0.8 * dt))
            else:
                pos += (fwd * jy + right * jx) * walk * dt
                pos.z += vz * 1.5 * dt
        x, lv = crit_damp(math.log(lens), lv, math.log(min(ZOOM_MAX, max(ZOOM_MIN, tgt))), ZOOM_W, dt)
        lens = math.exp(x)
        if step % SUB == 0:
            L.append(pos.copy()); Q.append(qcur.copy()); N.append(lens)
    return L, Q, N


def bake_take():
    """Turn the take into one keyframe per frame on PhoneCam. Returns the frame range."""
    cam, smp = cam_obj(), S["samples"]
    a = S["run"]["a"]
    fps = S["fps"]
    if len(smp) < 2:
        return None
    for i in range(1, len(smp)):
        if smp[i][2].dot(smp[i - 1][2]) < 0:
            t, l, q, ln, fd = smp[i]
            smp[i] = (t, l, -q, ln, fd)
    times = [x[0] for x in smp]
    n = max(1, int(times[-1] * fps))
    L, Q, N, F = [], [], [], []
    for i in range(n + 1):
        t = i / fps
        j = min(max(bisect.bisect_right(times, t), 1), len(smp) - 1)
        t0, l0, q0, n0, f0 = smp[j - 1]
        t1, l1, q1, n1, f1 = smp[j]
        u = 0.0 if t1 <= t0 else min(1.0, max(0.0, (t - t0) / (t1 - t0)))
        L.append(l0.lerp(l1, u)); Q.append(q0.slerp(q1, u)); N.append(n0 + (n1 - n0) * u); F.append(f0 + (f1 - f0) * u)
    rebuilt = rebuild_from_phone(n, fps)  # better: the full-rate phone stream (Blender lag can't make it stutter)
    if rebuilt:
        L, Q, N = rebuilt
        for i in range(1, len(Q)):
            if Q[i].dot(Q[i - 1]) < 0:
                Q[i] = -Q[i]
    sig = STAB_SIGMA.get(S.get("take_stab", "on"), 0.0) * fps
    if sig > 0.3:  # stabilizer: centred Gaussian smoothing (no lag)
        rad = int(sig * 3) + 1
        wts = [math.exp(-0.5 * (k / sig) ** 2) for k in range(-rad, rad + 1)]
        L2, Q2 = [], []
        for i in range(n + 1):
            acc_l, aw, ax, ay, az, wsum = Vector((0, 0, 0)), 0.0, 0.0, 0.0, 0.0, 0.0
            for k in range(-rad, rad + 1):
                j = i + k
                if 0 <= j <= n:
                    w = wts[k + rad]
                    q = Q[j] if Q[j].dot(Q[i]) >= 0 else -Q[j]
                    acc_l += L[j] * w
                    aw += q.w * w; ax += q.x * w; ay += q.y * w; az += q.z * w
                    wsum += w
            qq = Quaternion((aw, ax, ay, az)); qq.normalize()
            L2.append(acc_l / wsum); Q2.append(qq)
        L, Q = L2, Q2
    clear_take(cam)
    cam.rotation_mode = "QUATERNION"
    ride = bpy.data.objects.get(S.get("take_ride") or "")
    sc = scene()
    cine = S.get("take_fx") == "cinema"
    for i in range(n + 1):
        f = a + i
        if ride:
            sc.frame_set(f)
            mw = ride.matrix_world
            cam.location = mw @ L[i]
            cam.rotation_quaternion = mw.to_quaternion() @ Q[i]
        else:
            cam.location = L[i]
            cam.rotation_quaternion = Q[i]
        cam.data.lens = N[i]
        cam.keyframe_insert("location", frame=f)
        cam.keyframe_insert("rotation_quaternion", frame=f)
        cam.data.keyframe_insert("lens", frame=f)
        if cine:
            cam.data.dof.focus_distance = F[i]
            cam.data.dof.keyframe_insert("focus_distance", frame=f)
    S["take"] += 1
    name = f"PhoneCam_take_{S['take']:02d}"
    for id_ in (cam, cam.data):
        if id_.animation_data and id_.animation_data.action:
            act = id_.animation_data.action
            act.name, act.use_fake_user = name, True
    return a, a + n


def finish_recording():
    rng = bake_take()
    S["run"] = None
    if not rng:
        S.update(mode="LIVE", msg="Too short — record a little longer")
        goto_shot_start()
        return
    S.update(mode="REVIEW", take_range=rng, run={"a": rng[0], "b": rng[1], "t0": time.time()})
    scene().frame_set(rng[0])


def discard_take():
    cam = cam_obj()
    acts = {id_.animation_data.action for id_ in (cam, cam.data) if id_.animation_data and id_.animation_data.action}
    clear_take(cam)
    for act in acts:
        if act.users <= 1:
            bpy.data.actions.remove(act)


# ---------------------------------------------------------------- saving the video
def _safe(name):
    keep = "".join(c if c.isalnum() or c in " -_()" else " " for c in name).strip()
    return " ".join(keep.split()) or "Shot"


def _prefix(reel, fx):
    tag = {"fisheye": " - Fisheye", "cinema": " - Cinematic"}.get(fx, "")
    return f"{_safe(S.get('take_shot', 'Shot'))}{' - Vertical' if reel else ''}{tag} - take "


def _next_number(prefix):
    import re
    pat = re.compile(re.escape(prefix) + r"(\d+)")
    try:
        names = os.listdir(CFG["out_dir"])
    except OSError:
        names = []
    return max((int(m.group(1)) for f in names for m in [pat.match(f)] if m), default=0) + 1


def save_video(a, b):
    sc, cam = scene(), cam_obj()
    out = CFG["out_dir"]
    os.makedirs(out, exist_ok=True)
    r, im = sc.render, sc.render.image_settings
    prefs = bpy.context.preferences.view
    S["_restore"] = {"markers": [(m, m.camera) for m in sc.timeline_markers], "cam": sc.camera,
                     "range": (sc.frame_start, sc.frame_end), "path": r.filepath,
                     "res": (r.resolution_x, r.resolution_y, r.resolution_percentage),
                     "display": prefs.render_display_type, "mblur": (r.use_motion_blur, r.motion_blur_shutter),
                     "dof": cam.data.dof.use_dof, "simplify": r.use_simplify,
                     "media": getattr(im, "media_type", None), "fmt": im.file_format,
                     "ff": (r.ffmpeg.format, r.ffmpeg.codec, r.ffmpeg.constant_rate_factor)}
    cam.rotation_mode = "QUATERNION"
    for m in sc.timeline_markers:
        m.camera = cam  # markers bound to other cameras would switch cameras mid-render
    sc.camera = cam
    sc.frame_start, sc.frame_end = a, b
    reel, fx = bool(S.get("take_reel")), S.get("take_fx", "normal")
    r.resolution_x, r.resolution_y = video_size(reel)
    r.resolution_percentage = 100
    if CFG.get("motion_blur", True):
        r.use_motion_blur, r.motion_blur_shutter = True, 0.5
    r.use_simplify = False
    cam.data.dof.use_dof = fx == "cinema"
    cam.data.dof.aperture_fstop = CINE_FSTOP
    if hasattr(im, "media_type"):
        im.media_type = "VIDEO"
    im.file_format = "FFMPEG"
    r.ffmpeg.format, r.ffmpeg.codec, r.ffmpeg.constant_rate_factor = "MPEG4", "H264", "HIGH"
    r.filepath = os.path.join(out, f"_rendering_take_")
    prefix = _prefix(reel, fx)
    S.update(mode="SAVING", render=0.0, _rtotal=b - a + 1, _rdone=0, _rt0=time.time(), eta=int((b - a + 1) * 0.6),
             _rfile=r.filepath + f"{a:04d}-{b:04d}.mp4", _final=os.path.join(out, f"{prefix}{_next_number(prefix)}.mp4"))
    prefs.render_display_type = "NONE"  # render in the background, no pop-up window
    v = view3d()
    win = v[0] if v else bpy.context.window_manager.windows[0]
    area = v[1] if v else win.screen.areas[0]
    with bpy.context.temp_override(window=win, area=area):
        bpy.ops.render.render("INVOKE_DEFAULT", animation=True)


def on_render_frame(scn, *args):
    if S.get("mode") == "SAVING" and "_rdone" in S:
        S["_rdone"] += 1
        S["render"] = min(0.99, S["_rdone"] / max(1, S["_rtotal"]))
        per = (time.time() - S["_rt0"]) / S["_rdone"]
        S["eta"] = int(per * (S["_rtotal"] - S["_rdone"]))


def _restore_after_render(scn):
    R = S.pop("_restore", None)
    if not R:
        return False
    for m, c in R["markers"]:
        m.camera = c
    scn.camera = R["cam"]
    scn.frame_start, scn.frame_end = R["range"]
    r, im = scn.render, scn.render.image_settings
    r.filepath = R["path"]
    r.resolution_x, r.resolution_y, r.resolution_percentage = R["res"]
    r.use_motion_blur, r.motion_blur_shutter = R["mblur"]
    r.use_simplify = R["simplify"]
    if R["media"] is not None:
        im.media_type = R["media"]
    im.file_format = R["fmt"]
    r.ffmpeg.format, r.ffmpeg.codec, r.ffmpeg.constant_rate_factor = R["ff"]
    cam_obj().data.dof.use_dof = R["dof"]
    bpy.context.preferences.view.render_display_type = R["display"]
    return True


def _finish_video(src, final):
    try:
        os.replace(src, final)
        S.update(video=os.path.basename(final), msg="Saved", render=1.0)
    except OSError as e:
        S.update(video="", msg=f"Video made but could not be renamed: {e}")
    S["mode"] = "LIVE"
    S["cmd"] = "goto"


def fisheye_filter(w, h):
    w2, h2 = int(round(w * FISH_ZOOM / 2)) * 2, int(round(h * FISH_ZOOM / 2)) * 2
    return f"lenscorrection=k1={FISH_K1}:k2={FISH_K2}:i=bilinear,scale={w2}:{h2}:flags=bicubic,crop={w}:{h}"


def _fisheye_job(src, final, w, h):
    tmp = src.replace(".mp4", "_fish.mp4")
    ok = False
    ff = _ffmpeg()
    if ff:
        try:
            p = subprocess.run([ff, "-y", "-loglevel", "error", "-i", src, "-vf", fisheye_filter(w, h), "-c:v", "libx264",
                                "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", tmp],
                               capture_output=True, text=True, timeout=900)
            ok = p.returncode == 0 and os.path.exists(tmp)
        except Exception:
            ok = False
    if ok:
        os.remove(src)
        _finish_video(tmp, final)
    else:
        _finish_video(src, final)
        S["msg"] = "Saved without the fisheye effect (install ffmpeg for fisheye)"


def on_render_complete(scn, *args):
    res = (scn.render.resolution_x, scn.render.resolution_y)
    if not _restore_after_render(scn):
        return
    final = S["_final"]
    k = 2
    while os.path.exists(final):
        final = S["_final"].replace(".mp4", f" ({k}).mp4")
        k += 1
    if S.get("take_fx") == "fisheye":
        S.update(render=0.99, eta=8, msg="Adding the fisheye lens…")
        threading.Thread(target=_fisheye_job, args=(S["_rfile"], final, *res), daemon=True).start()
        return
    _finish_video(S["_rfile"], final)


def on_render_cancel(scn, *args):
    if _restore_after_render(scn):
        S.update(mode="LIVE", msg="Video cancelled", video="")
        S["cmd"] = "goto"


def on_load_post(*args):
    """A different .blend was opened: forget the old take and start fresh."""
    if not _srv:
        return
    S.update(mode="LIVE", run=None, samples=[], raw=None, recenter=True)
    S.pop("home", None)
    S["_need_home"] = True


# ---------------------------------------------------------------- phone buttons
def handle_cmd():
    cmd = S.pop("cmd", None)
    if not cmd:
        return
    if S["mode"] == "SAVING" and cmd != "goto":
        return
    if bpy.app.is_job_running("RENDER"):
        return
    if S["mode"] == "REVIEW":
        if cmd == "save":
            S["run"] = None
            save_video(*S["take_range"])
        elif cmd in ("retake", "rec"):
            S["run"] = None
            discard_take()
            S.update(mode="LIVE", msg="Take deleted. Ready for the next one.")
            goto_shot_start()
        elif cmd == "watch":
            a, b = S["take_range"]
            scene().frame_set(a)
            S["run"] = {"a": a, "b": b, "t0": time.time()}
        elif cmd in ("goto", "spot"):
            S["run"] = None
            discard_take()
            S["mode"] = "LIVE"
            goto_shot_start() if cmd == "goto" else jump_to_spot(S["spot"])
        return
    if cmd == "goto":
        stop_blender_playback()
        S.update(mode="LIVE", run=None)
        goto_shot_start()
    elif cmd == "spot":
        jump_to_spot(S["spot"])
    elif cmd == "practice" and S["mode"] == "LIVE":
        begin_run("PRACTICE")
    elif cmd == "rec" and S["mode"] in ("LIVE", "PRACTICE"):
        begin_run("REC")
    elif cmd == "stop":
        if S["mode"] == "REC":
            finish_recording()
        elif S["mode"] == "PRACTICE":
            S.update(mode="LIVE", run=None)


# ---------------------------------------------------------------- live preview for the phone
_vf = {"off": None, "t": 0.0}
_fish_cache = {}


def fish_index(w, h):
    import numpy as np
    key = (w, h)
    if key not in _fish_cache:
        ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
        cx, cy = (w - 1) / 2, (h - 1) / 2
        r0 = 0.5 * math.hypot(w, h)
        dx, dy = (xs - cx) / FISH_ZOOM, (ys - cy) / FISH_ZOOM
        r2 = (dx * dx + dy * dy) / (r0 * r0)
        m = 1 + FISH_K1 * r2 + FISH_K2 * r2 * r2
        sx = np.clip(np.rint(cx + dx * m), 0, w - 1).astype(np.int64)
        sy = np.clip(np.rint(cy + dy * m), 0, h - 1).astype(np.int64)
        _fish_cache[key] = (sy * w + sx).ravel()
    return _fish_cache[key]


def grab_viewfinder():
    import gpu
    import numpy as np
    cam, v = cam_obj(), view3d()
    if not v:
        S["msg_vf"] = "Open a 3D Viewport in Blender to see the preview"
        return
    S.pop("msg_vf", None)
    win, area, region = v
    w, h = (VF_H, VF_W) if S.get("_reel_on") else (VF_W, VF_H)
    if _vf["off"] is None or (_vf["off"].width, _vf["off"].height) != (w, h):
        if _vf["off"] is not None:
            _vf["off"].free()
        _vf["off"] = gpu.types.GPUOffScreen(w, h)
    off = _vf["off"]
    sc = scene()
    dg = bpy.context.evaluated_depsgraph_get()
    vm = cam.matrix_world.inverted()
    pm = cam.calc_matrix_camera(dg, x=w, y=h)
    sp = area.spaces.active
    show = sp.overlay.show_overlays
    sp.overlay.show_overlays = False
    try:
        with bpy.context.temp_override(window=win, area=area, region=region):
            off.draw_view3d(sc, bpy.context.view_layer, sp, region, vm, pm, do_color_management=True)
    finally:
        sp.overlay.show_overlays = show
    px = np.asarray(off.texture_color.read(), dtype=np.float32).reshape(-1) / 255.0
    if S["fx"] == "fisheye":
        px = px.reshape(-1, 4)[fish_index(w, h)].reshape(-1)
    img = bpy.data.images.get("_phonecam_preview")
    if img is not None and tuple(img.size) != (w, h):
        bpy.data.images.remove(img)
        img = None
    if img is None:
        img = bpy.data.images.new("_phonecam_preview", w, h, alpha=False)
        img.use_fake_user = False
    img.pixels.foreach_set(px)
    path = os.path.join(data_dir(), "_preview.jpg")
    img.filepath_raw, img.file_format = path, "JPEG"
    img.save(quality=72)
    with open(path, "rb") as f:
        S["jpg"] = f.read()


# ---------------------------------------------------------------- the main loop (Blender's main thread)
_last = [time.time()]
_dt_log = []


def _report(e):
    import traceback
    key = repr(e)
    if S.get("_last_err") != key:
        S["_last_err"] = key
        print("PhoneCam:", key, traceback.format_exc(limit=3))


def _redraw_panels():
    for w in bpy.context.window_manager.windows:
        for a in w.screen.areas:
            if a.type == "VIEW_3D":
                for r in a.regions:
                    if r.type == "UI":
                        r.tag_redraw()


def _status_text():
    if S.get("error"):
        return S["error"]
    if not S["ready"]:
        return "Starting… (first time takes a few seconds)"
    m = S["mode"]
    if m == "SAVING":
        return f"Saving video {int(S['render'] * 100)}%"
    if m == "REC":
        return "● Recording"
    if m == "REVIEW":
        return "Phone is reviewing a take"
    if m == "PRACTICE":
        return "Practice run"
    return "Phone connected" if connected() else "Waiting for your phone…"


def tick():
    now = time.time()
    dt, _last[0] = min(now - _last[0], 0.1), now
    if not _srv:
        return None
    try:
        handle_cmd()
        live = connected()
        st = _status_text()
        if st != S["status"] or now - S.get("_ui_t", 0) > 1.0:
            S["status"], S["_ui_t"] = st, now
            _redraw_panels()
        if bpy.app.is_job_running("RENDER"):
            return 1 / 30
        if S.get("_need_home") or "home" not in S:
            cam = cam_obj()
            S["home"] = (tuple(cam.location), tuple(cam.matrix_world.to_quaternion()), cam.data.lens)
            S["_need_home"] = False
        if now - S.get("_list_t", 0) > 1.0:
            S["_list_t"] = now
            refresh_scene_lists()
        if now - S.get("_net_t", 0) > 5.0 and S["mode"] in ("LIVE", "REVIEW") and S["ready"]:
            S["_net_t"] = now
            if ensure_cert():  # joined a different network: new certificate + QR code
                srv = _srv.get("cam")
                if srv is not None:
                    srv.ctx.load_cert_chain(os.path.join(data_dir(), "server.pem"), os.path.join(data_dir(), "server.key"))
                write_qr()
                S["msg"] = "New network detected"
        run = S["run"]
        if run and S["mode"] in ("PRACTICE", "REC", "REVIEW"):
            f = run["a"] + int((now - run["t0"]) * S["fps"])
            if f > run["b"]:
                if S["mode"] == "REVIEW":
                    S["run"] = None
                else:
                    S["cmd"] = "stop"
            elif f != scene().frame_current:
                scene().frame_set(f)
        if S["reel"] != S.get("_reel_on") and S["mode"] == "LIVE":
            apply_format()
        r_ = scene().render
        if live and CFG.get("light_viewport", True) and "_simp" not in S:
            S["_simp"] = (r_.use_simplify, r_.simplify_subdivision)
            r_.use_simplify, r_.simplify_subdivision = True, 1
        elif not live and "_simp" in S and S["mode"] != "SAVING":
            r_.use_simplify, r_.simplify_subdivision = S.pop("_simp")
        if live and CFG.get("follow_view", True) and now - S.get("_look_t", 0) > 0.5:
            S["_look_t"] = now
            cam = cam_obj()
            if any(a.spaces.active.camera != cam or a.spaces.active.region_3d.view_perspective != "CAMERA" for _w, a, _r in view3ds()[:1]):
                look_through_phonecam()
        if S["mode"] in ("LIVE", "PRACTICE", "REC"):
            try:
                if live:
                    apply_pose(dt)
                elif ride_obj():
                    place_in_ride(cam_obj(), ride_obj(), S["local_q"])
                update_focus(dt, now)
            except Exception as e:
                _report(e)
        if run and S["mode"] == "REC":
            capture_sample(now - run["t0"])
        S["frame"] = scene().frame_current
        if live and S["mode"] != "SAVING" and now - _vf["t"] > 1 / VF_FPS:
            _vf["t"] = now
            try:
                grab_viewfinder()
            except Exception as e:
                _report(e)
        _dt_log.append(dt)
        del _dt_log[:-240]
    except Exception as e:
        _report(e)
    return 1 / 60


# ---------------------------------------------------------------- web servers
def _check_key(handler, query):
    k = handler.headers.get("X-PhoneCam-Key", "") or query.get("k", "")
    return hmac.compare_digest(k.encode(), CFG.get("token", "").encode())


def _query(path):
    from urllib.parse import parse_qs, urlparse
    return {k: v[0] for k, v in parse_qs(urlparse(path).query).items()}


class CameraHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PhoneCam"

    def log_message(self, *a):
        pass

    def _send(self, body, ctype, code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        q = _query(self.path)
        if self.path.startswith("/ping"):  # the setup page checks "does the phone trust this computer yet?"
            return self._send(b"ok", "text/plain", extra={"Access-Control-Allow-Origin": "*"})
        if self.path.startswith("/view.jpg"):
            if not _check_key(self, q):
                return self._send(b"", "image/jpeg", 403)
            S["seen"] = time.time()
            jpg = S["jpg"]
            return self._send(jpg, "image/jpeg") if jpg else self._send(b"", "image/jpeg", 204)
        name = "manifest.webmanifest" if self.path.startswith("/manifest") else ("icon.png" if self.path.startswith("/icon") else "phone.html")
        ctype = {"phone.html": "text/html; charset=utf-8", "icon.png": "image/png", "manifest.webmanifest": "application/manifest+json"}[name]
        with open(os.path.join(WEB, name), "rb") as f:
            self._send(f.read(), ctype)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(n) or b"{}"
        if not _check_key(self, _query(self.path)):
            return self._send(b'{"error":"pairing"}', "application/json", 403)
        try:
            msg = json.loads(raw_body)
        except ValueError:
            msg = {}
        S["seen"] = time.time()
        q = msg.get("q")
        if isinstance(q, list) and len(q) == 4 and all(isinstance(c, (int, float)) and math.isfinite(c) for c in q):
            S["q"] = q
        for k in ("jx", "jy", "vz"):
            v = msg.get(k)
            if isinstance(v, (int, float)) and math.isfinite(v):
                S[k] = max(-1.0, min(1.0, float(v)))
        ln = _pos(msg.get("lens"), None)
        if ln is not None:
            S["lens"] = min(ZOOM_MAX, max(ZOOM_MIN, ln))
        run = S["run"]
        if S["mode"] == "REC" and run and S.get("raw") is not None:
            S["raw"].append((time.time() - run["t0"], S["q"] if "q" in msg else None, S["jx"], S["jy"], S["vz"],
                             S["lens"] if ln is not None else None, bool(msg.get("recenter"))))
        if msg.get("recenter"):
            S["recenter"] = True
        if "reel" in msg:
            S["reel"] = bool(msg["reel"])
        if msg.get("fx") in ("normal", "fisheye", "cinema"):
            S["fx"] = msg["fx"] if (msg["fx"] != "fisheye" or _ffmpeg()) else "normal"
        if msg.get("stab") in STAB_SIGMA:
            S["stab"] = msg["stab"]
        f = msg.get("focus")
        if isinstance(f, list) and len(f) == 2:
            try:
                S["focus"] = [min(1.0, max(0.0, float(f[0]))), min(1.0, max(0.0, float(f[1])))]
            except (TypeError, ValueError):
                pass
        if msg.get("cmd") in ("goto", "spot", "practice", "rec", "stop", "save", "retake", "watch"):
            S["cmd"] = msg["cmd"]
            if isinstance(msg.get("spot"), int):
                S["spot"] = msg["spot"]
        if isinstance(msg.get("shot"), str) and any(s["id"] == msg["shot"] for s in S["shots"]):
            S["shot"] = msg["shot"]
        sh = shot()
        fps = S["fps"] or 24.0
        out = {k: S[k] for k in ("mode", "take", "shot", "frame", "render", "video", "msg", "lens", "lens_rev", "spot", "eta", "fx", "stab", "rev")}
        out["af"] = round(S["focus_cur"], 1)
        out["fisheye_ok"] = bool(_ffmpeg())
        out["hint"] = S.get("msg_vf", "")
        tr = S.get("take_range")
        out.update(start=sh["start"], end=sh["end"], elapsed=round(time.time() - run["t0"], 1) if run else 0.0,
                   length=round((sh["end"] - sh["start"] + 1) / fps, 1),
                   take_len=round((tr[1] - tr[0] + 1) / fps, 1) if tr else 0.0, playing=bool(run))
        if msg.get("hello"):
            out["shots"] = [dict(id=s["id"], name=s["name"], secs=round((s["end"] - s["start"] + 1) / fps, 1)) for s in S["shots"]]
            out["spots"] = [s["name"] for s in S["spots"]]
        self._send(json.dumps(out).encode(), "application/json")


class SetupHandler(BaseHTTPRequestHandler):
    """Plain-http helper: hands the phone the computer's certificate once, then forwards to the camera page."""

    def log_message(self, *a):
        pass

    def _send(self, body, ctype, code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        local = self.client_address[0] in ("127.0.0.1", "::1")
        if self.path.startswith(("/PhoneCam-CA.cer", "/PhoneCam-CA.crt")):
            with open(os.path.join(data_dir(), "PhoneCam-CA.cer"), "rb") as f:
                body = f.read()
            disp = "PhoneCam-CA.crt" if self.path.startswith("/PhoneCam-CA.crt") else "PhoneCam-CA.cer"
            return self._send(body, "application/x-x509-ca-cert", extra={"Content-Disposition": f'attachment; filename="{disp}"'})
        if self.path.startswith("/qr.png") and local:
            with open(qr_path(), "rb") as f:
                return self._send(f.read(), "image/png")
        if self.path.startswith("/icon.png"):
            with open(os.path.join(WEB, "icon.png"), "rb") as f:
                return self._send(f.read(), "image/png")
        if self.path.startswith("/connect") and local:  # the computer's own browser: big QR code
            page = open(os.path.join(WEB, "connect.html"), encoding="utf-8").read()
            page = page.replace("{{CAMERA_URL}}", camera_url()).replace("{{SETUP_URL}}", setup_url())
            return self._send(page.encode(), "text/html; charset=utf-8")
        page = open(os.path.join(WEB, "setup.html"), encoding="utf-8").read()
        k = _query(self.path).get("k", "")
        ok = hmac.compare_digest(k.encode(), CFG.get("token", "").encode())
        cam = f"https://{self.headers.get('Host', ip()).split(':')[0]}:{CFG['port']}/?k={CFG['token']}" if ok else ""
        page = page.replace("{{CAMERA_URL}}", cam).replace("{{PING_URL}}", cam.split("/?")[0] + "/ping" if cam else "")
        self._send(page.encode(), "text/html; charset=utf-8")


def _serve(port, handler, tls):
    srv = ThreadingHTTPServer(("0.0.0.0", port), handler)
    srv.daemon_threads = True
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(os.path.join(data_dir(), "server.pem"), os.path.join(data_dir(), "server.key"))
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        srv.ctx = ctx
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _replace_handler(lst, fn):
    for h in list(lst):
        if getattr(h, "__name__", "") == fn.__name__ and getattr(h, "__module__", "") == fn.__module__:
            lst.remove(h)
    lst.append(fn)


def _prepare_and_serve():
    """Runs in a background thread: certificates (a few seconds the very first time), then both servers."""
    try:
        os.makedirs(data_dir(), exist_ok=True)
        ensure_cert()
        _srv["cam"] = _serve(CFG["port"], CameraHandler, tls=True)
        _srv["setup"] = _serve(CFG["setup_port"], SetupHandler, tls=False)
        write_qr()
        S["ready"] = True
        print(f"PhoneCam ready: scan the QR code in Blender's sidebar, or open {setup_url()} on your phone")
    except OSError as e:
        S["error"] = (f"Port {CFG['port']} or {CFG['setup_port']} is busy — change it in PhoneCam settings" if "Address already in use" in str(e)
                      or getattr(e, "errno", 0) in (48, 98, 10048) else f"Could not start: {e}")
        _shutdown_servers()
    except Exception as e:
        S["error"] = f"Could not start: {e}"
        _shutdown_servers()


def _shutdown_servers():
    for k in ("cam", "setup"):
        srv = _srv.pop(k, None)
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass


def start(cfg):
    stop()
    _reset_state()
    CFG.clear()
    CFG.update(cfg)
    os.makedirs(CFG["data_dir"], exist_ok=True)
    CFG["token"] = _token()
    _srv["starting"] = True
    threading.Thread(target=_prepare_and_serve, daemon=True).start()
    _srv.pop("starting", None)
    _srv["running"] = True
    for lst, fn in ((bpy.app.handlers.render_post, on_render_frame), (bpy.app.handlers.render_complete, on_render_complete),
                    (bpy.app.handlers.render_cancel, on_render_cancel), (bpy.app.handlers.load_post, on_load_post)):
        _replace_handler(lst, fn)
    if not bpy.app.timers.is_registered(tick):
        bpy.app.timers.register(tick, first_interval=0.1, persistent=True)


def stop():
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    _shutdown_servers()
    _srv.clear()
    for lst, fn in ((bpy.app.handlers.render_post, on_render_frame), (bpy.app.handlers.render_complete, on_render_complete),
                    (bpy.app.handlers.render_cancel, on_render_cancel), (bpy.app.handlers.load_post, on_load_post)):
        for h in list(lst):
            if getattr(h, "__name__", "") == fn.__name__ and getattr(h, "__module__", "") == fn.__module__:
                lst.remove(h)
    if "_simp" in S:
        try:
            r = scene().render
            r.use_simplify, r.simplify_subdivision = S.pop("_simp")
        except Exception:
            pass
    if _vf["off"] is not None:
        try:
            _vf["off"].free()
        except Exception:
            pass
        _vf["off"] = None
    S["ready"] = False


def running():
    return bool(_srv)
