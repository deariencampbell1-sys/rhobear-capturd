"""GAPS-NOW M8 (JOURNEY E10, CD-PLAYER reduced motion): every export has a poster.

The poster is the first shot's exported pixels: with a review open, the
selected (approved) take's pixels, so a recaptured first shot is the poster.
A demo whose first shot has no pixels gets no poster, never a placeholder.
"""
from __future__ import annotations

import base64

from capturd.engine.take_store import TakeStore
from capturd.walk.coordinator import DemoForge


def _spec(first_png):
    step = lambda i, png: {  # noqa: E731
        "index": i, "timestamp": 0, "pageUrl": "https://example.com", "pageTitle": "t",
        "interaction": {"type": "click", "target": {"selector": "body", "tagName": "", "text": "",
                        "boundingRect": {"x": 0, "y": 0, "width": 9, "height": 9}},
                        "hotspot": {"xPct": 50, "yPct": 50}},
        "annotation": "a", "screenshotPath": None,
        "screenshotBase64": base64.b64encode(png).decode() if png else None}
    return {"version": 1, "id": "d", "name": "d", "goal": "g", "createdAt": "2026-11-17T00:00:00Z",
            "viewport": {"width": 9, "height": 9}, "startUrl": "https://example.com",
            "aiAnnotations": {"style": "snappy", "summary": "", "animationTimeline": []},
            "steps": [step(0, first_png), step(1, b"second")]}


def test_the_html_export_writes_the_first_shot_as_poster(tmp_path):
    forge = DemoForge(demos_dir=tmp_path / "demos")
    forge.save_spec("d", _spec(b"\x89PNG first"))
    forge.export_demo("d", fmt="html")
    assert (forge.demo_dir("d") / "poster.png").read_bytes() == b"\x89PNG first"


def test_a_recaptured_first_shot_is_the_poster(tmp_path):
    forge = DemoForge(demos_dir=tmp_path / "demos")
    forge.save_spec("d", _spec(b"\x89PNG first"))
    store = TakeStore(forge)
    takes = store.open_review("d")["takes"]
    new = store.recapture("d", 0, 0, pixels=b"\x89PNG again",
                          expected_revision=takes[0]["sourceRevision"])
    store.approve("d", 0, 1, by="Neo", expected_revision=new["sourceRevision"])
    store.approve("d", 1, 0, by="Neo", expected_revision=takes[1]["sourceRevision"])
    forge.export_demo("d", fmt="html")
    assert (forge.demo_dir("d") / "poster.png").read_bytes() == b"\x89PNG again"


def test_no_pixels_no_poster(tmp_path):
    forge = DemoForge(demos_dir=tmp_path / "demos")
    forge.save_spec("d", _spec(None))
    forge.export_demo("d", fmt="html")
    assert not (forge.demo_dir("d") / "poster.png").exists()
