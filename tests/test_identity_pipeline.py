import unittest
from pathlib import Path

from identity_pipeline import (
    IdentityConfig,
    PersonIdentityPipeline,
    box_iou,
    padded_bounds,
    pose_match_has_quality,
    target_class_id,
)


class IdentityPipelineTests(unittest.TestCase):
    def test_target_class_id_supports_mapping_and_sequence_names(self) -> None:
        self.assertEqual(target_class_id({0: "1", 1: "other"}, "1"), 0)
        self.assertEqual(target_class_id(["other", "1"], "1"), 1)
        with self.assertRaisesRegex(ValueError, "target label"):
            target_class_id({0: "other"}, "1")

    def test_padded_bounds_clamp_to_frame(self) -> None:
        self.assertEqual(padded_bounds(0, 0, 100, 100, 160, 120), (0, 0, 108, 105))
        self.assertIsNone(padded_bounds(10, 10, 10, 20, 160, 120))

    def test_config_validation_happens_without_loading_models(self) -> None:
        config = IdentityConfig(Path("detector.pt"), Path("classifier.pt"), classification_confidence=1.2)
        with self.assertRaisesRegex(ValueError, "Identity confidence"):
            PersonIdentityPipeline(config)

    def test_pose_quality_requires_visible_keypoints(self) -> None:
        detection = (10.0, 10.0, 110.0, 210.0)
        pose_boxes = [(12.0, 12.0, 108.0, 208.0)]
        visible = [[0.9] * 8 + [0.1] * 9]
        hidden = [[0.9] * 5 + [0.1] * 12]
        self.assertGreater(box_iou(detection, pose_boxes[0]), 0.9)
        self.assertTrue(
            pose_match_has_quality(detection, pose_boxes, visible, 6, 0.35, 0.1)
        )
        self.assertFalse(
            pose_match_has_quality(detection, pose_boxes, hidden, 6, 0.35, 0.1)
        )

    def test_pose_config_validation(self) -> None:
        config = IdentityConfig(
            Path("detector.pt"),
            Path("classifier.pt"),
            pose_min_keypoints=0,
        )
        with self.assertRaisesRegex(ValueError, "minimum keypoints"):
            PersonIdentityPipeline(config)


if __name__ == "__main__":
    unittest.main()
