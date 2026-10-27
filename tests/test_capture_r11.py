"""r11 (L0 frontier round, capturd#49): WebSocket egress, capability type guard,
secret-position redaction. The WebSocket test drives a real Chromium through the
real guard and proves, with a server that records handshakes, that a refused
socket never reaches its server while a permitted one connects and delivers."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from capturd.shots import capture
from capturd.shots.capture import RestedCaptureError, build_capture_plan

_WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _RecordingWebSocketServer:
    """A minimal RFC 6455 server that records every handshake it sees and
    answers each one with a single text frame, "hello"."""

    def __init__(self, host: str) -> None:
        self.handshakes: list[str] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, 0))
        self._sock.listen(8)
        self.host, self.port = self._sock.getsockname()
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
            self.handshakes.append(head.split("\r\n", 1)[0])
            key = next(line.split(":", 1)[1].strip() for line in head.split("\r\n")
                       if line.lower().startswith("sec-websocket-key:"))
            accept = base64.b64encode(hashlib.sha1(key.encode() + _WS_MAGIC).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          "Connection: Upgrade\r\nSec-WebSocket-Accept: "
                          f"{accept}\r\n\r\n").encode())
            conn.sendall(b"\x81\x05hello")
            try:
                conn.recv(1024)
            except OSError:
                pass

    def close(self) -> None:
        self._stop = True
        self._sock.close()


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — http.server API
        body = b"<!doctype html><title>ws</title>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


_OPEN_SOCKET_JS = """(url) => new Promise((resolve) => {
  const ws = new WebSocket(url);
  ws.onmessage = (e) => { resolve({message: e.data}); ws.close(); };
  ws.onclose = (e) => resolve({closed: e.code});
  setTimeout(() => resolve({timeout: true}), 5000);
})"""


def test_r11_websocket_guard_blocks_private_host_and_connects_permitted_host():
    async_api = pytest.importorskip("playwright.async_api")
    page_server = ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=page_server.serve_forever, daemon=True).start()
    allowed = _RecordingWebSocketServer("127.0.0.1")   # the exact loopback opt-in
    refused = _RecordingWebSocketServer("127.0.0.2")   # another loopback address

    async def run():
        async with async_api.async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                context = await browser.new_context()
                await capture._install_navigation_guard(
                    context, allow_loopback=True, loopback_host="127.0.0.1")
                page = await context.new_page()
                await page.goto(f"http://127.0.0.1:{page_server.server_address[1]}/")
                blocked = await page.evaluate(
                    _OPEN_SOCKET_JS, f"ws://127.0.0.2:{refused.port}/socket")
                permitted = await page.evaluate(
                    _OPEN_SOCKET_JS, f"ws://127.0.0.1:{allowed.port}/socket")
                return blocked, permitted
            finally:
                await browser.close()

    try:
        with pytest.warns(RuntimeWarning, match="capture websocket refused"):
            blocked, permitted = asyncio.run(run())
    finally:
        page_server.shutdown()
        allowed.close()
        refused.close()
    # the refused server never saw a handshake: the guard closed the socket
    # before any connection, with the policy-violation code
    assert refused.handshakes == []
    assert blocked == {"closed": capture.WS_REFUSED_CLOSE_CODE}
    # the guard does not just break WebSockets: the permitted host handshakes
    # and its message reaches the page
    assert len(allowed.handshakes) == 1
    assert allowed.handshakes[0].startswith("GET /socket ")
    assert permitted == {"message": "hello"}


def test_r11_guard_refuses_a_context_without_websocket_interception():
    # an older Playwright cannot intercept WebSockets: capture refuses rather
    # than run with a scheme list that claims coverage it does not have
    context = SimpleNamespace(route=AsyncMock())
    with pytest.raises(RestedCaptureError, match="WebSocket guard unavailable"):
        asyncio.run(capture._install_navigation_guard(
            context, allow_loopback=False, loopback_host=None))
    context.route.assert_not_awaited()


@pytest.mark.parametrize("value", ["172.17.0.1", b"172.17.0.1", 172, None, ["10.0.0.5", 7], [""]])
def test_r11_allow_private_hosts_must_be_a_list_of_host_names(value):
    with pytest.raises(RestedCaptureError, match="^allow_private_hosts "):
        build_capture_plan({"urls": ["https://example.com"]}, allow_private_hosts=value)


def test_r11_allow_private_hosts_list_still_canonicalizes():
    _, _, settings = build_capture_plan(
        {"urls": ["https://example.com"]}, allow_private_hosts=["172.17.0.1"])
    assert settings["allow_private_hosts"] == ("172.17.0.1",)


def test_r11_redaction_keeps_the_refusal_target_and_drops_only_the_cookie_value():
    settings = {"cookies": [{"name": "session", "value": "s3cr3t-cookie-value",
                             "domain": "10.0.0.5", "path": "/"}],
                "format": "png"}
    with pytest.raises(RestedCaptureError) as refused:
        capture._reject_navigation_url(
            "http://10.0.0.5/admin", allow_loopback=False, loopback_host=None)
    canonical = str(refused.value)
    assert "10.0.0.5" in canonical
    outward = capture._redact_settings_secrets(
        f"{canonical}; sent s3cr3t-cookie-value", settings)
    # the IP, the slashes and the word "session" survive; the secret does not
    assert outward == f"{canonical}; sent <redacted>"


def test_r11_short_secret_values_are_replaced_only_as_whole_tokens():
    settings = {"cookies": [{"name": "consent", "value": "on", "path": "/"}],
                "headers": {"X-Api-Key": "k9"}}
    outward = capture._redact_settings_secrets(
        "online capture on host k9 failed; k99 is fine", settings)
    assert outward == "online capture <redacted> host <redacted> failed; k99 is fine"


def test_r11_other_secret_keys_are_redacted_by_every_leaf():
    settings = {"auth": {"username": "operator", "password": "hunter2-long"},
                "authorization": "Bearer abc.def.ghi"}
    outward = capture._redact_settings_secrets(
        "operator hunter2-long Bearer abc.def.ghi", settings)
    assert outward == "<redacted> <redacted> <redacted>"
