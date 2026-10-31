"""W3-06d — Captur'd states its own engine contract on both surfaces.

The engine manifest is the single source of truth for engine id, engine
version, contract version and artifact kinds. This file proves the two
surfaces Captur'd exposes — the MCP server and the hosted service — answer
with that manifest, through ``capturd.engine.load_manifest`` only:

* one read-only MCP tool (``engine.contract``), never Pro-gated;
* the service's ``/healthz`` answer carries the same object under
  ``engine_contract``;
* a manifest handed through the injected bytes seam reaches both surfaces,
  so neither may grow its own parse;
* a broken manifest is a typed error on the tool and a reported failure in
  health, never a partial answer and never a downed ``/healthz``.

Nothing here writes to disk: the manifest bytes go in through
``load_manifest(data=...)`` (the W3-06a seam).
"""

from __future__ import annotations

import asyncio
import json
import sys
import tomllib
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from capturd import engine as engine_mod
from capturd.mcp import server as mcp_server

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "service"))
sys.path.insert(0, str(_REPO))

from app import main as app_main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TOOL = "engine.contract"
MANIFEST_FILE = _REPO / "capturd" / "engine" / "manifest.json"


def _payload(manifest: dict | None = None) -> bytes:
    """Bytes for the seam: the packaged manifest, optionally overridden."""
    data = manifest if manifest is not None else engine_mod.load_manifest()
    return json.dumps(data).encode("utf-8")


def _call(server, tool: str = TOOL, args: dict | None = None) -> dict:
    """Call an MCP tool in-process; return its structured result."""

    async def go():
        async with Client(server) as client:
            result = await client.call_tool(tool, args or {})
            return result.structured_content

    return asyncio.run(go())


def _call_error(server, tool: str = TOOL, args: dict | None = None) -> str:
    """Call an MCP tool expecting a refusal; return the error text."""

    async def go():
        async with Client(server) as client:
            try:
                await client.call_tool(tool, args or {})
            except ToolError as exc:
                return str(exc)
        raise AssertionError(f"expected {tool} to be refused")

    return asyncio.run(go())


@pytest.fixture(autouse=True)
def _cold_contract_cache():
    """Both surfaces cache the manifest per process; start every test cold."""

    def clear():
        for module in (mcp_server, app_main):
            cache = getattr(module, "_manifest_for", None)
            if cache is not None:
                cache.cache_clear()

    clear()
    yield
    clear()


@pytest.fixture
def server():
    return mcp_server._build_server()


