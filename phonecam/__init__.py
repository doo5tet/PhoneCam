# SPDX-License-Identifier: GPL-3.0-or-later
"""PhoneCam - turn your phone into a handheld camera for Blender.

Open the sidebar (N) in the 3D Viewport -> PhoneCam tab -> Start, then scan the QR code with your phone.
"""
import os
import platform

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty

from . import engine

GUIDE_URL = "https://doo5tet.github.io/PhoneCam/"
_icons = {"pcoll": None, "rev": None}


def default_out_dir():
    return os.path.join(os.path.expanduser("~"), "Movies" if platform.system() == "Darwin" else "Videos", "PhoneCam")


def prefs():
    return bpy.context.preferences.addons[__package__].preferences


def data_dir():
    try:
        return bpy.utils.extension_path_user(__package__, path="", create=True)
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".phonecam")


class PHONECAM_Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    out_dir: StringProperty(name="Save videos to", subtype="DIR_PATH", default=default_out_dir())
    res: EnumProperty(name="Video size", default="720",
                      items=[("720", "720p (fast)", "1280×720 or 720×1280. Saves quickly"),
                             ("1080", "1080p (sharp)", "1920×1080 or 1080×1920. Takes about twice as long to save")])
    motion_blur: BoolProperty(name="Motion blur in saved videos", default=True,
                              description="Real-camera blur on fast moves, so 24 fps looks smooth")
    follow_view: BoolProperty(name="Show the phone's view in Blender", default=True,
                              description="While your phone is connected, the 3D Viewport looks through the PhoneCam camera")
    light_viewport: BoolProperty(name="Lighter viewport while filming", default=True,
                                 description="Uses Simplify while the phone is connected so the preview runs smoother. Saved videos are full quality")
    walk_speed: FloatProperty(name="Walk speed", default=3.0, min=0.2, max=30.0, unit="VELOCITY",
                              description="How fast the on-screen stick moves the camera (metres per second)")
    port: IntProperty(name="Camera port", default=8765, min=1024, max=65535, description="https port for the camera page")
    setup_port: IntProperty(name="Setup port", default=8766, min=1024, max=65535, description="http port for the one-time phone setup page")

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "out_dir")
        col.prop(self, "res")
        col.prop(self, "motion_blur")
        col.prop(self, "follow_view")
        col.prop(self, "light_viewport")
        col.prop(self, "walk_speed")
        row = col.row(align=True)
        row.prop(self, "port")
        row.prop(self, "setup_port")
        col.operator("phonecam.open_guide", icon="HELP")


def _cfg():
    p = prefs()
    out = bpy.path.abspath(p.out_dir) if p.out_dir else default_out_dir()
    try:
        chosen = bpy.context.window_manager.phonecam_ip
    except Exception:
        chosen = "AUTO"
    return dict(port=p.port, setup_port=p.setup_port, out_dir=out, res=p.res, motion_blur=p.motion_blur,
                follow_view=p.follow_view, light_viewport=p.light_viewport, walk_speed=p.walk_speed, data_dir=data_dir(),
                ip="" if chosen in ("AUTO", "", None) else chosen)


class PHONECAM_OT_start(bpy.types.Operator):
    bl_idname = "phonecam.start"
    bl_label = "Start PhoneCam"
    bl_description = "Start the phone connection. Then scan the QR code with your phone"

    def execute(self, context):
        engine.start(_cfg())
        return {"FINISHED"}


class PHONECAM_OT_stop(bpy.types.Operator):
    bl_idname = "phonecam.stop"
    bl_label = "Stop"
    bl_description = "Disconnect the phone and stop PhoneCam"

    def execute(self, context):
        engine.stop()
        return {"FINISHED"}


class PHONECAM_OT_copy_link(bpy.types.Operator):
    bl_idname = "phonecam.copy_link"
    bl_label = "Copy link"
    bl_description = "Copy the phone link, to send it to your phone yourself"

    def execute(self, context):
        context.window_manager.clipboard = engine.setup_url()
        self.report({"INFO"}, "Phone link copied")
        return {"FINISHED"}


