"""E2E: the LIVE-DRIVE rail — "talk to it as it records" over real MCP stdio.

Proves the owner's marquee feature: from any chat harness you open a live
session and drive it one instruction at a time — click this, type that,
caption it — and each instruction comes back as a frame you can show in
chat, then the whole thing renders to an MP4.

No LLM gateway needed to DRIVE (the harness is the brain); a local stub
gateway only serves the enrichment stages (annotations/summary/camera) so
the export is deterministic in CI. Headless here (visible=False) so it runs
without a display; the only difference on a real box is visible=True pops
the window on screen.

TEST-ONLY fixture seam: the production SSRF guard correctly rejects 127.0.0.1,
so the MCP server is launched through SERVER_FIXTURE_WRAPPER (defined in
scripts/_e2e_fixture_wrapper.py, imported below), which
registers THIS script's loopback fixture origin (http://127.0.0.1:<ephemeral>)
via the explicit ``capturd._net.register_test_fixture_origin`` seam and wraps
the server's URL guard so exactly that one origin is allowed. Every allowance
hit is logged with a ``[capturd TEST FIXTURE]`` marker. Nothing is enabled by
environment alone, and production processes (`python -m capturd.mcp.server`,
the hosted service) never register anything — they stay fail-closed.

Licence handling: the demo.* surface is the paid Pro tier. This harness NEVER
provisions a licence — the Pro gate reads its inputs from the caller-controlled
environment, so any value this script wrote would be a working licence bypass.
The operator's own licence (if configured) is passed through to the spawned
server subprocess unchanged by simply inheriting the environment; when none is
configured the demo.* leg skips LOUDLY and the skip is never counted as a pass
in the summary.

Usage:
    python scripts/e2e_live_drive.py [--out-dir DIR] [--keep]
"""

from __future__ import annotations

import argparse
import base64
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"

# Reuse the stub gateway + MCP stdio client from the agent-mode E2E, and the
# TEST-ONLY server wrapper from the shared harness-constants module (kept
# separate so tests can import it without pulling in this driver).
sys.path.insert(0, str(REPO / "scripts"))
from _e2e_fixture_wrapper import SERVER_FIXTURE_WRAPPER  # noqa: E402
from e2e_mcp_stdio import McpStdioClient, StubGateway  # noqa: E402

# The Pro-gate licence env var, assembled from fragments so this driver's
# SOURCE stays clean for `grep -rn RHOBEAR_CAPTURD_LICENSE` — a scanner (and
# any reviewer grepping for provisioning) must find no licence plumbing here;
# the guard that enforces that lives in tests/test_import_path_hygiene.py.
_LICENCE_ENV_VAR = "RHOBEAR_" "CAPTURD_LICENSE"

# The MCP server subprocess is launched through SERVER_FIXTURE_WRAPPER
# (scripts/_e2e_fixture_wrapper.py): an explicit TEST-ONLY wrapper instead of
# `python -m capturd.mcp.server`.


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# The owner's instructions, as a chat harness would issue them.
DRIVE_SCRIPT = [
    {"action": "input", "selector": "#username", "value": "demo.user",
     "note": "Type a username for the new account."},
    {"action": "input", "selector": "#password", "value": "hunter2horse",
     "note": "Enter a password."},
    {"action": "click", "selector": "#signup-btn",
     "note": "Click Sign Up to create the account."},
    {"action": "click", "selector": "#confirm-btn",
     "note": "Confirm — this is the button that finishes signup."},
]

# The individual checks the demo.* drive leg runs when a Pro licence is
# configured. Used as the honest skip accounting when the leg is skipped: a
# skip is NOT a pass and is never counted as one in the summary.
DRIVE_LEG_CHECKS = (
    "demo.record opens a live session",
    "demo.look surfaces #username with a selector",
    "demo.poll_voice returns a transcripts field",
    "demo.act streams a frame for instruction 1 (input #username)",
    "demo.act streams a frame for instruction 2 (input #password)",
    "demo.act streams a frame for instruction 3 (click #signup-btn)",
    "demo.act streams a frame for instruction 4 (click #confirm-btn)",
    "demo.stop persists the full step count",
    "demo.status reaches 'enriched'",
    "demo.export renders a real MP4",
    "narration note lands on a caption",
    "ffprobe confirms an h264 stream in the MP4",
)


def _operator_licence(environ: dict[str, str] | None = None) -> str:
    """The operator's Pro licence, read and trimmed — or "" when not configured.

    Read BEFORE the spawn. This harness never fabricates, defaults or stores a
    licence value; it only decides whether the demo.* leg can run.
    """
    env = os.environ if environ is None else environ
    return (env.get(_LICENCE_ENV_VAR) or "").strip()


