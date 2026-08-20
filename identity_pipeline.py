"""Latest-frame-only person detection plus target-identity classification."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Hashable, Mapping, Sequence

import cv2

from yolo_pipeline import PERSON_CLASS_ID, YoloStats


DEFAULT_IDENTITY_CONFIDENCE = 0.80
DEFAULT_IDENTITY_LABEL = "1"
CLASSIFIER_IMAGE_SIZE = 224
DEFAULT_POSE_CONFIDENCE = 0.35
DEFAULT_POSE_MIN_KEYPOINTS = 6
DEFAULT_POSE_KEYPOINT_CONFIDENCE = 0.35
DEFAULT_POSE_IOU_THRESHOLD = 0.10


@dataclass(frozen=True)
class IdentityConfig:
    """Models and thresholds used by the two-stage identity overlay."""

    detector_model_path: Path
    classifier_model_path: Path
    detection_confidence: float = 0.35
    classification_confidence: float = DEFAULT_IDENTITY_CONFIDENCE
    image_size: int = 640
    classifier_image_size: int = CLASSIFIER_IMAGE_SIZE
    device: str | int | None = None
    target_label: str = DEFAULT_IDENTITY_LABEL
    pose_model_path: Path | None = None
    pose_confidence: float = DEFAULT_POSE_CONFIDENCE
    pose_min_keypoints: int = DEFAULT_POSE_MIN_KEYPOINTS
    pose_keypoint_confidence: float = DEFAULT_POSE_KEYPOINT_CONFIDENCE
    pose_iou_threshold: float = DEFAULT_POSE_IOU_THRESHOLD


def default_identity_model_path() -> Path:
    """Find the local classifier trained from Camera Preview samples."""
    configured = os.environ.get("CAMERA_PREVIEW_IDENTITY_MODEL", "").strip()
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(__file__).resolve().parent
        / "training_samples"
        / "people_identity"
        / "runs"
        / "person_1_yolo11n_cls_works_20260820"
        / "weights"
        / "best.pt",
        Path(__file__).resolve().parent
        / "training_samples"
        / "people_identity"
        / "runs"
        / "person_1_yolo11n_cls"
        / "weights"
        / "best.pt",
        Path("/home/yundrone/yolo/models/person_1_yolo11n_cls.pt"),
    ]
    usable = [candidate for candidate in candidates if candidate is not None]
    for candidate in usable:
        if candidate.is_file():
            return candidate
    return usable[0]


def target_class_id(
    names: Mapping[int, str] | Sequence[str], target_label: str
) -> int:
    """Return the classifier class ID whose name equals ``target_label``."""
    items = names.items() if isinstance(names, Mapping) else enumerate(names)
    for class_id, name in items:
        if str(name) == target_label:
            return int(class_id)
    raise ValueError(f"Identity classifier does not contain target label: {target_label}")


def padded_bounds(
    left: float,
    top: float,
    right: float,
    bottom: float,
    frame_width: int,
    frame_height: int,
) -> tuple[int, int, int, int] | None:
    """Pad a detection slightly to match the classifier's training crops."""
    box_width = right - left
    box_height = bottom - top
    if box_width <= 1 or box_height <= 1:
        return None
    padded_left = max(0, round(left - box_width * 0.08))
    padded_top = max(0, round(top - box_height * 0.05))
    padded_right = min(frame_width, round(right + box_width * 0.08))
    padded_bottom = min(frame_height, round(bottom + box_height * 0.05))
    if padded_right - padded_left < 2 or padded_bottom - padded_top < 2:
        return None
    return padded_left, padded_top, padded_right, padded_bottom


