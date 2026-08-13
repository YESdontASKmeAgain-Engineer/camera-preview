"""Display local and network camera feeds in one movable-preview workspace."""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import html
import ipaddress
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import tkinter as tk
import uuid
import xml.etree.ElementTree as ET
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import (
    HTTPBasicAuthHandler,
    HTTPDigestAuthHandler,
    HTTPPasswordMgrWithDefaultRealm,
    Request,
    build_opener,
)
from xml.sax.saxutils import escape as xml_escape

import cv2
from PIL import Image, ImageTk


WINDOW_TITLE = "\u6444\u50cf\u5934\u9884\u89c8"
BACKENDS = (
    ("DirectShow", cv2.CAP_DSHOW),
    ("Media Foundation", cv2.CAP_MSMF),
)
MAX_ZOOM = 8.0
DISPLAY_INTERVAL_MS = 50
DEFAULT_CAPTURE_FOURCC = "MJPG"
MULTI_CAMERA_WIDTH = 640
MULTI_CAMERA_HEIGHT = 480
MULTI_CAMERA_FPS = 30
MULTI_CAMERA_FOURCC = "YUY2"
MULTI_CAMERA_DISPLAY_INTERVAL_MS = 33
DEFAULT_MAIN_WINDOW_WIDTH = 1280
DEFAULT_MAIN_WINDOW_HEIGHT = 800
DEFAULT_PANEL_WIDTH = 600
DEFAULT_PANEL_HEIGHT = 420
PANEL_PLACEMENTS_KEY = "panel_positions"
MAIN_WINDOW_PLACEMENT_KEY = "main_window_position"
PREVIEW_DISPLAY_OPTIONS_KEY = "preview_display_options"
BORDERLESS_WINDOW_PLACEMENT_KEY = "borderless_window_position"
LAN_STREAM_SETTINGS_KEY = "lan_stream_options"
LAN_STREAM_DEFAULT_PORT = 8080
LAN_STREAM_DEFAULT_QUALITY = 80
LAN_STREAM_MIN_QUALITY = 20
LAN_STREAM_MAX_QUALITY = 100
LAN_STREAM_FPS = 30.0
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
HOTKEY_ID = 0xCA01
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
VK_E = 0x45
DEFAULT_HOTKEY_MODIFIERS = MOD_CONTROL
HOTKEY_SETTINGS_FILENAME = "camera_preview_settings.json"
TK_SHIFT_MASK = 0x0001
TK_CONTROL_MASK = 0x0004
TK_ALT_MASK = 0x0008
ERROR_ALREADY_EXISTS = 183
SINGLE_INSTANCE_MUTEX_NAME = r"Local\CameraPreview-7F0D4E7E-9A77-4D08-9807-9B0F8B8E33E1"
_single_instance_mutex = None

SPECIAL_KEY_LABELS = {
    0x08: "Backspace",
    0x09: "Tab",
    0x0D: "Enter",
    0x1B: "Esc",
    0x20: "Space",
    0x21: "Page Up",
    0x22: "Page Down",
    0x23: "End",
    0x24: "Home",
    0x25: "Left",
    0x26: "Up",
    0x27: "Right",
    0x28: "Down",
    0x2D: "Insert",
    0x2E: "Delete",
}
KEYSYM_TO_VK = {
    "BackSpace": 0x08,
    "Tab": 0x09,
    "Return": 0x0D,
    "Escape": 0x1B,
    "space": 0x20,
    "Prior": 0x21,
    "Next": 0x22,
    "Home": 0x24,
    "End": 0x23,
    "Left": 0x25,
    "Up": 0x26,
    "Right": 0x27,
    "Down": 0x28,
    "Insert": 0x2D,
    "Delete": 0x2E,
}


@dataclass
class ZoomView:
    zoom: float = 1.0
    left: float = 0.0
    top: float = 0.0
    frame_width: int = 0
    frame_height: int = 0

    def reset(self) -> None:
        self.zoom = 1.0
        self.left = 0.0
        self.top = 0.0

    def update_frame_size(self, width: int, height: int) -> None:
        if (width, height) != (self.frame_width, self.frame_height):
            self.frame_width = width
            self.frame_height = height
            self.reset()

    def crop_size(self) -> tuple[int, int]:
        return (
            max(1, min(self.frame_width, round(self.frame_width / self.zoom))),
            max(1, min(self.frame_height, round(self.frame_height / self.zoom))),
        )

    def clamp_position(self) -> None:
        crop_width, crop_height = self.crop_size()
        self.left = min(max(self.left, 0.0), self.frame_width - crop_width)
        self.top = min(max(self.top, 0.0), self.frame_height - crop_height)

    def zoom_at(self, relative_x: float, relative_y: float, direction: int) -> None:
        """Zoom while keeping the source pixel under the cursor stationary."""
        if not self.frame_width or not self.frame_height or not direction:
            return

        relative_x = min(max(relative_x, 0.0), 1.0)
        relative_y = min(max(relative_y, 0.0), 1.0)
        old_crop_width, old_crop_height = self.crop_size()
        source_x = self.left + relative_x * old_crop_width
        source_y = self.top + relative_y * old_crop_height

        new_zoom = min(max(self.zoom * (1.25**direction), 1.0), MAX_ZOOM)
        if new_zoom == self.zoom:
            return

        self.zoom = new_zoom
        new_crop_width, new_crop_height = self.crop_size()
        self.left = source_x - relative_x * new_crop_width
        self.top = source_y - relative_y * new_crop_height
        self.clamp_position()

    def render(self, frame, output_width: int, output_height: int):
        height, width = frame.shape[:2]
        self.update_frame_size(width, height)
        self.clamp_position()
        crop_width, crop_height = self.crop_size()
        left = min(max(round(self.left), 0), width - crop_width)
        top = min(max(round(self.top), 0), height - crop_height)
        crop = frame[top : top + crop_height, left : left + crop_width]
        if (crop_width, crop_height) == (output_width, output_height):
            return crop
        return cv2.resize(crop, (output_width, output_height), interpolation=cv2.INTER_LINEAR)


@dataclass(frozen=True)
class GlobalHotkey:
    modifiers: int
    key: int

    @property
    def label(self) -> str:
        parts: list[str] = []
        if self.modifiers & MOD_CONTROL:
            parts.append("Ctrl")
        if self.modifiers & MOD_ALT:
            parts.append("Alt")
        if self.modifiers & MOD_SHIFT:
            parts.append("Shift")
        parts.append(self.key_label)
        return " + ".join(parts)

    @property
    def key_label(self) -> str:
        if 0x30 <= self.key <= 0x39 or 0x41 <= self.key <= 0x5A:
            return chr(self.key)
        if 0x70 <= self.key <= 0x87:
            return f"F{self.key - 0x70 + 1}"
        return SPECIAL_KEY_LABELS.get(self.key, f"Key {self.key}")


DEFAULT_HOTKEY = GlobalHotkey(DEFAULT_HOTKEY_MODIFIERS, VK_E)
MIN_WINDOW_WIDTH = 480
MIN_WINDOW_HEIGHT = 360
MAX_WINDOW_DIMENSION = 10000
MAX_WINDOW_COORDINATE = 100000
PURE_RESIZE_BORDER = 10
MIN_PURE_WINDOW_WIDTH = 160
MIN_PURE_WINDOW_HEIGHT = 120
NETWORK_DISCOVERY_TIMEOUT_SECONDS = 3.0
NETWORK_REQUEST_TIMEOUT_SECONDS = 5.0
NETWORK_STREAM_TIMEOUT_MILLISECONDS = 6000
WS_DISCOVERY_ADDRESS = "239.255.255.250"
WS_DISCOVERY_PORT = 3702
SOAP_ENVELOPE_NAMESPACE = "http://www.w3.org/2003/05/soap-envelope"
WS_DISCOVERY_2005_NAMESPACE = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
WS_DISCOVERY_2005_ADDRESSING_NAMESPACE = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
WS_DISCOVERY_1_1_NAMESPACE = "http://docs.oasis-open.org/ws-dd/ns/discovery"
WS_DISCOVERY_1_1_ADDRESSING_NAMESPACE = "http://www.w3.org/2005/08/addressing"
ONVIF_DEVICE_NAMESPACE = "http://www.onvif.org/ver10/device/wsdl"
ONVIF_MEDIA_NAMESPACE = "http://www.onvif.org/ver10/media/wsdl"
ONVIF_SCHEMA_NAMESPACE = "http://www.onvif.org/ver10/schema"
WS_SECURITY_NAMESPACE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-secext-1.0.xsd"
)
WS_UTILITY_NAMESPACE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-utility-1.0.xsd"
)
WS_PASSWORD_DIGEST_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
WS_NONCE_BASE64_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-soap-message-security-1.0#Base64Binary"
)


