"""MCP V2 additions — spec-intelligence tools (core) + engagement surface
(hosted), including ownership scoping via the x-capturd-user contextvar."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))


def _minimal_spec(demo_id: str, step_count: int = 4) -> dict:
    return {
        "version": 1, "id": demo_id, "name": "T", "goal": "g",
        "createdAt": "2026-08-29T00:00:00Z",
        "viewport": {"width": 1024, "height": 768}, "startUrl": "https://x.test",
        "steps": [
            {"index": i, "timestamp": i, "pageUrl": "https://x.test",
             "pageTitle": f"s{i}",
             "interaction": {"type": "click",
                             "target": {"selector": f"#b{i}", "tagName": "button",
                                        "text": f"Button {i}",
                                        "boundingRect": {"x": 700, "y": 300,
                                                         "width": 200, "height": 50}},
                             "hotspot": {"xPct": 50, "yPct": 50}},
             "annotation": f"Step {i} narration.",
             "screenshotBase64": "AAAA"}
            for i in range(step_count)
        ],
    }


def _call(fn, *args, **kwargs):
    return asyncio.run(fn(*args, **kwargs))


# ---------------------------------------------------------------------------
# Core spec-intelligence tools
# ---------------------------------------------------------------------------


@pytest.fixture
def forge_env(tmp_path):
    from capturd.walk.coordinator import DemoForge
    demos = tmp_path / "demos"
    demos.mkdir()
    forge = DemoForge(demos_dir=demos)
    forge.save_spec("v2-demo", _minimal_spec("v2-demo"))
    return forge


def _core_server(forge):
    from capturd.mcp.server import _build_server
    return _build_server(forge)


def test_core_has_v2_spec_tools(forge_env):
    srv = _core_server(forge_env)
    names = {t.name for t in asyncio.run(srv.list_tools())}
    assert {"demo.audit", "demo.optimize", "demo.personalize",
            "voice.list", "voice.preview", "voice.synthesize"} <= names


def test_core_demo_audit_scores(forge_env):
    srv = _core_server(forge_env)
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    out = _call(tools["demo.audit"].fn, "v2-demo")
    assert out["ok"] is True
    assert 0 <= out["overall"] <= 100
    assert "engagement" in out["scores"]
    assert out["behavioral_data"] is False   # no analytics passed → no fabrication


def test_core_demo_optimize_plan_then_apply(forge_env):
    srv = _core_server(forge_env)
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}

    plan = _call(tools["demo.optimize"].fn, "v2-demo", False)
    assert plan["applied"] is False

    applied = _call(tools["demo.optimize"].fn, "v2-demo", True)
    assert applied["applied"] is True
    spec = forge_env.load_spec("v2-demo")
    assert spec["steps"] == _minimal_spec("v2-demo")["steps"], \
        "clean spec must survive optimization unchanged"


def test_core_demo_personalize_preview_vs_save(forge_env):
    srv = _core_server(forge_env)
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    spec = forge_env.load_spec("v2-demo")
    spec["steps"][0]["annotation"] = "Hi {{name}}"
    forge_env.save_spec("v2-demo", spec)

    preview = _call(tools["demo.personalize"].fn, "v2-demo", {"name": "Acme"})
    assert preview["spec"]["steps"][0]["annotation"] == "Hi Acme"
    assert preview["saved_to_draft"] is False

    saved = _call(tools["demo.personalize"].fn, "v2-demo", {"name": "Acme"},
                  True)
    assert saved["saved_to_draft"] is True
    assert forge_env.load_spec("v2-demo")["steps"][0]["annotation"] == "Hi Acme"

    with pytest.raises(ValueError):
        _call(tools["demo.personalize"].fn, "v2-demo", {"bad key!": "x"})


# ---------------------------------------------------------------------------
# Hosted engagement surface
# ---------------------------------------------------------------------------


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    import mcp_service  # noqa: F401 — registers engagement tools

    from app import config, store
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "JOBS_DIR", data / "jobs")
    monkeypatch.setattr(config, "DB_PATH", data / "capturd.sqlite3")
    store.init()

    u = store.upsert_user("mcp-owner@test.dev")
    demo = store.create_demo(u["id"], "Hosted Demo",
                             __import__("json").dumps(_minimal_spec("h1")))

    import engagement_tools as et
    return {"store": store, "et": et, "demo_id": demo["id"], "uid": u["id"]}


def test_hosted_surface_tool_count_and_ownership(hosted, monkeypatch):
    import mcp_service
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    async def main():
        names = [t.name for t in await mcp_service.server.list_tools()]
        assert len(names) == len(set(names)), "no duplicate tool names"
        assert {"demo.publish", "demo.version.list", "demo.version.restore",
                "demo.audit.live", "analytics.demo", "analytics.session",
                "analytics.compare", "analytics.dropoff",
                "share.create", "share.trackable"} <= set(names)

        et = hosted["et"]
        async with Client(mcp_service.server) as c:
            # Real security behavior: no user context -> refusal.
            try:
                await c.call_tool("demo.publish", {"demo_id": hosted["demo_id"]})
                raised = False
            except ToolError:
                raised = True
            assert raised, "publish must refuse without a user context"

            # Production path: the proxy middleware sets the contextvar. The
            # in-memory client skips ASGI, so simulate what it provides.
            monkeypatch.setattr(et, "_user", lambda: hosted["uid"])
            await c.call_tool("demo.publish", {"demo_id": hosted["demo_id"]})
        assert hosted["store"].get_published(hosted["demo_id"]), "published!"

    asyncio.run(main())


def test_hosted_share_trackable_is_frontman_facade(hosted, monkeypatch):
    import mcp_service
    from app import frontman
    import engagement_tools as et
    from fastmcp import Client

    async def main():
        monkeypatch.setattr(et, "_user", lambda: hosted["uid"])
        async with Client(mcp_service.server) as c:
            await c.call_tool("demo.publish", {"demo_id": hosted["demo_id"]})

            mint_calls = []
            def fake_mint(url, contact_id="", name="", channel="capturd"):
                mint_calls.append({"url": url})
                return {"token": "tok-FM-1",
                        "send_url": "https://frontman.test/card/t?k=1"}

            monkeypatch.setattr(frontman, "mint_tracked_share", fake_mint)
            res = await c.call_tool("share.trackable", {
                "demo_id": hosted["demo_id"], "contact_id": "c-9",
                "name": "America"})
        return res, mint_calls

    res, mint_calls = asyncio.run(main())
    text = str(res.content) if hasattr(res, "content") else str(res)
    assert "tok-FM-1" not in text, "Frontman's token rides ITS url, not ours"
    assert mint_calls and mint_calls[0]["url"].endswith(f"/pub/d/{hosted['demo_id']}")