def box_iou(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    """Return intersection-over-union for two xyxy boxes."""
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    if intersection <= 0.0:
        return 0.0
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
    union = first_area + second_area - intersection
    return intersection / union if union > 0.0 else 0.0


def pose_match_has_quality(
    detection: tuple[float, float, float, float],
    pose_boxes: Sequence[Sequence[float]],
    keypoint_confidences: Sequence[Sequence[float]],
    min_keypoints: int,
    keypoint_confidence: float,
    iou_threshold: float,
) -> bool:
    """Return true when the best overlapping pose has enough visible keypoints."""
    best_index = -1
    best_iou = 0.0
    for index, pose_box in enumerate(pose_boxes):
        current_iou = box_iou(
            detection, tuple(float(value) for value in pose_box)
        )
        if current_iou > best_iou:
            best_iou = current_iou
            best_index = index
    if (
        best_index < 0
        or best_index >= len(keypoint_confidences)
        or best_iou < iou_threshold
    ):
        return False
    visible_keypoints = sum(
        float(confidence) >= keypoint_confidence
        for confidence in keypoint_confidences[best_index]
    )
    return visible_keypoints >= min_keypoints


class PersonIdentityPipeline:
    """Overlay only the configured identity while keeping newest-frame behavior."""

    def __init__(self, config: IdentityConfig) -> None:
        if not (0.0 <= config.detection_confidence <= 1.0):
            raise ValueError("Detection confidence must be between 0 and 1.")
        if not (0.0 <= config.classification_confidence <= 1.0):
            raise ValueError("Identity confidence must be between 0 and 1.")
        if config.image_size <= 0 or config.classifier_image_size <= 0:
            raise ValueError("Image sizes must be positive.")
        if not config.target_label.strip():
            raise ValueError("Identity target label must not be empty.")
        if not 0.0 <= config.pose_confidence <= 1.0:
            raise ValueError("Pose confidence must be between 0 and 1.")
        if config.pose_min_keypoints < 1:
            raise ValueError("Pose minimum keypoints must be positive.")
        if not 0.0 <= config.pose_keypoint_confidence <= 1.0:
            raise ValueError("Pose keypoint confidence must be between 0 and 1.")
        if not 0.0 <= config.pose_iou_threshold <= 1.0:
            raise ValueError("Pose IoU threshold must be between 0 and 1.")

        self.config = config
        self._condition = threading.Condition()
        self._pending: dict[Hashable, tuple[int, Any]] = {}
        self._results: dict[Hashable, tuple[int, Any, float | None]] = {}
        self._running = True
        self._error: str | None = None
        self._device_name = "YOLO"
        self._fps = 0.0
        self._frames = 0
        self._pose_ready = False
        self._pose_error: str | None = None
        self._worker = threading.Thread(
            target=self._run,
            name="camera-preview-person-identity",
            daemon=True,
        )
        self._worker.start()

    def submit(self, camera_key: Hashable, sequence: int, frame: Any) -> None:
        """Replace an older pending frame for the same camera."""
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
        """Return a copy of the newest annotated identity frame."""
        with self._condition:
            result = self._results.get(camera_key)
            if result is None:
                return None
            sequence, frame, _ = result
            return sequence, frame.copy()

    def latest_with_target(
        self, camera_key: Hashable
    ) -> tuple[int, Any, float | None] | None:
        """Return the newest annotated frame and its target confidence together."""
        with self._condition:
            result = self._results.get(camera_key)
            if result is None:
                return None
            sequence, frame, target_confidence = result
            return sequence, frame.copy(), target_confidence

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

    def pose_status(self) -> dict[str, object]:
        """Return the optional pose gate state for the service status endpoint."""
        with self._condition:
            return {
                "enabled": self.config.pose_model_path is not None,
                "ready": self._pose_ready,
                "error": self._pose_error,
                "model": (
                    str(self.config.pose_model_path)
                    if self.config.pose_model_path is not None
                    else None
                ),
                "min_keypoints": self.config.pose_min_keypoints,
                "keypoint_confidence": self.config.pose_keypoint_confidence,
            }

    def close(self) -> None:
        with self._condition:
            self._running = False
            self._pending.clear()
            self._condition.notify_all()
        if self._worker is not threading.current_thread():
            self._worker.join(timeout=3.0)

    def _load_models(self):
        if (
            self.config.pose_model_path is None
            and not self.config.detector_model_path.is_file()
        ):
            raise FileNotFoundError(
                f"YOLO detector was not found: {self.config.detector_model_path}"
            )
        if not self.config.classifier_model_path.is_file():
            raise FileNotFoundError(
                "Identity classifier was not found: "
                f"{self.config.classifier_model_path}"
            )
        if self.config.pose_model_path is not None and not self.config.pose_model_path.is_file():
            raise FileNotFoundError(
                f"Pose model was not found: {self.config.pose_model_path}"
            )

        try:
            import torch
            from ultralytics import YOLO
        except ImportError as error:
            raise RuntimeError(
                "Identity overlay needs ultralytics and torch in the active Python environment."
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

        detector = None
        if self.config.pose_model_path is None:
            detector = YOLO(str(self.config.detector_model_path))
        classifier = YOLO(str(self.config.classifier_model_path))
        pose_model = None
        if self.config.pose_model_path is not None:
            pose_model = YOLO(str(self.config.pose_model_path))
            with self._condition:
                self._pose_ready = True
        class_id = target_class_id(classifier.names, self.config.target_label)
        return detector, classifier, pose_model, device, class_id

    def _fail(self, error: Exception) -> None:
        with self._condition:
            self._error = str(error)
            self._running = False
            self._pending.clear()
            self._condition.notify_all()

    def _pose_matches_detection(self, detection, pose_result) -> bool:
        """Check whether a detector box has enough visible pose keypoints."""
        if pose_result is None:
            return True
        pose_boxes = getattr(getattr(pose_result, "boxes", None), "xyxy", None)
        keypoints = getattr(getattr(pose_result, "keypoints", None), "conf", None)
        if pose_boxes is None or keypoints is None:
            return False
        pose_box_values = pose_boxes.detach().cpu().tolist()
        keypoint_values = keypoints.detach().cpu().tolist()
        return pose_match_has_quality(
            detection,
            pose_box_values,
            keypoint_values,
            self.config.pose_min_keypoints,
            self.config.pose_keypoint_confidence,
            self.config.pose_iou_threshold,
        )

    def _annotate(
        self,
        frame,
        detector_result,
        classifier,
        device,
        class_id: int,
        pose_result=None,
    ) -> tuple[Any, float | None]:
        annotated = frame.copy()
        boxes = detector_result.boxes
        if boxes is None or len(boxes) == 0:
            return annotated, None

        frame_height, frame_width = frame.shape[:2]
        detections: list[tuple[int, int, int, int]] = []
        crops: list[Any] = []
        for box in boxes:
            left, top, right, bottom = box.xyxy[0].detach().cpu().tolist()
            if pose_result is not None:
                # Pose boxes already tightly enclose a person. Extra padding can
                # pull nearby people into the identity classifier crop.
                bounds = (
                    max(0, round(left)),
                    max(0, round(top)),
                    min(frame_width, round(right)),
                    min(frame_height, round(bottom)),
                )
            else:
                bounds = padded_bounds(
                    left, top, right, bottom, frame_width, frame_height
                )
            if bounds is None:
                continue
            padded_left, padded_top, padded_right, padded_bottom = bounds
            crop = frame[padded_top:padded_bottom, padded_left:padded_right]
            if crop.size == 0:
                continue
            detections.append(
                (
                    max(0, round(left)),
                    max(0, round(top)),
                    min(frame_width, round(right)),
                    min(frame_height, round(bottom)),
                )
            )
            crops.append(crop)

        if not crops:
            return annotated, None
        classifications = classifier.predict(
            crops,
            imgsz=self.config.classifier_image_size,
            device=device,
            batch=len(crops),
            verbose=False,
        )
        target_confidence: float | None = None
        for (left, top, right, bottom), classification in zip(
            detections, classifications, strict=True
        ):
            probability = float(classification.probs.data[class_id].item())
            if probability < self.config.classification_confidence:
                continue
            if not self._pose_matches_detection(
                (float(left), float(top), float(right), float(bottom)), pose_result
            ):
                continue
            if target_confidence is None or probability > target_confidence:
                target_confidence = probability
            self._draw_target(annotated, left, top, right, bottom, probability)
        return annotated, target_confidence

    def _draw_target(
        self, frame, left: int, top: int, right: int, bottom: int, probability: float
    ) -> None:
        color = (0, 220, 70)
        cv2.rectangle(frame, (left, top), (right, bottom), color, 2)
        text = f"{self.config.target_label} {probability:.0%}"
        (text_width, text_height), baseline = cv2.getTextSize(
            text, cv2.FONT_HERSHEY_SIMPLEX, 0.65, 2
        )
        label_top = max(0, top - text_height - baseline - 8)
        cv2.rectangle(
            frame,
            (left, label_top),
            (min(frame.shape[1], left + text_width + 10), top),
            color,
            thickness=-1,
        )
        cv2.putText(
            frame,
            text,
            (left + 5, top - baseline - 4),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )

    def _run(self) -> None:
        try:
            detector, classifier, pose_model, device, class_id = self._load_models()
        except Exception as error:
            with self._condition:
                self._pose_error = str(error) if self.config.pose_model_path else None
            self._fail(error)
            return

        while True:
            with self._condition:
                while self._running and not self._pending:
                    self._condition.wait(timeout=0.25)
                if not self._running:
                    return
                camera_key, (sequence, frame) = min(
                    self._pending.items(), key=lambda item: item[1][0]
                )
                del self._pending[camera_key]

            started = time.perf_counter()
            try:
                pose_result = None
                if pose_model is not None:
                    pose_result = pose_model.predict(
                        frame,
                        conf=self.config.pose_confidence,
                        imgsz=self.config.image_size,
                        device=device,
                        verbose=False,
                    )[0]
                    detector_result = pose_result
                else:
                    if detector is None:
                        raise RuntimeError("Person detector was not initialized.")
                    detector_result = detector.predict(
                        frame,
                        conf=self.config.detection_confidence,
                        imgsz=self.config.image_size,
                        device=device,
                        classes=[PERSON_CLASS_ID],
                        verbose=False,
                    )[0]
                annotated, target_confidence = self._annotate(
                    frame,
                    detector_result,
                    classifier,
                    device,
                    class_id,
                    pose_result,
                )
            except Exception as error:
                if pose_model is not None:
                    with self._condition:
                        self._pose_error = str(error)
                self._fail(error)
                return

            elapsed = max(time.perf_counter() - started, 1e-6)
            inference_fps = 1.0 / elapsed
            with self._condition:
                self._results[camera_key] = (
                    sequence,
                    annotated,
                    target_confidence,
                )
                self._fps = (
                    inference_fps
                    if self._frames == 0
                    else self._fps * 0.8 + inference_fps * 0.2
                )
                self._frames += 1
