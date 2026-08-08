# Camera Preview

A lightweight Windows desktop application for viewing UVC and ONVIF camera feeds.

## Features

- Simultaneous previews for all connected Windows UVC cameras
- One movable and resizable desktop window for each camera
- Restores each camera window's last screen position and size after reopening
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

By default, the application scans for all usable cameras and opens one window
per camera. Drag a window's title bar to place that feed anywhere on the
desktop. Use the `Scan` button after connecting another camera while the
application is already running.

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
| `Ctrl` + `H` | Globally hide or restore all preview windows |
| `\u5feb\u6377\u952e` button | Set a different `Ctrl` + key global hide/show hotkey |

Screenshots are saved in a `screenshots` folder beside the running application.

## Build A Windows EXE

```powershell
python -m pip install pyinstaller
pyinstaller --noconfirm --clean CameraPreview.spec
```

The generated executable is placed in `dist/CameraPreview.exe`.

## License

This project is released under the [MIT License](LICENSE).