@dataclass(frozen=True)
class WindowPlacement:
    """Saved bounds for a top-level window or a camera preview panel."""

    x: int
    y: int
    width: int
    height: int

    @classmethod
    def from_payload(
        cls,
        payload: object,
        min_width: int = MIN_WINDOW_WIDTH,
        min_height: int = MIN_WINDOW_HEIGHT,
    ) -> "WindowPlacement | None":
        if not isinstance(payload, dict):
            return None
        try:
            placement = cls(
                x=int(payload["x"]),
                y=int(payload["y"]),
                width=int(payload["width"]),
                height=int(payload["height"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

        if not (
            -MAX_WINDOW_COORDINATE <= placement.x <= MAX_WINDOW_COORDINATE
            and -MAX_WINDOW_COORDINATE <= placement.y <= MAX_WINDOW_COORDINATE
            and min_width <= placement.width <= MAX_WINDOW_DIMENSION
            and min_height <= placement.height <= MAX_WINDOW_DIMENSION
        ):
            return None
        return placement

    def to_payload(self) -> dict[str, int]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }


def is_valid_hotkey(hotkey: GlobalHotkey) -> bool:
    return hotkey.modifiers == MOD_CONTROL and 1 <= hotkey.key <= 0xFF


def virtual_key_from_keysym(keysym: str) -> int | None:
    if len(keysym) == 1 and keysym.isascii() and keysym.isalnum():
        return ord(keysym.upper())
    if keysym.startswith("F") and keysym[1:].isdigit():
        number = int(keysym[1:])
        if 1 <= number <= 24:
            return 0x70 + number - 1
    return KEYSYM_TO_VK.get(keysym)


def hotkey_from_event(event) -> GlobalHotkey | None:
    key = virtual_key_from_keysym(event.keysym)
    if key is None:
        return None
    modifiers = 0
    if event.state & TK_CONTROL_MASK:
        modifiers |= MOD_CONTROL
    if event.state & TK_ALT_MASK:
        modifiers |= MOD_ALT
    if event.state & TK_SHIFT_MASK:
        modifiers |= MOD_SHIFT
    hotkey = GlobalHotkey(modifiers, key)
    return hotkey if is_valid_hotkey(hotkey) else None


def selectable_hotkeys() -> dict[str, GlobalHotkey]:
    keys = list(range(ord("A"), ord("Z") + 1))
    keys.extend(range(ord("0"), ord("9") + 1))
    keys.extend(range(0x70, 0x70 + 12))
    return {
        hotkey.label: hotkey
        for hotkey in (GlobalHotkey(MOD_CONTROL, key) for key in keys)
    }


@dataclass
class OpenedCapture:
    index: int | str
    capture: cv2.VideoCapture
    backend: str
    display_name: str | None = None


@dataclass(frozen=True)
class DiscoveredNetworkCamera:
    endpoint: str
    host: str
    name: str = ""
    scopes: tuple[str, ...] = ()

    @property
    def display_name(self) -> str:
        if self.name:
            return f"{self.name} ({self.host})"
        return f"\u7f51\u7edc\u6444\u50cf\u5934 ({self.host})"


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_xml_text(root: ET.Element, local_name: str) -> str | None:
    for element in root.iter():
        if _xml_local_name(element.tag) == local_name and element.text:
            text = element.text.strip()
            if text:
                return text
    return None


def _find_xml_child_text(element: ET.Element, local_name: str) -> str | None:
    for child in element.iter():
        if _xml_local_name(child.tag) == local_name and child.text:
            text = child.text.strip()
            if text:
                return text
    return None


def _network_camera_name(scopes: Iterable[str]) -> str:
    marker = "/name/"
    for scope in scopes:
        if marker in scope:
            return unquote(scope.split(marker, 1)[1]).strip()
    return ""


def parse_onvif_discovery_response(
    payload: bytes, sender_host: str = ""
) -> list[DiscoveredNetworkCamera]:
    """Extract ONVIF device service endpoints from one WS-Discovery reply."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return []

    raw_xaddrs = _find_xml_text(root, "XAddrs")
    if not raw_xaddrs:
        return []
    scopes = tuple(
        text
        for element in root.iter()
        if _xml_local_name(element.tag) == "Scopes" and element.text
        for text in element.text.split()
    )
    name = _network_camera_name(scopes)
    cameras: list[DiscoveredNetworkCamera] = []
    for endpoint in raw_xaddrs.split():
        parsed = urlsplit(endpoint)
        if parsed.scheme.lower() not in {"http", "https"}:
            continue
        host = parsed.hostname or sender_host
        if not host:
            continue
        cameras.append(
            DiscoveredNetworkCamera(
                endpoint=endpoint,
                host=host,
                name=name,
                scopes=scopes,
            )
        )
    return cameras


def _ws_discovery_probe(
    discovery_namespace: str, addressing_namespace: str, target: str
) -> bytes:
    message_id = uuid.uuid4()
    action = f"{discovery_namespace}/Probe"
    message = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{SOAP_ENVELOPE_NAMESPACE}"
            xmlns:a="{addressing_namespace}"
            xmlns:d="{discovery_namespace}"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <s:Header>
    <a:MessageID>uuid:{message_id}</a:MessageID>
    <a:To s:mustUnderstand="true">{target}</a:To>
    <a:Action s:mustUnderstand="true">{action}</a:Action>
  </s:Header>
  <s:Body>
    <d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>
  </s:Body>
</s:Envelope>'''
    return message.encode("utf-8")


def discover_network_cameras(
    timeout: float = NETWORK_DISCOVERY_TIMEOUT_SECONDS,
) -> list[DiscoveredNetworkCamera]:
    """Discover ONVIF cameras on the local network without scanning IP ranges."""
    if timeout <= 0:
        raise ValueError("Discovery timeout must be positive.")

    probes = (
        _ws_discovery_probe(
            WS_DISCOVERY_2005_NAMESPACE,
            WS_DISCOVERY_2005_ADDRESSING_NAMESPACE,
            "urn:schemas-xmlsoap-org:ws:2005:04:discovery",
        ),
        _ws_discovery_probe(
            WS_DISCOVERY_1_1_NAMESPACE,
            WS_DISCOVERY_1_1_ADDRESSING_NAMESPACE,
            "urn:docs-oasis-open-org:ws-dd:ns:discovery",
        ),
    )
    discovered: dict[str, DiscoveredNetworkCamera] = {}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.bind(("", 0))
            for probe in probes:
                for target in (WS_DISCOVERY_ADDRESS, "255.255.255.255"):
                    try:
                        sock.sendto(probe, (target, WS_DISCOVERY_PORT))
                    except OSError:
                        continue

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(min(0.25, remaining))
                try:
                    payload, sender = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError as error:
                    raise RuntimeError(f"Network discovery failed: {error}") from error
                sender_host = sender[0] if sender else ""
                for camera in parse_onvif_discovery_response(payload, sender_host):
                    discovered.setdefault(camera.endpoint, camera)
    except OSError as error:
        raise RuntimeError(f"Network discovery failed: {error}") from error

    return sorted(discovered.values(), key=lambda camera: (camera.host, camera.name))


def _onvif_security_header(username: str, password: str) -> str:
    if not username:
        return ""
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode("utf-8") + password.encode("utf-8")).digest()
    ).decode("ascii")
    encoded_nonce = base64.b64encode(nonce).decode("ascii")
    return f'''<wsse:Security s:mustUnderstand="true"
        xmlns:wsse="{WS_SECURITY_NAMESPACE}"
        xmlns:wsu="{WS_UTILITY_NAMESPACE}">
      <wsse:UsernameToken>
        <wsse:Username>{xml_escape(username)}</wsse:Username>
        <wsse:Password Type="{WS_PASSWORD_DIGEST_TYPE}">{digest}</wsse:Password>
        <wsse:Nonce EncodingType="{WS_NONCE_BASE64_TYPE}">{encoded_nonce}</wsse:Nonce>
        <wsu:Created>{created}</wsu:Created>
      </wsse:UsernameToken>
    </wsse:Security>'''


def build_onvif_soap_envelope(
    body: str, action: str, username: str = "", password: str = ""
) -> bytes:
    """Build a SOAP 1.2 request with optional ONVIF WS-Security credentials."""
    security = _onvif_security_header(username, password)
    envelope = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{SOAP_ENVELOPE_NAMESPACE}"
            xmlns:wsa="{WS_DISCOVERY_1_1_ADDRESSING_NAMESPACE}">
  <s:Header>
    <wsa:Action s:mustUnderstand="true">{xml_escape(action)}</wsa:Action>
    {security}
  </s:Header>
  <s:Body>{body}</s:Body>
</s:Envelope>'''
    return envelope.encode("utf-8")


def _onvif_request(
    endpoint: str, body: str, action: str, username: str, password: str
) -> ET.Element:
    request = Request(
        endpoint,
        data=build_onvif_soap_envelope(body, action, username, password),
        headers={
            "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"',
            "SOAPAction": f'"{action}"',
        },
        method="POST",
    )
    password_manager = HTTPPasswordMgrWithDefaultRealm()
    if username:
        password_manager.add_password(None, endpoint, username, password)
    opener = build_opener(
        HTTPDigestAuthHandler(password_manager), HTTPBasicAuthHandler(password_manager)
    )
    try:
        with opener.open(request, timeout=NETWORK_REQUEST_TIMEOUT_SECONDS) as response:
            return ET.fromstring(response.read())
    except (HTTPError, URLError, OSError, TimeoutError, ET.ParseError) as error:
        raise RuntimeError(f"ONVIF request failed: {error}") from error


def _onvif_media_endpoint(root: ET.Element) -> str | None:
    for element in root.iter():
        if _xml_local_name(element.tag) in {"Media", "Media2"}:
            endpoint = _find_xml_child_text(element, "XAddr")
            if endpoint:
                return endpoint
    return None


def _with_rtsp_credentials(stream_url: str, username: str, password: str) -> str:
    if not username:
        return stream_url
    parsed = urlsplit(stream_url)
    if parsed.username is not None or not parsed.hostname:
        return stream_url
    user_info = quote(username, safe="")
    if password:
        user_info = f"{user_info}:{quote(password, safe='')}"
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, f"{user_info}@{host}", parsed.path, parsed.query, ""))


def resolve_onvif_stream_urls(
    camera: DiscoveredNetworkCamera, username: str = "", password: str = ""
) -> list[str]:
    """Ask an ONVIF camera for the RTSP stream URL of its available profiles."""
    capabilities_action = f"{ONVIF_DEVICE_NAMESPACE}/GetCapabilities"
    capabilities = _onvif_request(
        camera.endpoint,
        f'<tds:GetCapabilities xmlns:tds="{ONVIF_DEVICE_NAMESPACE}">'
        "<tds:Category>All</tds:Category>"
        "</tds:GetCapabilities>",
        capabilities_action,
        username,
        password,
    )
    media_endpoint = _onvif_media_endpoint(capabilities)
    if not media_endpoint:
        raise RuntimeError("The ONVIF camera did not provide a media service endpoint.")

    profiles_action = f"{ONVIF_MEDIA_NAMESPACE}/GetProfiles"
    profiles = _onvif_request(
        media_endpoint,
        f'<trt:GetProfiles xmlns:trt="{ONVIF_MEDIA_NAMESPACE}"/>',
        profiles_action,
        username,
        password,
    )
    tokens = [
        profile.attrib["token"]
        for profile in profiles.iter()
        if _xml_local_name(profile.tag) in {"Profiles", "Profile"}
        and profile.attrib.get("token")
    ]
    if not tokens:
        raise RuntimeError("The ONVIF camera did not provide a video profile.")

    stream_action = f"{ONVIF_MEDIA_NAMESPACE}/GetStreamUri"
    urls: list[str] = []
    for token in tokens[:4]:
        try:
            stream = _onvif_request(
                media_endpoint,
                f'''<trt:GetStreamUri xmlns:trt="{ONVIF_MEDIA_NAMESPACE}"
                    xmlns:tt="{ONVIF_SCHEMA_NAMESPACE}">
                  <trt:StreamSetup>
                    <tt:Stream>RTP-Unicast</tt:Stream>
                    <tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>
                  </trt:StreamSetup>
                  <trt:ProfileToken>{xml_escape(token)}</trt:ProfileToken>
                </trt:GetStreamUri>''',
                stream_action,
                username,
                password,
            )
        except RuntimeError:
            continue
        stream_url = _find_xml_text(stream, "Uri")
        if stream_url:
            urls.append(_with_rtsp_credentials(stream_url, username, password))
    unique_urls = list(dict.fromkeys(urls))
    if not unique_urls:
        raise RuntimeError("The ONVIF camera did not return an RTSP stream URL.")
    return unique_urls


def normalize_network_stream_url(stream_url: str) -> str:
    stream_url = stream_url.strip()
    parsed = urlsplit(stream_url)
    if parsed.scheme.lower() not in {"rtsp", "rtsps", "http", "https"} or not parsed.hostname:
        raise ValueError("Enter a complete RTSP or HTTP stream address.")
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("The stream address contains an invalid port.") from error
    return stream_url


def network_source_id(stream_url: str) -> str:
    parsed = urlsplit(stream_url)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    canonical = urlunsplit(
        (parsed.scheme.lower(), host.lower(), parsed.path or "/", parsed.query, "")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"network-{digest}"


def network_display_name(stream_url: str) -> str:
    host = urlsplit(stream_url).hostname
    return f"\u7f51\u7edc\u6444\u50cf\u5934 {host or stream_url}"


def _create_network_capture(stream_url: str, backend_id: int) -> cv2.VideoCapture:
    if backend_id == cv2.CAP_FFMPEG:
        open_timeout = getattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC", None)
        read_timeout = getattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC", None)
        if open_timeout is not None and read_timeout is not None:
            try:
                return cv2.VideoCapture(
                    stream_url,
                    backend_id,
                    [
                        open_timeout,
                        NETWORK_STREAM_TIMEOUT_MILLISECONDS,
                        read_timeout,
                        NETWORK_STREAM_TIMEOUT_MILLISECONDS,
                    ],
                )
            except cv2.error:
                pass
    return cv2.VideoCapture(stream_url, backend_id)


def open_network_capture(stream_url: str) -> OpenedCapture:
    stream_url = normalize_network_stream_url(stream_url)
    backend_candidates = (("FFmpeg", cv2.CAP_FFMPEG), ("OpenCV", cv2.CAP_ANY))
    for backend_name, backend_id in backend_candidates:
        capture = _create_network_capture(stream_url, backend_id)
        if not capture.isOpened():
            capture.release()
            continue
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ok, _ = capture.read()
        if ok:
            return OpenedCapture(
                index=network_source_id(stream_url),
                capture=capture,
                backend=backend_name,
                display_name=network_display_name(stream_url),
            )
        capture.release()
    raise RuntimeError("Could not open the network camera stream.")


def configure_capture(
    capture: cv2.VideoCapture,
    width: int,
    height: int,
    fps: int,
    fourcc: str = DEFAULT_CAPTURE_FOURCC,
) -> None:
    capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    capture.set(cv2.CAP_PROP_FPS, fps)
    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)


def open_capture(
    index: int,
    width: int,
    height: int,
    fps: int,
    fourcc: str = DEFAULT_CAPTURE_FOURCC,
) -> OpenedCapture:
    for backend_name, backend_id in BACKENDS:
        capture = cv2.VideoCapture(index, backend_id)
        if not capture.isOpened():
            capture.release()
            continue

        configure_capture(capture, width, height, fps, fourcc)
        ok, _ = capture.read()
        if ok:
            return OpenedCapture(index=index, capture=capture, backend=backend_name)
        capture.release()

    raise RuntimeError(f"Cannot open camera {index}.")


def open_captures(
    indices: Iterable[int],
    width: int,
    height: int,
    fps: int,
    fourcc: str = DEFAULT_CAPTURE_FOURCC,
) -> list[OpenedCapture]:
    captures: list[OpenedCapture] = []
    opened_indices: set[int] = set()
    for index in indices:
        if index < 0 or index in opened_indices:
            continue
        try:
            opened = open_capture(index, width, height, fps, fourcc)
        except RuntimeError:
            continue
        captures.append(opened)
        opened_indices.add(index)
    return captures


def select_local_captures(
    captures: list[OpenedCapture],
) -> list[OpenedCapture] | None:
    """Let the user choose which detected local cameras should be displayed."""
    if len(captures) <= 1:
        return captures

    root = tk.Tk()
    root.withdraw()
    dialog = tk.Toplevel(root)
    dialog.title("\u9009\u62e9\u6444\u50cf\u5934")
    dialog.resizable(False, False)
    dialog.geometry("480x330")
    dialog.transient(root)

    selected: list[OpenedCapture] | None = None

    container = ttk.Frame(dialog, padding=16)
    container.pack(fill=tk.BOTH, expand=True)
    ttk.Label(
        container,
        text=(
            f"\u68c0\u6d4b\u5230 {len(captures)} \u53f0 USB \u6444\u50cf\u5934\uff0c"
            "\u9009\u62e9\u8981\u663e\u793a\u7684\u8bbe\u5907\uff1a"
        ),
    ).pack(anchor=tk.W)

    list_frame = ttk.Frame(container)
    list_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 12))
    listbox = tk.Listbox(
        list_frame,
        height=min(max(len(captures), 4), 10),
        selectmode=tk.EXTENDED,
        exportselection=False,
    )
    listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=listbox.yview)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    listbox.configure(yscrollcommand=scrollbar.set)

    for opened in captures:
        name = opened.display_name or f"\u6444\u50cf\u5934 {opened.index}"
        listbox.insert(tk.END, f"{name} ({opened.backend})")
    listbox.selection_set(0)
    listbox.activate(0)

    def accept() -> None:
        nonlocal selected
        positions = listbox.curselection()
        if not positions:
            messagebox.showwarning(
                WINDOW_TITLE,
                "\u8bf7\u81f3\u5c11\u9009\u62e9\u4e00\u53f0\u6444\u50cf\u5934\u3002",
                parent=dialog,
            )
            return
        selected = [captures[position] for position in positions]
        dialog.destroy()

    def cancel() -> None:
        dialog.destroy()

    buttons = ttk.Frame(container)
    buttons.pack(fill=tk.X)
    ttk.Button(buttons, text="\u663e\u793a\u9009\u4e2d", command=accept).pack(
        side=tk.RIGHT
    )
    ttk.Button(buttons, text="\u53d6\u6d88", command=cancel).pack(
        side=tk.RIGHT, padx=(0, 6)
    )

    listbox.bind("<Double-Button-1>", lambda _event: accept())
    dialog.bind("<Return>", lambda _event: accept())
    dialog.bind("<Escape>", lambda _event: cancel())
    dialog.protocol("WM_DELETE_WINDOW", cancel)
    dialog.grab_set()
    dialog.deiconify()
    dialog.lift()
    dialog.focus_force()

    def center_dialog() -> None:
        try:
            dialog.deiconify()
            dialog.lift()
            dialog.focus_force()
            dialog.update_idletasks()
            left = max(0, (dialog.winfo_screenwidth() - dialog.winfo_width()) // 2)
            top = max(0, (dialog.winfo_screenheight() - dialog.winfo_height()) // 2)
            dialog.geometry(f"+{left}+{top}")
            listbox.focus_set()
        except tk.TclError:
            pass

    dialog.after_idle(center_dialog)
    try:
        root.wait_window(dialog)
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass

    if selected is None:
        for opened in captures:
            opened.capture.release()
        return None

    selected_ids = {id(opened) for opened in selected}
    for opened in captures:
        if id(opened) not in selected_ids:
            opened.capture.release()
    return selected


def list_cameras(max_index: int, width: int, height: int, fps: int) -> int:
    captures = open_captures(range(max_index + 1), width, height, fps)
    try:
        for opened in captures:
            actual_width = int(opened.capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_height = int(opened.capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            print(
                f"Camera {opened.index}: {actual_width}x{actual_height} via {opened.backend}"
            )
    finally:
        for opened in captures:
            opened.capture.release()

    if not captures:
        print("No usable camera was found.")
        return 1
    return 0


def application_directory() -> Path:
    """Return the folder carried with the application, never an AppData folder."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def portable_data_directory() -> Path:
    """Keep all persistent application data beside the portable executable."""
    return application_directory()


def acquire_single_instance() -> bool:
    """Keep another GUI instance from silently taking over the cameras."""
    global _single_instance_mutex
    if sys.platform != "win32":
        return True

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    create_mutex.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    mutex = create_mutex(None, False, SINGLE_INSTANCE_MUTEX_NAME)
    if not mutex:
        return True
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        close_handle(mutex)
        return False

    _single_instance_mutex = mutex
    return True


def hotkey_settings_path() -> Path:
    return portable_data_directory() / HOTKEY_SETTINGS_FILENAME


def load_settings_payload() -> dict[str, object]:
    try:
        payload = json.loads(hotkey_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_settings_payload(payload: dict[str, object]) -> None:
    settings_path = hotkey_settings_path()
    temporary_path = settings_path.with_suffix(f"{settings_path.suffix}.tmp")
    temporary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(settings_path)


def load_hotkey() -> GlobalHotkey:
    payload = load_settings_payload()
    try:
        hotkey = GlobalHotkey(int(payload["modifiers"]), int(payload["key"]))
    except (ValueError, KeyError, TypeError):
        return DEFAULT_HOTKEY
    if is_valid_hotkey(hotkey):
        return hotkey
    try:
        save_hotkey(DEFAULT_HOTKEY)
    except OSError:
        pass
    return DEFAULT_HOTKEY


def save_hotkey(hotkey: GlobalHotkey) -> None:
    payload = load_settings_payload()
    payload["modifiers"] = hotkey.modifiers
    payload["key"] = hotkey.key
    save_settings_payload(payload)


def _camera_source_key(camera_index: int | str) -> int | str | None:
    if isinstance(camera_index, int):
        return camera_index if camera_index >= 0 else None
    if not isinstance(camera_index, str):
        return None
    camera_index = camera_index.strip()
    if not camera_index:
        return None
    if camera_index.isdecimal():
        return int(camera_index)
    return camera_index


def _load_camera_placements(settings_key: str) -> dict[int | str, WindowPlacement]:
    payload = load_settings_payload()
    raw_placements = payload.get(settings_key)
    if not isinstance(raw_placements, dict):
        return {}

    placements: dict[int | str, WindowPlacement] = {}
    for raw_index, raw_placement in raw_placements.items():
        camera_index = _camera_source_key(raw_index)
        if camera_index is None:
            continue
        placement = WindowPlacement.from_payload(raw_placement)
        if placement is not None:
            placements[camera_index] = placement
    return placements


def _save_camera_placement(
    settings_key: str, camera_index: int | str, placement: WindowPlacement
) -> None:
    source_key = _camera_source_key(camera_index)
    if source_key is None:
        return
    payload = load_settings_payload()
    raw_placements = payload.get(settings_key)
    placements = dict(raw_placements) if isinstance(raw_placements, dict) else {}
    placements[str(source_key)] = placement.to_payload()
    payload[settings_key] = placements
    save_settings_payload(payload)


def load_window_placements() -> dict[int | str, WindowPlacement]:
    """Load legacy per-window bounds retained for compatibility."""
    return _load_camera_placements("window_positions")


def save_window_placement(camera_index: int | str, placement: WindowPlacement) -> None:
    """Save legacy per-window bounds retained for compatibility."""
    _save_camera_placement("window_positions", camera_index, placement)


def load_panel_placements() -> dict[int | str, WindowPlacement]:
    return _load_camera_placements(PANEL_PLACEMENTS_KEY)


def save_panel_placement(camera_index: int | str, placement: WindowPlacement) -> None:
    _save_camera_placement(PANEL_PLACEMENTS_KEY, camera_index, placement)


def load_main_window_placement() -> WindowPlacement | None:
    placement = WindowPlacement.from_payload(
        load_settings_payload().get(MAIN_WINDOW_PLACEMENT_KEY)
    )
    if placement is not None:
        return placement

    # Preserve the screen location from the former one-window-per-camera layout.
    legacy_placements = load_window_placements()
    legacy = next(iter(legacy_placements.values()), None)
    if legacy is None:
        return None
    return WindowPlacement(
        x=legacy.x,
        y=legacy.y,
        width=DEFAULT_MAIN_WINDOW_WIDTH,
        height=DEFAULT_MAIN_WINDOW_HEIGHT,
    )


def save_main_window_placement(placement: WindowPlacement) -> None:
    payload = load_settings_payload()
    payload[MAIN_WINDOW_PLACEMENT_KEY] = placement.to_payload()
    save_settings_payload(payload)


def load_borderless_window_placement() -> WindowPlacement | None:
    placement = WindowPlacement.from_payload(
        load_settings_payload().get(BORDERLESS_WINDOW_PLACEMENT_KEY),
        min_width=MIN_PURE_WINDOW_WIDTH,
        min_height=MIN_PURE_WINDOW_HEIGHT,
    )
    if placement is None:
        return None
    return WindowPlacement(
        x=placement.x,
        y=placement.y,
        width=max(MIN_PURE_WINDOW_WIDTH, placement.width),
        height=max(MIN_PURE_WINDOW_HEIGHT, placement.height),
    )


def save_borderless_window_placement(placement: WindowPlacement) -> None:
    payload = load_settings_payload()
    payload[BORDERLESS_WINDOW_PLACEMENT_KEY] = placement.to_payload()
    save_settings_payload(payload)


def load_preview_display_options() -> tuple[bool, bool]:
    payload = load_settings_payload()
    options = payload.get(PREVIEW_DISPLAY_OPTIONS_KEY)
    if not isinstance(options, dict):
        return True, False

    always_on_top = options.get("always_on_top")
    borderless = options.get("borderless")
    return (
        always_on_top if isinstance(always_on_top, bool) else True,
        borderless if isinstance(borderless, bool) else False,
    )


def save_preview_display_options(always_on_top: bool, borderless: bool) -> None:
    payload = load_settings_payload()
    payload[PREVIEW_DISPLAY_OPTIONS_KEY] = {
        "always_on_top": always_on_top,
        "borderless": borderless,
    }
    save_settings_payload(payload)


def load_lan_stream_options() -> tuple[int, int]:
    payload = load_settings_payload()
    options = payload.get(LAN_STREAM_SETTINGS_KEY)
    if not isinstance(options, dict):
        return LAN_STREAM_DEFAULT_PORT, LAN_STREAM_DEFAULT_QUALITY
    try:
        port = int(options.get("port", LAN_STREAM_DEFAULT_PORT))
    except (TypeError, ValueError):
        port = LAN_STREAM_DEFAULT_PORT
    try:
        quality = int(options.get("quality", LAN_STREAM_DEFAULT_QUALITY))
    except (TypeError, ValueError):
        quality = LAN_STREAM_DEFAULT_QUALITY
    port = min(max(port, 1), 65535)
    quality = min(max(quality, LAN_STREAM_MIN_QUALITY), LAN_STREAM_MAX_QUALITY)
    return port, quality


def save_lan_stream_options(port: int, quality: int) -> None:
    payload = load_settings_payload()
    payload[LAN_STREAM_SETTINGS_KEY] = {
        "port": int(port),
        "quality": int(quality),
    }
    save_settings_payload(payload)


def save_frame(frame, camera_index: int | str) -> Path:
    screenshot_dir = portable_data_directory() / "screenshots"
    screenshot_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_camera_index = re.sub(r"[^A-Za-z0-9_-]+", "_", str(camera_index)).strip("_")
    output_path = screenshot_dir / f"camera_{safe_camera_index or 'stream'}_{timestamp}.jpg"
    if not cv2.imwrite(str(output_path), frame):
        raise RuntimeError(f"Could not save {output_path}")
    return output_path


def lan_camera_id(camera_index: int | str) -> str:
    """Return a stable URL-safe identifier for one camera source."""
    raw = str(camera_index)
    readable = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-") or "camera"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{readable[:40]}-{digest}"


class _LANStreamRequestHandler(BaseHTTPRequestHandler):
    """HTTP endpoints used by the embedded LAN preview server."""

    server_version = "CameraPreviewLAN/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def stream_server(self) -> "LANStreamServer":
        return self.server.stream_server  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args) -> None:
        # The GUI application has no console in the portable build.
        return

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if path in {"", "/"}:
            self._send_html(self.stream_server.render_index())
            return

        stream_match = re.fullmatch(r"/stream/([^/]+)\.mjpg", path)
        snapshot_match = re.fullmatch(r"/snapshot/([^/]+)\.jpg", path)
        if stream_match:
            self._serve_stream(stream_match.group(1))
            return
        if snapshot_match:
            frame = self.stream_server.get_jpeg(snapshot_match.group(1))
            if frame is None:
                self._send_error(HTTPStatus.NOT_FOUND, "Camera frame is not available.")
            else:
                self._send_bytes(frame, "image/jpeg")
            return
        self._send_error(HTTPStatus.NOT_FOUND, "Not found.")

    def _send_html(self, content: str) -> None:
        self._send_bytes(content.encode("utf-8"), "text/html; charset=utf-8")

    def _send_bytes(self, payload: bytes, content_type: str) -> None:
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        payload = f"{status.value} {html.escape(message)}".encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass

    def _serve_stream(self, camera_id: str) -> None:
        boundary = b"camera-preview-frame"
        last_frame_id = -1
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=camera-preview-frame")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            while not self.stream_server.stopping:
                jpeg = self.stream_server.get_jpeg_with_id(camera_id)
                if jpeg is None:
                    if not self.stream_server.has_camera(camera_id):
                        return
                    self.stream_server.wait_for_frame(0.1)
                    continue
                frame_id, frame = jpeg
                if frame_id == last_frame_id:
                    self.stream_server.wait_for_frame(1.0 / LAN_STREAM_FPS)
                    continue
                packet = (
                    b"--" + boundary
                    + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode("ascii")
                    + b"\r\n\r\n"
                    + frame
                    + b"\r\n"
                )
                self.wfile.write(packet)
                self.wfile.flush()
                last_frame_id = frame_id
                self.stream_server.wait_for_frame(1.0 / LAN_STREAM_FPS)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return


class _LANThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class LANStreamServer:
    """Small dependency-free MJPEG server for cameras opened by the app."""

    def __init__(self, port: int, quality: int) -> None:
        self.port = int(port)
        self.quality = int(quality)
        self.stopping = False
        self._panels_lock = threading.RLock()
        self._panels: dict[str, "CameraPanel"] = {}
        self._cache_lock = threading.Lock()
        self._jpeg_cache: dict[str, tuple[int, int, bytes]] = {}
        self._jpeg_encode_locks: dict[str, threading.Lock] = {}
        self._frame_event = threading.Event()
        self.httpd = _LANThreadingHTTPServer(
            ("0.0.0.0", self.port), _LANStreamRequestHandler
        )
        self.httpd.daemon_threads = True
        self.httpd.stream_server = self  # type: ignore[attr-defined]
        self.port = int(self.httpd.server_address[1])
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.stopping = False
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="camera-preview-lan-stream",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        self._frame_event.set()
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=1.5)
        self.thread = None

    def set_panels(self, panels: Iterable["CameraPanel"]) -> None:
        mapping = {lan_camera_id(panel.camera_index): panel for panel in panels if panel.running}
        with self._panels_lock:
            self._panels = mapping
            valid = set(mapping)
        with self._cache_lock:
            self._jpeg_cache = {
                key: value for key, value in self._jpeg_cache.items() if key in valid
            }
            self._jpeg_encode_locks = {
                key: value for key, value in self._jpeg_encode_locks.items() if key in valid
            }
        self._frame_event.set()

    def has_camera(self, camera_id: str) -> bool:
        with self._panels_lock:
            return camera_id in self._panels

    def _panel(self, camera_id: str) -> "CameraPanel | None":
        with self._panels_lock:
            return self._panels.get(camera_id)

    def get_jpeg(self, camera_id: str) -> bytes | None:
        jpeg = self.get_jpeg_with_id(camera_id)
        return jpeg[1] if jpeg is not None else None

    def get_jpeg_with_id(self, camera_id: str) -> tuple[int, bytes] | None:
        panel = self._panel(camera_id)
        if panel is None or not panel.running:
            return None
        frame_id = panel._latest_frame_sequence()
        if not frame_id:
            return None
        with self._cache_lock:
            cached = self._jpeg_cache.get(camera_id)
            if cached is not None and cached[0] == frame_id and cached[1] == self.quality:
                return frame_id, cached[2]

            encode_lock = self._jpeg_encode_locks.setdefault(camera_id, threading.Lock())
        with encode_lock:
            frame_id = panel._latest_frame_sequence()
            if not frame_id:
                return None
            with self._cache_lock:
                cached = self._jpeg_cache.get(camera_id)
                if (
                    cached is not None
                    and cached[0] == frame_id
                    and cached[1] == self.quality
                ):
                    return frame_id, cached[2]
            frame, frame_id = panel._take_latest_frame_with_id()
            if frame is None:
                return None
            try:
                ok, encoded = cv2.imencode(
                    ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]
                )
            except cv2.error:
                return None
            if not ok:
                return None
            payload = encoded.tobytes()
            with self._cache_lock:
                self._jpeg_cache[camera_id] = (frame_id, self.quality, payload)
            return frame_id, payload

    def signal_frame_available(self) -> None:
        self._frame_event.set()

    def wait_for_frame(self, timeout: float) -> None:
        self._frame_event.wait(max(0.001, timeout))
        self._frame_event.clear()

    def render_index(self) -> str:
        with self._panels_lock:
            cameras = [
                (camera_id, panel.camera_name)
                for camera_id, panel in self._panels.items()
            ]
        rows = []
        for camera_id, name in cameras:
            safe_id = quote(camera_id, safe="")
            safe_name = html.escape(name)
            stream_url = f"/stream/{safe_id}.mjpg"
            snapshot_url = f"/snapshot/{safe_id}.jpg"
            rows.append(
                f'<section><h2>{safe_name}</h2><img src="{stream_url}" '
                f'alt="{safe_name}"><p><a href="{stream_url}">\u5355\u8def\u89c6\u9891\u6d41</a> '
                f'<a href="{snapshot_url}">\u5355\u5e27</a></p></section>'
            )
        if not rows:
            rows.append("<p>\u5f53\u524d\u6ca1\u6709\u5df2\u6253\u5f00\u7684\u6444\u50cf\u5934\u3002</p>")
        items = "".join(rows)
        return (
            "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>Camera Preview LAN</title>"
            "<style>body{font-family:Segoe UI,Arial,sans-serif;margin:1.5rem;line-height:1.5}"
            "main{display:grid;gap:1.25rem;grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}"
            "section{min-width:0}h1{grid-column:1/-1;margin:0}h2{font-size:1rem;margin:0 0 .5rem}"
            "img{background:#111;display:block;height:auto;max-width:100%;width:100%}"
            "p{margin:.5rem 0 0}a+a{margin-left:1rem}</style><main>"
            "<h1>Camera Preview</h1>"
            + items
            + "</main></html>"
        )

    def urls(self) -> list[str]:
        addresses: set[str] = set()
        try:
            host_name = socket.gethostname()
            for address in socket.gethostbyname_ex(host_name)[2]:
                try:
                    parsed = ipaddress.ip_address(address)
                except ValueError:
                    continue
                if parsed.version == 4 and not parsed.is_loopback:
                    addresses.add(address)
        except OSError:
            pass
        if not addresses:
            addresses.add("127.0.0.1")
        return [f"http://{address}:{self.port}/" for address in sorted(addresses)]


