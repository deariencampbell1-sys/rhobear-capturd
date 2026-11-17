"""GAPS-NOW M5 (JOURNEY E7, contract §4): record one shot again.

Recapture reloads the step's page in a headless browser, finds the recorded
target, repeats the click and screenshots the result: a new take with a new
`sourceRevision`, every other take untouched, selection unchanged until a
person approves. A target that no longer exists fails visibly and mints no
take; a recapture of identical pixels is reported, not faked as new.
"""
from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import http.server
import json
import threading
from pathlib import Path

import pytest

import capturd.mcp.server as server_mod
from capturd.engine import take_store
from capturd.engine.take_store import TakeStore, TakeStoreError
from capturd.walk.coordinator import DemoForge
from capturd.walk.recapture import RecaptureError, recapture_step

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def page_url():
    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Quiet, directory=str(FIXTURES)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/signup-flow.html"
    httpd.shutdown()


def _step(index, url, selector, png=b"old-pixels"):
    return {"index": index, "timestamp": 0, "pageUrl": url, "pageTitle": "t",
            "interaction": {"type": "click",
                            "target": {"selector": selector, "tagName": "BUTTON", "text": "",
                                       "boundingRect": {"x": 0, "y": 0, "width": 9, "height": 9}},
                            "hotspot": {"xPct": 50, "yPct": 50}},
            "annotation": f"Step {index}.",
            "screenshotBase64": base64.b64encode(png).decode(), "screenshotPath": None}


def _forge(tmp_path, url, selectors=("#signup-btn", "#confirm-btn")):
    forge = DemoForge(demos_dir=tmp_path / "demos")
    forge.save_spec("demo1", {
        "version": 1, "id": "demo1", "name": "demo1", "goal": "g",
        "createdAt": "2026-11-17T00:00:00Z", "viewport": {"width": 640, "height": 480},
        "startUrl": url,
        "aiAnnotations": {"style": "snappy", "summary": "", "animationTimeline": []},
        "steps": [_step(i, url, sel, png=f"old-{i}".encode()) for i, sel in enumerate(selectors)]})
    return forge


def test_recapture_step_returns_fresh_pixels(page_url):
    png = asyncio.run(recapture_step(_step(0, page_url, "#signup-btn"),
                                     {"width": 640, "height": 480}))
    assert png.startswith(b"\x89PNG")


def test_a_missing_target_is_a_named_failure(page_url):
    with pytest.raises(RecaptureError, match="#gone"):
        asyncio.run(recapture_step(_step(0, page_url, "#gone"), {"width": 640, "height": 480}))


def test_recapture_makes_a_new_take_and_touches_nothing_else(tmp_path, page_url):
    forge = _forge(tmp_path, page_url)
    store = TakeStore(forge)
    takes = store.open_review("demo1")["takes"]
    path = forge.demo_dir("demo1") / take_store.FILENAME
    before = json.loads(path.read_text())["takes"]
    png = asyncio.run(recapture_step(forge.load_spec("demo1")["steps"][0],
                                     {"width": 640, "height": 480}))
    new = store.recapture("demo1", 0, 0, pixels=png,
                          expected_revision=takes[0]["sourceRevision"])
    assert (new["shotIndex"], new["takeIndex"], new["state"]) == (0, 1, "ready")
    assert new["sourceRevision"] == hashlib.sha1(png).hexdigest() != takes[0]["sourceRevision"]
    assert (forge.demo_dir("demo1") / f"source_{new['sourceRevision']}.png").read_bytes() == png
    assert json.loads(path.read_text())["takes"][:2] == before
    assert store.review("demo1")["selection"][0] == 0


def test_identical_pixels_are_not_a_new_take(tmp_path, page_url):
    forge = _forge(tmp_path, page_url)
    store = TakeStore(forge)
    takes = store.open_review("demo1")["takes"]
    with pytest.raises(TakeStoreError, match="same pixels"):
        store.recapture("demo1", 0, 0, pixels=b"old-0",
                        expected_revision=takes[0]["sourceRevision"])
    assert len(store.review("demo1")["takes"]) == 2


def test_an_approved_recapture_is_what_the_export_shows(tmp_path, page_url):
    forge = _forge(tmp_path, page_url)
    store = TakeStore(forge)
    takes = store.open_review("demo1")["takes"]
    new = store.recapture("demo1", 0, 0, pixels=b"\x89PNG fresh",
                          expected_revision=takes[0]["sourceRevision"])
    store.approve("demo1", 0, 1, by="Neo", expected_revision=new["sourceRevision"])
    store.approve("demo1", 1, 0, by="Neo", expected_revision=takes[1]["sourceRevision"])
    html = forge.export_demo("demo1", fmt="html").read_text()
    assert base64.b64encode(b"\x89PNG fresh").decode() in html
    assert base64.b64encode(b"old-0").decode() not in html


def _call(server, name, args):
    result = asyncio.run(server.call_tool(name, args))
    return result.structured_content.get("result", result.structured_content)


def test_the_recapture_tool(tmp_path, page_url, monkeypatch):
    monkeypatch.setattr(server_mod, "_reject_private_url", lambda url: None)  # test-only fixture
    forge = _forge(tmp_path, page_url, selectors=("#signup-btn", "#gone"))
    server = server_mod._build_server(forge)
    takes = _call(server, "take.list", {"demo_id": "demo1"})["takes"]
    good = _call(server, "take.recapture", {"demo_id": "demo1", "shot_index": 0, "take_index": 0,
                                            "expected_revision": takes[0]["sourceRevision"]})
    assert good["ok"] is True and good["take"]["takeIndex"] == 1
    stale = _call(server, "take.recapture", {"demo_id": "demo1", "shot_index": 1, "take_index": 0,
                                             "expected_revision": takes[1]["sourceRevision"]})
    assert stale["ok"] is False and stale["code"] == "source_stale" and "#gone" in stale["detail"]
    assert len(_call(server, "take.list", {"demo_id": "demo1"})["takes"]) == 3


def test_the_recapture_tool_keeps_the_ssrf_guard(tmp_path, page_url):
    forge = _forge(tmp_path, page_url)
    server = server_mod._build_server(forge)
    takes = _call(server, "take.list", {"demo_id": "demo1"})["takes"]
    with pytest.raises(Exception, match="private"):
        asyncio.run(server.call_tool("take.recapture", {
            "demo_id": "demo1", "shot_index": 0, "take_index": 0,
            "expected_revision": takes[0]["sourceRevision"]}))
