# Camera Preview

A lightweight Windows desktop application for viewing a UVC camera feed.

## Features

- Simultaneous previews for all connected Windows UVC cameras
- One movable and resizable desktop window for each camera
- Mouse-wheel zoom centered on the cursor
- Standard Windows close button, plus keyboard shortcuts
- Screenshot capture
- Full-screen mode

## Requirements

- Windows 10 or newer
- Python 3.10 or newer
- A UVC-compatible USB camera

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
| `Ctrl` + `Shift` + `H` | Globally hide or restore all preview windows |

Screenshots are saved in a `screenshots` folder beside the running application.

## Build A Windows EXE

```powershell
python -m pip install pyinstaller
pyinstaller --noconfirm --clean CameraPreview.spec
```

The generated executable is placed in `dist/CameraPreview.exe`.

## License

This project is released under the [MIT License](LICENSE).