class CameraPanel:
    """One camera feed rendered as a draggable panel in the shared workspace."""

    def __init__(
        self,
        manager: "CameraManager",
        opened: OpenedCapture,
        position: int,
        saved_placement: WindowPlacement | None = None,
    ) -> None:
        self.manager = manager
        self.capture = opened.capture
        self.camera_index = opened.index
        self.backend = opened.backend
        self.camera_name = opened.display_name or f"\u6444\u50cf\u5934 {self.camera_index}"
        self.frame_interval = 1.0 / max(1, min(manager.fps, 30))
        self.window = manager.preview_window
        self.workspace = manager.workspace
        if self.window is None or self.workspace is None:
            raise RuntimeError("Preview workspace is not ready.")

        self.zoom_view = ZoomView()
        self.running = True
        self.frame_lock = threading.Lock()
        self.latest_frame = None
        self.frame_sequence = 0
        self.frame_count = 0
        self.fps = 0.0
        self.fps_start = time.perf_counter()
        self.status_text = "Opening camera..."
        self.status_until = 0.0
        self.image_photo = None
        self.image_bounds: tuple[int, int, int, int] | None = None
        self.source_size: tuple[int, int] | None = None
        self._drag_offset: tuple[int, int] | None = None
        self._draw_after_id: str | None = None
        self.chrome_visible = True

        placement = self._initial_placement(position, saved_placement)
        self.panel = tk.Frame(
            self.workspace,
            background="#15191f",
            highlightbackground="#536171",
            highlightthickness=1,
        )
        self.panel.place(
            x=placement.x,
            y=placement.y,
            width=placement.width,
            height=placement.height,
        )
        self._build_ui()
        self.set_chrome_visible(not manager.borderless)
        self.reader = threading.Thread(
            target=self._read_frames,
            name=f"camera-reader-{self.camera_index}",
            daemon=True,
        )
        self.reader.start()
        self._draw_after_id = self.window.after(
            self._display_interval_ms(), self._draw_frame
        )

    @staticmethod
    def _initial_placement(
        position: int, saved_placement: WindowPlacement | None = None
    ) -> WindowPlacement:
        if saved_placement is not None:
            return saved_placement
        column = position % 2
        row = position // 2
        return WindowPlacement(
            x=16 + column * (DEFAULT_PANEL_WIDTH + 16),
            y=16 + row * (DEFAULT_PANEL_HEIGHT + 16),
            width=DEFAULT_PANEL_WIDTH,
            height=DEFAULT_PANEL_HEIGHT,
        )

    def current_placement(self) -> WindowPlacement:
        self.window.update_idletasks()
        return WindowPlacement(
            x=self.panel.winfo_x(),
            y=self.panel.winfo_y(),
            width=self.panel.winfo_width(),
            height=self.panel.winfo_height(),
        )

    def _display_interval_ms(self) -> int:
        if len(self.manager.windows) > 1:
            return MULTI_CAMERA_DISPLAY_INTERVAL_MS
        return DISPLAY_INTERVAL_MS

    def _bind_drag_handle(self, widget) -> None:
        widget.bind("<ButtonPress-1>", self._begin_drag, add="+")
        widget.bind("<B1-Motion>", self._drag_panel, add="+")
        widget.bind("<ButtonRelease-1>", self._finish_drag, add="+")

    def _build_ui(self) -> None:
        header = tk.Frame(self.panel, background="#252d38", height=34)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        self.header = header

        display_name = self.camera_name
        if len(display_name) > 28:
            display_name = f"{display_name[:25]}..."
        name_label = tk.Label(
            header,
            background="#252d38",
            foreground="#f1f5f9",
            text=display_name,
            anchor=tk.W,
        )
        name_label.pack(side=tk.LEFT, padx=(8, 0))
        self.zoom_label = tk.Label(
            header,
            background="#252d38",
            foreground="#cbd5e1",
            text="\u7f29\u653e x1.0",
        )
        self.zoom_label.pack(side=tk.LEFT, padx=(12, 0))
        self.fps_label = tk.Label(
            header,
            background="#252d38",
            foreground="#cbd5e1",
            text="0.0 FPS",
        )
        self.fps_label.pack(side=tk.LEFT, padx=(12, 0))

        ttk.Button(header, text="X", width=3, command=self.close).pack(
            side=tk.RIGHT, padx=(0, 5), pady=3
        )
        ttk.Button(header, text="\u4fdd\u5b58", command=self.save_screenshot).pack(
            side=tk.RIGHT, padx=(0, 5), pady=3
        )
        ttk.Button(header, text="\u91cd\u7f6e", command=self.reset_zoom).pack(
            side=tk.RIGHT, padx=(0, 5), pady=3
        )

        self.canvas = tk.Canvas(
            self.panel,
            background="#101010",
            highlightthickness=0,
            takefocus=True,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.image_id = self.canvas.create_image(0, 0, anchor=tk.NW)
        self.message_id = self.canvas.create_text(
            16,
            16,
            anchor=tk.NW,
            fill="#f1f5f9",
            font=("Segoe UI", 11),
            text="Opening camera...",
        )

        for widget in (header, name_label, self.zoom_label, self.fps_label):
            self._bind_drag_handle(widget)
        self.panel.bind("<ButtonPress-1>", self._activate_panel, add="+")
        self.panel.bind(
            "<ButtonPress-1>", self._begin_borderless_window_drag, add="+"
        )
        self.panel.bind("<B1-Motion>", self._drag_borderless_window, add="+")
        self.panel.bind(
            "<ButtonRelease-1>", self._finish_borderless_window_drag, add="+"
        )
        self.panel.bind("<Motion>", self._update_borderless_window_cursor, add="+")
        self.panel.bind("<Leave>", self._clear_borderless_window_cursor, add="+")
        self.canvas.bind("<ButtonPress-1>", self._activate_panel, add="+")
        self.canvas.bind(
            "<ButtonPress-1>", self._begin_borderless_window_drag, add="+"
        )
        self.canvas.bind("<B1-Motion>", self._drag_borderless_window, add="+")
        self.canvas.bind(
            "<ButtonRelease-1>", self._finish_borderless_window_drag, add="+"
        )
        self.canvas.bind(
            "<Motion>", self._update_borderless_window_cursor, add="+"
        )
        self.canvas.bind(
            "<Leave>", self._clear_borderless_window_cursor, add="+"
        )
        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)

    def set_chrome_visible(self, visible: bool) -> None:
        if self.chrome_visible == visible:
            return
        self.chrome_visible = visible
        if visible:
            self.panel.configure(highlightthickness=1)
            self.header.pack(fill=tk.X, before=self.canvas)
        else:
            self.header.pack_forget()
            self.panel.configure(highlightthickness=0)

    def _activate_panel(self, _event=None) -> None:
        self.manager.activate_panel(self)

    def _begin_borderless_window_drag(self, event) -> str | None:
        return self.manager.begin_borderless_window_drag(event)

    def _drag_borderless_window(self, event) -> str | None:
        return self.manager.drag_borderless_window(event)

    def _finish_borderless_window_drag(self, _event=None) -> str | None:
        return self.manager.finish_borderless_window_drag()

    def _update_borderless_window_cursor(self, event) -> None:
        self.manager.update_borderless_window_cursor(event)

    def _clear_borderless_window_cursor(self, _event=None) -> None:
        self.manager.clear_borderless_window_cursor()

    def _begin_drag(self, event) -> str:
        self.manager.activate_panel(self)
        self._drag_offset = (
            event.x_root - self.panel.winfo_rootx(),
            event.y_root - self.panel.winfo_rooty(),
        )
        return "break"

    def _drag_panel(self, event) -> str:
        if self._drag_offset is None:
            return "break"
        offset_x, offset_y = self._drag_offset
        x = event.x_root - self.workspace.winfo_rootx() - offset_x
        y = event.y_root - self.workspace.winfo_rooty() - offset_y
        self._place_panel(x, y)
        return "break"

    def _finish_drag(self, _event) -> str:
        if self._drag_offset is not None:
            self._drag_offset = None
            self.manager.remember_panel_placement(self)
        return "break"

    def _place_panel(self, x: int, y: int) -> None:
        self.workspace.update_idletasks()
        panel_width = max(1, self.panel.winfo_width())
        panel_height = max(1, self.panel.winfo_height())
        visible_header_width = min(120, panel_width)
        visible_header_height = min(34, panel_height)
        max_x = max(0, self.workspace.winfo_width() - visible_header_width)
        max_y = max(0, self.workspace.winfo_height() - visible_header_height)
        self.panel.place_configure(
            x=min(max(round(x), 0), max_x),
            y=min(max(round(y), 0), max_y),
        )

    def _read_frames(self) -> None:
        failed_reads = 0
        while self.running:
            read_started = time.perf_counter()
            ok, frame = self.capture.read()
            read_elapsed = time.perf_counter() - read_started
            if not ok:
                failed_reads += 1
                if failed_reads >= 30:
                    self.status_text = "Camera is not returning frames."
                time.sleep(0.03)
                continue

            failed_reads = 0
            with self.frame_lock:
                self.latest_frame = frame
                self.frame_count += 1
                self.frame_sequence += 1
            self.manager.notify_frame_available()
            if read_elapsed < self.frame_interval:
                time.sleep(self.frame_interval - read_elapsed)

    def _take_latest_frame(self):
        with self.frame_lock:
            if self.latest_frame is None:
                return None
            return self.latest_frame.copy()

    def _take_latest_frame_with_id(self):
        with self.frame_lock:
            if self.latest_frame is None:
                return None, 0
            return self.latest_frame.copy(), self.frame_sequence

    def _latest_frame_sequence(self) -> int:
        with self.frame_lock:
            return self.frame_sequence if self.latest_frame is not None else 0

    def _update_fps(self) -> None:
        now = time.perf_counter()
        elapsed = now - self.fps_start
        if elapsed < 0.5:
            return
        with self.frame_lock:
            count = self.frame_count
            self.frame_count = 0
        self.fps = count / elapsed
        self.fps_start = now

    def _display_size(self, frame_width: int, frame_height: int) -> tuple[int, int, int, int]:
        canvas_width = max(1, self.canvas.winfo_width())
        canvas_height = max(1, self.canvas.winfo_height())
        scale = min(canvas_width / frame_width, canvas_height / frame_height)
        display_width = max(1, round(frame_width * scale))
        display_height = max(1, round(frame_height * scale))
        left = (canvas_width - display_width) // 2
        top = (canvas_height - display_height) // 2
        return left, top, display_width, display_height

    @staticmethod
    def _to_image(frame) -> Image.Image:
        if not frame.flags["C_CONTIGUOUS"]:
            frame = frame.copy()
        height, width = frame.shape[:2]
        return Image.frombuffer("RGB", (width, height), frame, "raw", "BGR", 0, 1)

    @classmethod
    def _to_photo(cls, frame, master=None):
        return ImageTk.PhotoImage(cls._to_image(frame), master=master)

    def _update_photo(self, frame) -> None:
        image = self._to_image(frame)
        width, height = image.size
        if (
            self.image_photo is None
            or self.image_photo.width() != width
            or self.image_photo.height() != height
        ):
            self.image_photo = ImageTk.PhotoImage(image, master=self.window)
            self.canvas.itemconfigure(self.image_id, image=self.image_photo)
        else:
            # Reusing the Tk image prevents allocation churn for every frame.
            self.image_photo.paste(image)

    def _draw_frame(self) -> None:
        self._draw_after_id = None
        if not self.running:
            return

        self._update_fps()
        frame = self._take_latest_frame()
        if frame is not None:
            source_height, source_width = frame.shape[:2]
            source_size = (source_width, source_height)
            if source_size != self.source_size:
                self.source_size = source_size
                self.manager._schedule_borderless_fit()
            left, top, display_width, display_height = self._display_size(source_width, source_height)
            display_frame = self.zoom_view.render(frame, display_width, display_height)
            self._update_photo(display_frame)
            self.canvas.coords(self.image_id, left, top)
            self.canvas.tag_lower(self.image_id)
            self.image_bounds = (left, top, display_width, display_height)
            self.zoom_label.configure(text=f"\u7f29\u653e x{self.zoom_view.zoom:.1f}")
            self.fps_label.configure(text=f"{self.fps:.1f} FPS")
            if time.perf_counter() > self.status_until:
                self.status_text = ""

        self.canvas.itemconfigure(self.message_id, text=self.status_text)
        try:
            self._draw_after_id = self.window.after(
                self._display_interval_ms(), self._draw_frame
            )
        except tk.TclError:
            pass

    def on_mouse_wheel(self, event) -> str | None:
        if not self.image_bounds or not event.delta:
            return None
        left, top, width, height = self.image_bounds
        mouse_x = event.x_root - self.canvas.winfo_rootx()
        mouse_y = event.y_root - self.canvas.winfo_rooty()
        if not (left <= mouse_x < left + width and top <= mouse_y < top + height):
            return "break"
        direction = 1 if event.delta > 0 else -1
        self.zoom_view.zoom_at((mouse_x - left) / width, (mouse_y - top) / height, direction)
        return "break"

    def reset_zoom(self) -> None:
        self.zoom_view.reset()

    def save_screenshot(self) -> None:
        frame = self._take_latest_frame()
        if frame is None:
            self.status_text = "No camera frame is available yet."
        else:
            output_path = save_frame(frame, self.camera_index)
            self.status_text = f"Saved: {output_path.name}"
        self.status_until = time.perf_counter() + 3.0

    def set_status(self, text: str, duration: float = 3.0) -> None:
        self.status_text = text
        self.status_until = time.perf_counter() + duration

    def toggle_fullscreen(self) -> None:
        self.manager.toggle_fullscreen()

    def on_escape(self, _event=None) -> None:
        self.manager.exit_fullscreen_or_close_active()

    def close(self, notify_manager: bool = True) -> None:
        if not self.running:
            return
        self.running = False
        if self._draw_after_id is not None:
            try:
                self.window.after_cancel(self._draw_after_id)
            except tk.TclError:
                pass
            self._draw_after_id = None
        self.manager.remember_panel_placement(self)
        try:
            self.capture.release()
        finally:
            try:
                self.panel.destroy()
            except tk.TclError:
                pass
        if notify_manager:
            self.manager.camera_closed(self)


