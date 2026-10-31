"""W3-06e-r1 — Pro retirement: every plan gets the features.

The paid-unlock path (``capturd.pro``, launch codes, signed licences, the
``activate`` command, the upgrade payload, the Pro display price and the
free-vs-Pro feature split) is deleted, not archived. This file is the
anti-resurrection guard and the behavior proof:

* it walks the tracked tree rather than a fixed file list, so a gate cannot
  come back in a file nobody remembered to update;
* it proves the three formerly gated MCP tools answer with their real result
  for every caller and that two of them changed payload SHAPE (not merely
  "did not raise");
* it pins the two load-protection gates the ruling keeps — the hourly rate
  limit and the per-account concurrency cap — by hitting each one;
* it proves ``/api/me`` no longer varies its feature surface by plan.

Both guard tests exclude this file from their own walk (it necessarily names
what they forbid, so it is the one file allowed to).
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client

REPO = Path(__file__).resolve().parents[1]

# The deleted unlock surface, named once. The two guard tests below scan every
# other tracked file for these; this file is the only allowed mention.
_FORBIDDEN_MODULE_TOKENS = ("capturd.pro", "is_pro", "require_pro", "PRO_CONFIG")
_FORBIDDEN_ENV_NAMES = (
    "CAPTURD_PRO_CODES",
    "CAPTURD_PRO_PUBKEY_B64",
    "CAPTURD_PRO_CHECKOUT_URL",
    "CAPTURD_PRO_PRICE",
    "RHOBEAR_CAPTURD_LICENSE",
)
_FORBIDDEN_USER_TEXT = (
    "upgrade", "checkout", "activate", "activation",
    "licence", "license", "pro_price", "pro_required", "$19",
)


def _tracked(*patterns: str) -> list[Path]:
    """Every git-tracked path matching *patterns* — a tree walk, not a list."""
    out = subprocess.run(
        ["git", "ls-files", "-z", *patterns],
        cwd=REPO, capture_output=True, text=True, check=True)
    return [REPO / p for p in out.stdout.split("\0") if p]


def _offenders(paths: list[Path], tokens: tuple[str, ...]) -> list[str]:
    """Report each token found in each path, excluding this guard file."""
    me = Path(__file__).resolve()
    hits: list[str] = []
    for path in paths:
        if path.resolve() == me:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in tokens:
            if token in text:
                hits.append(f"{path.relative_to(REPO)}: {token}")
    return hits


# ---------------------------------------------------------------------------
# Guard 1 — the module and its symbols are gone
# ---------------------------------------------------------------------------


def test_capturd_pro_is_gone():
    """``import capturd.pro`` fails and no tracked .py mentions the unlock."""
    proc = subprocess.run(
        [sys.executable, "-c", "import capturd.pro"],
        cwd=REPO, capture_output=True, text=True)
    assert proc.returncode != 0, "capturd.pro still imports — the module is deleted"
    assert "ModuleNotFoundError" in proc.stderr, proc.stderr

    hits = _offenders(_tracked("*.py"), _FORBIDDEN_MODULE_TOKENS)
    assert not hits, (
        "the Pro unlock survives in tracked Python — permanent deletion, not a "
        "flag:\n  " + "\n  ".join(hits))


# ---------------------------------------------------------------------------
# Guard 2 — the anti-resurrection scan for every unlock env name
# ---------------------------------------------------------------------------


def test_no_pro_or_licence_env_name_survives():
    """No tracked Python or shell file reads an unlock env name."""
    hits = _offenders(_tracked("*.py", "*.sh"), _FORBIDDEN_ENV_NAMES)
    assert not hits, (
        "a tracked code file reads a Pro-unlock environment name — the gate "
        "must not come back through the environment:\n  " + "\n  ".join(hits))


# ---------------------------------------------------------------------------
# The CLI: walk runs for anyone; activate is an unknown mode
# ---------------------------------------------------------------------------


def test_walk_runs_without_an_unlock(monkeypatch, tmp_path):
    """``capturd walk`` reaches its runner with no licence and no unlock env."""
    import capturd.walk.cli as walk_cli
    from capturd import cli

    for name in _FORBIDDEN_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.capturd/license here
    seen: list[list[str]] = []

    def fake_walk_main(argv):
        seen.append(list(argv))
        return 0

    monkeypatch.setattr(walk_cli, "main", fake_walk_main)
    rc = cli.main(["walk", "list"])
    assert rc == 0
    assert seen == [["list"]], "capturd walk must reach its runner with no unlock"


def test_activate_is_an_unknown_mode(capsys):
    """``capturd activate <code>`` is a mode error naming the modes that exist."""
    from capturd import cli

    rc = cli.main(["activate", "SOME-CODE"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "unknown mode 'activate'" in captured.err
    for mode in ("shots", "walk", "serve"):
        assert mode in captured.err
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# The three formerly gated MCP tools
# ---------------------------------------------------------------------------


class _StubRecorder:
    """Just enough recorder for demo.record's agent branch — no browser."""

    def __init__(self):
        self.finished = threading.Event()

    def agent_record(self):  # pragma: no cover - the tool's own thread calls it
        pass


