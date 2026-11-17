"""GAPS-NOW W3: Captur'd chrome is Captur'd blue, not the old sun-gold.

The viewer, the recorder's in-page overlay and the voice control are what a
person sees in a Captur'd export or capture; the board sets their accent to
#3aa7ff (engines.html `.th-capturd`)."""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SURFACES = [
    REPO / "capturd" / "walk" / "templates" / "viewer.html",
    REPO / "capturd" / "walk" / "recorder.py",
    REPO / "capturd" / "walk" / "voice.py",
]
_OLD = re.compile(r"f2c230|242\s*,\s*194\s*,\s*48|1a1206|sun-gold", re.IGNORECASE)


def test_no_surface_carries_the_old_gold():
    for path in SURFACES:
        hits = [(n, line.strip()) for n, line in
                enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                if _OLD.search(line)]
        assert not hits, f"{path.relative_to(REPO)} still carries sun-gold: {hits[:5]}"


def test_the_viewer_has_one_accent_token():
    html = (REPO / "capturd" / "walk" / "templates" / "viewer.html").read_text(encoding="utf-8")
    assert re.search(r"--accent:\s*#3aa7ff", html, re.IGNORECASE)
    assert html.lower().count("#3aa7ff") == 1, "the viewer names the blue once, as the token"
