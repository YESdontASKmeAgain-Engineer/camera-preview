import importlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

try:
    import flask  # noqa: F401
except ImportError:
    FLASK_AVAILABLE = False
else:
    FLASK_AVAILABLE = True


class _FakePipeline:
    instances = []

    def __init__(self, config) -> None:
        self.config = config
        self.closed = False
        type(self).instances.append(self)

    def stats(self):
        return SimpleNamespace(
            device_name="Fake GPU",
            fps=42.0,
            frames=1,
            error=None,
            ready=True,
        )

    def close(self) -> None:
        self.closed = True


@unittest.skipUnless(FLASK_AVAILABLE, "Flask is only required in the WSL stream service")
class PersonStreamServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = importlib.import_module("person_stream_service")
        _FakePipeline.instances.clear()

    def test_controls_toggle_yolo_and_save_raw_sample(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sample_directory = Path(directory) / "raw"
            with patch.object(self.module, "YoloPipeline", _FakePipeline):
                service = self.module.PersonStreamService(
                    Path(directory) / "model.pt",
                    confidence=0.35,
                    image_size=640,
                    sample_directory=sample_directory,
                )

                self.assertTrue(service.status()["yolo"]["enabled"])
                disabled = service.set_yolo_enabled(False)
                self.assertFalse(disabled["yolo"]["enabled"])
                self.assertTrue(_FakePipeline.instances[0].closed)

                enabled = service.set_yolo_enabled(True)
                self.assertTrue(enabled["yolo"]["enabled"])
                self.assertEqual(len(_FakePipeline.instances), 2)

                active = service.set_sample_collection(True, 1.0)
                self.assertTrue(active["collection"]["active"])
                frame = np.zeros((12, 16, 3), dtype=np.uint8)
                service._maybe_save_sample(frame, 7)

                saved_images = list(sample_directory.glob("*.jpg"))
                self.assertEqual(len(saved_images), 1)
                saved_frame = cv2.imread(str(saved_images[0]))
                self.assertEqual(saved_frame.shape[:2], (12, 16))
                self.assertEqual(service.status()["collection"]["count"], 1)

    def test_control_routes_return_current_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(self.module, "YoloPipeline", _FakePipeline):
                service = self.module.PersonStreamService(
                    Path(directory) / "model.pt",
                    confidence=0.35,
                    image_size=640,
                    sample_directory=Path(directory) / "raw",
                )
                client = self.module.build_app(service).test_client()

                response = client.post("/control/yolo", json={"enabled": False})
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.get_json()["yolo"]["enabled"])

                response = client.post(
                    "/control/collection",
                    json={"enabled": True, "interval_seconds": 1.0},
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["collection"]["active"])

                response = client.post("/control/yolo", json={"enabled": "yes"})
                self.assertEqual(response.status_code, 400)

    def test_target_capture_saves_once_until_the_target_leaves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target_capture_directory = Path(directory) / "screenshots"
            with patch.object(self.module, "PersonIdentityPipeline", _FakePipeline):
                service = self.module.PersonStreamService(
                    Path(directory) / "model.pt",
                    confidence=0.35,
                    image_size=640,
                    identity_model_path=Path(directory) / "identity.pt",
                    target_capture_directory=target_capture_directory,
                )
                enabled = service.set_target_capture(True)
                self.assertTrue(enabled["target_capture"]["enabled"])

                frame = np.zeros((12, 16, 3), dtype=np.uint8)
                service._maybe_save_target_capture(frame, 7, 0.91)
                service._maybe_save_target_capture(frame, 8, 0.93)
                self.assertEqual(len(list(target_capture_directory.glob("*.jpg"))), 1)
                self.assertEqual(service.status()["target_capture"]["count"], 1)

                service._target_last_seen_at = time.monotonic() - 2.0
                service._maybe_save_target_capture(frame, 9, None)
                service._maybe_save_target_capture(frame, 10, 0.92)
                self.assertEqual(len(list(target_capture_directory.glob("*.jpg"))), 2)

    def test_target_capture_control_route_updates_the_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(self.module, "PersonIdentityPipeline", _FakePipeline):
                service = self.module.PersonStreamService(
                    Path(directory) / "model.pt",
                    confidence=0.35,
                    image_size=640,
                    identity_model_path=Path(directory) / "identity.pt",
                    target_capture_directory=Path(directory) / "screenshots",
                )
                client = self.module.build_app(service).test_client()

                response = client.post(
                    "/control/target-capture", json={"enabled": True}
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["target_capture"]["enabled"])


if __name__ == "__main__":
    unittest.main()
