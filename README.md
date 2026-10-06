# RC UFO local browser controls

For the drone that returned profile 83 in your probe. This client uses the
observed TC nine-byte UDP control protocol and RTSP video over UDP. It does
not support the extended GL flight format.

Connect the PC to the drone's Wi-Fi, close the official app and other video
clients, then run from this project folder:

```powershell
python .\rcufo_controller.py
```

Your browser opens the local control page; keep the Python terminal running.
Click **Connect**. The camera appears in the page, and status should show
**ID 83**. Flight controls start disabled. Click **Enable controls**, then
use **Take off** and the keyboard. Land before disconnecting or closing.
Disabling controls sends neutral sticks followed by the app's `08 01`
message; that message's firmware effect has not been validated as a landing
or failsafe. Closing does not automatically land. Use **Quit controller**
in the page or Ctrl+C in the Python terminal after landing. If you close the
tab instead, the input watchdog disables controls but the backend keeps running.

## Controls

| Input | Action |
| --- | --- |
| W / S | Forward / backward |
| A / D | Left / right |
| Space / Shift | Up / down |
| Q / E | Yaw left / right |
| T | Take off |
| L | Land |
| Esc | Stop motors (emergency stop; may cause a fall) |
| F | Neutral sticks |

The speed slider sets stick deviation, initially +/-30 around 128. This
client uses the app's default fixed-height convention: centred throttle is
128. Axis direction is taken from the app's joystick code; physical flight
behaviour and command acceptance still need validation on your drone.

Other buttons provide gyro calibration, camera selection, and a snapshot.
Calibration should be used with the drone stationary on a level surface.
Headless mode sets the observed TC flag 0x10. There are no automatic
take-off, return, flip, or landing sequences.

## Drift and trim

If the drone drifts backward with no keys held, use **Pitch trim -> Forward +**
in small steps. Each click adds 2 to the resting pitch value; for example,
trim +4 sends pitch 132 with no movement input. Roll and yaw have equivalent
trim controls. **Reset trim** returns all three offsets to zero. Trim starts
at zero each time the Python controller is launched and remains set during
that session. No correction is applied automatically from the video.

Calibration and trim serve different purposes. Place the drone stationary
on a level surface, reset trim, and let the **two-second calibration command**
finish before taking off. The client shows a countdown and prevents take-off
from interrupting it. During calibration it transmits untrimmed neutral
sticks. This confirms the command duration, not successful firmware calibration.
Emergency stop and landing remain available during the countdown.

After calibration, use trim only for small remaining drift. A large required
offset should prompt comparison with the original app and inspection of the
drone's propellers and airframe, rather than continually increasing trim.

Releasing movement keys or leaving the window recentres sticks. The sender
operates independently of the video decoder at nominally 20 Hz. It disables
control if status replies disappear for three seconds or the UI stops
updating for half a second; re-enabling is always manual. This client-side
behaviour is not a guarantee of the drone's firmware failsafe.

## Video and dependencies

Python 3.10+, Pillow, FFmpeg, and a browser are needed. Pillow 11.3 and
FFmpeg 8.0.1 were found on this PC, so no installation is needed here.
For another Python environment, install Pillow with:

```powershell
python -m pip install -r requirements.txt
```

The interface is served only on 127.0.0.1 with a random access token; it is
not exposed to the drone LAN. API commands also reject cross-origin browser
requests. Do not share the printed control URL while the controller is running.

The app uses the installed FFmpeg RTP/JPEG decoder, with UDP transport and
a bounded queue of the latest decoded frame. Portrait input rotates 90
degrees by default to match the original app; change the Rotate dropdown
if needed. It does not upscale recordings to fake 4K/8K resolutions.

The camera fills the main workspace and scales to fit after rotation,
preserving its aspect ratio. **Focus view** hides the side panel for more
viewing space; **Show controls** restores it. The motor-stop button remains
visible in the header. On smaller windows the controls move below the image.
This enlarges the display without adding detail to the source video.

If Windows asks whether Python/FFmpeg may receive traffic, allow the
executables on the drone network so the UDP stream can arrive. A black
picture with live status may mean the media decoder failed: inspect the
session's `ffmpeg.log` rather than enabling controls to troubleshoot video.

```powershell
# Connect on launch, still with flight controls disabled:
python .\rcufo_controller.py --connect

# Override the FFmpeg path:
python .\rcufo_controller.py --ffmpeg 'C:\path\to\ffmpeg.exe'

# Exercise the interface offline; no network packets are sent:
python .\rcufo_controller.py --demo
```

## Session output

Each launch saves output under `results/flights/<UTC timestamp>/`:

- `events.jsonl`: connection changes, transmitted datagrams, commands,
  errors, and frame information; updated immediately.
- `ffmpeg.log`: the decoder's diagnostics (real connections only).
- `first-frame.jpg`: the first decoded camera frame, before display rotation.
- `snapshot-*.jpg`: snapshots you request, before display rotation.
- `session.json`: event counts after normal shutdown.

After trying it, tell Codex the result and it can inspect these files.
No control packets were sent to the real drone during development tests.

## Offline verification

```powershell
python -m unittest discover -s tests -v
```

Tests cover packet encoding, input mapping, watchdogs, emergency priority,
loopback UDP transmission, FFmpeg image output, and the local browser API.

## Project layout

```text
rcufo_controller.py    Python entry point for controls and video
drone_probe.py         Read-only service probe
rcufo/                Control/video modules and the local UI asset
tests/                Offline tests
docs/PROBE.md         Probe instructions
research/             Notes, original APK, decompiled source, screenshots
results/              Captured probe reports and flight session output
requirements.txt      Python dependencies
```

See [probe instructions](docs/PROBE.md) and [research notes](research/NOTES.md).