class PHONECAM_OT_big_qr(bpy.types.Operator):
    bl_idname = "phonecam.big_qr"
    bl_label = "Big QR code"
    bl_description = "Show a large QR code in your web browser"

    def execute(self, context):
        bpy.ops.wm.url_open(url=f"http://localhost:{engine.CFG.get('setup_port', 8766)}/connect")
        return {"FINISHED"}


class PHONECAM_OT_open_videos(bpy.types.Operator):
    bl_idname = "phonecam.open_videos"
    bl_label = "Open videos folder"
    bl_description = "Show the folder where your recorded videos are saved"

    def execute(self, context):
        out = engine.CFG.get("out_dir") or _cfg()["out_dir"]
        os.makedirs(out, exist_ok=True)
        bpy.ops.wm.path_open(filepath=out)
        return {"FINISHED"}


class PHONECAM_OT_open_guide(bpy.types.Operator):
    bl_idname = "phonecam.open_guide"
    bl_label = "How to use PhoneCam"
    bl_description = "Open the step-by-step guide in your browser"

    def execute(self, context):
        bpy.ops.wm.url_open(url=GUIDE_URL)
        return {"FINISHED"}


_ip_items = []


def _ip_enum(self, context):
    _ip_items.clear()
    _ip_items.append(("AUTO", "Automatic (Wi-Fi)", "Use the best Wi-Fi / Ethernet address"))
    from . import netinfo
    for ip, name in netinfo.candidates():
        _ip_items.append((ip, f"{ip}  ({name})", "Use this address if the phone can't connect"))
    return _ip_items


def _ip_changed(self, context):
    v = context.window_manager.phonecam_ip
    engine.CFG["ip"] = "" if v == "AUTO" else v
    if engine.running() and engine.S.get("ready"):
        engine.write_qr()


# ---------------------------------------------------------------- take tools: smooth keyframes, bake to a world camera
def _fcurves(id_):
    ad = getattr(id_, "animation_data", None)
    if not ad or not ad.action:
        return []
    try:
        from bpy_extras import anim_utils
        cb = anim_utils.action_get_channelbag_for_slot(ad.action, ad.action_slot)
        return list(cb.fcurves) if cb else []
    except Exception:
        return list(getattr(ad.action, "fcurves", []))


def _gauss(vals, sigma):
    import math
    if sigma <= 0.01 or len(vals) < 3:
        return list(vals)
    rad = int(sigma * 3) + 1
    w = [math.exp(-0.5 * (k / sigma) ** 2) for k in range(-rad, rad + 1)]
    out = []
    for i in range(len(vals)):
        a = s_ = 0.0
        for k in range(-rad, rad + 1):
            j = i + k
            if 0 <= j < len(vals):
                a += vals[j] * w[k + rad]; s_ += w[k + rad]
        out.append(a / s_)
    return out


def _take_object(context):
    ob = context.active_object
    if ob is not None and ob.type == "CAMERA" and _fcurves(ob):
        return ob
    ob = bpy.data.objects.get(engine.CAM)
    return ob if ob is not None and _fcurves(ob) else None


