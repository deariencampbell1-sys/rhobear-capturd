"""GAPS-NOW U3 (contract §1/§7): every export answers with an artifact reference.

The reference is content-addressed (id and revision from the SHA-256 of the
bytes), its kind is one the manifest lists, and its provenance is §7's
fields only. The per-demo registry (`artifacts.register`) refuses an id
re-registered at another revision.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

from capturd import engine
from test_take_store import _forge

PROVENANCE_KEYS = {"engineId", "engineVersion", "capability", "capabilityVersion",
                   "operationId", "attemptId"}


def _call(server, name, args):
    result = asyncio.run(server.call_tool(name, args))
    return result.structured_content.get("result", result.structured_content)


def test_an_html_export_returns_its_artifact(tmp_path):
    from capturd.mcp.server import _build_server

    forge = _forge(tmp_path)
    server = _build_server(forge)
    answer = _call(server, "demo.export", {"demo_id": "demo1", "format": "html",
                                           "operation_id": "op-art"})
    artifact = answer["artifact"]
    body = (forge.demo_dir("demo1") / "export.html").read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    assert artifact["revision"] == "sha256:" + digest
    assert artifact["artifactId"] == f"art_{digest[:24]}"
    assert artifact["kind"] == "text/html" and artifact["bytes"] == len(body)
    assert artifact["kind"] in engine.load_manifest()["artifactKinds"]
    prov = artifact["provenance"]
    assert set(prov) == PROVENANCE_KEYS
    assert prov["engineId"] == "capturd" and prov["capability"] == "walk-export"
    assert prov["operationId"] == "op-art" and prov["attemptId"].startswith("att_")
    registry = json.loads((forge.demo_dir("demo1") / "artifacts.json").read_text())
    assert registry[artifact["artifactId"]] == {"revision": artifact["revision"], "kind": "text/html"}


def test_the_manifest_advertises_the_html_export():
    walk = next(c for c in engine.load_manifest()["capabilities"] if c["id"] == "walk-export")
    assert "html" in [t.strip() for t in walk["params"]["fmt"].split("|")]
