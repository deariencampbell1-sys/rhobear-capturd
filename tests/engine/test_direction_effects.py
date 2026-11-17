"""GAPS-NOW M6 (rest): hold and spotlight direction reach the export.

`holdMs` and `effect: "spotlight"` map onto the viewer's existing `hold`
and `spotlightOn` keyframes for that shot, in the exported copy only. Voice,
cursor and transition direction stay refused until they render.
"""
from __future__ import annotations

import json
import re

import pytest

from capturd.engine.take_store import DirectionError, TakeStore, validate_direction
from test_take_store import _forge


def _embedded(html: str) -> dict:
    block = re.search(r'<script id="demo-data" type="application/json">\n(.*?)\n  </script>',
                      html, re.DOTALL)
    return json.loads(block.group(1).replace("<\\/", "</"))


def test_hold_and_spotlight_become_keyframes_of_that_shot(tmp_path):
    forge = _forge(tmp_path)
    store = TakeStore(forge)
    takes = store.open_review("demo1")["takes"]
    new = store.retake("demo1", 1, 0, direction={"holdMs": 1200, "effect": "spotlight"},
                       expected_revision=takes[1]["sourceRevision"])
    store.approve("demo1", 0, 0, by="Neo", expected_revision=takes[0]["sourceRevision"])
    store.approve("demo1", 1, 1, by="Neo", expected_revision=new["sourceRevision"])
    spec = _embedded(forge.export_demo("demo1", fmt="html").read_text())
    timeline = spec["aiAnnotations"]["animationTimeline"]
    assert {"stepIndex": 1, "action": "hold", "duration": 1200, "easing": "linear"} in timeline
    assert {"stepIndex": 1, "action": "spotlightOn", "target": "body", "duration": 500,
            "easing": "ease-in-out"} in timeline
    assert not [k for k in timeline if k["stepIndex"] == 0]
    assert forge.load_spec("demo1")["aiAnnotations"]["animationTimeline"] == []


@pytest.mark.parametrize("direction, field", [
    ({"holdMs": -1}, "holdMs"), ({"holdMs": 10_001}, "holdMs"), ({"holdMs": True}, "holdMs"),
    ({"effect": "ring"}, "effect"), ({"transition": "crossfade"}, "transition"),
])
def test_bad_effect_direction_is_named(direction, field):
    with pytest.raises(DirectionError) as exc:
        validate_direction(direction)
    assert exc.value.field == field


def test_effect_none_adds_nothing():
    assert validate_direction({"effect": "none"})["effect"] == "none"
