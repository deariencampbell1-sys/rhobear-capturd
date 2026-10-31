"""W3-06d-mcp-r2 — one contract, one implementation, no path on a public answer.

r2 is a review-fix round on top of r1. The engine manifest is the single
source of truth for engine id, engine version, contract version and artifact
kinds. This file proves the two surfaces Captur'd exposes — the MCP server
and the hosted service — answer with that manifest, through
``capturd.engine.load_manifest`` only:

* one implementation (``capturd.engine.contract``) is imported by both
  surfaces; an ``ast`` test proves neither surface defines its own contract
  function, its own bytes seam or its own manifest cache;
* one read-only MCP tool (``engine.contract``), never Pro-gated, keeps the
  manifest's filesystem path for the operator who asked for it;
* the service's public ``/healthz`` answer carries the same version metadata
  under ``engine_contract`` but **no filesystem path** — rendered or hidden
  under another name;
* the contract has one shape in both directions: ``ok`` is present and
  correctly valued whether or not the manifest parses, and a caller can
  branch on ``ok`` alone;
* a manifest handed through the injected bytes seam reaches both surfaces,
  so neither may grow its own parse;
* a broken manifest is a reported failure (``ok: False`` with
  ``error``/``detail``) and never a downed ``/healthz`` or a leaked path.

Nothing here writes to disk: the manifest bytes go in through the one
implementation's ``load_manifest(data=...)`` seam (the W3-06a seam).
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import tomllib
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from capturd import engine as engine_mod
from capturd.engine import contract as contract_mod
from capturd.mcp import server as mcp_server

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "service"))
sys.path.insert(0, str(_REPO))

from app import main as app_main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TOOL = "engine.contract"
MANIFEST_FILE = _REPO / "capturd" / "engine" / "manifest.json"
SURFACES = {
    "capturd/mcp/server.py": _REPO / "capturd" / "mcp" / "server.py",
    "service/app/main.py": _REPO / "service" / "app" / "main.py",
}


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


def _json_nodes(value):
    """Yield every (key, value) pair anywhere in a JSON tree."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from _json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_nodes(child)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _defined_names(tree: ast.Module) -> set[str]:
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _module_level_assigned_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _imported_from(tree: ast.Module, module: str) -> set[str]:
    """Local names bound by ``from module import name`` statements."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def _imported_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _module_aliases(tree: ast.Module) -> dict[str, str]:
    """Local name -> dotted module path for every import in the file."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _engine_private_reads(tree: ast.Module) -> list[tuple[int, str]]:
    """Private attributes read from an import of ``capturd.engine*``.

    Scoped to the engine-contract boundary: ``config._env`` elsewhere in the
    service is a deliberate vault-aware accessor, not contract plumbing.
    """
    aliases = _module_aliases(tree)
    reads: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not node.attr.startswith("_"):
            continue
        dotted = _dotted(node)
        if dotted is None:
            continue
        target = aliases.get(dotted.split(".")[0])
        if target is not None and target.startswith("capturd.engine"):
            reads.append((node.lineno, dotted))
    return reads


@pytest.fixture(autouse=True)
def _cold_contract_cache():
    """The one implementation caches the parse; start every test cold."""
    contract_mod._manifest_for.cache_clear()
    yield
    contract_mod._manifest_for.cache_clear()


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
    assert answer["ok"] is True
    assert set(answer["manifest"]) == set(on_disk)
    assert answer["manifest"] == on_disk
    assert answer["contractVersion"] == on_disk["contractVersion"]
    assert answer["manifestPath"] == str(contract_mod.MANIFEST_PATH)
    assert Path(answer["manifestPath"]).name == "manifest.json"


def test_service_reports_the_same_contract(server, service):
    mcp_answer = _call(server)
    response = service.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["engine_contract"]["ok"] is True
    assert {k: v for k, v in mcp_answer.items() if k != "manifestPath"} == body["engine_contract"]
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
    monkeypatch.setattr(contract_mod, "_MANIFEST_BYTES", _payload(injected))

    mcp_answer = _call(server)
    service_answer = service.get("/healthz").json()["engine_contract"]

    assert mcp_answer["manifest"] == injected
    assert service_answer["manifest"] == injected
    assert service_answer == {k: v for k, v in mcp_answer.items() if k != "manifestPath"}, (
        "the two surfaces disagree on an injected manifest — one of them "
        "grew its own parse instead of calling the one implementation")


# ---------------------------------------------------------------------------
# 4 — a broken manifest is a reported failure, never a partial answer
# ---------------------------------------------------------------------------

