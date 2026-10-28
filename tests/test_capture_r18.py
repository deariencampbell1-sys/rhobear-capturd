"""Apollo r18 (546687f5): owner-scoped context refusals and redirect-safe discovery fetch."""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from capturd.shots import capture
from capturd.shots.capture import RestedCaptureError, build_capture_plan


def _target(url):
    _, targets, settings = build_capture_plan(
        {"urls": [url], "retries": 0, "viewports": ["desktop"], "schemes": ["light"]},
    )
    return targets[0], settings


class _Page:
    def __init__(self, url, opener=None):
        self.url = url
        self._opener = opener
        self.route = AsyncMock()
        self.route_web_socket = AsyncMock()
        self.close = AsyncMock()
        self.screenshot = AsyncMock()

    def set_default_timeout(self, _ms):
        pass

    async def opener(self):
        return self._opener


@pytest.mark.asyncio
async def test_r18_concurrent_targets_keep_their_own_context_refusals(tmp_path, monkeypatch):
    """A's popup refusal lands while B is mid-attempt: each target reports only its own cause."""
    async def check(url, **_kwargs):  # public hosts pass without DNS; metadata is refused
        if "169.254.169.254" in url:
            raise RestedCaptureError("refusing private/internal peer")

    monkeypatch.setattr(capture, "_check_navigation_url", check)
    context = SimpleNamespace(route=AsyncMock(), route_web_socket=AsyncMock())
    owner_errors: dict = {}
    await capture._install_navigation_guard(
        context, allow_loopback=False, loopback_host=None, owner_errors=owner_errors,
    )
    context_guard = context.route.call_args.args[1]

    page_a, page_b = _Page("https://a.example/"), _Page("https://b.example/")
    pages = iter([page_a, page_b])
    context.new_page = AsyncMock(side_effect=lambda: next(pages))
    b_navigating, a_refused = asyncio.Event(), asyncio.Event()

    def popup_route(opener):
        popup = _Page("about:blank", opener=opener)
        return SimpleNamespace(
            request=SimpleNamespace(url="http://169.254.169.254/latest", resource_type="document",
                                    frame=SimpleNamespace(page=popup)),
            abort=AsyncMock(), continue_=AsyncMock(),
        )

    async def goto_a(*_a, **_k):
        await b_navigating.wait()
        await context_guard(popup_route(page_a))
        a_refused.set()
        raise TimeoutError("A navigation aborted")

    async def goto_b(*_a, **_k):
        b_navigating.set()
        await a_refused.wait()
        raise RuntimeError("B real navigation failure")

    page_a.goto = AsyncMock(side_effect=goto_a)
    page_b.goto = AsyncMock(side_effect=goto_b)

    target_a, settings = _target("https://a.example/")
    target_b, _ = _target("https://b.example/")
    result_a, result_b = await asyncio.gather(
        capture._capture_one(context, target_a, settings, tmp_path / "a.png", owner_errors=owner_errors),
        capture._capture_one(context, target_b, settings, tmp_path / "b.png", owner_errors=owner_errors),
    )
    assert result_a["status"] == result_b["status"] == "failed"
    assert "private/internal" in result_a["error"]
    assert result_b["error"] == "B real navigation failure"
    assert owner_errors == {}


@pytest.mark.asyncio
async def test_r18_unowned_refusal_is_not_pinned_on_a_concurrent_target(monkeypatch):
    context = SimpleNamespace(route=AsyncMock(), route_web_socket=AsyncMock())
    a_errors, b_errors = [], []
    owner_errors = {object(): a_errors, object(): b_errors}
    await capture._install_navigation_guard(
        context, allow_loopback=False, loopback_host=None, owner_errors=owner_errors,
    )
    route = SimpleNamespace(
        request=SimpleNamespace(url="http://10.0.0.5/", resource_type="document"),
        abort=AsyncMock(), continue_=AsyncMock(),
    )
    await context.route.call_args.args[1](route)
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()
    assert a_errors == b_errors == []


def test_r18_discovery_fetch_refuses_redirect_to_metadata(monkeypatch):
    """A public sitemap that 302s to cloud metadata must be refused at the hop."""

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server API
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    start = f"http://127.0.0.1:{server.server_port}/sitemap.xml"
    real_reject = capture._reject_private_url
    checked = []

    def reject(url, **kwargs):
        checked.append(url)
        if url == start:  # stand-in for a public attacker host
            return None
        return real_reject(url, **kwargs)

    monkeypatch.setattr(capture, "_reject_private_url", reject)
    try:
        with pytest.raises(RestedCaptureError, match="private/internal"):
            capture.expand_sitemap(start)
    finally:
        server.shutdown()
        server.server_close()
    assert checked == [start, "http://169.254.169.254/latest/meta-data/"]
