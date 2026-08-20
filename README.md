# Camera Preview

A lightweight Windows and Ubuntu desktop application for viewing UVC and ONVIF
camera feeds.

## Features

- Simultaneous previews for connected UVC cameras in one shared window
- Two local USB cameras automatically use uncompressed `YUY2` at `640x480 @ 30 FPS` for smooth dual preview
- Drag each camera panel by its top bar to arrange feeds anywhere in the workspace
- Restores the main window location and each camera panel location after reopening
- Always-on-top mode keeps the preview visible while switching applications
- Pure-picture mode hides the app border, toolbar, and camera panel controls
- Pure-picture mode can be moved by dragging and resized from its edges or corners
- Local-network ONVIF camera discovery, RTSP address lookup, and manual RTSP connection
- Built-in LAN MJPEG streaming to a browser, with no OBS installation required
- Mouse-wheel zoom centered on the cursor
- Standard Windows close button, plus keyboard shortcuts
- Screenshot capture
- Full-screen mode
- Configurable global hide/show hotkey, saved beside the application

## Requirements

- Windows 10 or newer, or Ubuntu 22.04/24.04 x86_64
- Python 3.10 or newer
- A UVC-compatible USB camera or an ONVIF/RTSP network camera

## Run From Source

```text
python -m pip install -r requirements.txt
python camera_preview.py
```

Use `--list` to find usable camera indexes:

```text
python camera_preview.py --list
```

By default, the application scans for usable cameras and opens the only camera
it finds. When multiple local cameras are available, a selection window opens
with the first camera selected; choose one or more cameras to display. Use
`--all` to skip that selection and open every usable camera. Drag a camera
panel's top bar to place that feed anywhere inside the workspace. Use the
`Scan` button after connecting another camera while the application is already
running.

Use the Network button to search for ONVIF cameras on the local network. Select a
device, enter its credentials if required, and choose Read RTSP to fill the stream
address automatically. A known RTSP address can also be pasted directly and opened with
Add Preview. When no USB camera is available, the application opens a small
launcher with the same network-search action.

## Optional YOLO Detection (WSL)

The source project overlays person-only YOLO detections on the desktop preview,
screenshots, and LAN MJPEG streams. Detection runs in one background worker and
keeps only the newest frame, so a slow inference device does not create an unbounded
video delay. The COCO `person` class (ID `0`) is the only class enabled by default.

The existing WSL deployment already contains the model and Python environment. From
Ubuntu, with `USB Camera3` attached to WSL, run:

```bash
cd /mnt/c/Users/YunDrone/CameraPreview
./run-wsl-camera-preview-yolo.sh
```

The equivalent command, with explicit options, is:

```bash
/home/yundrone/yolo/.venv/bin/python camera_preview.py \
  --camera 0 --width 640 --height 480 --fps 30 \
  --yolo --yolo-model /home/yundrone/yolo/models/yolo11n.pt \
  --start-lan-stream
```

On Windows, double-click
`C:\Users\YunDrone\yolo-realtime\start_camera_preview_yolo.bat`. It attaches
`USB Camera3`, starts the original Camera Preview with YOLO enabled, and opens the
LAN preview at `http://localhost:8080/`.

If the camera was previously released to Windows, Windows may show one UAC prompt
while the script attaches it back to WSL. To stop the integrated service and release
the camera to Windows, double-click
`C:\Users\YunDrone\yolo-realtime\stop_camera_yolo.bat`.

YOLO is opt-in: running `python camera_preview.py` without `--yolo` keeps the
original camera-only behavior. The optional Python package is listed in
`requirements-yolo.txt`; the CUDA-enabled WSL environment can continue to use the
already installed Ultralytics/PyTorch stack.

### Direct Windows Controls

When launched with `start_camera_preview_direct.bat`, the Windows preview toolbar
controls the local WSL service directly:

- `YOLO 开` / `YOLO 关` toggles the person-only overlay without disconnecting the camera.
- `采集样本` starts saving one raw, unannotated JPEG per second. Click `停止采集`
  when the recording session is complete.
- `目标自动截图` saves one annotated screenshot whenever identity label `1` appears.
  It rearms after the target has been absent for one second, so a person staying in
  frame does not create a continuous burst of duplicate screenshots.

Raw images are saved locally in
`training_samples/target_person/images/raw/`. This directory is ignored by Git so
training data is not uploaded with the source code. Use the raw images for labeling;
do not use the preview screenshots with YOLO boxes baked in.

Target screenshots are saved in the portable application's `screenshots/` directory.

The direct Windows launcher also enables an optional YOLO11 pose-quality gate. It
uses `models/yolo11n-pose.pt` to require at least six visible body keypoints before
the identity label `1` is drawn. Pose is only a quality filter; the identity
classifier remains responsible for deciding who the person is. To use the original
identity-only pipeline, remove the `--pose-model` and related `--pose-*` arguments
from `start_camera_preview_direct.ps1`.

