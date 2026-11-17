"""GAPS-NOW W4 (D10): the README says what the code does, nothing more.

* no separate Pro tier, unlock code or upgrade (retired, GAPS-NOW W1);
* no claim that canvas/WebGL pages play back in a video mode: the recorder
  only classifies content (recorder.py classify), and no viewer or exporter
  plays a video fallback;
* the MCP tool list in the README is exactly the server's tool list.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = (REPO / "README.md").read_text(encoding="utf-8")


def test_no_separate_paid_tier_is_advertised():
    for phrase in (r"\bPro\b", r"capturd activate", r"\bUpgrade\b", r"\bunlock\b"):
        assert not re.search(phrase, README, re.IGNORECASE), phrase


def test_no_video_fallback_is_claimed():
    assert not re.search(r"fall\s*back to video", README, re.IGNORECASE)


def test_install_steps_name_this_repo():
    assert "cd sunsponge-capture" not in README


def test_the_readme_tool_list_is_the_server_tool_list():
    from capturd.mcp.server import _build_server

    block = re.search(r"```text\n(# @capturd tools.*?)```", README, re.DOTALL)
    assert block, "README has no ```text block starting '# @capturd tools'"
    listed = set(re.findall(r"\b(?:demo|capture|engine)\.[a-z_]+\b", block.group(1)))
    served = {tool.name for tool in asyncio.run(_build_server().list_tools())}
    assert listed == served, {"missing": sorted(served - listed), "extra": sorted(listed - served)}
