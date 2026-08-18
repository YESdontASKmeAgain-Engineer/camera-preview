import socket
import unittest
from unittest.mock import MagicMock, patch

import camera_preview


def route_socket(address: str) -> MagicMock:
    route = MagicMock()
    route.__enter__.return_value.getsockname.return_value = (address, 49152)
    return route


class DiscoverLanIpv4AddressesTests(unittest.TestCase):
    @patch("camera_preview.socket.gethostbyname_ex")
    @patch("camera_preview.socket.gethostname", return_value="orangepicm4")
    @patch("camera_preview.socket.socket")
    def test_default_route_address_precedes_hostname_addresses(
        self, socket_factory, _gethostname, gethostbyname_ex
    ) -> None:
        socket_factory.side_effect = [
            route_socket("192.168.10.143"),
            route_socket("192.168.10.143"),
        ]
        gethostbyname_ex.return_value = (
            "orangepicm4",
            [],
            ["127.0.1.1", "172.17.0.1", "192.168.10.143"],
        )

        self.assertEqual(
            camera_preview.discover_lan_ipv4_addresses(),
            ["192.168.10.143", "172.17.0.1"],
        )

    @patch("camera_preview.socket.gethostbyname_ex")
    @patch("camera_preview.socket.gethostname", return_value="orangepicm4")
    @patch("camera_preview.socket.socket", side_effect=OSError("no route"))
    def test_hostname_loopback_falls_back_to_localhost(
        self, _socket_factory, _gethostname, gethostbyname_ex
    ) -> None:
        gethostbyname_ex.return_value = (
            "orangepicm4",
            ["localhost"],
            ["127.0.1.1", "127.0.0.1"],
        )

        self.assertEqual(camera_preview.discover_lan_ipv4_addresses(), ["127.0.0.1"])

    @patch("camera_preview.socket.gethostbyname_ex")
    @patch("camera_preview.socket.gethostname", return_value="camera-host")
    @patch("camera_preview.socket.socket")
    def test_invalid_and_duplicate_addresses_are_removed(
        self, socket_factory, _gethostname, gethostbyname_ex
    ) -> None:
        socket_factory.side_effect = [
            route_socket("0.0.0.0"),
            route_socket("192.168.1.25"),
        ]
        gethostbyname_ex.return_value = (
            "camera-host",
            [],
            ["not-an-ip", "224.0.0.1", "192.168.1.25"],
        )

        self.assertEqual(camera_preview.discover_lan_ipv4_addresses(), ["192.168.1.25"])


if __name__ == "__main__":
    unittest.main()