class PHONECAM_OT_smooth_keys(bpy.types.Operator):
    bl_idname = "phonecam.smooth_keys"
    bl_label = "Smooth keyframes"
    bl_description = "Smooth the recorded camera move (position, rotation, zoom). Uses the strength below"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        ob = _take_object(context)
        if ob is None:
            self.report({"WARNING"}, "Record a take first (or select a camera with keyframes)")
            return {"CANCELLED"}
        strength = context.scene.phonecam_smooth
        groups = {}
        for id_ in (ob, ob.data):
            for fc in _fcurves(id_):
                if fc.data_path in ("location", "rotation_quaternion", "rotation_euler", "lens", "dof.focus_distance"):
                    groups.setdefault((id(id_), fc.data_path), []).append(fc)
        n_keys = 0
        for (_i, path), fcs in groups.items():
            fcs.sort(key=lambda f: f.array_index)
            pts = [list(fc.keyframe_points) for fc in fcs]
            if not pts or len(pts[0]) < 3:
                continue
            span = max(1e-6, pts[0][-1].co.x - pts[0][0].co.x)
            sigma = strength * (len(pts[0]) - 1) / span  # strength is in frames
            cols = [[k.co.y for k in p] for p in pts]
            if path == "rotation_quaternion" and len(cols) == 4:
                for i in range(1, len(cols[0])):  # keep neighbours on one hemisphere
                    if sum(cols[c][i] * cols[c][i - 1] for c in range(4)) < 0:
                        for c in range(4):
                            cols[c][i] = -cols[c][i]
                cols = [_gauss(c, sigma) for c in cols]
                for i in range(len(cols[0])):
                    nrm = sum(cols[c][i] ** 2 for c in range(4)) ** 0.5 or 1.0
                    for c in range(4):
                        cols[c][i] /= nrm
            else:
                cols = [_gauss(c, sigma) for c in cols]
            for p, c in zip(pts, cols):
                for k, v in zip(p, c):
                    d = v - k.co.y
                    k.co.y = v; k.handle_left.y += d; k.handle_right.y += d
                n_keys += len(p)
            for fc in fcs:
                fc.update()
        self.report({"INFO"}, f"Smoothed {n_keys} keyframes on {ob.name}")
        return {"FINISHED"}


class PHONECAM_OT_bake_world(bpy.types.Operator):
    bl_idname = "phonecam.bake_world"
    bl_label = "Bake to World camera"
    bl_description = "Save the current take as its own camera with world-space keyframes, so new takes never overwrite it"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        src = _take_object(context)
        if src is None:
            self.report({"WARNING"}, "Record a take first")
            return {"CANCELLED"}
        sc = context.scene
        act = src.animation_data.action
        f0, f1 = int(act.frame_range[0]), int(round(act.frame_range[1]))
        name = act.name.replace("PhoneCam_take_", "PhoneCam Take ") if act.name.startswith("PhoneCam_take_") else src.name + " baked"
        cd = src.data.copy()
        ob = bpy.data.objects.new(name, cd)
        (src.users_collection[0] if src.users_collection else sc.collection).objects.link(ob)
        ob.rotation_mode = "QUATERNION"
        if src.parent is None and not src.constraints:  # already world space: copy the keyframes exactly (sub-frame keys too)
            for id_, s_id in ((ob, src), (cd, src.data)):
                if s_id.animation_data and s_id.animation_data.action:
                    a2 = s_id.animation_data.action.copy()
                    a2.name = name + (" cam" if id_ is cd else "")
                    id_.animation_data_create()
                    id_.animation_data.action = a2
                    try:
                        if id_.animation_data.action_slot is None and len(a2.slots):
                            id_.animation_data.action_slot = a2.slots[0]
                    except AttributeError:
                        pass
        else:  # parented / constrained: sample the real world position every frame
            keep = sc.frame_current
            for f in range(f0, f1 + 1):
                sc.frame_set(f)
                ob.matrix_world = src.matrix_world.copy()
                ob.keyframe_insert("location", frame=f); ob.keyframe_insert("rotation_quaternion", frame=f)
                cd.lens = src.data.lens; cd.keyframe_insert("lens", frame=f)
            sc.frame_set(keep)
        self.report({"INFO"}, f"Baked to camera '{name}'")
        return {"FINISHED"}


class PHONECAM_OT_show_viewport(bpy.types.Operator):
    bl_idname = "phonecam.show_viewport"
    bl_label = "Fix"
    bl_description = "Show a 3D Viewport (the phone's live preview is drawn from it)"

    def execute(self, context):
        ws = bpy.data.workspaces.get("Layout")
        if ws is not None:
            context.window.workspace = ws
        else:
            big = max(context.window.screen.areas, key=lambda a: a.width * a.height)
            big.type = "VIEW_3D"
        return {"FINISHED"}


