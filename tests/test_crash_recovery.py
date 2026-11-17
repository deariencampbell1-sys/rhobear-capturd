"""GAPS-NOW M2 / X8: a recording survives a crash as a partial demo.

* The recorder checkpoints demo.json after every committed step, with a
  session marker naming the recording process.
* A server that starts and finds a marker whose process is gone marks that
  demo interrupted and partial: its steps stay reviewable, never lost and
  never shown as finished.
"""
from __future__ import annotations

import json
import subprocess
import sys

from capturd.walk.coordinator import SESSION_MARKER, DemoForge

from test_demo_cancel import _call, fixture_url  # noqa: F401  (pytest fixture)


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_each_step_is_checkpointed_while_recording(tmp_path, fixture_url):  # noqa: F811
    import capturd.mcp.server as server_mod

    forge = DemoForge(demos_dir=tmp_path / "demos")
    server = server_mod._build_server(forge)
    session = _call(server, "demo.record", {"url": fixture_url, "name": "c", "mode": "live",
                                            "visible": False, "voice": False})["sessionId"]
    try:
        _call(server, "demo.act", {"session_id": session, "action": "click", "selector": "#signup-btn"})
        folder = forge.demo_dir(session)
        assert len(json.loads((folder / "demo.json").read_text())["steps"]) == 1
        marker = json.loads((folder / SESSION_MARKER).read_text())
        assert marker["state"] == "recording" and isinstance(marker["pid"], int)
    finally:
        _call(server, "demo.cancel", {"session_id": session})
    assert json.loads((folder / SESSION_MARKER).read_text())["state"] == "stopped"


def _crashed_demo(forge: DemoForge, pid: int) -> str:
    forge.save_spec("crashed", {
        "version": 1, "id": "crashed", "name": "c", "goal": "g", "createdAt": "2026-11-17T00:00:00Z",
        "viewport": {"width": 9, "height": 9}, "startUrl": "https://example.com",
        "aiAnnotations": {"style": "snappy", "summary": "", "animationTimeline": []},
        "steps": [{"index": 0, "timestamp": 0, "pageUrl": "https://example.com", "pageTitle": "t",
                   "interaction": {"type": "click", "target": {}, "hotspot": {}}}]})
    (forge.demo_dir("crashed") / SESSION_MARKER).write_text(json.dumps(
        {"state": "recording", "pid": pid, "sessionId": "crashed"}))
    return "crashed"


def test_a_dead_recording_is_marked_interrupted_on_start(tmp_path):
    import capturd.mcp.server as server_mod

    forge = DemoForge(demos_dir=tmp_path / "demos")
    demo = _crashed_demo(forge, _dead_pid())
    server_mod._build_server(DemoForge(demos_dir=tmp_path / "demos"))
    status = forge.get_status(demo)
    assert status["interrupted"] is True and status["partial"] is True
    assert status["totalSteps"] == 1 and status["status"] == "recorded"
    assert json.loads((forge.demo_dir(demo) / SESSION_MARKER).read_text())["state"] == "interrupted"


def test_a_recording_owned_by_a_live_process_is_left_alone(tmp_path):
    import os

    import capturd.mcp.server as server_mod

    forge = DemoForge(demos_dir=tmp_path / "demos")
    demo = _crashed_demo(forge, os.getppid())
    server_mod._build_server(DemoForge(demos_dir=tmp_path / "demos"))
    assert "interrupted" not in forge.get_status(demo)
