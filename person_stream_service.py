"""Serve CameraPreview's person-only YOLO output to a local Windows viewer."""

from __future__ import annotations

import argparse
import atexit
import os
import threading
import time
from pathlib import Path

import cv2
from flask import Flask, Response, jsonify, request

from identity_pipeline import (
    DEFAULT_IDENTITY_CONFIDENCE,
    DEFAULT_IDENTITY_LABEL,
    DEFAULT_POSE_CONFIDENCE,
    DEFAULT_POSE_KEYPOINT_CONFIDENCE,
    DEFAULT_POSE_MIN_KEYPOINTS,
    IdentityConfig,
    PersonIdentityPipeline,
    default_identity_model_path,
)
from yolo_pipeline import YoloConfig, YoloPipeline, default_model_path


DEFAULT_SAMPLE_INTERVAL_SECONDS = 1.0
MIN_SAMPLE_INTERVAL_SECONDS = 0.2
MAX_SAMPLE_INTERVAL_SECONDS = 10.0
SAMPLE_JPEG_QUALITY = 95
DEFAULT_TARGET_CAPTURE_REARM_SECONDS = 1.0
TARGET_CAPTURE_JPEG_QUALITY = 95


def default_sample_directory() -> Path:
    """Return a local, Git-ignored directory for raw training images."""
    configured = os.environ.get("CAMERA_PREVIEW_SAMPLE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return (
        Path(__file__).resolve().parent
        / "training_samples"
        / "target_person"
        / "images"
        / "raw"
    )


