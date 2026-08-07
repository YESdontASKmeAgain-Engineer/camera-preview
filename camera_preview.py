"""Display one or more Windows UVC cameras in movable desktop windows.

Each detected camera receives its own standard Windows window. Move a preview
by dragging its title bar. The mouse wheel zooms around the cursor inside an
individual preview.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import queue
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Iterable

import cv2


WINDOW_TITLE = "\u6444\u50cf\u5934\u9884\u89c8"
BACKENDS = (
    ("DirectShow", cv2.CAP_DSHOW),
    ("Media Foundation", cv2.CAP_MSMF),
)
MAX_ZOOM = 8.0
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
HOTKEY_ID = 0xCA01
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
VK_H = 0x48
DEFAULT_HOTKEY_MODIFIERS = MOD_CONTROL
HOTKEY_SETTINGS_FILENAME = "camera_preview_settings.json"
TK_SHIFT_MASK = 0x0001
TK_CONTROL_MASK = 0x0004
TK_ALT_MASK = 0x0008

SPECIAL_KEY_LABELS = {
    0x08: "Backspace",
    0x09: "Tab",
    0x0D: "Enter",
    0x1B: "Esc",
    0x20: "Space",
    0x21: "Page Up",
    0x22: "Page Down",
    0x23: "End",
    0x24: "Home",
    0x25: "Left",
    0x26: "Up",
    0x27: "Right",
    0x28: "Down",
    0x2D: "Insert",
    0x2E: "Delete",
}
KEYSYM_TO_VK = {
    "BackSpace": 0x08,
    "Tab": 0x09,
    "Return": 0x0D,
    "Escape": 0x1B,
    "space": 0x20,
    "Prior": 0x21,
    "Next": 0x22,
    "Home": 0x24,
    "End": 0x23,
    "Left": 0x25,
    "Up": 0x26,
    "Right": 0x27,
    "Down": 0x28,
    "Insert": 0x2D,
    "Delete": 0x2E,
}


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
        """Zoom while keeping the source pixel under the cursor stationary."""
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


@dataclass(frozen=True)
class GlobalHotkey:
    modifiers: int
    key: int

    @property
    def label(self) -> str:
        parts: list[str] = []
        if self.modifiers & MOD_CONTROL:
            parts.append("Ctrl")
        if self.modifiers & MOD_ALT:
            parts.append("Alt")
        if self.modifiers & MOD_SHIFT:
            parts.append("Shift")
        parts.append(self.key_label)
        return " + ".join(parts)

    @property
    def key_label(self) -> str:
        if 0x30 <= self.key <= 0x39 or 0x41 <= self.key <= 0x5A:
            return chr(self.key)
        if 0x70 <= self.key <= 0x87:
            return f"F{self.key - 0x70 + 1}"
        return SPECIAL_KEY_LABELS.get(self.key, f"Key {self.key}")


DEFAULT_HOTKEY = GlobalHotkey(DEFAULT_HOTKEY_MODIFIERS, VK_H)


def is_valid_hotkey(hotkey: GlobalHotkey) -> bool:
    return hotkey.modifiers == MOD_CONTROL and 1 <= hotkey.key <= 0xFF


def virtual_key_from_keysym(keysym: str) -> int | None:
    if len(keysym) == 1 and keysym.isascii() and keysym.isalnum():
        return ord(keysym.upper())
    if keysym.startswith("F") and keysym[1:].isdigit():
        number = int(keysym[1:])
        if 1 <= number <= 24:
            return 0x70 + number - 1
    return KEYSYM_TO_VK.get(keysym)


def hotkey_from_event(event) -> GlobalHotkey | None:
    key = virtual_key_from_keysym(event.keysym)
    if key is None:
        return None
    modifiers = 0
    if event.state & TK_CONTROL_MASK:
        modifiers |= MOD_CONTROL
    if event.state & TK_ALT_MASK:
        modifiers |= MOD_ALT
    if event.state & TK_SHIFT_MASK:
        modifiers |= MOD_SHIFT
    hotkey = GlobalHotkey(modifiers, key)
    return hotkey if is_valid_hotkey(hotkey) else None


@dataclass
class OpenedCapture:
    index: int
    capture: cv2.VideoCapture
    backend: str


def configure_capture(capture: cv2.VideoCapture, width: int, height: int, fps: int) -> None:
    capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    capture.set(cv2.CAP_PROP_FPS, fps)


def open_capture(index: int, width: int, height: int, fps: int) -> OpenedCapture:
    for backend_name, backend_id in BACKENDS:
        capture = cv2.VideoCapture(index, backend_id)
        if not capture.isOpened():
            capture.release()
            continue

        configure_capture(capture, width, height, fps)
        ok, _ = capture.read()
        if ok:
            return OpenedCapture(index=index, capture=capture, backend=backend_name)
        capture.release()

    raise RuntimeError(f"Cannot open camera {index}.")


def open_captures(
    indices: Iterable[int], width: int, height: int, fps: int
) -> list[OpenedCapture]:
    captures: list[OpenedCapture] = []
    opened_indices: set[int] = set()
    for index in indices:
        if index < 0 or index in opened_indices:
            continue
        try:
            opened = open_capture(index, width, height, fps)
        except RuntimeError:
            continue
        captures.append(opened)
        opened_indices.add(index)
    return captures


def list_cameras(max_index: int, width: int, height: int, fps: int) -> int:
    captures = open_captures(range(max_index + 1), width, height, fps)
    try:
        for opened in captures:
            actual_width = int(opened.capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_height = int(opened.capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            print(
                f"Camera {opened.index}: {actual_width}x{actual_height} via {opened.backend}"
            )
    finally:
        for opened in captures:
            opened.capture.release()

    if not captures:
        print("No usable camera was found.")
        return 1
    return 0


def application_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def hotkey_settings_path() -> Path:
    return application_directory() / HOTKEY_SETTINGS_FILENAME


def load_hotkey() -> GlobalHotkey:
    try:
        payload = json.loads(hotkey_settings_path().read_text(encoding="utf-8"))
        hotkey = GlobalHotkey(int(payload["modifiers"]), int(payload["key"]))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return DEFAULT_HOTKEY
    if is_valid_hotkey(hotkey):
        return hotkey
    try:
        save_hotkey(DEFAULT_HOTKEY)
    except OSError:
        pass
    return DEFAULT_HOTKEY


def save_hotkey(hotkey: GlobalHotkey) -> None:
    payload = {"modifiers": hotkey.modifiers, "key": hotkey.key}
    hotkey_settings_path().write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def save_frame(frame, camera_index: int) -> Path:
    screenshot_dir = application_directory() / "screenshots"
    screenshot_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output_path = screenshot_dir / f"camera_{camera_index}_{timestamp}.jpg"
    if not cv2.imwrite(str(output_path), frame):
        raise RuntimeError(f"Could not save {output_path}")
    return output_path


class CameraWindow:
    """A movable preview window for one camera capture."""

    def __init__(
        self,
        manager: "CameraManager",
        opened: OpenedCapture,
        position: int,
    ) -> None:
        self.manager = manager
        self.capture = opened.capture
        self.camera_index = opened.index
        self.backend = opened.backend
        self.window = tk.Toplevel(manager.root)
        self.window.title(f"{WINDOW_TITLE} - \u6444\u50cf\u5934 {self.camera_index}")
        self.window.minsize(480, 360)
        self.window.geometry(self._initial_geometry(position))
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        self.zoom_view = ZoomView()
        self.running = True
        self.fullscreen = False
        self.frame_lock = threading.Lock()
        self.latest_frame = None
        self.frame_count = 0
        self.fps = 0.0
        self.fps_start = time.perf_counter()
        self.status_text = "Opening camera..."
        self.status_until = 0.0
        self.image_photo = None
        self.image_bounds: tuple[int, int, int, int] | None = None

        self._build_ui()
        self.reader = threading.Thread(
            target=self._read_frames,
            name=f"camera-reader-{self.camera_index}",
            daemon=True,
        )
        self.reader.start()
        self.window.after(30, self._draw_frame)

    @staticmethod
    def _initial_geometry(position: int) -> str:
        column = position % 3
        row = position // 3
        left = 40 + column * 55
        top = 40 + row * 55
        return f"900x620+{left}+{top}"

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self.window, padding=(8, 6))
        toolbar.pack(fill=tk.X)
        ttk.Label(toolbar, text=f"\u6444\u50cf\u5934 {self.camera_index}").pack(side=tk.LEFT)
        self.zoom_label = ttk.Label(toolbar, text="\u7f29\u653e x1.0")
        self.zoom_label.pack(side=tk.LEFT, padx=(14, 0))
        self.fps_label = ttk.Label(toolbar, text="0.0 FPS")
        self.fps_label.pack(side=tk.LEFT, padx=(14, 0))

        ttk.Button(toolbar, text="\u91cd\u7f6e", command=self.reset_zoom).pack(side=tk.RIGHT)
        ttk.Button(toolbar, text="\u5168\u5c4f", command=self.toggle_fullscreen).pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        ttk.Button(toolbar, text="\u4fdd\u5b58", command=self.save_screenshot).pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        ttk.Button(toolbar, text="\u626b\u63cf", command=self.manager.scan_for_cameras).pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        ttk.Button(
            toolbar,
            text="\u5feb\u6377\u952e",
            command=lambda: self.manager.open_hotkey_dialog(self.window),
        ).pack(side=tk.RIGHT, padx=(0, 6))

        self.canvas = tk.Canvas(self.window, background="#101010", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.image_id = self.canvas.create_image(0, 0, anchor=tk.NW)
        self.message_id = self.canvas.create_text(
            16,
            16,
            anchor=tk.NW,
            fill="#f1f5f9",
            font=("Segoe UI", 11),
            text="Opening camera...",
        )

        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)
        self.window.bind("<MouseWheel>", self.on_mouse_wheel)
        self.window.bind("<Key-r>", lambda _event: self.reset_zoom())
        self.window.bind("<Key-R>", lambda _event: self.reset_zoom())
        self.window.bind("<Key-s>", lambda _event: self.save_screenshot())
        self.window.bind("<Key-S>", lambda _event: self.save_screenshot())
        self.window.bind("<Key-f>", lambda _event: self.toggle_fullscreen())
        self.window.bind("<Key-F>", lambda _event: self.toggle_fullscreen())
        self.window.bind("<F11>", lambda _event: self.toggle_fullscreen())
        self.window.bind("<Key-q>", lambda _event: self.close())
        self.window.bind("<Key-Q>", lambda _event: self.close())
        self.window.bind("<Escape>", self.on_escape)

    def _read_frames(self) -> None:
        failed_reads = 0
        while self.running:
            ok, frame = self.capture.read()
            if not ok:
                failed_reads += 1
                if failed_reads >= 30:
                    self.status_text = "Camera is not returning frames."
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
    def _to_photo(frame, master=None):
        # OpenCV converts BGR frames to PNG's RGB channel order when encoding.
        ok, encoded = cv2.imencode(".png", frame, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        if not ok:
            raise RuntimeError("Could not encode camera frame for display.")
        encoded_base64 = base64.b64encode(encoded.tobytes()).decode("ascii")
        return tk.PhotoImage(master=master, data=encoded_base64, format="png")

    def _draw_frame(self) -> None:
        if not self.running:
            return

        self._update_fps()
        frame = self._take_latest_frame()
        if frame is not None:
            source_height, source_width = frame.shape[:2]
            left, top, display_width, display_height = self._display_size(source_width, source_height)
            display_frame = self.zoom_view.render(frame, display_width, display_height)
            self.image_photo = self._to_photo(display_frame, self.window)
            self.canvas.itemconfigure(self.image_id, image=self.image_photo)
            self.canvas.coords(self.image_id, left, top)
            self.canvas.tag_lower(self.image_id)
            self.image_bounds = (left, top, display_width, display_height)
            self.zoom_label.configure(text=f"\u7f29\u653e x{self.zoom_view.zoom:.1f}")
            self.fps_label.configure(text=f"{self.fps:.1f} FPS")
            if time.perf_counter() > self.status_until:
                self.status_text = ""

        self.canvas.itemconfigure(self.message_id, text=self.status_text)
        try:
            self.window.after(30, self._draw_frame)
        except tk.TclError:
            pass

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
            self.status_text = "No camera frame is available yet."
        else:
            output_path = save_frame(frame, self.camera_index)
            self.status_text = f"Saved: {output_path.name}"
        self.status_until = time.perf_counter() + 3.0

    def set_status(self, text: str, duration: float = 3.0) -> None:
        self.status_text = text
        self.status_until = time.perf_counter() + duration

    def toggle_fullscreen(self) -> None:
        self.fullscreen = not self.fullscreen
        self.window.attributes("-fullscreen", self.fullscreen)

    def on_escape(self, _event) -> None:
        if self.fullscreen:
            self.toggle_fullscreen()
        else:
            self.close()

    def close(self, notify_manager: bool = True) -> None:
        if not self.running:
            return
        self.running = False
        try:
            self.capture.release()
        finally:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
        if notify_manager:
            self.manager.camera_closed(self)


class CameraManager:
    """Owns the hidden Tk root and all visible camera preview windows."""

    def __init__(
        self,
        captures: list[OpenedCapture],
        width: int,
        height: int,
        fps: int,
        max_index: int,
    ) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.max_index = max_index
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.protocol("WM_DELETE_WINDOW", self.close_all)
        self.windows: dict[int, CameraWindow] = {}
        self.shutting_down = False
        self.previews_hidden = False
        self.hotkey = load_hotkey()
        self._hotkey_events = queue.SimpleQueue()
        self._hotkey_stop = threading.Event()
        self._hotkey_ready = threading.Event()
        self._hotkey_thread: threading.Thread | None = None
        self._hotkey_thread_id: int | None = None
        self.hotkey_available = False
        self._start_hotkey_listener()
        self.root.after(80, self._poll_hotkey_events)

        for position, opened in enumerate(captures):
            self.add_camera(opened, position)

    def add_camera(self, opened: OpenedCapture, position: int | None = None) -> None:
        if opened.index in self.windows:
            opened.capture.release()
            return
        if position is None:
            position = len(self.windows)
        window = CameraWindow(self, opened, position)
        self.windows[opened.index] = window
        if self.previews_hidden:
            window.window.withdraw()

    def _start_hotkey_listener(self) -> None:
        if sys.platform != "win32":
            return
        self._hotkey_stop.clear()
        self._hotkey_ready = threading.Event()
        self._hotkey_thread_id = None
        self.hotkey_available = False
        self._hotkey_thread = threading.Thread(
            target=self._hotkey_loop,
            name="camera-preview-hotkey",
            daemon=True,
        )
        self._hotkey_thread.start()

    def _hotkey_loop(self) -> None:
        self._hotkey_thread_id = threading.get_native_id()
        user32 = ctypes.windll.user32
        hotkey = self.hotkey
        modifiers = hotkey.modifiers | MOD_NOREPEAT
        registered = bool(user32.RegisterHotKey(None, HOTKEY_ID, modifiers, hotkey.key))
        self.hotkey_available = registered
        self._hotkey_ready.set()
        if not registered:
            return

        message = wintypes.MSG()
        try:
            while not self._hotkey_stop.is_set():
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                if message.message == WM_HOTKEY:
                    self._hotkey_events.put(None)
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)
            self.hotkey_available = False

    def _poll_hotkey_events(self) -> None:
        pressed = False
        while True:
            try:
                self._hotkey_events.get_nowait()
            except queue.Empty:
                break
            pressed = True
        if pressed:
            self.toggle_visibility()
        if not self.shutting_down:
            try:
                self.root.after(80, self._poll_hotkey_events)
            except tk.TclError:
                pass

    def _stop_hotkey_listener(self) -> None:
        self._hotkey_stop.set()
        if sys.platform == "win32" and self._hotkey_thread_id is not None:
            ctypes.windll.user32.PostThreadMessageW(self._hotkey_thread_id, WM_QUIT, 0, 0)
        if self._hotkey_thread and self._hotkey_thread.is_alive():
            self._hotkey_thread.join(timeout=1.0)
        self._hotkey_thread = None
        self._hotkey_thread_id = None
        self.hotkey_available = False

    def toggle_visibility(self) -> None:
        if not self.windows:
            return
        self.previews_hidden = not self.previews_hidden
        for window in self.windows.values():
            if self.previews_hidden:
                window.window.withdraw()
            else:
                window.window.deiconify()
                window.window.lift()

    def set_hotkey(self, hotkey: GlobalHotkey) -> bool:
        if not is_valid_hotkey(hotkey):
            return False
        if hotkey == self.hotkey:
            return self.hotkey_available

        previous_hotkey = self.hotkey
        self._stop_hotkey_listener()
        self.hotkey = hotkey
        self._start_hotkey_listener()
        self._hotkey_ready.wait(timeout=1.0)
        if self.hotkey_available:
            save_hotkey(hotkey)
            for window in self.windows.values():
                window.set_status(f"Hotkey: {hotkey.label}")
            return True

        self._stop_hotkey_listener()
        self.hotkey = previous_hotkey
        self._start_hotkey_listener()
        self._hotkey_ready.wait(timeout=1.0)
        return False

    def open_hotkey_dialog(self, parent) -> None:
        dialog = tk.Toplevel(parent)
        dialog.title("\u8bbe\u7f6e\u5168\u5c40\u5feb\u6377\u952e")
        dialog.transient(parent)
        dialog.resizable(False, False)
        dialog.grab_set()

        container = ttk.Frame(dialog, padding=16)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="\u76f4\u63a5\u6309\u4e0b Ctrl \u52a0\u4e00\u4e2a\u6309\u952e\uff0c\u7136\u540e\u70b9\u51fb\u5e94\u7528\u3002",
        ).pack(anchor=tk.W)

        candidate = [self.hotkey]
        shortcut_text = tk.StringVar(value=candidate[0].label)
        capture = ttk.Entry(container, textvariable=shortcut_text, state="readonly", width=30)
        capture.pack(fill=tk.X, pady=(10, 12))

        def capture_shortcut(event) -> str:
            hotkey = hotkey_from_event(event)
            if hotkey is not None:
                candidate[0] = hotkey
                shortcut_text.set(hotkey.label)
            return "break"

        def use_default() -> None:
            candidate[0] = DEFAULT_HOTKEY
            shortcut_text.set(DEFAULT_HOTKEY.label)

        def close_dialog() -> None:
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass

        def apply_hotkey() -> None:
            try:
                changed = self.set_hotkey(candidate[0])
            except OSError as error:
                messagebox.showerror(WINDOW_TITLE, str(error), parent=dialog)
                return
            if changed:
                close_dialog()
                return
            messagebox.showerror(
                WINDOW_TITLE,
                f"{candidate[0].label} is unavailable. Choose another shortcut.",
                parent=dialog,
            )

        buttons = ttk.Frame(container)
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="\u5e94\u7528", command=apply_hotkey).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="\u53d6\u6d88", command=close_dialog).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(buttons, text="\u6062\u590d\u9ed8\u8ba4", command=use_default).pack(side=tk.LEFT)

        capture.bind("<KeyPress>", capture_shortcut)
        dialog.bind("<KeyPress>", capture_shortcut)
        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        dialog.after_idle(dialog.focus_force)

    def scan_for_cameras(self) -> None:
        if self.shutting_down:
            return
        known_indices = set(self.windows)
        added = 0
        for index in range(self.max_index + 1):
            if index in known_indices:
                continue
            try:
                opened = open_capture(index, self.width, self.height, self.fps)
            except RuntimeError:
                continue
            self.add_camera(opened)
            added += 1

        message = "No new cameras found." if not added else f"Added {added} camera(s)."
        for window in self.windows.values():
            window.set_status(message)

    def camera_closed(self, window: CameraWindow) -> None:
        if self.windows.get(window.camera_index) is window:
            del self.windows[window.camera_index]
        if not self.windows and not self.shutting_down:
            self.root.after_idle(self.close_all)

    def close_all(self) -> None:
        if self.shutting_down:
            return
        self.shutting_down = True
        self._stop_hotkey_listener()
        for window in list(self.windows.values()):
            window.close(notify_manager=False)
        self.windows.clear()
        try:
            self.root.quit()
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        self.root.mainloop()


def show_previews(
    captures: list[OpenedCapture], width: int, height: int, fps: int, max_index: int
) -> int:
    manager = CameraManager(captures, width, height, fps, max_index)
    manager.run()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Display one or more live camera previews.")
    parser.add_argument(
        "--camera",
        type=int,
        action="append",
        help="Camera index to open. Repeat the option to select multiple cameras.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Open every usable camera found up to --max-index.",
    )
    parser.add_argument("--width", type=int, default=1280, help="Requested frame width.")
    parser.add_argument("--height", type=int, default=720, help="Requested frame height.")
    parser.add_argument("--fps", type=int, default=30, help="Requested frame rate.")
    parser.add_argument("--list", action="store_true", help="List working camera indices and exit.")
    parser.add_argument("--max-index", type=int, default=8, help="Highest camera index scanned.")
    parser.add_argument("--self-test", action="store_true", help="Open selected cameras and exit.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if min(args.width, args.height, args.fps) <= 0:
        raise ValueError("Width, height, and fps must be positive.")
    if args.max_index < 0:
        raise ValueError("max-index must be zero or greater.")
    if args.list:
        return list_cameras(args.max_index, args.width, args.height, args.fps)

    scan_all = args.all or not args.camera
    indices: Iterable[int] = range(args.max_index + 1) if scan_all else args.camera
    captures = open_captures(indices, args.width, args.height, args.fps)
    if not captures:
        if scan_all:
            raise RuntimeError("No usable camera was found.")
        requested = ", ".join(str(index) for index in args.camera)
        raise RuntimeError(f"Cannot open requested camera(s): {requested}")

    try:
        for opened in captures:
            actual_width = int(opened.capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_height = int(opened.capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            print(
                f"Camera {opened.index}: {actual_width}x{actual_height} via {opened.backend}"
            )
        if args.self_test:
            print("Camera self-test passed.")
            return 0
        return show_previews(captures, args.width, args.height, args.fps, args.max_index)
    finally:
        for opened in captures:
            opened.capture.release()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        message = str(error)
        print(message, file=sys.stderr)
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, message, WINDOW_TITLE, 0x10)
        raise SystemExit(1) from error
