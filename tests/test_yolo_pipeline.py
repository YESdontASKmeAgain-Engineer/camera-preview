import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from yolo_pipeline import YoloConfig, YoloPipeline


class _FakeResult:
    def __init__(self, frame):
        self.frame = frame

    def plot(self):
        return self.frame + 1


class _FakeModel:
    last_kwargs = None

    def __init__(self, _path):
        pass

    def predict(self, frame, **kwargs):
        type(self).last_kwargs = kwargs
        return [_FakeResult(frame)]


class YoloPipelineTests(unittest.TestCase):
    def test_background_worker_returns_latest_annotation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "model.pt"
            model_path.write_bytes(b"test")
            fake_torch = types.SimpleNamespace(
                cuda=types.SimpleNamespace(
                    is_available=lambda: False,
                    get_device_name=lambda _index: "Fake GPU",
                )
            )
            fake_ultralytics = types.SimpleNamespace(YOLO=_FakeModel)
            with patch.dict(
                sys.modules,
                {"torch": fake_torch, "ultralytics": fake_ultralytics},
            ):
                pipeline = YoloPipeline(YoloConfig(model_path=model_path))
                try:
                    pipeline.submit("camera", 1, np.zeros((2, 2, 3), dtype=np.uint8))
                    deadline = time.monotonic() + 2.0
                    result = None
                    while time.monotonic() < deadline:
                        result = pipeline.latest("camera")
                        if result is not None:
                            break
                        time.sleep(0.01)
                    self.assertIsNotNone(result)
                    sequence, annotated = result
                    self.assertEqual(sequence, 1)
                    self.assertTrue(np.all(annotated == 1))
                    self.assertEqual(pipeline.stats().frames, 1)
                    self.assertIsNone(pipeline.stats().error)
                    self.assertEqual(_FakeModel.last_kwargs["classes"], [0])
                finally:
                    pipeline.close()

    def test_missing_model_is_reported_without_raising_from_submit(self) -> None:
        pipeline = YoloPipeline(
            YoloConfig(model_path=Path(tempfile.gettempdir()) / "does-not-exist.pt")
        )
        try:
            deadline = time.monotonic() + 2.0
            error = None
            while time.monotonic() < deadline:
                error = pipeline.stats().error
                if error:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(error)
            self.assertIn("not found", error.lower())
        finally:
            pipeline.close()


if __name__ == "__main__":
    unittest.main()
