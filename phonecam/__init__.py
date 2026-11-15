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
        for line in ("Timeline markers become Shots on the phone.",
                     "Every camera in the scene becomes a one-tap",
                     "    camera spot on the phone.",
                     "No markers? The whole timeline is one shot.",
                     "Recorded takes are keyframes on 'PhoneCam'.",
                     "Fisheye needs ffmpeg installed."):
            col.label(text=line)
        self.layout.operator("phonecam.open_guide", icon="HELP")


CLASSES = (PHONECAM_Preferences, PHONECAM_OT_start, PHONECAM_OT_stop, PHONECAM_OT_copy_link, PHONECAM_OT_big_qr,
           PHONECAM_OT_open_videos, PHONECAM_OT_open_guide, PHONECAM_PT_main, PHONECAM_PT_options, PHONECAM_PT_tips)


def register():
    import bpy.utils.previews
    _icons["pcoll"] = bpy.utils.previews.new()
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.WindowManager.phonecam_ip = EnumProperty(
        name="Network", items=_ip_enum, update=_ip_changed,
        description="Which of this computer's addresses the QR code uses. Change it if the phone can't connect")
    bpy.types.Scene.phonecam_ride = PointerProperty(
        type=bpy.types.Object, name="Ride along with",
        description="Optional: the camera moves with this object (a car, a horse, a spaceship...)")


def unregister():
    engine.stop()
    del bpy.types.Scene.phonecam_ride
    del bpy.types.WindowManager.phonecam_ip
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
    if _icons["pcoll"] is not None:
        bpy.utils.previews.remove(_icons["pcoll"])
        _icons["pcoll"] = None