class CameraManager:
    """Owns one preview window and the camera panels inside its workspace."""

    def __init__(
        self,
        captures: list[OpenedCapture],
        width: int,
        height: int,
        fps: int,
        max_index: int,
        capture_fourcc: str = DEFAULT_CAPTURE_FOURCC,
        available_local_indices: Iterable[int] | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.max_index = max_index
        self.capture_fourcc = capture_fourcc
        self.local_camera_indices: set[int] = {
            index
            for index in (available_local_indices or ())
            if isinstance(index, int) and index >= 0
        }
        self.local_camera_indices.update(
            opened.index
            for opened in captures
            if isinstance(opened.index, int) and opened.index >= 0
        )
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.protocol("WM_DELETE_WINDOW", self.close_all)
        self.windows: dict[int | str, CameraPanel] = {}
        self.panel_placements = load_panel_placements()
        self.main_window_placement = load_main_window_placement()
        self._windowed_preview_placement = self.main_window_placement
        self._windowed_panel_placements = dict(self.panel_placements)
        self._windowed_workspace_offset = (0, 0)
        self._saved_borderless_placement = load_borderless_window_placement()
        self._borderless_custom_size: tuple[int, int] | None = None
        self._borderless_content_offset = (0, 0)
        self._borderless_preview_position: tuple[int, int] | None = None
        self._borderless_window_drag_offset: tuple[int, int] | None = None
        self._borderless_resize_edges: str | None = None
        self._borderless_resize_start: tuple[int, int, int, int, int, int] | None = None
        self._borderless_resize_layout: list[
            tuple[CameraPanel, int, int, int, int]
        ] = []
        self._borderless_manual_geometry = False
        self.always_on_top, self.borderless = load_preview_display_options()
        self.preview_window: tk.Toplevel | None = None
        self.preview_toolbar: ttk.Frame | None = None
        self.workspace: tk.Frame | None = None
        self.active_panel: CameraPanel | None = None
        self.preview_fullscreen = False
        self.topmost_button: ttk.Button | None = None
        self.borderless_button: ttk.Button | None = None
        self._show_launcher_when_empty = not captures
        self.launcher_window: tk.Toplevel | None = None
        self.launcher_scan_button: ttk.Button | None = None
        self.launcher_status_var: tk.StringVar | None = None
        self._local_scan_events = queue.SimpleQueue()
        self._local_scan_poll_after_id: str | None = None
        self._local_scan_running = False
        self._borderless_fit_after_id: str | None = None
        self._network_dialog_cleanups: set = set()
        self.shutting_down = False
        self.lan_stream_port, self.lan_stream_quality = load_lan_stream_options()
        self.lan_stream_server: LANStreamServer | None = None
        self.lan_stream_button: ttk.Button | None = None
        self._hotkey_poll_after_id: str | None = None
        self._close_after_id: str | None = None
        self.previews_hidden = False
        self.hotkey = load_hotkey()
        self._hotkey_events = queue.SimpleQueue()
        self._hotkey_stop = threading.Event()
        self._hotkey_ready = threading.Event()
        self._hotkey_thread: threading.Thread | None = None
        self._hotkey_thread_id: int | None = None
        self.hotkey_available = False
        self._start_hotkey_listener()
        self._hotkey_poll_after_id = self.root.after(80, self._poll_hotkey_events)

        for position, opened in enumerate(captures):
            self.add_camera(opened, position)

        if not self.windows:
            self.show_launcher()

    def notify_frame_available(self) -> None:
        server = self.lan_stream_server
        if server is not None:
            server.signal_frame_available()

    def _refresh_lan_stream_panels(self) -> None:
        server = self.lan_stream_server
        if server is not None:
            server.set_panels(self.windows.values())

    def _update_lan_stream_button(self) -> None:
        if self.lan_stream_button is None:
            return
        try:
            self.lan_stream_button.configure(
                text=(
                    f"\u63a8\u6d41\u4e2d ({self.lan_stream_server.port})"
                    if self.lan_stream_server is not None
                    else "\u5c40\u57df\u7f51\u63a8\u6d41"
                )
            )
        except tk.TclError:
            self.lan_stream_button = None

    def stop_lan_stream(self) -> None:
        server = self.lan_stream_server
        self.lan_stream_server = None
        if server is not None:
            server.stop()
        self._update_lan_stream_button()

    def start_lan_stream(self, port: int, quality: int) -> LANStreamServer:
        if not (1 <= port <= 65535):
            raise ValueError("\u7aef\u53e3\u5fc5\u987b\u5728 1 \u5230 65535 \u4e4b\u95f4\u3002")
        if not (LAN_STREAM_MIN_QUALITY <= quality <= LAN_STREAM_MAX_QUALITY):
            raise ValueError(
                f"JPEG \u753b\u8d28\u5fc5\u987b\u5728 {LAN_STREAM_MIN_QUALITY} \u5230 {LAN_STREAM_MAX_QUALITY} \u4e4b\u95f4\u3002"
            )
        self.stop_lan_stream()
        try:
            server = LANStreamServer(port, quality)
            server.set_panels(self.windows.values())
            server.start()
        except OSError as error:
            raise RuntimeError(f"\u65e0\u6cd5\u76d1\u542c\u7aef\u53e3 {port}\uff1a{error}") from error
        self.lan_stream_port = server.port
        self.lan_stream_quality = quality
        self.lan_stream_server = server
        try:
            save_lan_stream_options(server.port, quality)
        except OSError:
            pass
        self._update_lan_stream_button()
        return server

    def _lan_stream_status_text(self) -> str:
        server = self.lan_stream_server
        if server is None:
            return "\u5c40\u57df\u7f51\u63a8\u6d41\u672a\u542f\u52a8\u3002"
        urls = server.urls()
        return "\u5df2\u542f\u52a8\uff1a\n" + "\n".join(urls)

    def open_lan_stream_dialog(self, parent) -> None:
        if self.shutting_down:
            return
        dialog = tk.Toplevel(parent)
        dialog.title("\u5c40\u57df\u7f51\u63a8\u6d41")
        dialog.transient(parent)
        dialog.resizable(False, False)
        dialog.grab_set()
        container = ttk.Frame(dialog, padding=16)
        container.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            container,
            text="\u65e0\u9700 OBS\uff1a\u542f\u52a8\u540e\uff0c\u5728\u540c\u4e00\u5c40\u57df\u7f51\u7684\u6d4f\u89c8\u5668\u6253\u5f00\u4e0b\u9762\u5730\u5740\u3002",
        ).pack(anchor=tk.W)
        form = ttk.Frame(container)
        form.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(form, text="\u7aef\u53e3").grid(row=0, column=0, sticky=tk.W)
        port_var = tk.StringVar(value=str(self.lan_stream_port))
        ttk.Entry(form, textvariable=port_var, width=10).grid(
            row=0, column=1, sticky=tk.W, padx=(8, 18)
        )
        ttk.Label(form, text="JPEG \u753b\u8d28").grid(row=0, column=2, sticky=tk.W)
        quality_var = tk.StringVar(value=str(self.lan_stream_quality))
        ttk.Spinbox(
            form,
            from_=LAN_STREAM_MIN_QUALITY,
            to=LAN_STREAM_MAX_QUALITY,
            textvariable=quality_var,
            width=7,
        ).grid(row=0, column=3, sticky=tk.W, padx=(8, 0))
        status_var = tk.StringVar(value=self._lan_stream_status_text())
        status_label = ttk.Label(
            container, textvariable=status_var, justify=tk.LEFT, wraplength=520
        )
        status_label.pack(anchor=tk.W, pady=(12, 0))

        def refresh_status() -> None:
            status_var.set(self._lan_stream_status_text())
            self._update_lan_stream_button()

        def start() -> None:
            try:
                port = int(port_var.get().strip())
                quality = int(quality_var.get().strip())
                server = self.start_lan_stream(port, quality)
            except (ValueError, RuntimeError) as error:
                status_var.set(str(error))
                return
            port_var.set(str(server.port))
            refresh_status()

        def stop() -> None:
            self.stop_lan_stream()
            refresh_status()

        def close_dialog() -> None:
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass

        buttons = ttk.Frame(container)
        buttons.pack(fill=tk.X, pady=(14, 0))
        ttk.Button(buttons, text="\u542f\u52a8/\u91cd\u542f", command=start).pack(side=tk.LEFT)
        ttk.Button(buttons, text="\u505c\u6b62", command=stop).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(buttons, text="\u5173\u95ed", command=close_dialog).pack(side=tk.RIGHT)
        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        dialog.after_idle(lambda: self._center_window(dialog, parent))

    @staticmethod
    def _geometry_from_placement(placement: WindowPlacement) -> str:
        return (
            f"{placement.width}x{placement.height}"
            f"+{placement.x}+{placement.y}"
        )

    def ensure_preview_window(self) -> None:
        if self.preview_window is not None:
            try:
                if self.preview_window.winfo_exists():
                    return
            except tk.TclError:
                pass
            self.preview_window = None
            self.preview_toolbar = None
            self.workspace = None
            self.topmost_button = None
            self.borderless_button = None
            self.lan_stream_button = None

        window = tk.Toplevel(self.root)
        self.preview_window = window
        window.title(WINDOW_TITLE)
        window.minsize(720, 480)
        if self.main_window_placement is not None:
            window.geometry(self._geometry_from_placement(self.main_window_placement))
        else:
            window.geometry(
                f"{DEFAULT_MAIN_WINDOW_WIDTH}x{DEFAULT_MAIN_WINDOW_HEIGHT}+60+60"
            )
        window.protocol("WM_DELETE_WINDOW", self.close_all)

        toolbar = ttk.Frame(window, padding=(8, 6))
        self.preview_toolbar = toolbar
        toolbar.pack(fill=tk.X)
        ttk.Label(toolbar, text=WINDOW_TITLE, font=("Segoe UI", 11)).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="\u5173\u95ed", command=self.close_all).pack(side=tk.RIGHT)
        ttk.Button(toolbar, text="\u5168\u5c4f", command=self.toggle_fullscreen).pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        self.borderless_button = ttk.Button(
            toolbar,
            command=self.toggle_borderless,
        )
        self.borderless_button.pack(side=tk.RIGHT, padx=(0, 6))
        self.topmost_button = ttk.Button(
            toolbar,
            command=self.toggle_always_on_top,
        )
        self.topmost_button.pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(
            toolbar,
            text="\u5feb\u6377\u952e",
            command=lambda: self.open_hotkey_dialog(window),
        ).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(
            toolbar,
            text="\u7f51\u7edc",
            command=lambda: self.open_network_camera_dialog(window),
        ).pack(side=tk.RIGHT, padx=(0, 6))
        self.lan_stream_button = ttk.Button(
            toolbar,
            command=lambda: self.open_lan_stream_dialog(window),
        )
        self.lan_stream_button.pack(side=tk.RIGHT, padx=(0, 6))
        self._update_lan_stream_button()
        ttk.Button(toolbar, text="\u626b\u63cf", command=self.scan_for_cameras).pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        ttk.Button(toolbar, text="\u5207\u6362", command=self.switch_camera).pack(
            side=tk.RIGHT, padx=(0, 6)
        )

        self.workspace = tk.Frame(window, background="#0f141b", highlightthickness=0)
        self.workspace.pack(fill=tk.BOTH, expand=True)
        self.workspace.bind(
            "<Motion>", self.update_borderless_window_cursor, add="+"
        )
        self.workspace.bind(
            "<Leave>", self.clear_borderless_window_cursor, add="+"
        )
        self.workspace.bind(
            "<ButtonPress-1>", self.begin_borderless_window_drag, add="+"
        )
        self.workspace.bind(
            "<B1-Motion>", self.drag_borderless_window, add="+"
        )
        self.workspace.bind(
            "<ButtonRelease-1>", self.finish_borderless_window_drag, add="+"
        )
        window.bind(
            "<B1-Motion>", self.drag_borderless_window, add="+"
        )
        window.bind(
            "<ButtonRelease-1>", self.finish_borderless_window_drag, add="+"
        )
        window.bind(
            "<Motion>", self.update_borderless_window_cursor, add="+"
        )
        window.bind("<Key-r>", lambda _event: self._reset_active_panel())
        window.bind("<Key-R>", lambda _event: self._reset_active_panel())
        window.bind("<Key-s>", lambda _event: self._save_active_panel())
        window.bind("<Key-S>", lambda _event: self._save_active_panel())
        window.bind("<Key-f>", lambda _event: self.toggle_fullscreen())
        window.bind("<Key-F>", lambda _event: self.toggle_fullscreen())
        window.bind("<F11>", lambda _event: self.toggle_fullscreen())
        window.bind("<Key-q>", lambda _event: self._close_active_panel())
        window.bind("<Key-Q>", lambda _event: self._close_active_panel())
        window.bind("<Escape>", self.exit_fullscreen_or_close_active)
        window.bind("<Control-b>", lambda _event: self.toggle_borderless())
        window.bind("<Control-B>", lambda _event: self.toggle_borderless())
        window.bind("<Control-t>", lambda _event: self.toggle_always_on_top())
        window.bind("<Control-T>", lambda _event: self.toggle_always_on_top())
        window.bind(
            "<Control-KeyPress-n>",
            lambda _event: self._handle_camera_switch(1),
        )
        window.bind(
            "<Control-KeyPress-p>",
            lambda _event: self._handle_camera_switch(-1),
        )
        window.bind("<Button-3>", self.show_preview_menu, add="+")
        if self.borderless:
            self._capture_windowed_layout()
        self._apply_preview_display_options()

    def _show_preview_window(self) -> None:
        self.ensure_preview_window()
        if self.preview_window is None or self.previews_hidden:
            return
        try:
            self.preview_window.deiconify()
            self.preview_window.lift()
        except tk.TclError:
            pass

    def _hide_preview_window(self) -> None:
        if self.preview_window is None:
            return
        try:
            if self.preview_window.winfo_exists():
                self.preview_window.withdraw()
        except tk.TclError:
            pass

    def _save_preview_display_options(self) -> None:
        try:
            save_preview_display_options(self.always_on_top, self.borderless)
        except OSError:
            pass

    def _update_preview_display_buttons(self) -> None:
        if self.topmost_button is not None:
            self.topmost_button.configure(
                text="\u53d6\u6d88\u7f6e\u9876"
                if self.always_on_top
                else "\u7f6e\u9876"
            )
        if self.borderless_button is not None:
            self.borderless_button.configure(
                text="\u663e\u793a\u8fb9\u6846"
                if self.borderless
                else "\u7eaf\u753b\u9762"
            )

    def _apply_preview_display_options(self) -> None:
        if self.preview_window is None:
            return
        try:
            self.preview_window.attributes("-topmost", self.always_on_top)
            self.preview_window.overrideredirect(self.borderless)
            self.preview_window.minsize(
                1 if self.borderless else 720,
                1 if self.borderless else 480,
            )
            if self.preview_toolbar is not None:
                if self.borderless:
                    self.preview_toolbar.pack_forget()
                elif not self.preview_toolbar.winfo_manager():
                    self.preview_toolbar.pack(fill=tk.X, before=self.workspace)
            for panel in self.windows.values():
                panel.set_chrome_visible(not self.borderless)
            self._update_preview_display_buttons()
            if self.always_on_top:
                self.preview_window.lift()
        except tk.TclError:
            pass

    def _capture_windowed_layout(self) -> None:
        if self.preview_window is None or self.workspace is None:
            return
        try:
            self._borderless_preview_position = None
            self._borderless_window_drag_offset = None
            self._borderless_resize_edges = None
            self._borderless_resize_start = None
            self._borderless_resize_layout = []
            self._borderless_manual_geometry = False
            self._borderless_custom_size = None
            self._borderless_content_offset = (0, 0)
            self.preview_window.update_idletasks()
            self._windowed_preview_placement = WindowPlacement(
                x=self.preview_window.winfo_x(),
                y=self.preview_window.winfo_y(),
                width=self.preview_window.winfo_width(),
                height=self.preview_window.winfo_height(),
            )
            self._windowed_workspace_offset = (
                self.workspace.winfo_rootx() - self.preview_window.winfo_rootx(),
                self.workspace.winfo_rooty() - self.preview_window.winfo_rooty(),
            )
            for panel in self.windows.values():
                self._windowed_panel_placements[panel.camera_index] = (
                    panel.current_placement()
                )
        except tk.TclError:
            pass

    def _restore_windowed_layout(self) -> None:
        if self.preview_window is None:
            return
        try:
            if self._windowed_preview_placement is not None:
                self.preview_window.geometry(
                    self._geometry_from_placement(self._windowed_preview_placement)
                )
            for panel in self.windows.values():
                placement = self._windowed_panel_placements.get(panel.camera_index)
                if placement is not None:
                    panel.panel.place_configure(
                        x=placement.x,
                        y=placement.y,
                        width=placement.width,
                        height=placement.height,
                    )
            self.preview_window.update_idletasks()
        except tk.TclError:
            pass

    def _schedule_borderless_fit(self) -> None:
        if (
            not self.borderless
            or not self.windows
            or self._borderless_fit_after_id is not None
        ):
            return
        try:
            self._borderless_fit_after_id = self.root.after_idle(
                self._fit_borderless_preview_to_panels
            )
        except tk.TclError:
            pass

    def _fit_borderless_preview_to_panels(self) -> None:
        self._borderless_fit_after_id = None
        if (
            not self.borderless
            or self._borderless_manual_geometry
            or self.preview_window is None
            or self.workspace is None
            or not self.windows
        ):
            return

        try:
            self.preview_window.update_idletasks()
            targets: list[tuple[CameraPanel, WindowPlacement, int, int]] = []
            for panel in self.windows.values():
                placement = self._windowed_panel_placements.get(panel.camera_index)
                if placement is None:
                    placement = panel.current_placement()
                    self._windowed_panel_placements[panel.camera_index] = placement
                width = max(1, placement.width)
                if panel.source_size is None:
                    height = max(1, placement.height)
                else:
                    source_width, source_height = panel.source_size
                    height = max(1, round(width * source_height / source_width))
                targets.append((panel, placement, width, height))

            left = min(placement.x for _, placement, _, _ in targets)
            top = min(placement.y for _, placement, _, _ in targets)
            right = max(placement.x + width for _, placement, width, _ in targets)
            bottom = max(placement.y + height for _, placement, _, height in targets)
            window_width = max(1, right - left)
            window_height = max(1, bottom - top)
            self._borderless_content_offset = (left, top)
            layout_width = window_width
            layout_height = window_height
            saved = self._saved_borderless_placement

            base = self._windowed_preview_placement
            if base is None:
                base = WindowPlacement(
                    x=self.preview_window.winfo_x(),
                    y=self.preview_window.winfo_y(),
                    width=self.preview_window.winfo_width(),
                    height=self.preview_window.winfo_height(),
                )
                self._windowed_preview_placement = base
            if self._borderless_preview_position is None:
                if saved is not None:
                    window_x = saved.x
                    window_y = saved.y
                    self._borderless_custom_size = (
                        max(MIN_PURE_WINDOW_WIDTH, saved.width),
                        max(MIN_PURE_WINDOW_HEIGHT, saved.height),
                    )
                else:
                    offset_x, offset_y = self._windowed_workspace_offset
                    window_x = base.x + offset_x + left
                    window_y = base.y + offset_y + top
                    window_x = min(
                        max(0, window_x),
                        max(0, self.preview_window.winfo_screenwidth() - window_width),
                    )
                    window_y = min(
                        max(0, window_y),
                        max(0, self.preview_window.winfo_screenheight() - window_height),
                    )
                window_x = min(
                    max(0, window_x),
                    max(0, self.preview_window.winfo_screenwidth() - window_width),
                )
                window_y = min(
                    max(0, window_y),
                    max(0, self.preview_window.winfo_screenheight() - window_height),
                )
                self._borderless_preview_position = (window_x, window_y)
            else:
                window_x, window_y = self._borderless_preview_position

            if self._borderless_custom_size is not None:
                window_width, window_height = self._borderless_custom_size
                window_x = min(
                    max(0, window_x),
                    max(0, self.preview_window.winfo_screenwidth() - window_width),
                )
                window_y = min(
                    max(0, window_y),
                    max(0, self.preview_window.winfo_screenheight() - window_height),
                )
                self._borderless_preview_position = (window_x, window_y)

            for panel, placement, width, height in targets:
                if self._borderless_custom_size is not None:
                    scale_x = window_width / layout_width
                    scale_y = window_height / layout_height
                    panel_x = round((placement.x - left) * scale_x)
                    panel_y = round((placement.y - top) * scale_y)
                    panel_width = max(1, round(width * scale_x))
                    panel_height = max(1, round(height * scale_y))
                else:
                    panel_x = placement.x - left
                    panel_y = placement.y - top
                    panel_width = width
                    panel_height = height
                panel.panel.place_configure(
                    x=panel_x,
                    y=panel_y,
                    width=panel_width,
                    height=panel_height,
                )
            self.preview_window.geometry(
                f"{window_width}x{window_height}{window_x:+d}{window_y:+d}"
            )
            self.preview_window.update_idletasks()
        except tk.TclError:
            pass

    def _borderless_resize_edges_at(self, event) -> str:
        if (
            not self.borderless
            or self.preview_window is None
            or self.preview_fullscreen
        ):
            return ""
        try:
            self.preview_window.update_idletasks()
            width = max(1, self.preview_window.winfo_width())
            height = max(1, self.preview_window.winfo_height())
            x = event.x_root - self.preview_window.winfo_rootx()
            y = event.y_root - self.preview_window.winfo_rooty()
        except tk.TclError:
            return ""

        border = PURE_RESIZE_BORDER
        edges = ""
        if 0 <= x <= border:
            edges += "w"
        elif width - border <= x <= width:
            edges += "e"
        if 0 <= y <= border:
            edges += "n"
        elif height - border <= y <= height:
            edges += "s"
        return edges

    @staticmethod
    def _borderless_cursor_for_edges(edges: str) -> str:
        if edges in ("n", "s"):
            return "sb_v_double_arrow"
        if edges in ("e", "w"):
            return "sb_h_double_arrow"
        if edges == "nw":
            return "top_left_corner"
        if edges == "se":
            return "bottom_right_corner"
        if edges == "ne":
            return "top_right_corner"
        if edges == "sw":
            return "bottom_left_corner"
        return ""

    def update_borderless_window_cursor(self, event) -> None:
        cursor = self._borderless_cursor_for_edges(
            self._borderless_resize_edges_at(event)
        )
        try:
            event.widget.configure(cursor=cursor)
        except tk.TclError:
            pass

    def clear_borderless_window_cursor(self, event=None) -> None:
        widget = getattr(event, "widget", None) or self.workspace
        if widget is None:
            return
        try:
            widget.configure(cursor="")
        except tk.TclError:
            pass

    def _cancel_borderless_fit(self) -> None:
        if self._borderless_fit_after_id is None:
            return
        try:
            self.root.after_cancel(self._borderless_fit_after_id)
        except tk.TclError:
            pass
        self._borderless_fit_after_id = None

    def begin_borderless_window_drag(self, event) -> str | None:
        if (
            not self.borderless
            or self.preview_window is None
            or self.preview_fullscreen
        ):
            return None
        try:
            self.preview_window.update_idletasks()
            edges = self._borderless_resize_edges_at(event)
            if edges:
                window_x = self.preview_window.winfo_x()
                window_y = self.preview_window.winfo_y()
                window_width = max(1, self.preview_window.winfo_width())
                window_height = max(1, self.preview_window.winfo_height())
                self._borderless_resize_edges = edges
                self._borderless_resize_start = (
                    event.x_root,
                    event.y_root,
                    window_x,
                    window_y,
                    window_width,
                    window_height,
                )
                self._borderless_resize_layout = []
                for panel in self.windows.values():
                    panel.panel.update_idletasks()
                    self._borderless_resize_layout.append(
                        (
                            panel,
                            panel.panel.winfo_x(),
                            panel.panel.winfo_y(),
                            panel.panel.winfo_width(),
                            panel.panel.winfo_height(),
                        )
                    )
                self._borderless_manual_geometry = True
                self._borderless_custom_size = (window_width, window_height)
                self._cancel_borderless_fit()
                return "break"
            self._borderless_window_drag_offset = (
                event.x_root - self.preview_window.winfo_rootx(),
                event.y_root - self.preview_window.winfo_rooty(),
            )
        except tk.TclError:
            self._borderless_window_drag_offset = None
        return "break"

    def drag_borderless_window(self, event) -> str | None:
        if self._borderless_resize_start is not None:
            return self._resize_borderless_window(event)
        if self._borderless_window_drag_offset is None or self.preview_window is None:
            return None
        try:
            offset_x, offset_y = self._borderless_window_drag_offset
            window_x = round(event.x_root - offset_x)
            window_y = round(event.y_root - offset_y)
            self.preview_window.geometry(f"{window_x:+d}{window_y:+d}")
            self._borderless_preview_position = (window_x, window_y)
        except tk.TclError:
            self._borderless_window_drag_offset = None
        return "break"

    def _resize_borderless_window(self, event) -> str:
        if self.preview_window is None or self._borderless_resize_start is None:
            return "break"
        start_x, start_y, start_window_x, start_window_y, start_width, start_height = (
            self._borderless_resize_start
        )
        edges = self._borderless_resize_edges or ""
        delta_x = event.x_root - start_x
        delta_y = event.y_root - start_y
        window_x = start_window_x
        window_y = start_window_y
        window_width = start_width
        window_height = start_height
        if "e" in edges:
            window_width = start_width + delta_x
        if "s" in edges:
            window_height = start_height + delta_y
        if "w" in edges:
            window_x = start_window_x + delta_x
            window_width = start_width - delta_x
        if "n" in edges:
            window_y = start_window_y + delta_y
            window_height = start_height - delta_y

        if window_width < MIN_PURE_WINDOW_WIDTH:
            if "w" in edges:
                window_x -= MIN_PURE_WINDOW_WIDTH - window_width
            window_width = MIN_PURE_WINDOW_WIDTH
        if window_height < MIN_PURE_WINDOW_HEIGHT:
            if "n" in edges:
                window_y -= MIN_PURE_WINDOW_HEIGHT - window_height
            window_height = MIN_PURE_WINDOW_HEIGHT

        window_width = min(window_width, MAX_WINDOW_DIMENSION)
        window_height = min(window_height, MAX_WINDOW_DIMENSION)
        try:
            self.preview_window.geometry(
                f"{round(window_width)}x{round(window_height)}"
                f"{round(window_x):+d}{round(window_y):+d}"
            )
            self.preview_window.update_idletasks()
            scale_x = window_width / max(1, start_width)
            scale_y = window_height / max(1, start_height)
            for panel, x, y, width, height in self._borderless_resize_layout:
                panel.panel.place_configure(
                    x=round(x * scale_x),
                    y=round(y * scale_y),
                    width=max(1, round(width * scale_x)),
                    height=max(1, round(height * scale_y)),
                )
            self._borderless_preview_position = (round(window_x), round(window_y))
            self._borderless_custom_size = (round(window_width), round(window_height))
        except tk.TclError:
            pass
        return "break"

    def _remember_borderless_window_position(self) -> None:
        if not self.borderless or self.preview_window is None:
            return
        try:
            self.preview_window.update_idletasks()
            window_x = self.preview_window.winfo_x()
            window_y = self.preview_window.winfo_y()
            window_width = max(1, self.preview_window.winfo_width())
            window_height = max(1, self.preview_window.winfo_height())
        except tk.TclError:
            return

        self._borderless_preview_position = (window_x, window_y)
        borderless_placement = WindowPlacement(
            x=window_x,
            y=window_y,
            width=window_width,
            height=window_height,
        )
        self._saved_borderless_placement = borderless_placement
        try:
            save_borderless_window_placement(borderless_placement)
        except OSError:
            pass
        base = self._windowed_preview_placement
        if base is None:
            return
        offset_x, offset_y = self._windowed_workspace_offset
        content_x, content_y = self._borderless_content_offset
        placement = WindowPlacement(
            x=window_x - offset_x - content_x,
            y=window_y - offset_y - content_y,
            width=base.width,
            height=base.height,
        )
        self._windowed_preview_placement = placement
        self.main_window_placement = placement
        try:
            save_main_window_placement(placement)
        except OSError:
            pass

    def finish_borderless_window_drag(self) -> str | None:
        if self._borderless_resize_start is not None:
            self._borderless_resize_start = None
            self._borderless_resize_edges = None
            self._borderless_resize_layout = []
            self._borderless_manual_geometry = False
            self._remember_borderless_window_position()
            return "break"
        if self._borderless_window_drag_offset is None:
            return None
        self._borderless_window_drag_offset = None
        self._remember_borderless_window_position()
        return "break"

    def toggle_always_on_top(self) -> None:
        self.always_on_top = not self.always_on_top
        self._apply_preview_display_options()
        self._save_preview_display_options()

    def toggle_borderless(self) -> None:
        if self.borderless:
            self._remember_borderless_window_position()
            self.borderless = False
            self._apply_preview_display_options()
            self._restore_windowed_layout()
            self._borderless_preview_position = None
            self._borderless_window_drag_offset = None
            self._borderless_resize_edges = None
            self._borderless_resize_start = None
            self._borderless_resize_layout = []
            self._borderless_manual_geometry = False
            self._borderless_custom_size = None
        else:
            self._capture_windowed_layout()
            self.borderless = True
            self._apply_preview_display_options()
            self._schedule_borderless_fit()
        self._save_preview_display_options()

    def show_preview_menu(self, event) -> str:
        if self.preview_window is None:
            return "break"
        menu = tk.Menu(self.preview_window, tearoff=False)
        menu.add_command(
            label="\u53d6\u6d88\u7f6e\u9876"
            if self.always_on_top
            else "\u7f6e\u9876",
            command=self.toggle_always_on_top,
        )
        menu.add_command(
            label="\u663e\u793a\u8fb9\u6846"
            if self.borderless
            else "\u7eaf\u753b\u9762",
            command=self.toggle_borderless,
        )
        menu.add_command(label="\u5207\u6362\u6444\u50cf\u5934", command=self.switch_camera)
        menu.add_separator()
        menu.add_command(label="\u5168\u5c4f", command=self.toggle_fullscreen)
        menu.add_command(label="\u5173\u95ed", command=self.close_all)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _reset_active_panel(self) -> None:
        if self.active_panel is not None and self.active_panel.running:
            self.active_panel.reset_zoom()

    def _save_active_panel(self) -> None:
        if self.active_panel is not None and self.active_panel.running:
            self.active_panel.save_screenshot()

    def _close_active_panel(self) -> None:
        if self.active_panel is not None and self.active_panel.running:
            self.active_panel.close()

    def _handle_camera_switch(self, direction: int) -> str:
        self.switch_camera(direction)
        return "break"

    def _replace_panel_camera(
        self, panel: CameraPanel, opened: OpenedCapture
    ) -> bool:
        try:
            placement = panel.current_placement()
        except tk.TclError:
            placement = None

        old_index = panel.camera_index
        panel.close(notify_manager=False)
        self.windows.pop(old_index, None)
        if self.active_panel is panel:
            self.active_panel = None

        if placement is not None:
            self.panel_placements[opened.index] = placement
        return self.add_camera(opened, position=0)

    def switch_camera(self, direction: int = 1) -> None:
        """Activate or replace the current panel with the next local camera."""
        if self.shutting_down:
            return
        direction = -1 if direction < 0 else 1
        local_indices = sorted(self.local_camera_indices)
        if not local_indices:
            return

        source_panel = self.active_panel
        if source_panel is None or not isinstance(source_panel.camera_index, int):
            source_panel = next(
                (
                    panel
                    for panel in self.windows.values()
                    if isinstance(panel.camera_index, int)
                ),
                None,
            )
        current_index = source_panel.camera_index if source_panel is not None else None
        if len(local_indices) == 1 and current_index == local_indices[0]:
            if source_panel is not None:
                source_panel.set_status(
                    "\u6ca1\u6709\u5176\u4ed6\u53ef\u7528\u7684\u672c\u5730\u6444\u50cf\u5934\u3002"
                )
            return

        if current_index in local_indices:
            start = local_indices.index(current_index)
        else:
            start = -1 if direction > 0 else 0

        for step in range(1, len(local_indices) + 1):
            candidate_index = local_indices[(start + direction * step) % len(local_indices)]
            if candidate_index == current_index:
                continue

            existing = self.windows.get(candidate_index)
            if existing is not None:
                self.activate_panel(existing)
                existing.set_status(f"\u5df2\u5207\u6362\u5230 \u6444\u50cf\u5934 {candidate_index}")
                return

            try:
                opened = open_capture(
                    candidate_index,
                    self.width,
                    self.height,
                    self.fps,
                    self.capture_fourcc,
                )
            except RuntimeError:
                continue

            if source_panel is None:
                if self.add_camera(opened):
                    return
                continue
            if self._replace_panel_camera(source_panel, opened):
                return

        if source_panel is not None and source_panel.running:
            source_panel.set_status("\u6ca1\u6709\u627e\u5230\u5176\u4ed6\u53ef\u7528\u7684\u672c\u5730\u6444\u50cf\u5934\u3002")

    def activate_panel(self, panel: CameraPanel) -> None:
        if not panel.running:
            return
        self.active_panel = panel
        try:
            panel.panel.lift()
            panel.canvas.focus_set()
        except tk.TclError:
            pass

    def toggle_fullscreen(self) -> None:
        if self.preview_window is None:
            return
        self.preview_fullscreen = not self.preview_fullscreen
        try:
            self.preview_window.attributes("-fullscreen", self.preview_fullscreen)
        except tk.TclError:
            self.preview_fullscreen = False

    def exit_fullscreen_or_close_active(self, _event=None) -> None:
        if self.preview_fullscreen:
            self.toggle_fullscreen()
        else:
            self._close_active_panel()

    def add_camera(self, opened: OpenedCapture, position: int | None = None) -> bool:
        if opened.index in self.windows:
            opened.capture.release()
            return False
        if isinstance(opened.index, int) and opened.index >= 0:
            self.local_camera_indices.add(opened.index)
        if position is None:
            position = len(self.windows)
        self.ensure_preview_window()
        panel = CameraPanel(
            self,
            opened,
            position,
            self.panel_placements.get(opened.index),
        )
        self.windows[opened.index] = panel
        if opened.index not in self._windowed_panel_placements:
            self._windowed_panel_placements[opened.index] = panel.current_placement()
        self.activate_panel(panel)
        self._apply_capture_performance_profile()
        self.hide_launcher()
        self._show_preview_window()
        self._schedule_borderless_fit()
        self._refresh_lan_stream_panels()
        return True

    def _apply_capture_performance_profile(self) -> None:
        target_fps = self.fps
        if len(self.windows) > 1:
            target_fps = min(target_fps, MULTI_CAMERA_FPS)
        frame_interval = 1.0 / max(1, target_fps)
        for window in self.windows.values():
            window.frame_interval = frame_interval

    def _switch_local_captures_to_multi_profile(self) -> None:
        self.width = min(self.width, MULTI_CAMERA_WIDTH)
        self.height = min(self.height, MULTI_CAMERA_HEIGHT)
        self.fps = min(self.fps, MULTI_CAMERA_FPS)
        self.capture_fourcc = MULTI_CAMERA_FOURCC
        for panel in self.windows.values():
            if isinstance(panel.camera_index, int):
                configure_capture(
                    panel.capture,
                    self.width,
                    self.height,
                    self.fps,
                    self.capture_fourcc,
                )
        self._apply_capture_performance_profile()

    def remember_panel_placement(self, panel: CameraPanel) -> None:
        try:
            placement = panel.current_placement()
        except tk.TclError:
            return

        if self.borderless:
            placement = self._windowed_panel_placements.get(
                panel.camera_index, placement
            )
        else:
            self._windowed_panel_placements[panel.camera_index] = placement

        self.panel_placements[panel.camera_index] = placement
        try:
            save_panel_placement(panel.camera_index, placement)
        except OSError:
            pass

    def remember_main_window_placement(self) -> None:
        if self.preview_window is None or self.borderless:
            return
        try:
            self.preview_window.update_idletasks()
            placement = WindowPlacement(
                x=self.preview_window.winfo_x(),
                y=self.preview_window.winfo_y(),
                width=self.preview_window.winfo_width(),
                height=self.preview_window.winfo_height(),
            )
        except tk.TclError:
            return

        self.main_window_placement = placement
        try:
            save_main_window_placement(placement)
        except OSError:
            pass

    def show_launcher(self) -> None:
        if self.shutting_down:
            return
        if self.launcher_window is not None:
            try:
                if self.launcher_window.winfo_exists():
                    self.launcher_window.deiconify()
                    self.launcher_window.lift()
                    self.launcher_window.focus_force()
                    return
            except tk.TclError:
                pass
            self.launcher_window = None

        launcher = tk.Toplevel(self.root)
        self.launcher_window = launcher
        launcher.title(WINDOW_TITLE)
        launcher.resizable(False, False)
        launcher.protocol("WM_DELETE_WINDOW", self.close_all)

        container = ttk.Frame(launcher, padding=18)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(container, text=WINDOW_TITLE, font=("Segoe UI", 14)).pack(
            anchor=tk.W, pady=(0, 14)
        )
        self.launcher_scan_button = ttk.Button(
            container,
            text="\u626b\u63cf USB \u6444\u50cf\u5934",
            command=self.scan_for_cameras,
        )
        self.launcher_scan_button.pack(fill=tk.X)
        ttk.Button(
            container,
            text="\u67e5\u627e\u7f51\u7edc\u6444\u50cf\u5934",
            command=lambda: self.open_network_camera_dialog(launcher),
        ).pack(fill=tk.X, pady=(8, 0))
        ttk.Button(
            container,
            text="\u5c40\u57df\u7f51\u63a8\u6d41",
            command=lambda: self.open_lan_stream_dialog(launcher),
        ).pack(fill=tk.X, pady=(8, 0))
        ttk.Button(container, text="\u9000\u51fa", command=self.close_all).pack(
            fill=tk.X, pady=(8, 0)
        )
        self.launcher_status_var = tk.StringVar(
            value="\u70b9\u51fb\u201c\u626b\u63cf USB \u6444\u50cf\u5934\u201d\u5f00\u59cb\u3002"
        )
        ttk.Label(
            container,
            textvariable=self.launcher_status_var,
            justify=tk.LEFT,
            wraplength=324,
        ).pack(fill=tk.X, pady=(12, 0))
        launcher.geometry("360x278")
        launcher.deiconify()
        launcher.lift()
        launcher.focus_force()
        launcher.after_idle(lambda: self._center_window(launcher, self.root))

    def hide_launcher(self) -> None:
        if self.launcher_window is None:
            return
        try:
            if self.launcher_window.winfo_exists():
                self.launcher_window.withdraw()
        except tk.TclError:
            self.launcher_window = None

    def _set_launcher_status(self, message: str) -> None:
        if self.launcher_status_var is not None:
            self.launcher_status_var.set(message)
        if self.launcher_window is not None:
            try:
                if self.launcher_window.winfo_exists():
                    self.launcher_window.update_idletasks()
            except tk.TclError:
                self.launcher_window = None

    @staticmethod
    def _center_window(dialog, parent) -> None:
        try:
            dialog.update_idletasks()
            screen_width = dialog.winfo_screenwidth()
            screen_height = dialog.winfo_screenheight()
            left = max(0, (screen_width - dialog.winfo_width()) // 2)
            top = max(0, (screen_height - dialog.winfo_height()) // 2)
            if parent.winfo_viewable():
                left = max(0, parent.winfo_rootx() + (parent.winfo_width() - dialog.winfo_width()) // 2)
                top = max(0, parent.winfo_rooty() + (parent.winfo_height() - dialog.winfo_height()) // 2)
            dialog.geometry(f"+{left}+{top}")
            dialog.lift()
        except tk.TclError:
            pass

    def _start_hotkey_listener(self) -> None:
        if sys.platform != "win32":
            return
        self._hotkey_stop.clear()
        self._hotkey_ready = threading.Event()
        self._hotkey_thread_id = None
        self.hotkey_available = False
        self._hotkey_thread = threading.Thread(
            target=self._hotkey_loop,
            name="camera-preview-hotkey",
            daemon=True,
        )
        self._hotkey_thread.start()

    def _hotkey_loop(self) -> None:
        self._hotkey_thread_id = threading.get_native_id()
        user32 = ctypes.windll.user32
        hotkey = self.hotkey
        modifiers = hotkey.modifiers | MOD_NOREPEAT
        registered = bool(user32.RegisterHotKey(None, HOTKEY_ID, modifiers, hotkey.key))
        self.hotkey_available = registered
        self._hotkey_ready.set()
        if not registered:
            return

        message = wintypes.MSG()
        try:
            while not self._hotkey_stop.is_set():
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                if message.message == WM_HOTKEY:
                    self._hotkey_events.put(None)
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)
            self.hotkey_available = False

    def _poll_hotkey_events(self) -> None:
        self._hotkey_poll_after_id = None
        pressed = False
        while True:
            try:
                self._hotkey_events.get_nowait()
            except queue.Empty:
                break
            pressed = True
        if pressed:
            self.toggle_visibility()
        if not self.shutting_down:
            try:
                self._hotkey_poll_after_id = self.root.after(80, self._poll_hotkey_events)
            except tk.TclError:
                pass

    def _stop_hotkey_listener(self) -> None:
        self._hotkey_stop.set()
        if sys.platform == "win32" and self._hotkey_thread_id is not None:
            ctypes.windll.user32.PostThreadMessageW(self._hotkey_thread_id, WM_QUIT, 0, 0)
        if self._hotkey_thread and self._hotkey_thread.is_alive():
            self._hotkey_thread.join(timeout=1.0)
        self._hotkey_thread = None
        self._hotkey_thread_id = None
        self.hotkey_available = False

    def toggle_visibility(self) -> None:
        if not self.windows:
            return
        self.previews_hidden = not self.previews_hidden
        if self.previews_hidden:
            self._hide_preview_window()
        else:
            self._show_preview_window()

    def set_hotkey(self, hotkey: GlobalHotkey) -> bool:
        if not is_valid_hotkey(hotkey):
            return False
        if hotkey == self.hotkey:
            return self.hotkey_available

        previous_hotkey = self.hotkey
        self._stop_hotkey_listener()
        self.hotkey = hotkey
        self._start_hotkey_listener()
        self._hotkey_ready.wait(timeout=1.0)
        if self.hotkey_available:
            save_hotkey(hotkey)
            for window in self.windows.values():
                window.set_status(f"Hotkey: {hotkey.label}")
            return True

        self._stop_hotkey_listener()
        self.hotkey = previous_hotkey
        self._start_hotkey_listener()
        self._hotkey_ready.wait(timeout=1.0)
        return False

    def open_hotkey_dialog(self, parent) -> None:
        dialog = tk.Toplevel(parent)
        dialog.title("\u8bbe\u7f6e\u5168\u5c40\u5feb\u6377\u952e")
        dialog.transient(parent)
        dialog.resizable(False, False)
        dialog.grab_set()

        container = ttk.Frame(dialog, padding=16)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="\u9009\u62e9 Ctrl \u52a0\u4e00\u4e2a\u6309\u952e\uff0c\u6216\u76f4\u63a5\u6309\u4e0b\u65b0\u7ec4\u5408\u952e\uff0c\u7136\u540e\u70b9\u51fb\u5e94\u7528\u3002",
        ).pack(anchor=tk.W)

        candidate = [self.hotkey]
        shortcut_options = selectable_hotkeys()
        shortcut_options.setdefault(candidate[0].label, candidate[0])
        shortcut_text = tk.StringVar(value=candidate[0].label)
        selector = ttk.Combobox(
            container,
            textvariable=shortcut_text,
            values=list(shortcut_options),
            state="readonly",
            width=28,
        )
        selector.pack(fill=tk.X, pady=(10, 12))

        def select_shortcut(_event=None) -> None:
            selected = shortcut_options.get(shortcut_text.get())
            if selected is not None:
                candidate[0] = selected

        def capture_shortcut(event) -> str:
            hotkey = hotkey_from_event(event)
            if hotkey is not None:
                candidate[0] = hotkey
                shortcut_text.set(hotkey.label)
            return "break"

        def use_default() -> None:
            candidate[0] = DEFAULT_HOTKEY
            shortcut_text.set(DEFAULT_HOTKEY.label)

        def close_dialog() -> None:
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass

        def apply_hotkey() -> None:
            try:
                changed = self.set_hotkey(candidate[0])
            except OSError as error:
                messagebox.showerror(WINDOW_TITLE, str(error), parent=dialog)
                return
            if changed:
                close_dialog()
                return
            messagebox.showerror(
                WINDOW_TITLE,
                f"{candidate[0].label} is unavailable. Choose another shortcut.",
                parent=dialog,
            )

        buttons = ttk.Frame(container)
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="\u5e94\u7528", command=apply_hotkey).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="\u53d6\u6d88", command=close_dialog).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(buttons, text="\u6062\u590d\u9ed8\u8ba4", command=use_default).pack(side=tk.LEFT)

        selector.bind("<<ComboboxSelected>>", select_shortcut)
        selector.bind("<KeyPress>", capture_shortcut)
        dialog.bind("<KeyPress>", capture_shortcut)
        dialog.protocol("WM_DELETE_WINDOW", close_dialog)

        def position_dialog() -> None:
            try:
                dialog.update_idletasks()
                left = parent.winfo_rootx() + (parent.winfo_width() - dialog.winfo_width()) // 2
                top = parent.winfo_rooty() + (parent.winfo_height() - dialog.winfo_height()) // 2
                dialog.geometry(f"+{max(0, left)}+{max(0, top)}")
                dialog.lift()
                dialog.focus_force()
            except tk.TclError:
                pass

        dialog.after_idle(position_dialog)

    def open_network_camera_dialog(self, parent) -> None:
        if self.shutting_down:
            return

        dialog = tk.Toplevel(parent)
        dialog.title("\u7f51\u7edc\u6444\u50cf\u5934")
        dialog.transient(parent)
        dialog.geometry("780x500")
        dialog.minsize(680, 430)
        dialog.grab_set()

        container = ttk.Frame(dialog, padding=14)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(container, text="ONVIF \u8bbe\u5907").pack(anchor=tk.W)

        devices_frame = ttk.Frame(container)
        devices_frame.pack(fill=tk.BOTH, expand=True, pady=(6, 10))
        tree = ttk.Treeview(
            devices_frame,
            columns=("name", "host", "endpoint"),
            show="headings",
            height=9,
        )
        tree.heading("name", text="\u8bbe\u5907")
        tree.heading("host", text="IP \u5730\u5740")
        tree.heading("endpoint", text="ONVIF \u5730\u5740")
        tree.column("name", width=190, minwidth=140, stretch=False)
        tree.column("host", width=130, minwidth=100, stretch=False)
        tree.column("endpoint", width=390, minwidth=260, stretch=True)
        scrollbar = ttk.Scrollbar(devices_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        credentials = ttk.Frame(container)
        credentials.pack(fill=tk.X)
        ttk.Label(credentials, text="\u7528\u6237\u540d").grid(row=0, column=0, sticky=tk.W)
        username_var = tk.StringVar()
        ttk.Entry(credentials, textvariable=username_var, width=20).grid(
            row=0, column=1, sticky=tk.EW, padx=(6, 14)
        )
        ttk.Label(credentials, text="\u5bc6\u7801").grid(row=0, column=2, sticky=tk.W)
        password_var = tk.StringVar()
        ttk.Entry(credentials, textvariable=password_var, width=20, show="*").grid(
            row=0, column=3, sticky=tk.EW, padx=(6, 0)
        )
        credentials.columnconfigure(1, weight=1)
        credentials.columnconfigure(3, weight=1)

        stream_row = ttk.Frame(container)
        stream_row.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(stream_row, text="RTSP \u5730\u5740").pack(side=tk.LEFT)
        stream_url_var = tk.StringVar()
        stream_selector = ttk.Combobox(
            stream_row,
            textvariable=stream_url_var,
            state="normal",
        )
        stream_selector.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))

        status_var = tk.StringVar(value="\u70b9\u51fb\u641c\u7d22\u53d1\u73b0\u5c40\u57df\u7f51 ONVIF \u6444\u50cf\u5934\u3002")
        ttk.Label(container, textvariable=status_var).pack(anchor=tk.W, pady=(8, 0))

        buttons = ttk.Frame(container)
        buttons.pack(fill=tk.X, pady=(10, 0))
        search_button = ttk.Button(buttons, text="\u641c\u7d22", command=lambda: None)
        search_button.pack(side=tk.LEFT)
        resolve_button = ttk.Button(buttons, text="\u8bfb\u53d6 RTSP", command=lambda: None)
        resolve_button.pack(side=tk.LEFT, padx=(6, 0))
        connect_button = ttk.Button(buttons, text="\u6dfb\u52a0\u9884\u89c8", command=lambda: None)
        connect_button.pack(side=tk.RIGHT)
        cancel_button = ttk.Button(buttons, text="\u5173\u95ed", command=lambda: None)
        cancel_button.pack(side=tk.RIGHT, padx=(0, 6))

        discovered_by_item: dict[str, DiscoveredNetworkCamera] = {}
        event_queue: queue.SimpleQueue = queue.SimpleQueue()
        active_operation: list[str | None] = [None]
        dialog_open = [True]
        poll_after_id: list[str | None] = [None]
        state_lock = threading.Lock()

        def selected_camera() -> DiscoveredNetworkCamera | None:
            selection = tree.selection()
            if not selection:
                return None
            return discovered_by_item.get(selection[0])

        def set_busy(busy: bool) -> None:
            state = tk.DISABLED if busy else tk.NORMAL
            for button in (search_button, resolve_button, connect_button):
                button.configure(state=state)

        def release_connect_result(result: object) -> None:
            if isinstance(result, OpenedCapture):
                result.capture.release()

        def cleanup_dialog() -> None:
            releases: list[object] = []
            with state_lock:
                if not dialog_open[0]:
                    return
                dialog_open[0] = False
                while True:
                    try:
                        operation, succeeded, result = event_queue.get_nowait()
                    except queue.Empty:
                        break
                    if operation == "connect" and succeeded:
                        releases.append(result)
            self._network_dialog_cleanups.discard(cleanup_dialog)
            if poll_after_id[0] is not None:
                try:
                    dialog.after_cancel(poll_after_id[0])
                except tk.TclError:
                    pass
                poll_after_id[0] = None
            for result in releases:
                release_connect_result(result)

        def close_dialog() -> None:
            cleanup_dialog()
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass

        def on_dialog_destroy(event) -> None:
            if event.widget is dialog:
                cleanup_dialog()

        def start_operation(operation: str, status: str, action) -> None:
            if active_operation[0] is not None:
                return
            active_operation[0] = operation
            set_busy(True)
            status_var.set(status)

            def worker() -> None:
                try:
                    result = action()
                except Exception as error:
                    with state_lock:
                        if dialog_open[0]:
                            event_queue.put((operation, False, str(error)))
                    return
                should_release = False
                with state_lock:
                    if dialog_open[0]:
                        event_queue.put((operation, True, result))
                    elif operation == "connect":
                        should_release = True
                if should_release:
                    release_connect_result(result)

            threading.Thread(
                target=worker,
                name=f"network-camera-{operation}",
                daemon=True,
            ).start()

        def search_network() -> None:
            start_operation(
                "discover",
                "\u6b63\u5728\u641c\u7d22\u5c40\u57df\u7f51 ONVIF \u6444\u50cf\u5934...",
                discover_network_cameras,
            )

        def resolve_stream() -> None:
            camera = selected_camera()
            if camera is None:
                status_var.set("\u8bf7\u5148\u5728\u5217\u8868\u4e2d\u9009\u62e9\u4e00\u53f0\u8bbe\u5907\u3002")
                return
            username = username_var.get().strip()
            password = password_var.get()
            start_operation(
                "resolve",
                "\u6b63\u5728\u8bfb\u53d6 ONVIF \u89c6\u9891\u6d41\u5730\u5740...",
                lambda: resolve_onvif_stream_urls(camera, username, password),
            )

        def connect_stream() -> None:
            try:
                stream_url = normalize_network_stream_url(stream_url_var.get())
            except ValueError as error:
                status_var.set(str(error))
                return
            start_operation(
                "connect",
                "\u6b63\u5728\u8fde\u63a5\u7f51\u7edc\u6444\u50cf\u5934...",
                lambda: open_network_capture(stream_url),
            )

        def handle_events() -> None:
            poll_after_id[0] = None
            try:
                while True:
                    operation, succeeded, result = event_queue.get_nowait()
                    active_operation[0] = None
                    set_busy(False)
                    if not succeeded:
                        status_var.set(str(result))
                        continue
                    if operation == "discover":
                        cameras = result
                        existing_items = tree.get_children()
                        if existing_items:
                            tree.delete(*existing_items)
                        discovered_by_item.clear()
                        for index, camera in enumerate(cameras):
                            item_id = f"camera-{index}"
                            discovered_by_item[item_id] = camera
                            tree.insert(
                                "",
                                tk.END,
                                iid=item_id,
                                values=(camera.display_name, camera.host, camera.endpoint),
                            )
                        if cameras:
                            status_var.set(f"\u627e\u5230 {len(cameras)} \u53f0 ONVIF \u8bbe\u5907\u3002")
                        else:
                            status_var.set("\u672a\u627e\u5230 ONVIF \u8bbe\u5907\u3002\u8bf7\u786e\u8ba4\u8bbe\u5907\u4e0e\u7535\u8111\u5904\u4e8e\u540c\u4e00\u5c40\u57df\u7f51\u3002")
                    elif operation == "resolve":
                        stream_urls = result
                        stream_selector.configure(values=stream_urls)
                        stream_url_var.set(stream_urls[0])
                        status_var.set(f"\u5df2\u8bfb\u53d6 {len(stream_urls)} \u4e2a RTSP \u89c6\u9891\u6d41\u3002")
                    elif operation == "connect":
                        opened = result
                        if self.add_camera(opened):
                            status_var.set("\u5df2\u6dfb\u52a0\u7f51\u7edc\u6444\u50cf\u5934\u3002")
                            close_dialog()
                        else:
                            status_var.set("\u8fd9\u4e2a\u7f51\u7edc\u6444\u50cf\u5934\u5df2\u7ecf\u6253\u5f00\u3002")
            except queue.Empty:
                pass
            try:
                if dialog_open[0] and dialog.winfo_exists():
                    poll_after_id[0] = dialog.after(80, handle_events)
            except tk.TclError:
                pass

        search_button.configure(command=search_network)
        resolve_button.configure(command=resolve_stream)
        connect_button.configure(command=connect_stream)
        cancel_button.configure(command=close_dialog)
        self._network_dialog_cleanups.add(cleanup_dialog)
        dialog.bind("<Destroy>", on_dialog_destroy)
        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        poll_after_id[0] = dialog.after(80, handle_events)
        dialog.after_idle(lambda: self._center_window(dialog, parent))

    def scan_for_cameras(self) -> None:
        if self.shutting_down or self._local_scan_running:
            return
        known_indices = set(self.windows)
        local_camera_count = sum(
            isinstance(camera_index, int) for camera_index in known_indices
        )
        self._local_scan_running = True
        if self.launcher_scan_button is not None:
            self.launcher_scan_button.configure(state=tk.DISABLED)
        self._set_launcher_status(
            "\u6b63\u5728\u626b\u63cf USB \u6444\u50cf\u5934\uff0c\u8bf7\u7a0d\u5019\u2026"
        )

        def worker() -> None:
            opened_captures: list[OpenedCapture] = []
            scan_local_camera_count = local_camera_count
            try:
                for index in range(self.max_index + 1):
                    if index in known_indices:
                        continue
                    use_multi_profile = scan_local_camera_count >= 1
                    width = (
                        min(self.width, MULTI_CAMERA_WIDTH)
                        if use_multi_profile
                        else self.width
                    )
                    height = (
                        min(self.height, MULTI_CAMERA_HEIGHT)
                        if use_multi_profile
                        else self.height
                    )
                    fps = (
                        min(self.fps, MULTI_CAMERA_FPS)
                        if use_multi_profile
                        else self.fps
                    )
                    fourcc = (
                        MULTI_CAMERA_FOURCC
                        if use_multi_profile
                        else self.capture_fourcc
                    )
                    try:
                        opened = open_capture(index, width, height, fps, fourcc)
                    except RuntimeError:
                        continue
                    opened_captures.append(opened)
                    scan_local_camera_count += 1
            except Exception as error:
                for opened in opened_captures:
                    opened.capture.release()
                self._local_scan_events.put(([], str(error)))
            else:
                self._local_scan_events.put((opened_captures, None))

        threading.Thread(
            target=worker,
            name="camera-preview-local-scan",
            daemon=True,
        ).start()
        self._local_scan_poll_after_id = self.root.after(80, self._poll_local_scan_events)

    def _poll_local_scan_events(self) -> None:
        self._local_scan_poll_after_id = None
        try:
            opened_captures, error = self._local_scan_events.get_nowait()
        except queue.Empty:
            if self._local_scan_running and not self.shutting_down:
                self._local_scan_poll_after_id = self.root.after(
                    80, self._poll_local_scan_events
                )
            return

        added = 0
        local_camera_count = sum(
            isinstance(camera_index, int) for camera_index in self.windows
        )
        for opened in opened_captures:
            if self.shutting_down:
                opened.capture.release()
                continue
            if not self.add_camera(opened):
                continue
            local_camera_count += 1
            if local_camera_count > 1:
                self._switch_local_captures_to_multi_profile()
            added += 1

        self._local_scan_running = False
        if self.launcher_scan_button is not None:
            try:
                self.launcher_scan_button.configure(state=tk.NORMAL)
            except tk.TclError:
                self.launcher_scan_button = None

        if error:
            message = f"\u626b\u63cf\u5931\u8d25\uff1a{error}"
        elif added:
            message = f"\u5df2\u627e\u5230\u5e76\u6253\u5f00 {added} \u53f0 USB \u6444\u50cf\u5934\u3002"
        else:
            message = (
                "\u672a\u627e\u5230\u53ef\u7528\u7684 USB \u6444\u50cf\u5934\u3002"
                "\u8bf7\u68c0\u67e5\u8fde\u63a5\uff0c\u5e76\u5173\u95ed\u5360\u7528\u6444\u50cf\u5934\u7684\u5176\u4ed6\u7a0b\u5e8f\u3002"
            )
        self._set_launcher_status(message)
        for window in self.windows.values():
            window.set_status(message)

    def camera_closed(self, panel: CameraPanel) -> None:
        if self.windows.get(panel.camera_index) is panel:
            del self.windows[panel.camera_index]
        self._refresh_lan_stream_panels()
        self._windowed_panel_placements.pop(panel.camera_index, None)
        if self.active_panel is panel:
            self.active_panel = next(iter(self.windows.values()), None)
        self._apply_capture_performance_profile()
        if self.windows and self.borderless:
            self._schedule_borderless_fit()
        if not self.windows and not self.shutting_down:
            self._hide_preview_window()
            if self._show_launcher_when_empty:
                self.show_launcher()
            else:
                self._close_after_id = self.root.after_idle(self.close_all)

    def close_all(self) -> None:
        if self.shutting_down:
            return
        self.shutting_down = True
        self.remember_main_window_placement()
        for callback_id in (
            self._hotkey_poll_after_id,
            self._close_after_id,
            self._local_scan_poll_after_id,
            self._borderless_fit_after_id,
        ):
            if callback_id is None:
                continue
            try:
                self.root.after_cancel(callback_id)
            except tk.TclError:
                pass
        self._hotkey_poll_after_id = None
        self._close_after_id = None
        self._local_scan_poll_after_id = None
        self._local_scan_running = False
        self._borderless_fit_after_id = None
        self._stop_hotkey_listener()
        self.stop_lan_stream()
        for cleanup_dialog in tuple(self._network_dialog_cleanups):
            cleanup_dialog()
        if self.launcher_window is not None:
            try:
                self.launcher_window.destroy()
            except tk.TclError:
                pass
            self.launcher_window = None
        for window in list(self.windows.values()):
            window.close(notify_manager=False)
        self.windows.clear()
        self.active_panel = None
        if self.preview_window is not None:
            try:
                self.preview_window.destroy()
            except tk.TclError:
                pass
            self.preview_window = None
            self.workspace = None
        try:
            self.root.quit()
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        self.root.mainloop()


