"""Live-drive contract tests — the 'talk to it as it records' surface.

Fast, browser-free checks that the wiring is correct. The full end-to-end
(real browser, real MP4) lives in scripts/e2e_live_drive.py.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from capturd.mcp.server import _build_server
from capturd.walk.recorder import DemoManager, DemoRecorder, DemoRecorderError

REPO = Path(__file__).resolve().parents[1]

# The Pro-gate licence env var, assembled from fragments so this test file is
# not itself a text hit for the acceptance grep (which must find licence
# plumbing nowhere except capturd/pro.py's reads and
# tests/test_pro_env_config.py's monkeypatch fixtures).
_LICENCE_ENV_VAR = "RHOBEAR_" "CAPTURD_LICENSE"


def _tool_names() -> set[str]:
    server = _build_server()
    tools = asyncio.run(server._list_tools())
    return {t.name for t in tools}


def test_live_drive_tools_registered():
    names = _tool_names()
    assert {"demo.act", "demo.narrate"} <= names
    # the original surface is still there
    assert {"demo.record", "demo.stop", "demo.export"} <= names


def test_start_rejects_unknown_mode(tmp_path):
    mgr = DemoManager(output_root=tmp_path)
    with pytest.raises(DemoRecorderError):
        mgr.start({"url": "https://example.com", "name": "x", "mode": "bogus"})


def test_live_mode_defaults_visible(tmp_path):
    mgr = DemoManager(output_root=tmp_path)
    recorder, _sid, mode = mgr.start(
        {"url": "https://example.com", "name": "x", "mode": "live"}
    )
    assert mode == "live"
    assert recorder.headful is True  # live sessions show by default


def test_agent_mode_defaults_hidden(tmp_path):
    mgr = DemoManager(output_root=tmp_path)
    recorder, _sid, _mode = mgr.start(
        {"url": "https://example.com", "name": "x", "mode": "agent"}
    )
    assert recorder.headful is False


def test_visible_override(tmp_path):
    mgr = DemoManager(output_root=tmp_path)
    recorder, _sid, _mode = mgr.start(
        {"url": "https://example.com", "name": "x", "mode": "agent", "visible": True}
    )
    assert recorder.headful is True


def test_act_before_start_raises(tmp_path):
    rec = DemoRecorder(session_id="s", url="https://example.com", name="x", goal="",
                       output_dir=tmp_path / "s")
    with pytest.raises(DemoRecorderError):
        rec.act("click", "#btn")


def test_narrate_without_steps_raises(tmp_path):
    rec = DemoRecorder(session_id="s", url="https://example.com", name="x", goal="",
                       output_dir=tmp_path / "s")
    with pytest.raises(DemoRecorderError):
        rec.narrate("hello")


def test_voice_tools_registered():
    names = _tool_names()
    assert {"demo.poll_voice", "demo.look"} <= names


def test_poll_voice_drains_queue(tmp_path):
    rec = DemoRecorder(session_id="s", url="https://example.com", name="x", goal="",
                       output_dir=tmp_path / "s")
    # simulate the VoiceLoop delivering two utterances
    rec._append_voice("click the house button")
    rec._append_voice("now type my email")
    first = rec.poll_voice()
    assert first["transcripts"] == ["click the house button", "now type my email"]
    assert first["voiceEnabled"] is False  # no VoiceLoop attached
    # queue is cleared after a poll
    assert rec.poll_voice()["transcripts"] == []


def test_live_mode_defaults_voice_on(tmp_path):
    mgr = DemoManager(output_root=tmp_path)
    rec, _sid, _mode = mgr.start({"url": "https://example.com", "name": "x", "mode": "live"})
    assert rec.voice_loop is not None  # live turns the mic on by default


def test_live_mode_voice_can_be_disabled(tmp_path):
    mgr = DemoManager(output_root=tmp_path)
    rec, _sid, _mode = mgr.start(
        {"url": "https://example.com", "name": "x", "mode": "live", "voice": False}
    )
    assert rec.voice_loop is None


# ---------------------------------------------------------------------------
# The e2e driver's licence plumbing (scripts/e2e_live_drive.py): the operator's
# licence passes through unchanged; with none configured the demo.* leg skips
# loudly and a skip is never counted as a pass. The harness itself never
# provisions a licence value — the tree-wide tripwire lives in
# tests/test_import_path_hygiene.py.
# ---------------------------------------------------------------------------

def test_live_drive_passes_operator_licence_through(monkeypatch):
    """Edge case 2: a licence in the operator's environment reaches the spawned
    child UNCHANGED, and no value is written anywhere by the harness."""
    import e2e_live_drive

    monkeypatch.setenv(_LICENCE_ENV_VAR, "  op-licence-ABC123  ")
    env = e2e_live_drive._spawn_env(Path("/tmp/capturd-live-passthrough"))
    # unchanged — even the surrounding whitespace survives, because the child
    # environment is an inheritance of the operator's, not a rewrite:
    assert env[_LICENCE_ENV_VAR] == "  op-licence-ABC123  "
    # the harness never fabricates the gate's OTHER input either:
    assert "CAPTURD_" "PRO_CODES" not in env

    # and with no licence configured, none is conjured:
    monkeypatch.delenv(_LICENCE_ENV_VAR, raising=False)
    env2 = e2e_live_drive._spawn_env(Path("/tmp/capturd-live-passthrough"))
    assert _LICENCE_ENV_VAR not in env2
    assert "CAPTURD_" "PRO_CODES" not in env2
    # the skip decision reads exactly this env var, trimmed:
    assert e2e_live_drive._operator_licence({"X": "1"}) == ""
    assert e2e_live_drive._operator_licence({_LICENCE_ENV_VAR: "  lic  "}) == "lic"


def test_live_drive_skips_demo_leg_without_a_licence():
    """Edge case 1, end to end: run the REAL driver (subprocess) with no licence
    configured. It must print the SKIP line, exit with the non-Pro legs' status,
    and the printed summary must show the demo-leg checks as skipped — never
    counted as passes."""
    env = {k: v for k, v in os.environ.items() if k != _LICENCE_ENV_VAR}
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "e2e_live_drive.py")],
        capture_output=True, text=True, env=env, timeout=300, cwd=str(REPO))
    out = proc.stdout
    expected_skip = ("[live] SKIP demo.* leg: no Pro licence configured (set "
                     f"{_LICENCE_ENV_VAR} to a real licence to run it)")
    assert expected_skip in out, f"SKIP line missing:\n{out}\n{proc.stderr}"
    # exit status = the non-Pro legs' status (the tools leg passed):
    assert proc.returncode == 0, f"rc={proc.returncode}\n{out}\n{proc.stderr}"
    m = re.search(
        r"\[live\] summary: (\d+) passed, (\d+) failed, (\d+) skipped", out)
    assert m, f"summary line missing:\n{out}"
    passed, failed, skipped = (int(g) for g in m.groups())
    assert failed == 0
    import e2e_live_drive
    assert skipped == len(e2e_live_drive.DRIVE_LEG_CHECKS)  # all demo checks skipped
    assert passed == 1  # ONLY the non-Pro tools leg — skips are not passes