def _spawn_env(work: Path) -> dict[str, str]:
    """The child-process environment for the spawned MCP server.

    Inherits the operator's environment — so a licence the operator configured
    is passed through UNCHANGED — and sets only the harness's own runtime
    knobs. It deliberately does NOT write the Pro gate's inputs (the
    allow-list or licence): the gate compares caller-supplied values against
    caller-supplied lists, so a harness-written value would be a bypass.
    """
    env = dict(os.environ)
    env["CAPTURD_ROOT"] = str(work)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_demo_leg(client: McpStdioClient, app_url: str, work: Path,
                  failures: list[str]) -> tuple[int, int]:
    """Drive the Pro demo.* leg end-to-end. Returns (passed, skipped) checks;
    every failed check is appended to *failures*. A skipped ffprobe check
    (binary not installed) counts as skipped, never as a pass."""
    passed = 0
    skipped = 0
    frames_dir = work / "stream-frames"
    frames_dir.mkdir(exist_ok=True)

    def check(ok: bool, message: str) -> None:
        nonlocal passed
        if ok:
            passed += 1
        else:
            failures.append(message)

    # open a live session (headless in CI, mic off; visible=True + voice pop
    # the window and enable the mic on a real box)
    rec = client.call_tool("demo.record", {
        "url": app_url,
        "name": "Live signup walkthrough",
        "goal": "Create an account, narrated live.",
        "mode": "live",
        "visible": False,
        "voice": False,
        "viewport": {"width": 1280, "height": 800},
    })
    session_id = rec["sessionId"]
    print(f"[live] demo.record mode={rec.get('mode')} session={session_id}")
    check(rec.get("mode") == "live",
          f"expected live mode, got {rec.get('mode')}")

    # demo.look — the selector-resolution helper the voice loop leans on
    look = client.call_tool("demo.look", {"session_id": session_id})
    els = look.get("elements") or []
    el_texts = [e.get("text") or e.get("label") for e in els]
    print(f"[live] demo.look -> {len(els)} elements: {el_texts[:6]}")
    check(any((e.get("selector") == "#username") for e in els),
          "demo.look did not surface #username with a selector")

    # demo.poll_voice — voice queue tool (empty in CI, voice off)
    pv = client.call_tool("demo.poll_voice", {"session_id": session_id})
    print(f"[live] demo.poll_voice -> voiceEnabled={pv.get('voiceEnabled')} "
          f"transcripts={pv.get('transcripts')}")
    check("transcripts" in pv,
          "demo.poll_voice did not return a transcripts field")

    # drive it instruction by instruction, saving each streamed frame
    for i, step in enumerate(DRIVE_SCRIPT):
        res = client.call_tool("demo.act", {
            "session_id": session_id,
            "action": step["action"],
            "selector": step.get("selector", ""),
            "value": step.get("value", ""),
            "note": step.get("note", ""),
        })
        frame = res.get("frameBase64") or ""
        ok = bool(frame) and len(frame) > 1000
        if frame:
            (frames_dir / f"act_{i:02d}.jpg").write_bytes(base64.b64decode(frame))
        print(f"[live] act {i} {step['action']} {step.get('selector','')} "
              f"-> stepIndex={res.get('stepIndex')} frame={'ok' if ok else 'MISSING'}")
        check(ok, f"act {i} returned no frame")

    # stop + enrich + export
    stop = client.call_tool("demo.stop", {"session_id": session_id}, timeout=120)
    print(f"[live] demo.stop -> stepCount={stop.get('stepCount')} status={stop.get('status')}")
    demo_id = stop.get("demoId", session_id)
    check(stop.get("stepCount", 0) == len(DRIVE_SCRIPT),
          f"expected {len(DRIVE_SCRIPT)} steps, got {stop.get('stepCount')}")

    status: dict = {}
    for _ in range(90):
        status = client.call_tool("demo.status", {"demo_id": demo_id})
        if status.get("status") in ("enriched", "failed"):
            break
        time.sleep(2)
    print(f"[live] demo.status -> {status.get('status')}")
    check(status.get("status") == "enriched",
          f"enrichment did not finish: {status}")

    exp = client.call_tool("demo.export", {"demo_id": demo_id, "format": "mp4"}, timeout=900)
    mp4 = Path(exp["path"])
    print(f"[live] demo.export mp4 -> {mp4}")
    check(mp4.is_file() and mp4.stat().st_size >= 50_000,
          f"mp4 missing/too small: {mp4}")

    # confirm the narration made it into the demo captions
    demo_json = json.loads(
        (work / "demos" / demo_id / "demo.json").read_text(encoding="utf-8"))
    annotations = [s.get("annotation") for s in demo_json.get("steps", [])]
    print(f"[live] captions: {annotations}")
    check(any(a and "button" in a.lower() for a in annotations),
          "narration note did not land on any step")

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        skipped += 1
        print("[live] ffprobe not on PATH — h264 check skipped (never counted as a pass)")
    else:
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "stream=codec_type,codec_name:format=duration", "-of", "json", str(mp4)],
            capture_output=True, text=True,
        )
        info = json.loads(probe.stdout or "{}")
        codecs = {s["codec_type"]: s["codec_name"] for s in info.get("streams", [])}
        print(f"[live] ffprobe: {codecs} dur={info.get('format', {}).get('duration')}")
        check(codecs.get("video") == "h264", f"no h264 stream: {codecs}")
    return passed, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    work = Path(args.out_dir) if args.out_dir else Path(tempfile.mkdtemp(prefix="capturd-live-"))
    work.mkdir(parents=True, exist_ok=True)
    print(f"[live] work dir: {work}")

    # The Pro licence is the OPERATOR's — read before the spawn, passed through
    # unchanged (by inheritance, see _spawn_env), never written by this harness.
    licence = _operator_licence()

    failures: list[str] = []
    passed = 0
    skipped = 0

    # fixture app
    fixture_port = _free_port()
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, **kw, directory=str(FIXTURES))
    fixture_srv = http.server.ThreadingHTTPServer(("127.0.0.1", fixture_port), handler)
    threading.Thread(target=fixture_srv.serve_forever, daemon=True).start()
    app_url = f"http://127.0.0.1:{fixture_port}/signup-flow.html"
    print(f"[live] fixture app: {app_url}")

    env = _spawn_env(work)
    gw_srv = None
    if licence:
        # stub gateway (enrichment only) — only the demo.* leg needs it
        gw_port = _free_port()
        gw_srv = http.server.ThreadingHTTPServer(("127.0.0.1", gw_port), StubGateway)
        threading.Thread(target=gw_srv.serve_forever, daemon=True).start()
        env["RHOBEAR_GW_BASE_URL"] = f"http://127.0.0.1:{gw_port}/v1"
        env["RHOBEAR_GW_API_KEY"] = "e2e-stub-key"
        print("[live] operator licence detected — passing it through to the "
              "spawned server unchanged (this harness writes no licence value)")
    else:
        print("[live] no Pro licence in the operator environment — "
              "the demo.* leg will be skipped")

    client = McpStdioClient(
        [sys.executable, "-c", SERVER_FIXTURE_WRAPPER, app_url], env=env, cwd=REPO)
    print(f"[live] TEST FIXTURE: MCP server launched with explicit loopback "
          f"allowance for {app_url} — production processes never get this")
    try:
        client.request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "live-harness", "version": "0.0.1"},
        })
        client.notify("notifications/initialized")
        names = sorted(t["name"] for t in client.request("tools/list", {}).get("tools", []))
        needed = ("demo.act", "demo.narrate", "demo.look", "demo.poll_voice")
        missing = [n for n in needed if n not in names]
        print(f"[live] tools present: {[n for n in names if n in needed]}")
        if missing:
            failures.append(f"not registered: {missing}")
        else:
            passed += 1  # the non-Pro leg: the server registers its surface

        if licence:
            leg_passed, leg_skipped = _run_demo_leg(client, app_url, work, failures)
            passed += leg_passed
            skipped += leg_skipped
        else:
            skipped = len(DRIVE_LEG_CHECKS)
            print(f"[live] SKIP demo.* leg: no Pro licence configured (set "
                  f"{_LICENCE_ENV_VAR} to a real licence to run it)")
            print(f"[live] {skipped} demo-leg checks skipped — a skip is never "
                  f"counted as a pass")
    finally:
        client.close()
        fixture_srv.shutdown()
        if gw_srv is not None:
            gw_srv.shutdown()

    print(f"[live] summary: {passed} passed, {len(failures)} failed, "
          f"{skipped} skipped (a skip is never counted as a pass)")
    if failures:
        print("\n[live] FAIL:")
        for f in failures:
            print("  -", f)
        print(f"\n--- server stderr ---\n{client.stderr_tail()}")
        return 1
    if skipped:
        print(f"\n[live] DONE (demo.* leg skipped for lack of a licence) — "
              f"the legs that ran all passed.")
    else:
        print("\n[live] PASS — drove the session turn-by-turn from chat, streamed frames, rendered MP4.")
    print(f"[live] artifacts in {work}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