def default_target_capture_directory() -> Path:
    """Save automatic captures beside the Windows application's own screenshots."""
    configured = os.environ.get("CAMERA_PREVIEW_TARGET_CAPTURE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    project_directory = Path(__file__).resolve().parent
    packaged_directory = project_directory / "dist" / "CameraPreview"
    if packaged_directory.is_dir():
        return packaged_directory / "screenshots"
    return project_directory / "screenshots"


class PersonStreamService:
    """Capture one WSL webcam and publish the latest person-only annotation."""

    def __init__(
        self,
        model_path: Path,
        confidence: float,
        image_size: int,
        sample_directory: Path | None = None,
        identity_model_path: Path | None = None,
        identity_confidence: float = DEFAULT_IDENTITY_CONFIDENCE,
        identity_label: str = DEFAULT_IDENTITY_LABEL,
        target_capture_directory: Path | None = None,
        pose_model_path: Path | None = None,
        pose_confidence: float = DEFAULT_POSE_CONFIDENCE,
        pose_min_keypoints: int = DEFAULT_POSE_MIN_KEYPOINTS,
        pose_keypoint_confidence: float = DEFAULT_POSE_KEYPOINT_CONFIDENCE,
    ) -> None:
        self.yolo_config = YoloConfig(
            model_path=model_path,
            confidence=confidence,
            image_size=image_size,
        )
        self.identity_config = (
            IdentityConfig(
                detector_model_path=model_path,
                classifier_model_path=identity_model_path,
                detection_confidence=confidence,
                classification_confidence=identity_confidence,
                image_size=image_size,
                target_label=identity_label,
                pose_model_path=pose_model_path,
                pose_confidence=pose_confidence,
                pose_min_keypoints=pose_min_keypoints,
                pose_keypoint_confidence=pose_keypoint_confidence,
            )
            if identity_model_path is not None
            else None
        )
        self.pipeline: YoloPipeline | PersonIdentityPipeline | None = self._new_pipeline()
        self.yolo_enabled = True
        self.capture: cv2.VideoCapture | None = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.latest_jpeg: bytes | None = None
        self.latest_frame_id = 0
        self.latest_raw_frame_id = 0
        self.error: str | None = None
        self.worker: threading.Thread | None = None
        self.sample_directory = sample_directory or default_sample_directory()
        self.sample_collection_active = False
        self.sample_interval_seconds = DEFAULT_SAMPLE_INTERVAL_SECONDS
        self.next_sample_at = 0.0
        self.sample_count = 0
        self.last_sample_path: Path | None = None
        self.sample_error: str | None = None
        self.target_capture_directory = (
            target_capture_directory or default_target_capture_directory()
        )
        self.target_capture_enabled = False
        self._target_capture_armed = True
        self._target_last_seen_at: float | None = None
        self.target_visible = False
        self.target_confidence: float | None = None
        self.target_capture_count = 0
        self.last_target_capture_path: Path | None = None
        self.target_capture_error: str | None = None

    def _new_pipeline(self) -> YoloPipeline | PersonIdentityPipeline:
        if self.identity_config is not None:
            return PersonIdentityPipeline(self.identity_config)
        return YoloPipeline(self.yolo_config)

    def start(self) -> None:
        capture = cv2.VideoCapture(0, cv2.CAP_V4L2)
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        capture.set(cv2.CAP_PROP_FPS, 30)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not capture.isOpened():
            capture.release()
            pipeline = self.pipeline
            if pipeline is not None:
                pipeline.close()
            raise RuntimeError(
                "Could not open /dev/video0. Attach USB Camera3 to WSL first."
            )
        self.capture = capture
        self.worker = threading.Thread(
            target=self._run,
            name="camera-preview-person-stream",
            daemon=True,
        )
        self.worker.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            self.sample_collection_active = False
            pipeline = self.pipeline
            self.pipeline = None
        if self.capture is not None:
            self.capture.release()
        if self.worker is not None and self.worker is not threading.current_thread():
            self.worker.join(timeout=2.0)
        if pipeline is not None:
            pipeline.close()

    def snapshot(self) -> tuple[bytes | None, int, str | None]:
        with self.lock:
            pipeline = self.pipeline
        pipeline_error = pipeline.stats().error if pipeline is not None else None
        with self.lock:
            return self.latest_jpeg, self.latest_frame_id, self.error or pipeline_error

    def status(self) -> dict[str, object]:
        with self.lock:
            pipeline = self.pipeline
            yolo_enabled = self.yolo_enabled
            latest_raw_frame_id = self.latest_raw_frame_id
            error = self.error
            collection = {
                "active": self.sample_collection_active,
                "count": self.sample_count,
                "interval_seconds": self.sample_interval_seconds,
                "directory": str(self.sample_directory),
                "last_sample": (
                    str(self.last_sample_path) if self.last_sample_path is not None else None
                ),
                "error": self.sample_error,
            }
            target_capture = {
                "enabled": self.target_capture_enabled,
                "count": self.target_capture_count,
                "directory": str(self.target_capture_directory),
                "last_capture": (
                    str(self.last_target_capture_path)
                    if self.last_target_capture_path is not None
                    else None
                ),
                "error": self.target_capture_error,
            }
            target_visible = self.target_visible
            target_confidence = self.target_confidence
            latest_frame_id = self.latest_frame_id

        stats = pipeline.stats() if pipeline is not None else None
        pose_status_method = getattr(pipeline, "pose_status", None)
        if callable(pose_status_method):
            pose = pose_status_method()
        else:
            pose = {
                "enabled": False,
                "ready": False,
                "error": None,
                "model": None,
                "min_keypoints": None,
                "keypoint_confidence": None,
            }
        pose["enabled"] = bool(pose["enabled"] and yolo_enabled)
        pose["ready"] = bool(pose["ready"] and yolo_enabled)
        yolo_ready = bool(stats is not None and stats.ready)
        yolo_error = stats.error if stats is not None else None
        return {
            "device": stats.device_name if stats is not None else "YOLO disabled",
            "fps": stats.fps if stats is not None else 0.0,
            "frames": latest_frame_id,
            "ready": yolo_ready if yolo_enabled else latest_raw_frame_id > 0,
            "error": error or yolo_error,
            "yolo": {
                "enabled": yolo_enabled,
                "ready": yolo_ready,
                "error": yolo_error,
            },
            "identity": {
                "enabled": self.identity_config is not None and yolo_enabled,
                "ready": yolo_ready if self.identity_config is not None else False,
                "error": yolo_error if self.identity_config is not None else None,
                "label": (
                    self.identity_config.target_label
                    if self.identity_config is not None
                    else None
                ),
                "confidence": (
                    self.identity_config.classification_confidence
                    if self.identity_config is not None
                    else None
                ),
                "model": (
                    str(self.identity_config.classifier_model_path)
                    if self.identity_config is not None
                    else None
                ),
                "target_visible": (
                    target_visible
                    if self.identity_config is not None and yolo_enabled
                    else False
                ),
                "target_confidence": (
                    target_confidence
                    if self.identity_config is not None
                    and yolo_enabled
                    and target_visible
                    else None
                ),
            },
            "pose": pose,
            "collection": collection,
            "target_capture": target_capture,
        }

    def set_yolo_enabled(self, enabled: bool) -> dict[str, object]:
        """Enable or disable inference without interrupting the camera stream."""
        if not isinstance(enabled, bool):
            raise ValueError("YOLO enabled must be true or false.")

        pipeline_to_close: YoloPipeline | PersonIdentityPipeline | None = None
        with self.lock:
            if enabled and not self.yolo_enabled:
                self.pipeline = self._new_pipeline()
                self.yolo_enabled = True
                if self.target_capture_enabled:
                    self._target_capture_armed = True
            elif not enabled and self.yolo_enabled:
                pipeline_to_close = self.pipeline
                self.pipeline = None
                self.yolo_enabled = False
                self.target_visible = False
                self.target_confidence = None
                self._target_last_seen_at = None

        if pipeline_to_close is not None:
            pipeline_to_close.close()
        return self.status()

    def set_target_capture(self, enabled: bool) -> dict[str, object]:
        """Enable one annotated screenshot each time the configured target appears."""
        if not isinstance(enabled, bool):
            raise ValueError("Target capture enabled must be true or false.")
        if self.identity_config is None:
            raise RuntimeError("Target capture needs the identity classifier to be enabled.")
        if enabled:
            try:
                self.target_capture_directory.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise RuntimeError(
                    "Could not create target capture directory: "
                    f"{self.target_capture_directory}"
                ) from error

        with self.lock:
            was_enabled = self.target_capture_enabled
            self.target_capture_enabled = enabled
            self._target_capture_armed = True
            if enabled and not was_enabled:
                self.target_capture_count = 0
                self.last_target_capture_path = None
                self.target_capture_error = None
        return self.status()

    def set_sample_collection(
        self, enabled: bool, interval_seconds: float | None = None
    ) -> dict[str, object]:
        """Start or stop saving raw camera frames at a bounded interval."""
        if not isinstance(enabled, bool):
            raise ValueError("Sample collection enabled must be true or false.")
        if interval_seconds is None:
            interval_seconds = self.sample_interval_seconds
        try:
            interval_seconds = float(interval_seconds)
        except (TypeError, ValueError) as error:
            raise ValueError("Sample interval must be a number.") from error
        if not MIN_SAMPLE_INTERVAL_SECONDS <= interval_seconds <= MAX_SAMPLE_INTERVAL_SECONDS:
            raise ValueError(
                "Sample interval must be between "
                f"{MIN_SAMPLE_INTERVAL_SECONDS:g} and {MAX_SAMPLE_INTERVAL_SECONDS:g} seconds."
            )
        if enabled:
            try:
                self.sample_directory.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise RuntimeError(
                    f"Could not create sample directory: {self.sample_directory}"
                ) from error

        with self.lock:
            was_active = self.sample_collection_active
            self.sample_interval_seconds = interval_seconds
            self.sample_collection_active = enabled
            self.next_sample_at = 0.0
            if enabled and not was_active:
                self.sample_count = 0
                self.last_sample_path = None
                self.sample_error = None
        return self.status()

    def _publish(self, frame, frame_id: int) -> None:
        encoded, buffer = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85]
        )
        if not encoded:
            return
        with self.lock:
            self.latest_jpeg = buffer.tobytes()
            self.latest_frame_id = frame_id

    def _maybe_save_sample(self, frame, frame_id: int) -> None:
        now = time.monotonic()
        with self.lock:
            if (
                not self.sample_collection_active
                or now < self.next_sample_at
            ):
                return
            self.next_sample_at = now + self.sample_interval_seconds
            directory = self.sample_directory

        filename = f"target_person_{time.time_ns()}_{frame_id:06d}.jpg"
        output_path = directory / filename
        try:
            saved = cv2.imwrite(
                str(output_path),
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), SAMPLE_JPEG_QUALITY],
            )
            if not saved:
                raise OSError("OpenCV could not encode the JPEG image.")
        except (cv2.error, OSError) as error:
            with self.lock:
                self.sample_collection_active = False
                self.sample_error = f"Could not save sample: {error}"
            return

        with self.lock:
            self.sample_count += 1
            self.last_sample_path = output_path
            self.sample_error = None

    def _maybe_save_target_capture(
        self, frame, frame_id: int, target_confidence: float | None
    ) -> None:
        """Save one annotated frame for each distinct target appearance."""
        now = time.monotonic()
        with self.lock:
            if target_confidence is None:
                self.target_visible = False
                self.target_confidence = None
                if (
                    self._target_last_seen_at is None
                    or now - self._target_last_seen_at
                    >= DEFAULT_TARGET_CAPTURE_REARM_SECONDS
                ):
                    self._target_capture_armed = True
                return

            self.target_visible = True
            self.target_confidence = target_confidence
            self._target_last_seen_at = now
            if not self.target_capture_enabled or not self._target_capture_armed:
                return
            self._target_capture_armed = False
            directory = self.target_capture_directory

        output_path = directory / f"target_1_{time.time_ns()}_{frame_id:06d}.jpg"
        try:
            saved = cv2.imwrite(
                str(output_path),
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), TARGET_CAPTURE_JPEG_QUALITY],
            )
            if not saved:
                raise OSError("OpenCV could not encode the JPEG image.")
        except (cv2.error, OSError) as error:
            with self.lock:
                self.target_capture_error = f"Could not save target capture: {error}"
            return

        with self.lock:
            self.target_capture_count += 1
            self.last_target_capture_path = output_path
            self.target_capture_error = None

    def _run(self) -> None:
        assert self.capture is not None
        sequence = 0
        last_result_sequence = 0
        failed_reads = 0
        while not self.stop_event.is_set():
            ok, frame = self.capture.read()
            if not ok or frame is None:
                failed_reads += 1
                if failed_reads >= 30:
                    with self.lock:
                        self.error = "Camera is not returning frames."
                time.sleep(0.03)
                continue

            failed_reads = 0
            sequence += 1
            with self.lock:
                self.latest_raw_frame_id = sequence
                pipeline = self.pipeline if self.yolo_enabled else None
            self._maybe_save_sample(frame, sequence)

            if pipeline is None:
                self._publish(frame, sequence)
                continue

            pipeline.submit("camera", sequence, frame.copy())
            target_confidence: float | None = None
            if isinstance(pipeline, PersonIdentityPipeline):
                result = pipeline.latest_with_target("camera")
            else:
                result = pipeline.latest("camera")
            if result is None:
                if sequence == 1 or pipeline.stats().error is not None:
                    self._publish(frame, sequence)
                continue

            if isinstance(pipeline, PersonIdentityPipeline):
                result_sequence, annotated, target_confidence = result
            else:
                result_sequence, annotated = result
            if result_sequence > last_result_sequence:
                if isinstance(pipeline, PersonIdentityPipeline):
                    self._maybe_save_target_capture(
                        annotated, result_sequence, target_confidence
                    )
                self._publish(annotated, result_sequence)
                last_result_sequence = result_sequence


