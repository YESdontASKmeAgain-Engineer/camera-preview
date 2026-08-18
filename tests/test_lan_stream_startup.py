import unittest
from unittest.mock import patch

import camera_preview


class LanStreamStartupTests(unittest.TestCase):
    @patch("camera_preview.CameraManager")
    def test_show_previews_can_start_saved_lan_stream(self, manager_type) -> None:
        manager = manager_type.return_value
        manager.lan_stream_port = 8080
        manager.lan_stream_quality = 90

        result = camera_preview.show_previews(
            [],
            320,
            240,
            15,
            1,
            auto_start_lan_stream=True,
        )

        self.assertEqual(result, 0)
        manager.start_lan_stream.assert_called_once_with(8080, 90)
        manager.run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