def _preflight(layout):
    from . import netinfo
    col = layout.column(align=True)
    S = engine.S
    on = engine.running()
    ip = engine.ip() if on else netinfo.main_ip()
    rows = [(ip != "127.0.0.1", f"Wi-Fi address {ip}" if ip != "127.0.0.1" else "No Wi-Fi / Ethernet address found", None)]
    if on:
        ok = bool(S.get("ready")) and not S.get("error")
        rows.append((ok, f"Phone link on ports {engine.CFG.get('port')} / {engine.CFG.get('setup_port')}" if ok
                     else (S.get("error") or "Starting the phone link…"), None))
    else:
        rows.append((None, "Click Start PhoneCam to open the phone link", None))
    has3d = bool(engine.view3ds())
    rows.append((has3d, "3D Viewport open (live preview)" if has3d else "No 3D Viewport: the phone preview needs one", None if has3d else "phonecam.show_viewport"))
    rows.append((True, "Camera 'PhoneCam' ready" if bpy.data.objects.get(engine.CAM) else "Camera 'PhoneCam' is made when you start", None))
    sc = bpy.context.scene
    fps = sc.render.fps / (sc.render.fps_base or 1)
    kr = S.get("krate") or 0
    rows.append((True, f"Scene {fps:g} fps · keyframes at {kr if kr else round(fps, 2):g} fps", None))
    ff = engine._ffmpeg()
    rows.append((True if ff else None, "ffmpeg found (Fisheye ready)" if ff else "ffmpeg not found (only needed for Fisheye)", None))
    if on and engine.connected():
        rtt = S.get("rtt")
        rows.append((True, f"Phone connected · {('WebSocket' if S.get('ws') else 'HTTP')}" + (f" · {rtt:.0f} ms" if rtt else ""), None))
    elif on:
        rows.append((None, "Waiting for your phone to connect", None))
    for ok, text, fix in rows:
        r = col.row(align=True)
        r.label(text=text, icon="CHECKMARK" if ok else ("ERROR" if ok is False else "INFO"))
        if fix:
            r.operator(fix)


def _qr_icon():
    rev = engine.S.get("qr_rev")
    path = engine.qr_path()
    if not rev or not os.path.exists(path):
        return None
    pc = _icons["pcoll"]
    key = f"qr{rev}"
    if key not in pc:
        pc.load(key, path, "IMAGE", force_reload=True)
    return pc[key].icon_id


