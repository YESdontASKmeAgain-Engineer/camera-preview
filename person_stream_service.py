"""Serve CameraPreview's person-only YOLO output to a local Windows viewer."""

from __future__ import annotations

import argparse
import atexit
import threading
import time
from pathlib import Path

import cv2
from flask import Flask, Response, jsonify

from yolo_pipeline import YoloConfig, YoloPipeline, default_model_path


class PersonStreamService:
    """Capture one WSL webcam and publish the latest person-only annotation."""

    def __init__(self, model_path: Path, confidence: float, image_size: int) -> None:
        self.pipeline = YoloPipeline(
            YoloConfig(
                model_path=model_path,
                confidence=confidence,
                image_size=image_size,
            )
        )
        self.capture: cv2.VideoCapture | None = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.latest_jpeg: bytes | None = None
        self.latest_frame_id = 0
        self.error: str | None = None
        self.worker: threading.Thread | None = None

    def start(self) -> None:
        capture = cv2.VideoCapture(0, cv2.CAP_V4L2)
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        capture.set(cv2.CAP_PROP_FPS, 30)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not capture.isOpened():
            capture.release()
            self.pipeline.close()
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
        if self.capture is not None:
            self.capture.release()
        if self.worker is not None and self.worker is not threading.current_thread():
            self.worker.join(timeout=2.0)
        self.pipeline.close()

    def snapshot(self) -> tuple[bytes | None, int, str | None]:
        pipeline_error = self.pipeline.stats().error
        with self.lock:
            return self.latest_jpeg, self.latest_frame_id, self.error or pipeline_error

    def status(self) -> dict[str, object]:
        stats = self.pipeline.stats()
        with self.lock:
            return {
                "device": stats.device_name,
                "fps": stats.fps,
                "frames": self.latest_frame_id,
                "ready": stats.ready,
                "error": self.error or stats.error,
            }

    def _publish(self, frame, frame_id: int) -> None:
        encoded, buffer = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85]
        )
        if not encoded:
            return
        with self.lock:
            self.latest_jpeg = buffer.tobytes()
            self.latest_frame_id = frame_id

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
            self.pipeline.submit("camera", sequence, frame.copy())
            result = self.pipeline.latest("camera")
            if result is None:
                if sequence == 1:
                    self._publish(frame, sequence)
                continue

            result_sequence, annotated = result
            if result_sequence > last_result_sequence:
                self._publish(annotated, result_sequence)
                last_result_sequence = result_sequence


def build_app(service: PersonStreamService) -> Flask:
    app = Flask(__name__)

    @app.get("/status")
    def status():
        return jsonify(service.status())

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
    args = parser.parse_args()

    service = PersonStreamService(Path(args.model), args.conf, args.imgsz)
    service.start()
    atexit.register(service.stop)
    print(f"Person stream: http://127.0.0.1:{args.port}/video_feed")
    build_app(service).run(
        host="127.0.0.1",
        port=args.port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
