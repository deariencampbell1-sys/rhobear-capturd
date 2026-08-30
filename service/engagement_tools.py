"""Hosted engagement MCP tools — registered ON TOP of the core server only
when the MCP harness runs inside the Captur'd service (mcp_service.py).

These tools are meaningless locally (they need the hosted store), so the core
surface stays clean: 23 core + 3 voice tools everywhere, +10 engagement tools
hosted. Ownership comes from the service's MCP proxy, which injects
``x-capturd-user``; a middleware stashes it in a contextvar that every tool
checks before touching a demo.

Tool list (orthogonal, no competitor-chasing):

* demo.publish            freeze the current draft into an immutable version
* demo.version.list       version history
* demo.version.restore    copy an old version back into the draft
* demo.audit              structural audit (+ live analytics when published)
* analytics.demo          full engagement payload
* analytics.session       one viewer session's event trail
* analytics.compare       version A vs version B
* analytics.dropoff       per-step reach/exit/dropoff only
* share.create            public hosted URL
* share.trackable         Frontman-attributed URL (facade — no second token system)
"""
from __future__ import annotations

import contextvars
import logging

log = logging.getLogger("capturd.engagement-mcp")

#: set by the ASGI middleware in mcp_service.py from x-capturd-user
user_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "capturd_mcp_user", default="")


def _user() -> str:
    uid = user_id_var.get()
    if not uid:
        raise ValueError("no MCP user context — call through the hosted /mcp endpoint")
    return uid


def _own(store, demo_id: str) -> dict:
    demo = store.get_demo(demo_id)
    if not demo or demo["owner_uid"] != _user():
        raise ValueError(f"demo not found: {demo_id}")
    return demo


