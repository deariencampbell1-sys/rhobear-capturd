"""Service-level proofs for the render-caps fix lane.

Covers two red-team findings against the REAL app (FastAPI TestClient + a
real SQLite DB), not proxies:

  * Item 2 (HIGH) — the Free=1 gate used to be check-here/increment-there across
    two transactions up to 600s apart, so N concurrent Free calls all read 0 and
    passed. ``try_acquire`` is now an atomic BEGIN IMMEDIATE reserve, proven with
    real OS threads racing the primitive. (W3-06e-r1 retired the free-vs-Pro
    generation split; the primitive stays as the load-protection seam.)
  * Item 1 (HIGH) — paid render had no cap on Pro. Now a per-account concurrency
    cap and a sliding hourly rate limit (429 + Retry-After), applied to Pro too.

The Stripe buy path (old item 3) is gone with the separate checkout
(GAPS-NOW W1/W6); its absence is pinned in tests/test_pro_retirement.py.

run_job / the SSRF resolver are mocked here only because this box has no
film.py rig and no network — the gates under test are the caps logic,
not the render or the SSRF guard (the latter has its own test_ssrf_guard.py).
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Make `app` (service/) and `capturd` (repo root) importable when pytest runs
# from the repo root.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "service"))
sys.path.insert(0, str(_REPO))


# ---------------------------------------------------------------------------
# Fixtures — temp DB + dirs, honest caps, an authed client
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.config as config
    import app.store as store

    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "JOBS_DIR", data / "jobs")
    monkeypatch.setattr(config, "DB_PATH", data / "capturd.sqlite3")
    monkeypatch.setattr(config, "RENDER_MAX_CONCURRENT", 2)
    monkeypatch.setattr(config, "RENDER_MAX_PER_HOUR", 100)
    store.init()
    return store


@pytest.fixture
def client(db, monkeypatch):
    from app import main as app_main
    from render_worker import JobResult

    # SSRF resolve needs DNS; the caps logic isn't about SSRF, so no-op it here.
    monkeypatch.setattr(app_main, "_reject_private_url", lambda url: None)
    # No film.py rig on this box — default the render to a clean 'done' so the
    # background task keeps the reserved slot. Tests that need a failure
    # override app_main.run_job themselves.
    monkeypatch.setattr(app_main, "run_job",
                        lambda spec, out_dir: JobResult(spec.job_id, "done",
                                                        output=str(out_dir / "x.mp4")))
    with TestClient(app_main.app) as c:
        yield c


def _authed(client, store, email: str, plan: str = "free") -> dict:
    u = store.upsert_user(email)
    if plan == "pro":
        store.set_plan(u["id"], "pro")
    tok = store.new_session(u["id"])
    client.cookies.set("capturd_session", tok)
    return u


def _ok_body(url: str = "https://example.com") -> dict:
    return {"url": url, "template": "saas-walkthrough", "name": "t"}


# ---------------------------------------------------------------------------
# Item 2 — Free=1 gate is race-proof (atomic reserve)
# ---------------------------------------------------------------------------


def test_free_reserve_atomic_under_concurrency(db):
    """The headline race proof: N threads reserve the same Free slot; exactly
    one wins. The old check-then-increment let all N through."""
    store = db
    uid = store.upsert_user("race@example.com")["id"]
    N = 12
    with ThreadPoolExecutor(max_workers=N) as ex:
        results = list(ex.map(
            lambda _: store.try_acquire(uid, "generation",
                                        limit=1, window_seconds=None),
            range(N)))
    assert results.count(True) == 1, f"expected exactly 1 winner, got {results.count(True)}"
    assert results.count(False) == N - 1
    assert store.usage_count(uid, "generation") == 1


def test_no_free_generation_cap(client, db):
    """The free-vs-Pro generation split is retired: a plan-less user's second
    render is accepted (the rate limit and concurrency cap still apply)."""
    store = db
    _authed(client, store, "free@example.com", plan="free")
    # first render accepted
    r1 = client.post("/api/generate", json=_ok_body())
    assert r1.status_code == 202
    # second render is no longer rejected at a Free lifetime limit
    r2 = client.post("/api/generate", json=_ok_body())
    assert r2.status_code == 202


def test_failed_render_does_not_lock_the_account_out(client, db, monkeypatch):
    """A failed render no longer has a per-plan slot to refund; the account can
    retry — no plan-level lockout exists."""
    from app import main as app_main
    from render_worker import JobResult

    store = db
    _authed(client, store, "retry@example.com", plan="free")
    # force the render to fail
    monkeypatch.setattr(app_main, "run_job",
                        lambda spec, out_dir: JobResult(spec.job_id, "failed", detail="boom"))
    r = client.post("/api/generate", json=_ok_body())
    assert r.status_code == 202
    uid = store.get_user_by_email("retry@example.com")["id"]
    # no plan-level generation slot exists to hold or refund
    assert store.usage_count(uid, "generation") == 0
    # …so the user can actually retry
    r2 = client.post("/api/generate", json=_ok_body())
    assert r2.status_code == 202


# ---------------------------------------------------------------------------
# Item 1 — concurrency cap (Pro too)
# ---------------------------------------------------------------------------


def test_concurrency_gate_atomic_under_concurrency(db):
    """N threads queue jobs under max_concurrent=2; exactly 2 win. Proves the
    count-and-insert is one transaction (two threads can't both read '1 in
    flight' and both queue a third)."""
    store = db
    uid = store.upsert_user("conc@example.com")["id"]
    N = 8
    with ThreadPoolExecutor(max_workers=N) as ex:
        results = list(ex.map(
            lambda _: store.try_queue_job(uid, "walk", max_concurrent=2),
            range(N)))
    queued = [r for r in results if r is not None]
    assert len(queued) == 2, f"expected exactly 2 queued, got {len(queued)}"
    assert len({*queued}) == 2  # distinct job ids


def test_concurrency_cap_429_with_retry_after_for_pro(client, db):
    """A Pro user at the in-flight cap gets 429 + Retry-After (Pro is NOT
    exempt). Pre-seed the cap's worth of queued jobs, then the next is rejected."""
    store = db
    u = _authed(client, store, "pro@example.com", plan="pro")
    # fill the in-flight cap (max_concurrent defaults to 2 in the `db` fixture)
    for i in range(2):
        store.record_job(f"seed-{i}", u["id"], "walk", "queued")
    r = client.post("/api/generate", json=_ok_body())
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert "in-flight" in r.json()["detail"]


def test_rate_limit_429_with_retry_after_for_pro(client, db, monkeypatch):
    """A Pro user over the hourly rate window gets 429 + Retry-After."""
    import app.config as config
    from app import main as app_main
    from render_worker import JobResult

    store = db
    u = _authed(client, store, "rate@example.com", plan="pro")
    monkeypatch.setattr(config, "RENDER_MAX_PER_HOUR", 1)
    monkeypatch.setattr(app_main, "run_job",
                        lambda spec, out_dir: JobResult(spec.job_id, "done",
                                                        output=str(out_dir / "x.mp4")))
    r1 = client.post("/api/generate", json=_ok_body())
    assert r1.status_code == 202
    r2 = client.post("/api/generate", json=_ok_body())
    assert r2.status_code == 429
    assert "Retry-After" in r2.headers
    assert int(r2.headers["Retry-After"]) >= 1
    assert "rate limit" in r2.json()["detail"]


def test_concurrency_reject_refunds_the_rate_reservation(client, db):
    """When a later gate rejects, earlier reservations are refunded — a 429'd
    request must not count toward the rate window (no render happened)."""
    store = db
    u = _authed(client, store, "conc-free@example.com", plan="free")
    # fill the in-flight cap so the next request is rejected at the concurrency gate
    for i in range(2):
        store.record_job(f"seed-{i}", u["id"], "walk", "queued")
    before = store.usage_count(u["id"], "render_request")
    r = client.post("/api/generate", json=_ok_body())
    assert r.status_code == 429
    assert "in-flight" in r.json()["detail"]
    after = store.usage_count(u["id"], "render_request")
    assert before == after, "429 must refund the render_request reservation"
