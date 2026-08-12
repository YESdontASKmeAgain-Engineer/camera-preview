# Camera Preview

A lightweight Windows desktop application for viewing UVC and ONVIF camera feeds.

## Features

- Simultaneous previews for all connected Windows UVC cameras in one shared window
- Two local USB cameras automatically use uncompressed `YUY2` at `640x480 @ 30 FPS` for smooth dual preview
- Drag each camera panel by its top bar to arrange feeds anywhere in the workspace
- Restores the main window location and each camera panel location after reopening
- Always-on-top mode keeps the preview visible while switching applications
- Pure-picture mode hides the app border, toolbar, and camera panel controls
- Pure-picture mode can be moved by dragging and resized from its edges or corners
- Local-network ONVIF camera discovery, RTSP address lookup, and manual RTSP connection
- Mouse-wheel zoom centered on the cursor
- Standard Windows close button, plus keyboard shortcuts
- Screenshot capture
- Full-screen mode
- Configurable global hide/show hotkey, saved beside the application

## Requirements

- Windows 10 or newer
- Python 3.10 or newer
- A UVC-compatible USB camera or an ONVIF/RTSP network camera

## Run From Source

```powershell
python -m pip install -r requirements.txt
python camera_preview.py
```

Use `--list` to find usable camera indexes:

```powershell
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

## Build A Windows EXE

```powershell
python -m pip install pyinstaller
pyinstaller --noconfirm --clean CameraPreview.spec
```

The portable build is placed in `dist/CameraPreview/`. Keep `CameraPreview.exe`
and its `_internal` folder together when moving the application.

## Portable Data

The application never stores its own persistent data in `AppData`, `LocalAppData`,
or `Roaming`. `camera_preview_settings.json` is created beside
`CameraPreview.exe` and holds the hotkey, window location, panel layout, and display
mode. Screenshots are saved to the `screenshots` folder beside the executable.

## License

This project is released under the [MIT License](LICENSE).