@pytest.fixture
def service(tmp_path, monkeypatch):
    """The real app on a temp DB, so lifespan never touches the host tree."""
    monkeypatch.setattr(app_main.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(app_main.config, "JOBS_DIR", tmp_path / "data" / "jobs")
    monkeypatch.setattr(app_main.config, "DB_PATH", tmp_path / "data" / "capturd.sqlite3")
    with TestClient(app_main.app) as client:
        yield client


# ---------------------------------------------------------------------------
# 1-2 — both surfaces report the manifest
# ---------------------------------------------------------------------------

def test_mcp_reports_the_manifest(server):
    answer = _call(server)
    on_disk = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    assert set(answer["manifest"]) == set(on_disk)
    assert answer["manifest"] == on_disk
    assert answer["contractVersion"] == on_disk["contractVersion"]
    assert answer["manifestPath"] == str(engine_mod._MANIFEST_PATH)
    assert Path(answer["manifestPath"]).name == "manifest.json"


def test_service_reports_the_same_contract(server, service):
    mcp_answer = _call(server)
    response = service.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["engine_contract"] == mcp_answer
    # /healthz keeps every field its current consumers read
    assert "billing_configured" in body and "edition" in body


# ---------------------------------------------------------------------------
# 3 — anti-drift: both surfaces go through the one loader seam
# ---------------------------------------------------------------------------

def test_both_surfaces_read_through_one_loader(monkeypatch, server, service):
    injected = {
        **engine_mod.load_manifest(),
        "engineVersion": "9.9.9-injected",
        "displayName": "Injected Captur'd",
    }
    payload = _payload(injected)
    monkeypatch.setattr(mcp_server, "_MANIFEST_BYTES", payload)
    monkeypatch.setattr(app_main, "_MANIFEST_BYTES", payload)

    mcp_answer = _call(server)
    service_answer = service.get("/healthz").json()["engine_contract"]

    assert mcp_answer["manifest"] == injected
    assert service_answer == mcp_answer, (
        "the two surfaces disagree on an injected manifest — one of them "
        "grew its own parse instead of calling the one loader")


# ---------------------------------------------------------------------------
# 4 — a broken manifest is a typed error, never a partial answer
# ---------------------------------------------------------------------------

def test_a_broken_manifest_is_a_typed_error_not_a_partial_answer(
        monkeypatch, server, service):
    bad = b'{"schemaVersion": 1, "contractVersion": '  # truncated JSON
    monkeypatch.setattr(mcp_server, "_MANIFEST_BYTES", bad)
    monkeypatch.setattr(app_main, "_MANIFEST_BYTES", bad)

    with pytest.raises(engine_mod.ManifestError):
        mcp_server.engine_contract()
    error = _call_error(server)
    assert "manifest" in error.lower()

    response = service.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True                     # health survives
    contract = body["engine_contract"]
    assert contract["ok"] is False                # failure is reported
    assert contract["error"] == "ManifestError"
    assert "manifest" in contract["detail"].lower()


# ---------------------------------------------------------------------------
# 5 — not hardcoded: different injected bytes give different answers
# ---------------------------------------------------------------------------

def test_the_manifest_is_not_hardcoded(monkeypatch, server, service):
    versions = ("111.0.0", "222.0.0")
    seen = []
    for version in versions:
        payload = _payload({**engine_mod.load_manifest(), "engineVersion": version})
        monkeypatch.setattr(mcp_server, "_MANIFEST_BYTES", payload)
        monkeypatch.setattr(app_main, "_MANIFEST_BYTES", payload)
        mcp_answer = _call(server)
        service_answer = service.get("/healthz").json()["engine_contract"]
        assert mcp_answer == service_answer
        seen.append(service_answer["manifest"]["engineVersion"])
    assert seen == list(versions)


# ---------------------------------------------------------------------------
# 6-7 — the extension changes nothing existing and is never Pro-gated
# ---------------------------------------------------------------------------

def test_an_existing_tool_is_unchanged(tmp_path):
    from capturd.walk.coordinator import DemoForge

    forge = DemoForge(demos_dir=tmp_path / "demos")
    server = mcp_server._build_server(forge=forge)
    assert _call(server, "demo.list") == {"demos": [], "count": 0}


def test_the_contract_tool_is_not_pro_gated(monkeypatch, server, service):
    monkeypatch.setattr(mcp_server, "is_pro", lambda: False)
    mcp_answer = _call(server)
    service_answer = service.get("/healthz").json()["engine_contract"]
    assert mcp_answer.get("error") != "pro_required"
    assert mcp_answer["manifest"]["engine"] == "capturd"
    assert service_answer == mcp_answer


# ---------------------------------------------------------------------------
# Edge cases from the brief
# ---------------------------------------------------------------------------

def test_engine_version_disagreement_reports_the_manifest_not_pyproject(
        monkeypatch, server, service):
    pyproject_version = tomllib.loads(
        (_REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    manifest_version = "0.0.0-manifest"
    assert manifest_version != pyproject_version
    payload = _payload({**engine_mod.load_manifest(), "engineVersion": manifest_version})
    monkeypatch.setattr(mcp_server, "_MANIFEST_BYTES", payload)
    monkeypatch.setattr(app_main, "_MANIFEST_BYTES", payload)
    assert _call(server)["manifest"]["engineVersion"] == manifest_version
    assert service.get("/healthz").json()["engine_contract"]["manifest"][
        "engineVersion"] == manifest_version


def test_missing_required_key_is_a_typed_error_and_health_still_answers(
        monkeypatch, server, service):
    incomplete = dict(engine_mod.load_manifest())
    del incomplete["artifactKinds"]
    payload = _payload(incomplete)
    monkeypatch.setattr(mcp_server, "_MANIFEST_BYTES", payload)
    monkeypatch.setattr(app_main, "_MANIFEST_BYTES", payload)
    with pytest.raises(engine_mod.ManifestError, match="artifactKinds"):
        mcp_server.engine_contract()
    assert "artifactKinds" in _call_error(server)
    body = service.get("/healthz").json()
    assert body["ok"] is True
    assert body["engine_contract"]["ok"] is False
    assert "artifactKinds" in body["engine_contract"]["detail"]


def test_absent_manifest_file_error_names_the_path_it_looked_for(monkeypatch):
    missing = _REPO / "capturd" / "engine" / "does-not-exist.json"
    monkeypatch.setattr(engine_mod, "_MANIFEST_PATH", missing)
    monkeypatch.setattr(mcp_server, "_MANIFEST_BYTES", None)
    with pytest.raises(engine_mod.ManifestError) as excinfo:
        mcp_server.engine_contract()
    assert str(missing) in str(excinfo.value)


def test_two_calls_read_the_manifest_once(monkeypatch, server):
    calls = []
    real = mcp_server.load_manifest

    def counting(data=None):
        calls.append(data)
        return real(data)

    monkeypatch.setattr(mcp_server, "load_manifest", counting)
    payload = _payload({**engine_mod.load_manifest(), "engineVersion": "1.2.3-once"})
    monkeypatch.setattr(mcp_server, "_MANIFEST_BYTES", payload)
    first = _call(server)
    second = _call(server)
    assert first == second
    assert len(calls) == 1, "the manifest must be loaded once per process, not per call"


def test_unknown_argument_is_refused(server):
    assert "bogus" in _call_error(server, TOOL, {"bogus": 1})


def test_service_contract_is_unaffected_by_missing_pro_configuration(service):
    # /healthz gates nothing; the contract field is the manifest, not an
    # entitlement, so an unconfigured Pro deployment still reports it.
    contract = service.get("/healthz").json()["engine_contract"]
    assert contract["manifest"]["engine"] == "capturd"
    assert "pro_required" not in json.dumps(contract)