def build_server(store, analytics, frontman):
    """Build the engagement FastMCP sub-server (mounted by mcp_service)."""
    from fastmcp import FastMCP

    mcp = FastMCP("capturd-engagement")

    @mcp.tool(name="demo.publish", description=(
        "Publish a hosted demo: freezes the current draft into a new immutable "
        "version and points the public URL at it. Prospects already viewing an "
        "older version are unaffected."), timeout=30.0)
    async def _demo_publish(demo_id: str) -> dict:
        _own(store, demo_id)
        demo = store.get_demo(demo_id)
        spec_json = demo["draft_spec"]
        import json as _json
        try:
            steps_count = len((_json.loads(spec_json) or {}).get("steps") or [])
        except Exception:                                # noqa: BLE001
            steps_count = 0
        ver = store.publish_version(demo_id, spec_json, steps_count)
        if not ver:
            raise ValueError("publish failed")
        return {"ok": True, "version": ver}


    @mcp.tool(name="demo.version.list", description=(
        "List a hosted demo's immutable published versions (newest first) and "
        "which one is currently live."), timeout=15.0)
    async def _demo_version_list(demo_id: str) -> dict:
        _own(store, demo_id)
        return {"ok": True, "versions": store.list_versions(demo_id),
                "published_version_id": store.get_demo(demo_id)["published_version_id"]}


    @mcp.tool(name="demo.version.restore", description=(
        "Restore an older published version into the mutable draft. History is "
        "never destroyed; call demo.publish to ship the restored draft."), timeout=15.0)
    async def _demo_version_restore(demo_id: str, version_id: str) -> dict:
        _own(store, demo_id)
        ok = store.restore_version_to_draft(demo_id, version_id)
        if not ok:
            raise ValueError(f"version not found: {version_id}")
        return {"ok": True, "restored_to_draft": version_id,
                "note": "history intact; publish to make it live"}


    # ---- analytics ----------------------------------------------------------

    @mcp.tool(name="analytics.demo", description=(
        "Full engagement analytics for a hosted demo: sessions, starts, "
        "completion rate, avg/median engaged time, per-step reach/exits/"
        "dropoff/replays, CTA conversion, branch distribution, device "
        "completion, return rate."), timeout=30.0)
    async def _analytics_demo(demo_id: str, version_id: str | None = None) -> dict:
        _own(store, demo_id)
        return analytics.demo_analytics(demo_id, version_id)


    @mcp.tool(name="analytics.session", description=(
        "One anonymous viewer session's full event trail for a demo (events in "
        "order). The attribution token is never included."), timeout=15.0)
    async def _analytics_session(demo_id: str, session_id: str) -> dict:
        _own(store, demo_id)
        import sqlite3
        from app import config as _config
        with sqlite3.connect(_config.DB_PATH, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            sess = conn.execute("SELECT * FROM viewer_sessions WHERE id=? AND demo_id=?",
                                (session_id, demo_id)).fetchone()
            if not sess:
                raise ValueError(f"session not found: {session_id}")
            events = [dict(r) for r in conn.execute(
                "SELECT event,step_id,branch_id,choice_id,cta_id,elapsed_ms,at "
                "FROM demo_events WHERE session_id=? ORDER BY id",
                (session_id,)).fetchall()]
        s = dict(sess)
        s.pop("attribution_token", None)   # token stays server-side, always
        return {"ok": True, "session": s, "events": events}


    @mcp.tool(name="analytics.compare", description=(
        "Version A vs version B on the same demo: completion, engaged time, "
        "sessions, with a delta block."), timeout=30.0)
    async def _analytics_compare(demo_id: str, version_a: str, version_b: str) -> dict:
        _own(store, demo_id)
        a = analytics.demo_analytics(demo_id, version_a)
        b = analytics.demo_analytics(demo_id, version_b)
        return {"ok": True, "a": a, "b": b, "delta": {
            "completion_rate": round((b["completion_rate"] or 0)
                                     - (a["completion_rate"] or 0), 3),
            "avg_completion_pct": round(b["avg_completion_pct"]
                                        - a["avg_completion_pct"], 1),
            "avg_engaged_ms": b["avg_engaged_ms"] - a["avg_engaged_ms"],
            "sessions": b["sessions"] - a["sessions"],
        }}


    @mcp.tool(name="analytics.dropoff", description=(
        "Per-step reach / exits / dropoff% / replays — where viewers leave, "
        "step by step."), timeout=30.0)
    async def _analytics_dropoff(demo_id: str, version_id: str | None = None) -> dict:
        _own(store, demo_id)
        a = analytics.demo_analytics(demo_id, version_id)
        return {"ok": True, "demo_id": demo_id, "version_id": version_id,
                "steps": a["steps"], "completion_rate": a["completion_rate"]}


    # ---- sharing ------------------------------------------------------------

    @mcp.tool(name="demo.audit.live", description=(
        "Audit a hosted demo: deterministic structural checks PLUS live "
        "engagement analytics from real viewer sessions (never fabricated). "
        "Scores pacing, framing, narration, interaction clarity, CTA, mobile, "
        "branches, engagement; returns evidence-bearing findings ordered by "
        "impact."), timeout=30.0)
    async def _demo_audit(demo_id: str, version_id: str | None = None) -> dict:
        import json as _json
        from capturd.walk.audit import audit_spec
        _own(store, demo_id)
        demo = store.get_demo(demo_id)
        spec = _json.loads(demo["draft_spec"])
        live = None
        if version_id or store.get_published(demo_id):
            try:
                live = analytics.demo_analytics(demo_id, version_id)
            except Exception:                            # noqa: BLE001
                live = None
        return {"ok": True, **audit_spec(spec, analytics=live)}

    @mcp.tool(name="share.create", description=(
        "Get the public hosted URL for a published demo (unattributed)."), timeout=15.0)
    async def _share_create(demo_id: str) -> dict:
        _own(store, demo_id)
        if not store.get_published(demo_id):
            raise ValueError("demo has no published version — call demo.publish first")
        from app import config as _config
        return {"ok": True, "trackable": False,
                "url": f"{_config.BASE_URL.rstrip('/')}/pub/d/{demo_id}"}


    @mcp.tool(name="share.trackable", description=(
        "Mint a Frontman-attributed share URL for a published demo. The opaque "
        "token is Frontman's own (this is a facade — Captur'd never invents a "
        "second prospect-token system). Step-level behavior stays in Captur'd; "
        "Frontman receives only demo-open/return/complete/cta/branch."), timeout=30.0)
    async def _share_trackable(demo_id: str, contact_id: str = "",
                               name: str = "") -> dict:
        _own(store, demo_id)
        if not store.get_published(demo_id):
            raise ValueError("demo has no published version — call demo.publish first")
        from app import config as _config
        public_url = f"{_config.BASE_URL.rstrip('/')}/pub/d/{demo_id}"
        minted = frontman.mint_tracked_share(public_url, contact_id=contact_id,
                                             name=name)
        if not minted:
            return {"ok": True, "trackable": False, "attributed": False,
                    "url": public_url,
                    "note": "Frontman bridge unconfigured — unattributed share"}
        return {"ok": True, "trackable": True, "attributed": True,
                "url": minted.get("send_url") or public_url,
                "note": "attribution minted BY Frontman; Captur'd keeps detailed "
                        "events, Frontman gets only sales-significant signals"}

    return mcp