class _StubForge:
    """Duck-typed DemoForge for the three formerly gated tools."""

    def start_recording(self, payload):
        return _StubRecorder(), "stub-session", "agent"

    async def stylize_demo(self, demo_id, style):
        return {"style": style}

    async def regenerate_step(self, demo_id, step_index, aspects):
        return {"stepIndex": step_index, "aspects": aspects}


def _call(server, tool: str, args: dict | None = None):
    """Call an MCP tool in-process; return its structured result."""

    async def go():
        async with Client(server) as client:
            result = await client.call_tool(tool, args or {})
            return result.structured_content

    return asyncio.run(go())


def _build_stub_server(monkeypatch):
    from capturd.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "_reject_private_url", lambda url: None)
    return mcp_server._build_server(forge=_StubForge())


def test_the_three_gated_tools_answer_for_every_plan(monkeypatch, tmp_path):
    """demo.record / demo.stylize / demo.regenerate return their real result.

    A leftover ``~/.capturd/license`` on disk changes nothing, and the
    formerly gated tools change payload SHAPE — not merely "no error raised".
    """
    lic_dir = tmp_path / ".capturd"
    lic_dir.mkdir()
    (lic_dir / "license").write_text("OLD-LICENCE-VALUE", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in _FORBIDDEN_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    server = _build_stub_server(monkeypatch)

    record = _call(server, "demo.record",
                   {"url": "https://example.com", "name": "t", "goal": "g"})
    assert record.get("error") != "pro_required", record
    assert set(record) == {"sessionId", "mode", "message"}, record

    stylize = _call(server, "demo.stylize", {"demo_id": "d", "style": "smooth"})
    assert stylize.get("error") != "pro_required", stylize
    assert stylize == {"ok": True, "style": "smooth"}, stylize

    regenerate = _call(server, "demo.regenerate",
                       {"demo_id": "d", "step_index": 0, "aspects": ["cursor"]})
    assert regenerate.get("error") != "pro_required", regenerate
    assert regenerate == {"ok": True, "stepIndex": 0, "aspects": ["cursor"]}, regenerate


def test_a_stale_operator_environment_is_ignored(monkeypatch, tmp_path):
    """A leftover unlock export is not read and breaks nothing."""
    monkeypatch.setenv(_FORBIDDEN_ENV_NAMES[0], "STALE-CODE")
    monkeypatch.setenv(_FORBIDDEN_ENV_NAMES[-1], "STALE-LICENCE")
    monkeypatch.setenv("HOME", str(tmp_path))
    server = _build_stub_server(monkeypatch)

    record = _call(server, "demo.record",
                   {"url": "https://example.com", "name": "t", "goal": "g"})
    assert record.get("error") != "pro_required", record


# ---------------------------------------------------------------------------
# No user-facing paywall text
# ---------------------------------------------------------------------------


def _assert_no_paywall_text(payload, where: str) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True)
    lowered = text.lower()
    hits = [word for word in _FORBIDDEN_USER_TEXT if word in lowered]
    assert not hits, f"{where} still shows paywall text {hits}: {text}"


def test_no_upgrade_or_checkout_text_a_user_can_see(service, monkeypatch):
    """No service answer, MCP result or CLI help mentions the paid unlock."""
    from capturd import cli

    client, store = service
    user = store.upsert_user("text@example.com")
    client.cookies.set("capturd_session", store.new_session(user["id"]))

    _assert_no_paywall_text(client.get("/healthz").json(), "/healthz")
    _assert_no_paywall_text(client.get("/api/me").json(), "/api/me")

    server = _build_stub_server(monkeypatch)
    _assert_no_paywall_text(
        _call(server, "demo.record",
              {"url": "https://example.com", "name": "t", "goal": "g"}),
        "demo.record")
    _assert_no_paywall_text(
        _call(server, "demo.stylize", {"demo_id": "d", "style": "smooth"}),
        "demo.stylize")
    _assert_no_paywall_text(
        _call(server, "demo.regenerate",
              {"demo_id": "d", "step_index": 0, "aspects": ["cursor"]}),
        "demo.regenerate")

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert cli.main(["--help"]) == 0
    _assert_no_paywall_text(output.getvalue(), "capturd --help")


