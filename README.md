# Camera Preview

A lightweight Windows desktop application for viewing a UVC camera feed.

## Features

- Live preview for Windows UVC cameras
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

The default camera index is `0`. For another camera, use for example:

```powershell
python camera_preview.py --camera 1
```

## Controls

| Control | Action |
| --- | --- |
| Mouse wheel over the image | Zoom in or out at the cursor |
| `R` | Reset zoom |
| `S` | Save a screenshot |
| `F` or `F11` | Toggle full screen |
| `Esc` or `Q` | Close the application |

Screenshots are saved in a `screenshots` folder beside the running application.

## Build A Windows EXE

```powershell
python -m pip install pyinstaller
pyinstaller --noconfirm --clean CameraPreview.spec
```

The generated executable is placed in `dist/CameraPreview.exe`.

## License

This project is released under the [MIT License](LICENSE).