def show_previews(
    captures: list[OpenedCapture],
    width: int,
    height: int,
    fps: int,
    max_index: int,
    capture_fourcc: str = DEFAULT_CAPTURE_FOURCC,
    available_local_indices: Iterable[int] | None = None,
) -> int:
    manager = CameraManager(
        captures,
        width,
        height,
        fps,
        max_index,
        capture_fourcc,
        available_local_indices,
    )
    manager.run()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Display one or more live camera previews.")
    parser.add_argument(
        "--camera",
        type=int,
        action="append",
        help="Camera index to open. Repeat the option to select multiple cameras.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Skip startup selection and open every usable camera up to --max-index.",
    )
    parser.add_argument("--width", type=int, default=1280, help="Requested frame width.")
    parser.add_argument("--height", type=int, default=720, help="Requested frame height.")
    parser.add_argument("--fps", type=int, default=30, help="Requested frame rate.")
    parser.add_argument("--list", action="store_true", help="List working camera indices and exit.")
    parser.add_argument("--max-index", type=int, default=8, help="Highest camera index scanned.")
    parser.add_argument("--self-test", action="store_true", help="Open selected cameras and exit.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if min(args.width, args.height, args.fps) <= 0:
        raise ValueError("Width, height, and fps must be positive.")
    if args.max_index < 0:
        raise ValueError("max-index must be zero or greater.")
    if args.list:
        return list_cameras(args.max_index, args.width, args.height, args.fps)

    if not args.self_test and not acquire_single_instance():
        hotkey = load_hotkey()
        raise RuntimeError(
            f"{WINDOW_TITLE}\u5df2\u7ecf\u5728\u8fd0\u884c\u3002"
            f"\u6309 {hotkey.label} \u663e\u793a\u6216\u9690\u85cf\u7a97\u53e3\u3002"
        )

    scan_all = args.all or not args.camera
    auto_select = not args.camera and not args.all and not args.self_test
    indices: Iterable[int] = range(args.max_index + 1) if scan_all else args.camera
    capture_width = args.width
    capture_height = args.height
    capture_fps = args.fps
    capture_fourcc = DEFAULT_CAPTURE_FOURCC
    captures = open_captures(
        indices,
        capture_width,
        capture_height,
        capture_fps,
        capture_fourcc,
    )
    available_local_indices = [
        opened.index
        for opened in captures
        if isinstance(opened.index, int) and opened.index >= 0
    ]
    try:
        if not captures:
            if not scan_all:
                requested = ", ".join(str(index) for index in args.camera)
                raise RuntimeError(f"Cannot open requested camera(s): {requested}")
            if args.self_test:
                raise RuntimeError("No usable camera was found.")
            return show_previews(
                [],
                capture_width,
                capture_height,
                capture_fps,
                args.max_index,
                capture_fourcc,
                available_local_indices,
            )

        if auto_select and len(captures) > 1:
            selected = select_local_captures(captures)
            if selected is None:
                captures = []
                return 0
            captures = selected
            indices = [int(opened.index) for opened in captures]

        if len(captures) > 1:
            expected_count = len(captures)
            for opened in captures:
                opened.capture.release()
            time.sleep(0.2)
            capture_width = min(args.width, MULTI_CAMERA_WIDTH)
            capture_height = min(args.height, MULTI_CAMERA_HEIGHT)
            capture_fps = min(args.fps, MULTI_CAMERA_FPS)
            capture_fourcc = MULTI_CAMERA_FOURCC
            captures = open_captures(
                indices,
                capture_width,
                capture_height,
                capture_fps,
                capture_fourcc,
            )
            if len(captures) < expected_count:
                for opened in captures:
                    opened.capture.release()
                time.sleep(0.2)
                capture_width = args.width
                capture_height = args.height
                capture_fps = args.fps
                capture_fourcc = DEFAULT_CAPTURE_FOURCC
                captures = open_captures(
                    indices,
                    capture_width,
                    capture_height,
                    capture_fps,
                    capture_fourcc,
                )

        for opened in captures:
            actual_width = int(opened.capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_height = int(opened.capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            actual_fps = opened.capture.get(cv2.CAP_PROP_FPS)
            print(
                f"Camera {opened.index}: {actual_width}x{actual_height} "
                f"@ {actual_fps:.1f} FPS {capture_fourcc} via {opened.backend}"
            )
        if args.self_test:
            print("Camera self-test passed.")
            return 0
        return show_previews(
            captures,
            capture_width,
            capture_height,
            capture_fps,
            args.max_index,
            capture_fourcc,
            available_local_indices,
        )
    finally:
        for opened in captures:
            opened.capture.release()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        message = str(error)
        print(message, file=sys.stderr)
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, message, WINDOW_TITLE, 0x10)
        raise SystemExit(1) from error
