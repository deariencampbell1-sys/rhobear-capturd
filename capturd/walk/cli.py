"""`capturd walk` CLI — thin wrapper over the same coordinator the MCP surface uses.

Subcommands: record / stop / status / list / export / edit / delete.
Both the human CLI and the agent MCP surface land on the same code path
(``capturd.walk.coordinator.DemoForge``) — no duplicate business logic.

W1 (recorder-as-agent-entrypoint), W2 (zoom pipeline), W3 (voice-sync), W4
(expanded MCP surface) will fill in the actual implementations. This file
is the argparse skeleton the workers hang their work on.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capturd walk",
        description="Agent-made interactive product walkthroughs.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("record", help="Start a walkthrough recording session")
    p.add_argument("--url", required=True, help="URL to walk")
    p.add_argument("--name", required=True, help="Demo name")
    p.add_argument("--goal", required=True, help="What the flow demonstrates")
    p.add_argument("--agent", action="store_true",
                   help="Agent-driven (LLM picks each next click). Default: headful, human clicks.")
    p.add_argument("--viewport", default="1440x900", help="Recording viewport (WxH)")

    p = sub.add_parser("stop", help="Stop a recording and kick off AI enrichment")
    p.add_argument("--session-id", required=True)

    p = sub.add_parser("status", help="Check enrichment progress for a demo")
    p.add_argument("--demo-id", required=True)

    p = sub.add_parser("list", help="List recorded demos")
    p.add_argument("--json", action="store_true", help="Machine-readable output")

    p = sub.add_parser("export", help="Render a demo as viewer HTML / MP4 / GIF")
    p.add_argument("--demo-id", required=True)
    p.add_argument("--format", choices=["html", "mp4", "gif"], default="html")
    p.add_argument("--out", help="Output path (default: alongside the demo JSON)")

    p = sub.add_parser("edit", help="Edit a step's annotation / voiceover / camera")
    p.add_argument("--demo-id", required=True)
    p.add_argument("--step", type=int, required=True)
    p.add_argument("--annotation", help="Rewrite the annotation")
    p.add_argument("--regenerate-voice", action="store_true")

    p = sub.add_parser("delete", help="Delete a demo and its files")
    p.add_argument("--demo-id", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.cmd == "record":
        return _cmd_record(args)

    # Other subcommands — spine stubs until W2-W4.
    sys.stderr.write(
        f"[spine-stub] `capturd walk {args.cmd}` — implementation lands in W2-W4.\n"
    )
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    """Dispatch `capturd walk record` — agent or human mode."""
    import asyncio

    from capturd.walk.recorder import DemoManager, DemoRecorderError

    parts = args.viewport.split("x")
    viewport = {"width": 1440, "height": 900}
    if len(parts) == 2:
        try:
            viewport = {"width": int(parts[0]), "height": int(parts[1])}
        except ValueError:
            sys.stderr.write(f"invalid viewport: {args.viewport}\n")
            return 1

    payload: dict[str, Any] = {
        "url": args.url,
        "name": args.name,
        "goal": args.goal,
        "viewport": viewport,
        "mode": "agent" if args.agent else "human",
    }

    mgr = DemoManager()

    try:
        recorder, session_id, mode = mgr.start(payload)
    except DemoRecorderError as exc:
        sys.stderr.write(f"record failed: {exc}\n")
        return 1

    if mode == "agent":
        sys.stderr.write(f"Recording (agent mode) session={session_id}...\n")
        try:
            spec = asyncio.run(recorder.agent_record())
        except DemoRecorderError as exc:
            sys.stderr.write(f"agent record failed: {exc}\n")
            return 1
        sys.stdout.write(json.dumps({
            "sessionId": session_id,
            "mode": "agent",
            "steps": len(spec.steps),
        }, indent=2) + "\n")
        sys.stderr.write(
            f"Done. {len(spec.steps)} steps recorded → "
            f"demos/{session_id}/demo.json\n"
        )
    else:
        sys.stderr.write(
            f"Recording (human mode) session={session_id}.\n"
            f"Open the browser and click through the flow. "
            f"Send SIGINT or call `capturd walk stop --session-id {session_id}` to finish.\n"
        )
        try:
            asyncio.run(recorder.start())
        except DemoRecorderError as exc:
            sys.stderr.write(f"human record failed: {exc}\n")
            return 1
        # Human mode: the recorder loop runs in background. The user stops
        # it from another terminal. We keep the event loop alive.
        try:
            asyncio.get_event_loop().run_forever()
        except KeyboardInterrupt:
            spec = recorder.stop()
            sys.stdout.write(json.dumps({
                "sessionId": session_id,
                "mode": "human",
                "steps": len(spec.steps),
            }, indent=2) + "\n")
            sys.stderr.write(
                f"Done. {len(spec.steps)} steps recorded → "
                f"demos/{session_id}/demo.json\n"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