def test_a_broken_manifest_is_a_typed_error_not_a_partial_answer(monkeypatch, server, service):
    bad = b'{"schemaVersion": 1, "contractVersion": '  # truncated JSON
    monkeypatch.setattr(contract_mod, "_MANIFEST_BYTES", bad)

    tool_answer = _call(server)
    assert tool_answer["ok"] is False
    assert tool_answer["error"] == "ManifestError"
    assert tool_answer["manifest"] is None and tool_answer["contractVersion"] is None
    assert "manifest" in tool_answer["detail"].lower()

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
        monkeypatch.setattr(contract_mod, "_MANIFEST_BYTES", payload)
        mcp_answer = _call(server)
        service_answer = service.get("/healthz").json()["engine_contract"]
        assert mcp_answer["manifest"]["engineVersion"] == version
        assert service_answer["manifest"]["engineVersion"] == version
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
    # Patch to True: a gate in front of the contract would have to refuse the
    # answer even when Pro is active — this is the direction the r1 test
    # (patched to False) could not catch.
    monkeypatch.setattr(mcp_server, "is_pro", lambda: True)
    mcp_answer = _call(server)
    service_answer = service.get("/healthz").json()["engine_contract"]
    assert mcp_answer["ok"] is True
    assert mcp_answer["manifest"]["engine"] == "capturd"
    assert service_answer == {k: v for k, v in mcp_answer.items() if k != "manifestPath"}
    # And Free gets the identical contract — the tool consults no entitlement.
    monkeypatch.setattr(mcp_server, "is_pro", lambda: False)
    assert _call(server) == mcp_answer


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
    monkeypatch.setattr(contract_mod, "_MANIFEST_BYTES", payload)
    assert _call(server)["manifest"]["engineVersion"] == manifest_version
    assert service.get("/healthz").json()["engine_contract"]["manifest"][
        "engineVersion"] == manifest_version


def test_missing_required_key_is_a_typed_error_and_health_still_answers(
        monkeypatch, server, service):
    incomplete = dict(engine_mod.load_manifest())
    del incomplete["artifactKinds"]
    monkeypatch.setattr(contract_mod, "_MANIFEST_BYTES", _payload(incomplete))

    tool_answer = _call(server)
    assert tool_answer["ok"] is False
    assert "artifactKinds" in tool_answer["detail"]
    assert str(MANIFEST_FILE.parent) not in tool_answer["detail"]

    body = service.get("/healthz").json()
    assert body["ok"] is True
    assert body["engine_contract"]["ok"] is False
    assert "artifactKinds" in body["engine_contract"]["detail"]
    assert str(MANIFEST_FILE.parent) not in body["engine_contract"]["detail"]


def test_a_missing_manifest_is_reported_without_a_filesystem_path(
        monkeypatch, server, service):
    missing = contract_mod.MANIFEST_PATH

    def unreadable(data=None):
        raise engine_mod.ManifestError(
            "packaged engine manifest unreadable: [Errno 2] No such file or "
            f"directory: '{missing}'")

    monkeypatch.setattr(contract_mod, "load_manifest", unreadable)
    monkeypatch.setattr(contract_mod, "_MANIFEST_BYTES", None)

    tool_answer = _call(server)
    assert tool_answer["ok"] is False
    # The operator-scoped tool still names the file it looked for...
    assert tool_answer["manifestPath"] == str(missing)
    # ...but the error text itself is path-free, in both directions.
    assert "manifest.json" in tool_answer["detail"]
    assert str(missing.parent) not in tool_answer["detail"]

    response = service.get("/healthz")
    assert response.status_code == 200
    health = response.json()["engine_contract"]
    assert health["ok"] is False
    assert str(missing.parent) not in health["detail"]
    assert str(missing.parent) not in response.text


def test_two_calls_read_the_manifest_once(monkeypatch, server):
    calls = []
    real = contract_mod.load_manifest

    def counting(data=None):
        calls.append(data)
        return real(data)

    monkeypatch.setattr(contract_mod, "load_manifest", counting)
    payload = _payload({**engine_mod.load_manifest(), "engineVersion": "1.2.3-once"})
    monkeypatch.setattr(contract_mod, "_MANIFEST_BYTES", payload)
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


# ---------------------------------------------------------------------------
# r2 review fixes
# ---------------------------------------------------------------------------

def test_the_health_answer_carries_no_filesystem_path(monkeypatch, service):
    manifest_dir = str(MANIFEST_FILE.parent)
    broken = b'{"schemaVersion": 1, "contractVersion": '
    for payload in (None, broken):
        if payload is not None:
            monkeypatch.setattr(contract_mod, "_MANIFEST_BYTES", payload)
        response = service.get("/healthz")
        assert response.status_code == 200
        # Walk the whole serialised answer: a leak under any key fails,
        # not just one named ``manifestPath``.
        for key, value in _json_nodes(response.json()):
            assert key != "manifestPath", "the public answer must not name a manifest path"
            if isinstance(value, str):
                assert manifest_dir not in value, (
                    f"the public health answer leaked the manifest directory in {key!r}: "
                    f"{value!r}")
        assert manifest_dir not in response.text