class PHONECAM_PT_main(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "PhoneCam"
    bl_label = "PhoneCam"

    def draw(self, context):
        layout = self.layout
        if not engine.running():
            col = layout.column(align=True)
            col.label(text="Use your phone as a camera", icon="OUTLINER_OB_CAMERA")
            col.separator()
            col.scale_y = 1.0
            big = layout.column()
            big.scale_y = 1.8
            big.operator("phonecam.start", icon="PLAY")
            box = layout.box().column(align=True)
            box.label(text="1  Click Start")
            box.label(text="2  Scan the QR code with your phone")
            box.label(text="3  Phone and computer on the same Wi-Fi")
            layout.operator("phonecam.open_guide", icon="HELP")
            return
        S = engine.S
        box = layout.box()
        live = engine.connected()
        icon = "ERROR" if S.get("error") else ("REC" if S.get("mode") == "REC" else ("CHECKMARK" if live else "TIME"))
        box.label(text=S.get("status", ""), icon=icon)
        if S.get("video"):
            box.label(text=f"Last video: {S['video']}", icon="FILE_MOVIE")
        if S.get("ready") and not S.get("error"):
            if not live:
                qr = _qr_icon()
                if qr:
                    col = layout.column(align=True)
                    col.template_icon(icon_value=qr, scale=9.0)
                    col.label(text="Scan with your phone's camera app")
                layout.prop(context.window_manager, "phonecam_ip", text="Network")
                row = layout.row(align=True)
                row.operator("phonecam.big_qr", icon="ZOOM_IN")
                row.operator("phonecam.copy_link", icon="COPYDOWN")
            else:
                col = layout.column(align=True)
                sh = engine.shot()
                col.label(text=f"Shot: {sh['name']}  (frames {sh['start']}–{sh['end']})", icon="MARKER_HLT")
                col.label(text=f"Lens: {engine.S.get('lens_cur', 24) / engine.ZOOM_1X:.1f}×  ·  {('Vertical 9:16' if S.get('reel') else 'Wide 16:9')}")
        layout.operator("phonecam.open_videos", icon="FILE_FOLDER")
        layout.operator("phonecam.stop", icon="CANCEL")


class PHONECAM_PT_preflight(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "PhoneCam"
    bl_label = "Preflight check"
    bl_parent_id = "PHONECAM_PT_main"

    def draw(self, context):
        _preflight(self.layout)


class PHONECAM_PT_takes(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "PhoneCam"
    bl_label = "Takes"
    bl_parent_id = "PHONECAM_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        col = self.layout.column(align=True)
        ob = _take_object(context)
        col.label(text=(ob.animation_data.action.name if ob else "No take recorded yet"), icon="ACTION")
        col.prop(context.scene, "phonecam_smooth", text="Smoothing (frames)")
        col.operator("phonecam.smooth_keys", icon="SMOOTHCURVE")
        col.operator("phonecam.bake_world", icon="CAMERA_DATA")


class PHONECAM_PT_options(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "PhoneCam"
    bl_label = "Options"
    bl_parent_id = "PHONECAM_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        p = prefs()
        col = self.layout.column()
        col.prop(context.scene, "phonecam_ride")
        col.prop(p, "res")
        col.prop(p, "motion_blur")
        col.prop(p, "walk_speed")
        col.prop(p, "out_dir", text="")
        col.label(text="Port changes apply after Stop → Start", icon="INFO")


class PHONECAM_PT_tips(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "PhoneCam"
    bl_label = "Tips"
    bl_parent_id = "PHONECAM_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        col = self.layout.column(align=True)
        for line in ("Rig presets, tripod, sliders and AR tracking:",
                     "    tap ⚙ on the phone.",
                     "Timeline markers become Shots on the phone.",
                     "Every camera in the scene becomes a one-tap",
                     "    camera spot on the phone.",
                     "No markers? The whole timeline is one shot.",
                     "Recorded takes are keyframes on 'PhoneCam'.",
                     "Fisheye needs ffmpeg installed."):
            col.label(text=line)
        self.layout.operator("phonecam.open_guide", icon="HELP")


CLASSES = (PHONECAM_Preferences, PHONECAM_OT_start, PHONECAM_OT_stop, PHONECAM_OT_copy_link, PHONECAM_OT_big_qr,
           PHONECAM_OT_open_videos, PHONECAM_OT_open_guide, PHONECAM_OT_smooth_keys, PHONECAM_OT_bake_world,
           PHONECAM_OT_show_viewport, PHONECAM_PT_main, PHONECAM_PT_preflight, PHONECAM_PT_takes, PHONECAM_PT_options,
           PHONECAM_PT_tips)


def register():
    import bpy.utils.previews
    _icons["pcoll"] = bpy.utils.previews.new()
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.WindowManager.phonecam_ip = EnumProperty(
        name="Network", items=_ip_enum, update=_ip_changed,
        description="Which of this computer's addresses the QR code uses. Change it if the phone can't connect")
    bpy.types.Scene.phonecam_smooth = FloatProperty(name="Smoothing", default=2.0, min=0.5, max=12.0,
                                                    description="How strongly 'Smooth keyframes' smooths the take, in frames")
    bpy.types.Scene.phonecam_ride = PointerProperty(
        type=bpy.types.Object, name="Ride along with",
        description="Optional: the camera moves with this object (a car, a horse, a spaceship...)")


def unregister():
    engine.stop()
    del bpy.types.Scene.phonecam_ride
    del bpy.types.Scene.phonecam_smooth
    del bpy.types.WindowManager.phonecam_ip
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
    if _icons["pcoll"] is not None:
        bpy.utils.previews.remove(_icons["pcoll"])
        _icons["pcoll"] = None