# ---------------------------------------------------------------------------
# The two load-protection gates the ruling keeps
# ---------------------------------------------------------------------------


def _ok_body() -> dict:
    return {"url": "https://example.com", "template": "saas-walkthrough", "name": "t"}


def test_the_rate_limit_still_bites(service, monkeypatch):
    """The hourly render cap still answers 429 + Retry-After for any plan."""
    import app.config as config

    client, store = service
    monkeypatch.setattr(config, "RENDER_MAX_PER_HOUR", 1)
    user = store.upsert_user("rate@example.com")
    client.cookies.set("capturd_session", store.new_session(user["id"]))

    first = client.post("/api/generate", json=_ok_body())
    assert first.status_code == 202, first.text
    second = client.post("/api/generate", json=_ok_body())
    assert second.status_code == 429, second.text
    assert "Retry-After" in second.headers
    assert "rate limit" in second.json()["detail"]


def test_the_concurrency_cap_still_bites(service):
    """The per-account in-flight cap still answers 429 + Retry-After."""
    client, store = service
    user = store.upsert_user("concurrency@example.com")
    client.cookies.set("capturd_session", store.new_session(user["id"]))
    # fill the in-flight cap (RENDER_MAX_CONCURRENT = 2 in the fixture)
    for i in range(2):
        store.record_job(f"seed-{i}", user["id"], "walk", "queued")

    response = client.post("/api/generate", json=_ok_body())
    assert response.status_code == 429, response.text
    assert "Retry-After" in response.headers
    assert "in-flight" in response.json()["detail"]


# ---------------------------------------------------------------------------
# /api/me — one feature surface for every plan
# ---------------------------------------------------------------------------


def test_a_user_with_no_plan_gets_the_same_features(service):
    """The /api/me answer no longer varies its features by plan."""
    client, store = service
    free = store.upsert_user("free-plan@example.com")
    pro = store.upsert_user("pro-plan@example.com")
    store.set_plan(pro["id"], "pro")

    client.cookies.set("capturd_session", store.new_session(free["id"]))
    free_me = client.get("/api/me").json()
    client.cookies.set("capturd_session", store.new_session(pro["id"]))
    pro_me = client.get("/api/me").json()

    def features(answer: dict) -> dict:
        # email/plan are identity labels and mcp_endpoint carries the per-user
        # token; everything else is the feature surface and must be identical.
        shape = {k: v for k, v in answer.items() if k not in ("email", "plan")}
        if shape.get("mcp_endpoint"):
            shape["mcp_endpoint"] = "present"
        return shape

    assert features(free_me) == features(pro_me), (free_me, pro_me)
    assert free_me["mcp_endpoint"], "every plan gets an MCP endpoint"
    assert pro_me["mcp_endpoint"]
    assert "pro_price" not in free_me and "pro_price" not in pro_me

    # The advertised endpoint is not a paid gate either: the proxy refuses
    # only an invalid token, never a plan.
    token = store.mcp_token_for(free["id"])
    proxied = client.post(f"/mcp/{token}", json={})
    assert proxied.status_code != 402, proxied.text


# ---------------------------------------------------------------------------
# Fixtures — the real hosted app on a temp DB, render rig stubbed
# ---------------------------------------------------------------------------


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(REPO / "service"))
    import app.config as config
    import app.store as store

    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "JOBS_DIR", data / "jobs")
    monkeypatch.setattr(config, "DB_PATH", data / "capturd.sqlite3")
    monkeypatch.setattr(config, "RENDER_MAX_CONCURRENT", 2)
    monkeypatch.setattr(config, "RENDER_MAX_PER_HOUR", 100)
    store.init()

    import app.main as app_main
    from render_worker import JobResult

    monkeypatch.setattr(app_main, "_reject_private_url", lambda url: None)
    monkeypatch.setattr(app_main, "_MCP_UPSTREAM", "http://127.0.0.1:1")
    monkeypatch.setattr(
        app_main, "run_job",
        lambda spec, out_dir: JobResult(spec.job_id, "done",
                                        output=str(out_dir / "x.mp4")))
    with TestClient(app_main.app) as client:
        yield client, store
