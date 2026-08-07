"""Display a Windows UVC camera in a responsive desktop window.

Controls: mouse wheel zooms around the cursor, R resets zoom, S saves a
screenshot, F/F11 toggles full screen, and Q closes the program.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import ttk

import cv2


WINDOW_TITLE = "摄像头预览"
BACKENDS = (
    ("DirectShow", cv2.CAP_DSHOW),
    ("Media Foundation", cv2.CAP_MSMF),
)
MAX_ZOOM = 8.0


@dataclass
class ZoomView:
    zoom: float = 1.0
    left: float = 0.0
    top: float = 0.0
    frame_width: int = 0
    frame_height: int = 0

    def reset(self) -> None:
        self.zoom = 1.0
        self.left = 0.0
        self.top = 0.0

    def update_frame_size(self, width: int, height: int) -> None:
        if (width, height) != (self.frame_width, self.frame_height):
            self.frame_width = width
            self.frame_height = height
            self.reset()

    def crop_size(self) -> tuple[int, int]:
        return (
            max(1, min(self.frame_width, round(self.frame_width / self.zoom))),
            max(1, min(self.frame_height, round(self.frame_height / self.zoom))),
        )

    def clamp_position(self) -> None:
        crop_width, crop_height = self.crop_size()
        self.left = min(max(self.left, 0.0), self.frame_width - crop_width)
        self.top = min(max(self.top, 0.0), self.frame_height - crop_height)

    def zoom_at(self, relative_x: float, relative_y: float, direction: int) -> None:
        """Zoom while keeping the source pixel under the mouse stationary."""
        if not self.frame_width or not self.frame_height or not direction:
            return

        relative_x = min(max(relative_x, 0.0), 1.0)
        relative_y = min(max(relative_y, 0.0), 1.0)
        old_crop_width, old_crop_height = self.crop_size()
        source_x = self.left + relative_x * old_crop_width
        source_y = self.top + relative_y * old_crop_height

        new_zoom = min(max(self.zoom * (1.25**direction), 1.0), MAX_ZOOM)
        if new_zoom == self.zoom:
            return

        self.zoom = new_zoom
        new_crop_width, new_crop_height = self.crop_size()
        self.left = source_x - relative_x * new_crop_width
        self.top = source_y - relative_y * new_crop_height
        self.clamp_position()

    def render(self, frame, output_width: int, output_height: int):
        height, width = frame.shape[:2]
        self.update_frame_size(width, height)
        self.clamp_position()
        crop_width, crop_height = self.crop_size()
        left = min(max(round(self.left), 0), width - crop_width)
        top = min(max(round(self.top), 0), height - crop_height)
        crop = frame[top : top + crop_height, left : left + crop_width]
        if (crop_width, crop_height) == (output_width, output_height):
            return crop
        return cv2.resize(crop, (output_width, output_height), interpolation=cv2.INTER_LINEAR)


def configure_capture(capture: cv2.VideoCapture, width: int, height: int, fps: int) -> None:
    capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    capture.set(cv2.CAP_PROP_FPS, fps)


def open_capture(index: int, width: int, height: int, fps: int) -> tuple[cv2.VideoCapture, str]:
    for backend_name, backend_id in BACKENDS:
        capture = cv2.VideoCapture(index, backend_id)
        if not capture.isOpened():
            capture.release()
            continue

        configure_capture(capture, width, height, fps)
        ok, _ = capture.read()
        if ok:
            return capture, backend_name
        capture.release()

    raise RuntimeError(
        f"Cannot open camera {index}. Run with --list to find an available camera index."
    )


def list_cameras(max_index: int, width: int, height: int, fps: int) -> int:
    found = 0
    for index in range(max_index + 1):
        try:
            capture, backend = open_capture(index, width, height, fps)
        except RuntimeError:
            continue

        try:
            actual_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            print(f"Camera {index}: {actual_width}x{actual_height} via {backend}")
            found += 1
        finally:
            capture.release()

    if not found:
        print("No usable camera was found.")
        return 1
    return 0


def application_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def save_frame(frame) -> Path:
    screenshot_dir = application_directory() / "screenshots"
    screenshot_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = screenshot_dir / f"camera_{timestamp}.jpg"
    if not cv2.imwrite(str(output_path), frame):
        raise RuntimeError(f"Could not save {output_path}")
    return output_path


class CameraApp:
    """Tk owns all UI work; a daemon thread continuously reads the camera."""

    def __init__(self, capture: cv2.VideoCapture, camera_index: int) -> None:
        self.capture = capture
        self.camera_index = camera_index
        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.geometry("1000x700")
        self.root.minsize(480, 360)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.zoom_view = ZoomView()
        self.running = True
        self.fullscreen = False
        self.frame_lock = threading.Lock()
        self.latest_frame = None
        self.frame_count = 0
        self.fps = 0.0
        self.fps_start = time.perf_counter()
        self.status_text = "正在打开摄像头..."
        self.status_until = 0.0
        self.image_photo = None
        self.image_bounds: tuple[int, int, int, int] | None = None

        self._build_ui()
        self.reader = threading.Thread(target=self._read_frames, name="camera-reader", daemon=True)
        self.reader.start()
        self.root.after(15, self._draw_frame)

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self.root, padding=(8, 6))
        toolbar.pack(fill=tk.X)
        ttk.Label(toolbar, text=f"摄像头 {self.camera_index}").pack(side=tk.LEFT)
        self.zoom_label = ttk.Label(toolbar, text="缩放 x1.0")
        self.zoom_label.pack(side=tk.LEFT, padx=(14, 0))
        self.fps_label = ttk.Label(toolbar, text="0.0 FPS")
        self.fps_label.pack(side=tk.LEFT, padx=(14, 0))
        ttk.Button(toolbar, text="重置", command=self.reset_zoom).pack(side=tk.RIGHT)
        ttk.Button(toolbar, text="全屏", command=self.toggle_fullscreen).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(toolbar, text="保存", command=self.save_screenshot).pack(side=tk.RIGHT, padx=(0, 6))

        self.canvas = tk.Canvas(self.root, background="#101010", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.image_id = self.canvas.create_image(0, 0, anchor=tk.NW)
        self.message_id = self.canvas.create_text(
            16,
            16,
            anchor=tk.NW,
            fill="#f1f5f9",
            font=("Segoe UI", 11),
            text="正在打开摄像头...",
        )
        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)
        self.root.bind("<MouseWheel>", self.on_mouse_wheel)
        self.root.bind("<Key-r>", lambda _event: self.reset_zoom())
        self.root.bind("<Key-R>", lambda _event: self.reset_zoom())
        self.root.bind("<Key-s>", lambda _event: self.save_screenshot())
        self.root.bind("<Key-S>", lambda _event: self.save_screenshot())
        self.root.bind("<Key-f>", lambda _event: self.toggle_fullscreen())
        self.root.bind("<Key-F>", lambda _event: self.toggle_fullscreen())
        self.root.bind("<F11>", lambda _event: self.toggle_fullscreen())
        self.root.bind("<Key-q>", lambda _event: self.close())
        self.root.bind("<Key-Q>", lambda _event: self.close())
        self.root.bind("<Escape>", self.on_escape)

    def _read_frames(self) -> None:
        failed_reads = 0
        while self.running:
            ok, frame = self.capture.read()
            if not ok:
                failed_reads += 1
                if failed_reads >= 30:
                    self.status_text = "摄像头没有返回画面。"
                time.sleep(0.03)
                continue

            failed_reads = 0
            with self.frame_lock:
                self.latest_frame = frame
                self.frame_count += 1

    def _take_latest_frame(self):
        with self.frame_lock:
            if self.latest_frame is None:
                return None
            return self.latest_frame.copy()

    def _update_fps(self) -> None:
        now = time.perf_counter()
        elapsed = now - self.fps_start
        if elapsed < 0.5:
            return
        with self.frame_lock:
            count = self.frame_count
            self.frame_count = 0
        self.fps = count / elapsed
        self.fps_start = now

    def _display_size(self, frame_width: int, frame_height: int) -> tuple[int, int, int, int]:
        canvas_width = max(1, self.canvas.winfo_width())
        canvas_height = max(1, self.canvas.winfo_height())
        scale = min(canvas_width / frame_width, canvas_height / frame_height)
        display_width = max(1, round(frame_width * scale))
        display_height = max(1, round(frame_height * scale))
        left = (canvas_width - display_width) // 2
        top = (canvas_height - display_height) // 2
        return left, top, display_width, display_height

    @staticmethod
    def _to_photo(frame):
        # OpenCV's PNG encoder converts its BGR frame to the PNG RGB order.
        # Converting here first would swap red and blue twice.
        ok, encoded = cv2.imencode(".png", frame, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        if not ok:
            raise RuntimeError("Could not encode camera frame for display.")
        encoded_base64 = base64.b64encode(encoded.tobytes()).decode("ascii")
        return tk.PhotoImage(data=encoded_base64, format="png")

    def _draw_frame(self) -> None:
        if not self.running:
            return

        self._update_fps()
        frame = self._take_latest_frame()
        if frame is not None:
            source_height, source_width = frame.shape[:2]
            left, top, display_width, display_height = self._display_size(source_width, source_height)
            display_frame = self.zoom_view.render(frame, display_width, display_height)
            self.image_photo = self._to_photo(display_frame)
            self.canvas.itemconfigure(self.image_id, image=self.image_photo)
            self.canvas.coords(self.image_id, left, top)
            self.canvas.tag_lower(self.image_id)
            self.image_bounds = (left, top, display_width, display_height)
            self.zoom_label.configure(text=f"缩放 x{self.zoom_view.zoom:.1f}")
            self.fps_label.configure(text=f"{self.fps:.1f} FPS")
            if time.perf_counter() > self.status_until:
                self.status_text = ""

        self.canvas.itemconfigure(self.message_id, text=self.status_text)
        self.root.after(30, self._draw_frame)

    def on_mouse_wheel(self, event) -> str | None:
        if not self.image_bounds or not event.delta:
            return None
        left, top, width, height = self.image_bounds
        mouse_x = event.x_root - self.canvas.winfo_rootx()
        mouse_y = event.y_root - self.canvas.winfo_rooty()
        if not (left <= mouse_x < left + width and top <= mouse_y < top + height):
            return "break"
        direction = 1 if event.delta > 0 else -1
        self.zoom_view.zoom_at((mouse_x - left) / width, (mouse_y - top) / height, direction)
        return "break"

    def reset_zoom(self) -> None:
        self.zoom_view.reset()

    def save_screenshot(self) -> None:
        frame = self._take_latest_frame()
        if frame is None:
            self.status_text = "尚未收到摄像头画面。"
        else:
            output_path = save_frame(frame)
            self.status_text = f"已保存：{output_path.name}"
        self.status_until = time.perf_counter() + 3.0

    def toggle_fullscreen(self) -> None:
        self.fullscreen = not self.fullscreen
        self.root.attributes("-fullscreen", self.fullscreen)

    def on_escape(self, _event) -> None:
        if self.fullscreen:
            self.toggle_fullscreen()
        else:
            self.close()

    def close(self) -> None:
        if not self.running:
            return
        self.running = False
        try:
            self.capture.release()
        finally:
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def show_preview(capture: cv2.VideoCapture, camera_index: int) -> int:
    app = CameraApp(capture, camera_index)
    app.run()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Display a live camera preview.")
    parser.add_argument("--camera", type=int, default=0, help="Camera index to open (default: 0).")
    parser.add_argument("--width", type=int, default=1280, help="Requested frame width.")
    parser.add_argument("--height", type=int, default=720, help="Requested frame height.")
    parser.add_argument("--fps", type=int, default=30, help="Requested frame rate.")
    parser.add_argument("--list", action="store_true", help="List working camera indices and exit.")
    parser.add_argument("--max-index", type=int, default=8, help="Highest index checked by --list.")
    parser.add_argument("--self-test", action="store_true", help="Read one frame and exit without opening a window.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if min(args.width, args.height, args.fps) <= 0:
        raise ValueError("Width, height, and fps must be positive.")
    if args.list:
        return list_cameras(args.max_index, args.width, args.height, args.fps)

    capture, backend = open_capture(args.camera, args.width, args.height, args.fps)
    try:
        actual_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"Camera {args.camera}: {actual_width}x{actual_height} via {backend}")
        if args.self_test:
            print("Camera self-test passed.")
            return 0
        return show_preview(capture, args.camera)
    finally:
        capture.release()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        message = str(error)
        print(message, file=sys.stderr)
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, message, WINDOW_TITLE, 0x10)
        raise SystemExit(1) from error
