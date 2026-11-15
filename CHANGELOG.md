# Changelog

## 1.1.1

- **Smooth zoom on/off** (⚙ settings): off = the lens buttons and pinch switch zoom instantly, in the live view and in saved takes.

## 1.1.0

- **Rig presets**: Handheld, Dolly, Crane, Cinematic, Raw — plus **Tripod** (rotation only).
- **Live sliders** on the phone: Sensitivity, Smoothing, Position scale.
- **6DoF position tracking** through WebXR AR (Android Chrome and other WebXR browsers).
- **Keyframe rate**: record takes at 24, 25, 30 or 60 fps (or the scene rate), sub-frame accurate.
- **Artificial horizon** with pitch ladder and roll readout.
- **Preflight check** panel in Blender with one-click fixes.
- **Take tools**: Smooth keyframes, Bake to World camera.
- **WebSocket** link for lower latency (automatic HTTP fallback) and a live latency readout.
- The same camera rules drive the live view and the rebuilt take, so saved takes match what you filmed.

## 1.0.0 — first public release

- Your phone (iPhone or Android, in Safari or Chrome) becomes a handheld camera for Blender 4.2+.
- iPhone-style zoom: .5 · 1× · 2 · 3 · 5 lens buttons with eased glides, pinch-to-zoom, zoom dial.
- Looks: Video, Cinematic (depth of field, autofocus, tap-to-focus), Fisheye (needs ffmpeg).
- Stabilizer (On / Action / Off), wide 16:9 or vertical 9:16, grid and level.
- Record → review → save to MP4 with your scene's render engine and motion blur.
- Smooth takes: the full-rate phone stream is replayed to rebuild the camera path.
- Timeline markers become shots, scene cameras become camera spots, optional ride-along object.
- One-time phone setup with a private certificate made in pure Python; pairing key in the QR code.
