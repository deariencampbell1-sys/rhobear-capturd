"""GAPS-NOW M11 (JOURNEY X5): a shot without pixels never looks like a real step.

The viewer's stand-in for a missing screenshot says the shot was not
captured, gives the recorded reason, and says what to do; it no longer
reads "Step N / placeholder screenshot" like a normal frame.
"""
from __future__ import annotations

import base64
import re

from capturd.walk.viewer import render_viewer_to_file


def _spec():
    step = {"index": 0, "timestamp": 0, "pageUrl": "https://example.com", "pageTitle": "t",
            "interaction": {"type": "click", "target": {"selector": "#buy", "text": ""},
                            "hotspot": {"xPct": 50, "yPct": 50}},
            "annotation": "Buy it.", "screenshotBase64": None, "screenshotPath": None,
            "screenshotError": "selector <#buy> not found"}
    return {"version": 1, "id": "d", "name": "d", "goal": "g", "createdAt": "2026-11-17T00:00:00Z",
            "viewport": {"width": 640, "height": 480}, "startUrl": "https://example.com",
            "aiAnnotations": {"style": "snappy", "summary": "", "animationTimeline": []},
            "steps": [step]}


def test_the_missing_shot_is_labelled_not_captured(tmp_path):
    from playwright.sync_api import sync_playwright

    html = render_viewer_to_file(_spec(), tmp_path / "v.html")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 640, "height": 480})
        page.goto(html.resolve().as_uri())
        page.wait_for_function("() => document.getElementById('screenshot').src.length > 0")
        src = page.evaluate("() => document.getElementById('screenshot').src")
        browser.close()
    svg = base64.b64decode(src.split(",", 1)[1]).decode()
    assert "Shot 1 was not captured" in svg
    assert "selector &lt;#buy&gt; not found" in svg
    assert "Record this shot again" in svg
    assert not re.search(r"placeholder screenshot", svg)
