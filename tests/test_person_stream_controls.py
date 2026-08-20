import unittest

import camera_preview


class PersonStreamControlUrlTests(unittest.TestCase):
    def test_loopback_person_stream_has_control_base_url(self) -> None:
        self.assertEqual(
            camera_preview.person_stream_control_base_url(
                "http://127.0.0.1:8765/video_feed"
            ),
            "http://127.0.0.1:8765",
        )

    def test_other_streams_cannot_be_controlled(self) -> None:
        for stream_url in (
            "http://192.168.1.30:8765/video_feed",
            "https://127.0.0.1:8765/video_feed",
            "http://127.0.0.1:8765/other-feed",
            None,
        ):
            with self.subTest(stream_url=stream_url):
                self.assertIsNone(
                    camera_preview.person_stream_control_base_url(stream_url)
                )


if __name__ == "__main__":
    unittest.main()