def build_app(service: PersonStreamService) -> Flask:
    app = Flask(__name__)

    def control_payload() -> dict[str, object]:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object.")
        return payload

    @app.get("/status")
    def status():
        return jsonify(service.status())

    @app.post("/control/yolo")
    def control_yolo():
        try:
            payload = control_payload()
            return jsonify(service.set_yolo_enabled(payload.get("enabled")))
        except (RuntimeError, ValueError) as error:
            return jsonify({"error": str(error)}), 400

    @app.post("/control/collection")
    def control_collection():
        try:
            payload = control_payload()
            return jsonify(
                service.set_sample_collection(
                    payload.get("enabled"),
                    payload.get("interval_seconds"),
                )
            )
        except (RuntimeError, ValueError) as error:
            return jsonify({"error": str(error)}), 400

    @app.post("/control/target-capture")
    def control_target_capture():
        try:
            payload = control_payload()
            return jsonify(service.set_target_capture(payload.get("enabled")))
        except (RuntimeError, ValueError) as error:
            return jsonify({"error": str(error)}), 400

    @app.get("/video_feed")
    def video_feed():
        def stream():
            last_frame_id = -1
            while not service.stop_event.is_set():
                jpeg, frame_id, _ = service.snapshot()
                if jpeg is not None and frame_id != last_frame_id:
                    yield (
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(jpeg)).encode("ascii")
                        + b"\r\n\r\n"
                        + jpeg
                        + b"\r\n"
                    )
                    last_frame_id = frame_id
                time.sleep(1.0 / 30.0)

        return Response(stream(), mimetype="multipart/x-mixed-replace; boundary=frame")

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--model", default=str(default_model_path()))
    parser.add_argument("--conf", type=float, default=0.35)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--sample-dir", default=str(default_sample_directory()))
    parser.add_argument(
        "--target-capture-dir",
        default=str(default_target_capture_directory()),
        help="Directory for annotated screenshots captured when the target appears.",
    )
    parser.add_argument(
        "--identity-model",
        default=str(default_identity_model_path()),
        help="YOLO classification weights used to identify label '1'.",
    )
    parser.add_argument(
        "--identity-conf",
        type=float,
        default=DEFAULT_IDENTITY_CONFIDENCE,
        help="Minimum classifier probability for drawing the target label.",
    )
    parser.add_argument(
        "--identity-label",
        default=DEFAULT_IDENTITY_LABEL,
        help="Classifier class name to draw in the stream.",
    )
    parser.add_argument(
        "--pose-model",
        default=None,
        help="Optional YOLO pose weights used to reject low-quality identity crops.",
    )
    parser.add_argument(
        "--pose-conf",
        type=float,
        default=DEFAULT_POSE_CONFIDENCE,
        help="Minimum confidence for pose person detections.",
    )
    parser.add_argument(
        "--pose-min-keypoints",
        type=int,
        default=DEFAULT_POSE_MIN_KEYPOINTS,
        help="Visible keypoints required before accepting an identity match.",
    )
    parser.add_argument(
        "--pose-keypoint-conf",
        type=float,
        default=DEFAULT_POSE_KEYPOINT_CONFIDENCE,
        help="Confidence required for a pose keypoint to count as visible.",
    )
    parser.add_argument(
        "--disable-identity",
        action="store_true",
        help="Use the original person-only overlay instead of the identity classifier.",
    )
    args = parser.parse_args()

    identity_model_path = None
    if not args.disable_identity:
        candidate = Path(args.identity_model).expanduser()
        if candidate.is_file():
            identity_model_path = candidate
        else:
            print(f"Identity classifier not found; using person-only overlay: {candidate}")

    pose_model_path = None
    if args.pose_model and identity_model_path is not None:
        candidate = Path(args.pose_model).expanduser()
        if candidate.is_file():
            pose_model_path = candidate
        else:
            print(f"Pose model not found; continuing without pose filtering: {candidate}")

    service = PersonStreamService(
        model_path=Path(args.model),
        confidence=args.conf,
        image_size=args.imgsz,
        sample_directory=Path(args.sample_dir).expanduser(),
        identity_model_path=identity_model_path,
        identity_confidence=args.identity_conf,
        identity_label=args.identity_label,
        target_capture_directory=Path(args.target_capture_dir).expanduser(),
        pose_model_path=pose_model_path,
        pose_confidence=args.pose_conf,
        pose_min_keypoints=args.pose_min_keypoints,
        pose_keypoint_confidence=args.pose_keypoint_conf,
    )
    service.start()
    atexit.register(service.stop)
    print(f"Person stream: http://127.0.0.1:{args.port}/video_feed")
    print(f"Raw training samples: {service.sample_directory}")
    print(f"Target screenshots: {service.target_capture_directory}")
    if identity_model_path is not None:
        print(
            "Identity overlay: "
            f"{args.identity_label} ({identity_model_path}, conf={args.identity_conf:g})"
        )
    if pose_model_path is not None:
        print(
            "Pose quality gate: "
            f"{pose_model_path} (min keypoints={args.pose_min_keypoints})"
        )
    build_app(service).run(
        host="127.0.0.1",
        port=args.port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