def test_the_contract_has_one_shape_in_both_directions(monkeypatch, server, service):
    healthy_answer = _call(server)
    healthy_health = service.get("/healthz").json()["engine_contract"]

    monkeypatch.setattr(contract_mod, "_MANIFEST_BYTES", b'{"schemaVersion": ')
    broken_answer = _call(server)
    broken_health = service.get("/healthz").json()["engine_contract"]

    assert healthy_answer["ok"] is True and healthy_health["ok"] is True
    assert broken_answer["ok"] is False and broken_health["ok"] is False

    # Same keys whether or not the file parses, per surface...
    assert set(healthy_answer) == set(broken_answer)
    assert set(healthy_health) == set(broken_health)
    # ...and the tool adds exactly the path the public answer must not have.
    assert set(healthy_health) == set(healthy_answer) - {"manifestPath"}

    # A caller can branch on ``ok`` alone.
    for answer in (healthy_answer, healthy_health):
        assert answer["manifest"] is not None
        assert answer["contractVersion"] == answer["manifest"]["contractVersion"]
        assert answer["error"] is None and answer["detail"] is None
    for answer in (broken_answer, broken_health):
        assert answer["manifest"] is None and answer["contractVersion"] is None
        assert answer["error"] == "ManifestError"
        assert "manifest" in answer["detail"].lower()


def test_the_mcp_tool_still_reports_the_path(server):
    answer = _call(server)
    assert answer["manifestPath"] == str(contract_mod.MANIFEST_PATH)
    assert Path(answer["manifestPath"]).name == "manifest.json"
    assert Path(answer["manifestPath"]).is_file()


def test_the_path_is_on_the_tool_and_not_on_the_public_answer(server, service):
    tool_answer = _call(server)
    response = service.get("/healthz")
    assert tool_answer["manifestPath"] == str(contract_mod.MANIFEST_PATH)
    assert "manifestPath" not in response.json()
    assert "manifestPath" not in json.dumps(response.json())
    assert str(MANIFEST_FILE.parent) not in response.text


def test_there_is_one_implementation():
    for relative, path in SURFACES.items():
        tree = _tree(path)
        defined = _defined_names(tree)
        assert "engine_contract" not in defined, (
            f"{relative} defines its own engine_contract — the plumbing belongs "
            "to capturd.engine.contract")
        assert "_manifest_for" not in defined, (
            f"{relative} defines its own manifest cache — the cache belongs to "
            "capturd.engine.contract")
        assert "_MANIFEST_BYTES" not in _module_level_assigned_names(tree), (
            f"{relative} defines its own bytes seam — the seam belongs to "
            "capturd.engine.contract")
        assert "load_manifest" not in _imported_from(tree, "capturd.engine"), (
            f"{relative} reads the manifest directly — only capturd.engine.contract "
            "may call load_manifest")
        assert "capturd.engine.contract" in _imported_modules(tree), (
            f"{relative} does not import the one implementation")
        assert "engine_contract" in _imported_from(tree, "capturd.engine.contract"), (
            f"{relative} does not import engine_contract from capturd.engine.contract")
    assert contract_mod.engine_contract.__module__ == "capturd.engine.contract"


def test_no_caller_reads_a_private_attribute_of_another_module():
    for relative, path in {**SURFACES,
                           "capturd/engine/contract.py":
                               _REPO / "capturd" / "engine" / "contract.py"}.items():
        reads = _engine_private_reads(_tree(path))
        assert not reads, (
            f"{relative} reaches into the engine module's privates: "
            + ", ".join(f"line {line}: {expr}" for line, expr in reads)
            + " — the contract implementation must expose what callers need")


def test_the_two_surfaces_share_the_one_implementation():
    assert mcp_server.engine_contract is contract_mod.engine_contract
    assert app_main.engine_contract is contract_mod.engine_contract


def test_service_starts_and_stays_up_with_no_manifest_on_disk(tmp_path, monkeypatch):
    def missing(data=None):
        raise engine_mod.ManifestError("packaged engine manifest unreadable: no such file")

    monkeypatch.setattr(contract_mod, "load_manifest", missing)
    monkeypatch.setattr(app_main.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(app_main.config, "JOBS_DIR", tmp_path / "data" / "jobs")
    monkeypatch.setattr(app_main.config, "DB_PATH", tmp_path / "data" / "capturd.sqlite3")

    with TestClient(app_main.app) as client:      # startup must not raise
        response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    contract = body["engine_contract"]
    assert contract["ok"] is False
    assert contract["error"] == "ManifestError"
    assert "manifest" in contract["detail"].lower()
    assert str(MANIFEST_FILE.parent) not in response.text
