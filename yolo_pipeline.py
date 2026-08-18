"""Optional, latest-frame-only YOLO inference for Camera Preview."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Hashable


PERSON_CLASS_ID = 0


@dataclass(frozen=True)
class YoloConfig:
    """Configuration passed to the background inference worker."""

    model_path: Path
    confidence: float = 0.35
    image_size: int = 640
    device: str | int | None = None
    classes: tuple[int, ...] = (PERSON_CLASS_ID,)


@dataclass(frozen=True)
class YoloStats:
    """Small immutable snapshot used by the GUI without sharing mutable state."""

    device_name: str
    fps: float
    frames: int
    error: str | None
    ready: bool


def default_model_path() -> Path:
    """Find the model used by the WSL deployment, while supporting local builds."""
    configured = os.environ.get("CAMERA_PREVIEW_YOLO_MODEL", "").strip()
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(__file__).resolve().parent / "models" / "yolo11n.pt",
        Path("/home/yundrone/yolo/models/yolo11n.pt"),
        Path("/home/yundrone/yolo/yolo11n.pt"),
    ]
    usable = [candidate for candidate in candidates if candidate is not None]
    for candidate in usable:
        if candidate.is_file():
            return candidate
    return usable[0]


class YoloPipeline:
    """Run one sequential YOLO worker while keeping only the newest input frame."""

    def __init__(self, config: YoloConfig) -> None:
        if not (0.0 <= config.confidence <= 1.0):
            raise ValueError("YOLO confidence must be between 0 and 1.")
        if config.image_size <= 0:
            raise ValueError("YOLO image size must be positive.")

        self.config = config
        self._condition = threading.Condition()
        self._pending: dict[Hashable, tuple[int, Any]] = {}
        self._results: dict[Hashable, tuple[int, Any]] = {}
        self._running = True
        self._error: str | None = None
        self._device_name = "YOLO"
        self._fps = 0.0
        self._frames = 0
        self._worker = threading.Thread(
            target=self._run,
            name="camera-preview-yolo",
            daemon=True,
        )
        self._worker.start()

    def submit(self, camera_key: Hashable, sequence: int, frame: Any) -> None:
        """Replace an older pending frame for this camera."""
        if sequence <= 0:
            return
        with self._condition:
            if not self._running or self._error is not None:
                return
            previous = self._pending.get(camera_key)
            if previous is None or sequence > previous[0]:
                self._pending[camera_key] = (sequence, frame)
                self._condition.notify()

    def latest(self, camera_key: Hashable) -> tuple[int, Any] | None:
        """Return a copy of the latest annotated result, if one exists."""
        with self._condition:
            result = self._results.get(camera_key)
            if result is None:
                return None
            sequence, frame = result
            return sequence, frame.copy()

    def remove(self, camera_key: Hashable) -> None:
        with self._condition:
            self._pending.pop(camera_key, None)
            self._results.pop(camera_key, None)

    def stats(self) -> YoloStats:
        with self._condition:
            return YoloStats(
                device_name=self._device_name,
                fps=self._fps,
                frames=self._frames,
                error=self._error,
                ready=self._frames > 0 and self._error is None,
            )

    def close(self) -> None:
        with self._condition:
            self._running = False
            self._pending.clear()
            self._condition.notify_all()
        if self._worker is not threading.current_thread():
            self._worker.join(timeout=3.0)

    def _load_model(self):
        if not self.config.model_path.is_file():
            raise FileNotFoundError(
                f"YOLO model was not found: {self.config.model_path}"
            )

        try:
            import torch
            from ultralytics import YOLO
        except ImportError as error:
            raise RuntimeError(
                "YOLO needs ultralytics and torch in the active Python environment."
            ) from error

        cuda_available = bool(torch.cuda.is_available())
        device = self.config.device
        if device is None:
            device = 0 if cuda_available else "cpu"
        if cuda_available and str(device).lower() not in {"cpu", "mps"}:
            try:
                self._device_name = torch.cuda.get_device_name(0)
            except Exception:
                self._device_name = f"CUDA ({device})"
        else:
            self._device_name = "CPU"
        return YOLO(str(self.config.model_path)), device

    def _fail(self, error: Exception) -> None:
        with self._condition:
            self._error = str(error)
            self._running = False
            self._pending.clear()
            self._condition.notify_all()

    def _run(self) -> None:
        try:
            model, device = self._load_model()
        except Exception as error:
            self._fail(error)
            return

        while True:
            with self._condition:
                while self._running and not self._pending:
                    self._condition.wait(timeout=0.25)
                if not self._running:
                    return
                camera_key, (sequence, frame) = min(
                    self._pending.items(),
                    key=lambda item: item[1][0],
                )
                del self._pending[camera_key]

            started = time.perf_counter()
            try:
                result = model.predict(
                    frame,
                    conf=self.config.confidence,
                    imgsz=self.config.image_size,
                    device=device,
                    classes=list(self.config.classes),
                    verbose=False,
                )[0]
                annotated = result.plot()
            except Exception as error:
                self._fail(error)
                return

            elapsed = max(time.perf_counter() - started, 1e-6)
            inference_fps = 1.0 / elapsed
            with self._condition:
                self._results[camera_key] = (sequence, annotated)
                self._fps = (
                    inference_fps
                    if self._frames == 0
                    else self._fps * 0.8 + inference_fps * 0.2
                )
                self._frames += 1
