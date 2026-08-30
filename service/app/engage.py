"""Engagement routes — public viewer-telemetry ingest + owner demo lifecycle.

Public surface (no auth, anti-enumeration by construction):

* POST /api/pub/events   — viewer event batches. Always answers 202 with the
  same body for known AND unknown demo ids; invalid events are dropped
  server-side; nothing about demo/token existence leaks through the response.
* GET  /api/pub/demos/{demo_id}/viewer — the published version spec for
  playback. Unknown demo, unpublished demo, stray token, no token: identical
  404 shape. No contact data is ever derivable from a token.

Owner surface (session-cookie auth):

* POST /api/demos                          create demo (draft)
* GET  /api/demos                          list own demos
* GET  /api/demos/{id}                     demo + draft + version list
* PUT  /api/demos/{id}/draft               save draft (published versions untouched)
* POST /api/demos/{id}/publish             freeze draft -> immutable version
* GET  /api/demos/{id}/versions            version history
* POST /api/demos/{id}/versions/{vid}/restore
* POST /api/demos/{id}/share               share.create / share.trackable (Frontman)
* GET  /api/demos/{id}/analytics           engagement analytics payload
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import analytics, config, frontman, store
from .auth import require_user

log = logging.getLogger("capturd.engage")

router = APIRouter()

_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_STEP_RE = re.compile(r"^step-\d{1,4}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_BATCH = 100
_MAX_EVENTS_PER_SESSION_HOUR = 5000


class EventBatch(BaseModel):
    sessionId: str = Field(min_length=8, max_length=64)
    demoId: str = Field(min_length=1, max_length=64)
    versionId: str | None = Field(default=None, max_length=64)
    events: list = Field(default_factory=list, max_length=_MAX_BATCH)  # junk tolerated, dropped in _clean_events
    deviceClass: str | None = Field(default=None, max_length=16)
    attributionToken: str | None = Field(default=None, max_length=128)
    source: dict | None = None


def _clean_events(raw_events: list[dict]) -> list[dict]:
    """Validate the capturd:event vocabulary + field bounds. Invalid entries
    are dropped (counted), never fatal."""
    out = []
    for ev in raw_events:
        if not isinstance(ev, dict):
            continue
        name = ev.get("event")
        if not isinstance(name, str) or not store.valid_event(name):
            continue
        clean: dict = {"event": name}
        step_id = ev.get("stepId")
        if isinstance(step_id, str) and _STEP_RE.match(step_id):
            clean["step_id"] = step_id
        for k in ("branchId", "choiceId", "ctaId"):
            v = ev.get(k)
            if v is None:
                continue
            if isinstance(v, str) and _ID_RE.match(v):
                clean[{"branchId": "branch_id", "choiceId": "choice_id",
                       "ctaId": "cta_id"}[k]] = v
        el = ev.get("elapsedMs")
        if isinstance(el, int) and 0 <= el <= 2**31 - 1:
            clean["elapsed_ms"] = el
        out.append(clean)
    return out


def _prior_signal_counts(session_id: str) -> dict[tuple, int]:
    """Per-session event counts BEFORE the current batch is stored, so the
    current batch's own events don't self-dedupe."""
    with sqlite3.connect(config.DB_PATH, timeout=30) as conn:
        cur = conn.execute(
            "SELECT event, choice_id, cta_id, COUNT(*) FROM demo_events "
            "WHERE session_id=? GROUP BY event, choice_id, cta_id",
            (session_id,))
        return {(r[0], r[1], r[2]): r[3] for r in cur.fetchall()}


def _fire_frontman_signals(session: dict, cleaned: list[dict],
                           prior: dict[tuple, int]) -> None:
    """Sales-significant signals only; deduped per session; fail-open."""
    if not session.get("attribution_token"):
        return
    token = session["attribution_token"]

    with sqlite3.connect(config.DB_PATH, timeout=30) as conn:
        prior_sessions = conn.execute(
            "SELECT COUNT(*) FROM viewer_sessions WHERE attribution_token=? AND id<>?",
            (token, session["id"])).fetchone()[0]

    seen_in_batch: list[tuple] = []
    for ev in cleaned:
        name = ev["event"]
        sig = None
        if name == "demo_open":
            # demo-open vs demo-return: this token's earlier sessions decide.
            sig = "demo-return" if prior_sessions else "demo-open"
        elif name == "demo_complete":
            sig = "demo-complete"
        elif name == "cta_click":
            sig = "demo-cta"
        elif name == "branch_select":
            sig = "demo-branch"

        if not sig:
            continue
        key = (name, ev.get("choice_id"), ev.get("cta_id"))
        already = prior.get(key, 0) + seen_in_batch.count(key)
        if already:
            continue                     # dedupe: once per session per action
        seen_in_batch.append(key)
        frontman.signal(token, sig, meta={
            "demo_id": session["demo_id"],
            "version_id": session.get("version_id"),
            "choice_id": ev.get("choice_id"),
            "cta_id": ev.get("cta_id"),
        })


# ---- public ---------------------------------------------------------------


@router.post("/api/pub/events", include_in_schema=False)
async def pub_events(batch: EventBatch):
    """Viewer telemetry ingest. Tolerant by design: analytics must never be in
    the prospect's way. Unknown demo ids and dropped events are indistinguishable
    in the response."""
    if not _SESSION_RE.match(batch.sessionId):
        raise HTTPException(status_code=400, detail="bad envelope")
    try:
        demo = store.get_demo(batch.demoId)
        if demo is None:
            # Existence-hiding: identical response, nothing stored.
            return {"ok": True}

        cleaned = _clean_events(batch.events)
        version_id = batch.versionId if (
            batch.versionId and _ID_RE.match(batch.versionId)) else None
        session = store.upsert_viewer_session(
            batch.sessionId, batch.demoId, version_id,
            device_class=(batch.deviceClass or "desktop"),
            source=batch.source,
            attribution_token=batch.attributionToken,
        )
        # Attribution binds ONCE (first batch wins); later batches can't swap it.
        if batch.attributionToken and session.get("attribution_token") \
                and session["attribution_token"] != batch.attributionToken:
            pass  # keep the original binding

        # Bounded per-session volume so one client can't flood the ledger.
        hour_ago = int(time.time()) - 3600
        with sqlite3.connect(config.DB_PATH, timeout=30) as conn:
            n_recent = conn.execute(
                "SELECT COUNT(*) FROM demo_events WHERE session_id=? AND at>=?",
                (batch.sessionId, hour_ago)).fetchone()[0]
        if n_recent < _MAX_EVENTS_PER_SESSION_HOUR and cleaned:
            prior = _prior_signal_counts(batch.sessionId)   # BEFORE insert
            store.record_events(session, cleaned)
            _fire_frontman_signals(session, cleaned, prior)
        return {"ok": True}
    except HTTPException:
        raise
    except Exception:                                    # noqa: BLE001 — fail-open
        log.exception("event ingest failed (swallowed)")
        return {"ok": True}


@router.get("/api/pub/demos/{demo_id}/viewer", include_in_schema=False)
async def pub_viewer(demo_id: str):
    """Published playback payload. Identical 404 for unknown/unpublished —
    the response never differs based on token or existence hints."""
    try:
        ver = store.get_published(demo_id)
        if not ver:
            raise HTTPException(status_code=404, detail="not_found")
        spec = json.loads(ver["spec_json"])
        return {
            "demoId": demo_id,
            "versionId": ver["id"],
            "versionNumber": ver["version_number"],
            "spec": spec,
        }
    except HTTPException:
        raise
    except Exception:                                    # noqa: BLE001
        log.exception("viewer payload failed")
        raise HTTPException(status_code=404, detail="not_found")


# ---- owner ----------------------------------------------------------------


class DemoCreate(BaseModel):
    title: str = Field(default="", max_length=200)
    spec: dict


class DraftUpdate(BaseModel):
    spec: dict


class ShareRequest(BaseModel):
    trackable: bool = False
    contact_id: str = Field(default="", max_length=128)
    name: str = Field(default="", max_length=120)
    channel: str = Field(default="capturd", max_length=24)


def _own_demo(request: Request, demo_id: str) -> dict:
    u = require_user(request)
    demo = store.get_demo(demo_id)
    if not demo or demo["owner_uid"] != u["id"]:
        raise HTTPException(status_code=404, detail="not_found")
    return demo


@router.post("/api/demos")
async def create_demo(request: Request, body: DemoCreate):
    u = require_user(request)
    spec_json = json.dumps(body.spec)
    demo = store.create_demo(u["id"], body.title, spec_json)
    return {"ok": True, "demo": demo}


@router.get("/api/demos")
async def list_demos(request: Request):
    u = require_user(request)
    return {"demos": store.list_demos(u["id"])}


@router.get("/api/demos/{demo_id}")
async def get_demo(request: Request, demo_id: str):
    demo = _own_demo(request, demo_id)
    demo["draft_spec"] = json.loads(demo["draft_spec"])
    demo["versions"] = store.list_versions(demo_id)
    return demo


@router.put("/api/demos/{demo_id}/draft")
async def put_draft(request: Request, demo_id: str, body: DraftUpdate):
    _own_demo(request, demo_id)
    ok = store.save_draft(demo_id, json.dumps(body.spec))
    return {"ok": ok}


@router.post("/api/demos/{demo_id}/publish")
async def publish(request: Request, demo_id: str):
    demo = _own_demo(request, demo_id)
    spec = json.loads(demo["draft_spec"])
    steps_count = len(spec.get("steps") or [])
    ver = store.publish_version(demo_id, demo["draft_spec"], steps_count)
    if not ver:
        raise HTTPException(status_code=500, detail="publish_failed")
    return {"ok": True, "version": ver}


@router.get("/api/demos/{demo_id}/versions")
async def versions(request: Request, demo_id: str):
    _own_demo(request, demo_id)
    return {"versions": store.list_versions(demo_id)}


@router.post("/api/demos/{demo_id}/versions/{version_id}/restore")
async def restore(request: Request, demo_id: str, version_id: str):
    _own_demo(request, demo_id)
    ok = store.restore_version_to_draft(demo_id, version_id)
    if not ok:
        raise HTTPException(status_code=404, detail="not_found")
    return {"ok": True}


@router.post("/api/demos/{demo_id}/share")
async def share(request: Request, demo_id: str, body: ShareRequest):
    """share.create (trackable=False) / share.trackable (trackable=True).

    Trackable shares are a FACADE over Frontman attribution: the opaque token
    is minted BY Frontman through the adapter. Captur'd never invents a
    competing prospect-token system."""
    _own_demo(request, demo_id)
    ver = store.get_published(demo_id)
    if not ver:
        raise HTTPException(status_code=409, detail="publish_first")
    public_url = f"{config.BASE_URL.rstrip('/')}/pub/d/{demo_id}"
    if not body.trackable:
        return {"ok": True, "url": public_url, "trackable": False}
    minted = frontman.mint_tracked_share(
        public_url, contact_id=body.contact_id, name=body.name,
        channel=body.channel or "capturd")
    if not minted:
        # Unattributed fallback — the demo still ships; attribution is opt-in.
        return {"ok": True, "url": public_url, "trackable": False,
                "attributed": False}
    # The prospect link is the CAPTUR'D pub URL carrying Frontman's opaque
    # token (?fm=). Frontman keeps its own card-side tracking; the viewer's
    # telemetry binds the token server-side and signals back through the
    # adapter. The token is opaque — no contact data ever rides with it.
    url = f"{public_url}?fm={minted['token']}"
    return {"ok": True, "url": url, "trackable": True, "attributed": True}


@router.get("/api/demos/{demo_id}/analytics")
async def demo_analytics_route(request: Request, demo_id: str, version_id: str | None = None):
    _own_demo(request, demo_id)
    return analytics.demo_analytics(demo_id, version_id)
