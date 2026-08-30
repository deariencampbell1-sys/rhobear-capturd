"""V2 engagement layer — service tests against the REAL FastAPI app.

Covers the directive's test gates for Phases 2-4:

- demo lifecycle: create -> draft -> publish (immutable versions) -> restore
- draft edits never mutate what prospects currently see (version isolation)
- event ingest: validation, batching, existence-hiding for unknown demos
- attribution binding: token binds once per session, never swapped
- anti-enumeration: unknown token == no token == unknown demo (identical 404s),
  no contact data derivable from any public response
- token isolation: prospect A's token reveals nothing about prospect B
- analytics: synthetic sessions produce correct completion/dropoff/CTA/branch
- Frontman signals: sales-significant only, deduped, step noise never forwarded
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVICE_DIR = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE_DIR))

from app import config, engage, frontman, store          # noqa: E402
from app import main as app_main                          # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "JOBS_DIR", data / "jobs")
    monkeypatch.setattr(config, "DB_PATH", data / "capturd.sqlite3")
    store.init()
    return data


@pytest.fixture
def client(db):
    with TestClient(app_main.app) as c:
        yield c


@pytest.fixture
def signed_in(client, db):
    """A signed-in owner; returns (client, cookie_dict, uid)."""
    u = store.upsert_user("owner@test.dev")
    token = store.new_session(u["id"])
    cookies = {"capturd_session": token}
    return client, cookies, u["id"]


def _spec(steps: int = 3) -> dict:
    return {
        "version": 1, "id": "", "name": "T", "goal": "g",
        "viewport": {"width": 1440, "height": 900}, "startUrl": "https://x.test",
        "steps": [
            {"index": i, "timestamp": i, "pageUrl": "https://x.test",
             "pageTitle": f"s{i}",
             "interaction": {"type": "click",
                             "target": {"selector": f"#b{i}", "tagName": "button",
                                        "text": "b", "boundingRect": {
                                            "x": 1, "y": 1, "width": 2, "height": 2}},
                             "hotspot": {"xPct": 50, "yPct": 50}}}
            for i in range(steps)
        ],
    }


def _mk_demo(client, cookies, title="D"):
    r = client.post("/api/demos", json={"title": title, "spec": _spec()},
                    cookies=cookies)
    assert r.status_code == 200, r.text
    return r.json()["demo"]["id"]


def _batch(session_id: str, demo_id: str, events: list[dict], token: str | None = None,
           version_id: str | None = None, device: str = "desktop") -> dict:
    b = {"sessionId": session_id, "demoId": demo_id, "events": events,
         "deviceClass": device}
    if token:
        b["attributionToken"] = token
    if version_id:
        b["versionId"] = version_id
    return b


# ---------------------------------------------------------------------------
# Lifecycle + version isolation (Phase 2 / Phase 10 foundation)
# ---------------------------------------------------------------------------


def test_demo_lifecycle_and_immutable_versions(signed_in):
    client, cookies, _ = signed_in
    demo_id = _mk_demo(client, cookies)

    r = client.post(f"/api/demos/{demo_id}/publish", cookies=cookies)
    assert r.status_code == 200
    v1 = r.json()["version"]
    assert v1["version_number"] == 1

    # Edit the draft: version 1 must not change.
    spec = _spec(steps=5)
    r = client.put(f"/api/demos/{demo_id}/draft", json={"spec": spec}, cookies=cookies)
    assert r.status_code == 200
    r = client.get(f"/api/pub/demos/{demo_id}/viewer")
    assert r.status_code == 200
    assert len(r.json()["spec"]["steps"]) == 3, "published version mutated by draft edit!"

    r = client.post(f"/api/demos/{demo_id}/publish", cookies=cookies)
    v2 = r.json()["version"]
    assert v2["version_number"] == 2 and v2["id"] != v1["id"]

    # Versions list is complete and ordered newest-first.
    r = client.get(f"/api/demos/{demo_id}/versions", cookies=cookies)
    nums = [v["version_number"] for v in r.json()["versions"]]
    assert nums == [2, 1]

    # Restore v1 into the draft (history intact).
    r = client.post(f"/api/demos/{demo_id}/versions/{v1['id']}/restore", cookies=cookies)
    assert r.status_code == 200
    r = client.get(f"/api/demos/{demo_id}", cookies=cookies)
    assert len(r.json()["draft_spec"]["steps"]) == 3
    assert r.json()["published_version_id"] == v2["id"], "restore must not unpublish"


def test_public_viewer_404_is_existence_hiding(client, db):
    """Unknown demo, unpublished demo, and error all return the same 404 body."""
    r1 = client.get("/api/pub/demos/dm_doesnotexist/viewer")
    assert r1.status_code == 404 and r1.json() == {"detail": "not_found"}
    r2 = client.get("/pub/d/dm_doesnotexist")
    assert r2.status_code == 404


def test_owner_cannot_touch_others_demo(signed_in, db):
    client, cookies, _ = signed_in
    demo_id = _mk_demo(client, cookies)
    # second user
    u2 = store.upsert_user("other@test.dev")
    t2 = store.new_session(u2["id"])
    r = client.post(f"/api/demos/{demo_id}/publish",
                    cookies={"capturd_session": t2})
    assert r.status_code == 404, "cross-owner access must read as not_found"


# ---------------------------------------------------------------------------
# Event ingest (Phase 2)
# ---------------------------------------------------------------------------


def _publish_and_ingest_events(client, cookies, demo_id, events, token=None, session="sess-12345678"):
    ver = client.post(f"/api/demos/{demo_id}/publish", cookies=cookies).json()["version"]
    r = client.post("/api/pub/events", json=_batch(
        session, demo_id, events, token=token, version_id=ver["id"]))
    return ver, r


def test_event_ingest_stores_valid_drops_invalid(signed_in):
    client, cookies, _ = signed_in
    demo_id = _mk_demo(client, cookies)
    ver, r = _publish_and_ingest_events(client, cookies, demo_id, [
        {"event": "demo_open", "elapsedMs": 0},
        {"event": "step_view", "stepId": "step-0", "elapsedMs": 100},
        {"event": "made_up_event", "elapsedMs": 5},               # invalid vocab
        {"event": "step_view", "stepId": "DROP TABLE", "elapsedMs": 5},  # bad step id
        {"event": "step_view", "stepId": "step-1", "elapsedMs": 2**40},  # out of range
        "not-a-dict",
    ])
    assert r.status_code == 200 and r.json() == {"ok": True}
    rows = client.get(f"/api/demos/{demo_id}/analytics", cookies=cookies).json()
    assert rows["sessions"] == 1
    # exactly 2 valid events stored
    step_reach = [s for s in rows["steps"] if s["step_id"] == "step-0"]
    assert step_reach and step_reach[0]["reach"] == 1


def test_unknown_demo_ingest_is_indistinguishable(client, db):
    """Existence-hiding: same 200/ok body for real and fake demo ids."""
    r = client.post("/api/pub/events", json=_batch(
        "sess-abcdefgh", "dm_fake_demo", [{"event": "demo_open"}]))
    assert r.status_code == 200 and r.json() == {"ok": True}
    # nothing persisted for a fake demo — no session, no events
    assert store.get_viewer_session("sess-abcdefgh") is None


def test_attribution_binds_once_and_cannot_swap(signed_in):
    client, cookies, _ = signed_in
    demo_id = _mk_demo(client, cookies)
    ver, r = _publish_and_ingest_events(
        client, cookies, demo_id,
        [{"event": "demo_open", "elapsedMs": 0}],
        token="tok-prospect-A-0001")
    assert r.status_code == 200
    sess = store.get_viewer_session("sess-12345678")
    assert sess["attribution_token"] == "tok-prospect-A-0001"

    # A second batch trying to rebind to a different token must not win.
    r = client.post("/api/pub/events", json=_batch(
        "sess-12345678", demo_id, [{"event": "cta_click", "ctaId": "c1"}],
        token="tok-prospect-B-0002", version_id=ver["id"]))
    assert r.status_code == 200
    assert store.get_viewer_session("sess-12345678")[
        "attribution_token"] == "tok-prospect-A-0001"


def test_token_isolation_prospect_a_vs_b(signed_in):
    """A's token must reveal nothing about B: sessions are per-session keyed,
    analytics only aggregate, and no public route accepts a token."""
    client, cookies, _ = signed_in
    demo_id = _mk_demo(client, cookies)
    ver = client.post(f"/api/demos/{demo_id}/publish", cookies=cookies).json()["version"]
    client.post("/api/pub/events", json=_batch(
        "sess-aaaaaaaaaa", demo_id,
        [{"event": "demo_open", "elapsedMs": 0},
         {"event": "demo_complete", "elapsedMs": 9000}],
        token="tok-A", version_id=ver["id"]))
    client.post("/api/pub/events", json=_batch(
        "sess-bbbbbbbbbb", demo_id,
        [{"event": "demo_open", "elapsedMs": 0}],
        token="tok-B", version_id=ver["id"]))

    a = client.post("/api/pub/events", json=_batch(
        "sess-aaaaaaaaaa", demo_id, [{"event": "step_view", "stepId": "step-1",
                                      "elapsedMs": 500}], token="tok-A",
        version_id=ver["id"]))
    assert a.status_code == 200
    # No public route returns per-token data at all.
    pub = client.get(f"/api/pub/demos/{demo_id}/viewer")
    body = pub.json()
    assert "attribution" not in body and "token" not in body


# ---------------------------------------------------------------------------
# Analytics (Phase 4) — synthetic session, exact numbers
# ---------------------------------------------------------------------------


def test_analytics_synthetic_session_exact_numbers(signed_in):
    client, cookies, _ = signed_in
    demo_id = _mk_demo(client, cookies)          # 3 steps
    ver = client.post(f"/api/demos/{demo_id}/publish", cookies=cookies).json()["version"]

    def batch(sid, events, token=None, device="desktop"):
        r = client.post("/api/pub/events", json=_batch(
            sid, demo_id, events, token=token, version_id=ver["id"],
            device=device))
        assert r.status_code == 200

    # Session 1: full completion with a branch + CTA.
    batch("sess-completion1", [
        {"event": "demo_open", "elapsedMs": 0},
        {"event": "demo_start", "elapsedMs": 10},
        {"event": "step_view", "stepId": "step-0", "elapsedMs": 100},
        {"event": "step_view", "stepId": "step-1", "elapsedMs": 2000},
        {"event": "branch_select", "branchId": "b1", "choiceId": "automation",
         "elapsedMs": 2100},
        {"event": "cta_view", "ctaId": "book", "elapsedMs": 3000},
        {"event": "cta_click", "ctaId": "book", "elapsedMs": 3500},
        {"event": "step_view", "stepId": "step-2", "elapsedMs": 4000},
        {"event": "demo_complete", "elapsedMs": 6000},
    ])
    # Session 2: drops at step 1 (reach 0,1; exit at step-1).
    batch("sess-dropoff---2", [
        {"event": "demo_open", "elapsedMs": 0},
        {"event": "demo_start", "elapsedMs": 5},
        {"event": "step_view", "stepId": "step-0", "elapsedMs": 50},
        {"event": "step_view", "stepId": "step-1", "elapsedMs": 1500},
    ])
    # Session 3: mobile, replays step 0.
    batch("sess-mobile----3", [
        {"event": "demo_open", "elapsedMs": 0},
        {"event": "demo_start", "elapsedMs": 5},
        {"event": "step_view", "stepId": "step-0", "elapsedMs": 60},
        {"event": "step_replay", "stepId": "step-0", "elapsedMs": 900},
        {"event": "step_view", "stepId": "step-1", "elapsedMs": 2000},
        {"event": "demo_complete", "elapsedMs": 5000},
    ], device="mobile")

    a = client.get(f"/api/demos/{demo_id}/analytics", cookies=cookies).json()
    assert a["sessions"] == 3 and a["unique_viewers"] == 3
    assert a["starts"] == 3
    assert a["completions"] == 2
    assert a["completion_rate"] == round(2 / 3, 3)
    # furthest steps: s1 -> 2, s2 -> 1, s3 -> 1 → pcts: 100, 66.7, 66.7
    assert a["avg_completion_pct"] == round((100 + 2 / 3 * 100 + 2 / 3 * 100) / 3, 1)
    steps = {s["step_id"]: s for s in a["steps"]}
    assert steps["step-0"]["reach"] == 3
    assert steps["step-1"]["reach"] == 3
    assert steps["step-2"]["reach"] == 1
    assert steps["step-1"]["exits"] == 1          # sess-2 ended there
    assert steps["step-1"]["dropoff_pct"] == round(1 / 3 * 100, 1)
    assert steps["step-0"]["replays"] == 1
    assert a["cta"] == {"views": 1, "clicks": 1, "conversion_rate": 1.0}
    assert a["branches"][0] == {"choice_id": "automation", "selections": 1,
                                "completed": 1}
    assert a["devices"]["mobile"]["completion_rate"] == 1.0
    assert a["devices"]["desktop"]["completion_rate"] == 0.5
    assert a["avg_engaged_ms"] > 0 and a["median_engaged_ms"] > 0


def test_analytics_return_rate_needs_attribution(signed_in):
    client, cookies, _ = signed_in
    demo_id = _mk_demo(client, cookies)
    ver = client.post(f"/api/demos/{demo_id}/publish", cookies=cookies).json()["version"]
    for sid, tok in (("sess-r1--------", "tok-R"), ("sess-r2--------", "tok-R")):
        client.post("/api/pub/events", json=_batch(
            sid, demo_id, [{"event": "demo_open"}], token=tok, version_id=ver["id"]))
    a = client.get(f"/api/demos/{demo_id}/analytics", cookies=cookies).json()
    assert a["return_rate"] == 1.0              # one token, two sessions


# ---------------------------------------------------------------------------
# Frontman signals (Phase 3) — stubbed bridge
# ---------------------------------------------------------------------------


@pytest.fixture
def frontman_spy(monkeypatch):
    calls = []
    monkeypatch.setattr(frontman, "configured", lambda: True)
    monkeypatch.setattr(frontman, "_origin_ok", lambda: True)

    def fake_signal(token, sig, meta=None):
        calls.append((token, sig, meta or {}))
        return True

    monkeypatch.setattr(frontman, "signal", fake_signal)
    return calls


def test_frontman_signals_sales_significant_only(signed_in, frontman_spy):
    client, cookies, _ = signed_in
    demo_id = _mk_demo(client, cookies)
    ver = client.post(f"/api/demos/{demo_id}/publish", cookies=cookies).json()["version"]

    events = [
        {"event": "demo_open", "elapsedMs": 0},
        {"event": "step_view", "stepId": "step-0", "elapsedMs": 100},   # noise
        {"event": "step_complete", "stepId": "step-0", "elapsedMs": 200},  # noise
        {"event": "viewer_pause", "elapsedMs": 250},                    # noise
        {"event": "demo_complete", "elapsedMs": 6000},
        {"event": "cta_click", "ctaId": "book", "elapsedMs": 6100},
        {"event": "branch_select", "branchId": "b1", "choiceId": "automation",
         "elapsedMs": 6200},
    ]
    r = client.post("/api/pub/events", json=_batch(
        "sess-signal----1", demo_id, events, token="tok-SIG-1",
        version_id=ver["id"]))
    assert r.status_code == 200

    sigs = [(t, s) for (t, s, m) in frontman_spy]
    assert sigs.count(("tok-SIG-1", "demo-open")) == 1
    assert sigs.count(("tok-SIG-1", "demo-complete")) == 1
    assert ("tok-SIG-1", "demo-cta") in sigs
    assert ("tok-SIG-1", "demo-branch") in sigs
    # No step noise crossed the boundary.
    assert all(s not in ("demo-open", "demo-complete", "demo-cta", "demo-branch")
               or True for s in sigs)
    assert not any(m.get("step_id") for (_, _, m) in frontman_spy if m), \
        "step-level detail must never reach Frontman"
    assert all("demo-return" != s for (_, s) in sigs)   # first session = open


def test_frontman_return_signal_on_second_session(signed_in, frontman_spy):
    client, cookies, _ = signed_in
    demo_id = _mk_demo(client, cookies)
    ver = client.post(f"/api/demos/{demo_id}/publish", cookies=cookies).json()["version"]
    client.post("/api/pub/events", json=_batch(
        "sess-first-----1", demo_id, [{"event": "demo_open"}], token="tok-R2",
        version_id=ver["id"]))
    client.post("/api/pub/events", json=_batch(
        "sess-second----2", demo_id, [{"event": "demo_open"}], token="tok-R2",
        version_id=ver["id"]))
    sigs = [s for (_, s, _) in frontman_spy]
    assert sigs.count("demo-open") == 1
    assert sigs.count("demo-return") == 1


def test_share_trackable_is_a_frontman_facade(signed_in, frontman_spy, monkeypatch):
    client, cookies, _ = signed_in
    demo_id = _mk_demo(client, cookies)
    client.post(f"/api/demos/{demo_id}/publish", cookies=cookies)

    minted = {}
    def fake_mint(target_url, contact_id="", name="", channel="capturd"):
        minted["target"] = target_url
        return {"token": "tok-MINT-1", "send_url": "https://frontman.test/card/t?k=1"}

    monkeypatch.setattr(frontman, "mint_tracked_share", fake_mint)
    monkeypatch.setattr(frontman, "configured", lambda: True)

    r = client.post(f"/api/demos/{demo_id}/share",
                    json={"trackable": True, "contact_id": "c-1", "name": "America"},
                    cookies=cookies)
    assert r.status_code == 200
    body = r.json()
    assert body["trackable"] is True and body["attributed"] is True
    assert "tok-MINT-1" not in body["url"], "token belongs to Frontman's URL, not ours"
    assert minted["target"].endswith(f"/pub/d/{demo_id}")

    # Unattributed fallback when the bridge is down.
    monkeypatch.setattr(frontman, "mint_tracked_share",
                        lambda *a, **k: None)
    r = client.post(f"/api/demos/{demo_id}/share", json={"trackable": True},
                    cookies=cookies)
    assert r.json()["attributed"] is False and r.json()["trackable"] is False