## Built-In LAN Streaming

Click `局域网推流` in the toolbar, leave the default port at `8080`, and click
`启动/重启`. The dialog shows the addresses available on this computer. On another
device connected to the same LAN, open the displayed address in a browser, for
example:

```text
http://192.168.10.25:8080/
```

The page displays every camera currently open in the application and provides a
direct MJPEG video-stream link plus a single-frame JPEG link for each one. The
streams update as cameras are added or closed. Stop the stream from the same dialog;
it also stops automatically when Camera Preview exits.

To feed another application, select the camera under the single-stream pull address
list and choose `Copy pull URL`. Paste the resulting
`http://.../stream/...mjpg` address into an application that accepts HTTP MJPEG
input. The normal `Copy URL` button copies the browser overview page instead.

The LAN-stream dialog can enable password protection. Set a password and confirm
it, then restart the stream. Every browser, snapshot, and MJPEG request must use
HTTP Basic credentials with username `camera` and that password. In Camera Preview's
Network dialog, enter those credentials before adding the HTTP MJPEG address. The
portable settings file stores only a salted PBKDF2 password hash, never the plaintext
password. This protects access on the LAN; the stream is still HTTP, not HTTPS, so
use a trusted network when the password itself must also be encrypted in transit.

Windows may ask whether to allow Camera Preview through the firewall the first time
the stream starts. Allow it on `Private networks` to make the address accessible to
other devices on the LAN. This lightweight mode is designed for browser preview and
uses JPEG compression; for RTSP/H.264 output, use a dedicated video encoder/server.

To open only selected cameras, repeat `--camera`:

```powershell
python camera_preview.py --camera 0 --camera 1
```

## Controls

| Control | Action |
| --- | --- |
| Mouse wheel over the image | Zoom in or out at the cursor |
| `R` | Reset zoom |
| `S` | Save a screenshot |
| `F` or `F11` | Toggle full screen |
| `Esc` or `Q` | Close the application |
| `Ctrl` + `N` | Switch to the next available local camera |
| `Ctrl` + `P` | Switch to the previous available local camera |
| `Ctrl` + `E` | Globally hide or restore all preview windows |
| `\u5feb\u6377\u952e` button | Set a different `Ctrl` + key global hide/show hotkey |
| `\u53d6\u6d88\u7f6e\u9876` / `\u7f6e\u9876` | Keep the preview above other applications or disable it |
| `\u7eaf\u753b\u9762` | Hide the app border and controls; right-click a preview to restore them |
| Pure-picture edge or corner | Drag to resize the pure-picture window |

Screenshots are saved in a `screenshots` folder beside the running application.

## Ubuntu Portable Build

Download the matching package from the latest GitHub Release:

- `CameraPreview-ubuntu-x86_64.tar.gz` for Intel/AMD PCs
- `CameraPreview-ubuntu-arm64.tar.gz` for ARM64 devices such as Orange Pi and
  Raspberry Pi running 64-bit Ubuntu

Then extract and run it:

```bash
tar -xzf CameraPreview-ubuntu-x86_64.tar.gz
cd CameraPreview
./CameraPreview
```

The packaged builds target Ubuntu 22.04 and newer. If Ubuntu reports a missing
shared library, install the runtime dependencies:

```bash
sudo apt update
sudo apt install libgl1 libglib2.0-0 libsm6 libxext6 libxrender1
```

For a local USB camera, make sure the account can access `/dev/video*`:

```bash
sudo usermod -aG video "$USER"
```

Log out and back in after changing the `video` group. The global Ctrl+key
hide/show hotkey and Windows single-instance protection are Windows-only;
preview, USB/ONVIF camera access, LAN streaming, password protection, and
portable settings work on Ubuntu.

## Build A Windows EXE

```powershell
python -m pip install pyinstaller
pyinstaller --noconfirm --clean CameraPreview.spec
```

The portable build is placed in `dist/CameraPreview/`. Keep `CameraPreview.exe`
and its `_internal` folder together when moving the application.

## Build On Ubuntu

```bash
sudo apt update
sudo apt install python3-tk libgl1 libglib2.0-0 libsm6 libxext6 libxrender1
python3 -m pip install --user -r requirements.txt pyinstaller
python3 -m PyInstaller --noconfirm --clean CameraPreview.spec
```

The Linux binary is `dist/CameraPreview/CameraPreview`.

## Portable Data

The application never stores its own persistent data in `AppData`, `LocalAppData`,
or `Roaming`. `camera_preview_settings.json` is created beside the executable and
holds the hotkey, window location, panel layout, and display mode, plus LAN-stream
port and quality settings. Screenshots are saved to the `screenshots` folder beside
the executable.

## License

This project is released under the [MIT License](LICENSE).
