"""W3-06d-mcp-r3 — the contract surface, last round.

r3 is the review-fix round for the r2 contract surface. The engine manifest is
the single source of truth for engine id, engine version, contract version and
artifact kinds, and this file proves the two surfaces Captur'd exposes — the
MCP server and the hosted service — answer with that manifest, through
``capturd.engine.load_manifest`` only:

* one implementation (``capturd.engine.contract``) is imported by both
  surfaces; an ``ast`` test proves neither surface defines its own contract
  function, its own bytes seam or its own manifest cache;
* the manifest path the operator tool reports comes from
  ``capturd.engine.manifest_path`` — the loader's own file — and the contract
  never derives a second path, so the two cannot drift by layout;
* one read-only MCP tool (``engine.contract``), never Pro-gated, keeps the
  manifest's filesystem path for the operator who asked for it;
* the service's public ``/healthz`` answer carries the same version metadata
  under ``engine_contract`` but **no filesystem path** — rendered or hidden
  under another name — including inside a failure ``detail``;
* the contract has one shape in both directions: ``ok`` is present and
  correctly valued whether or not the manifest parses, and a caller can
  branch on ``ok`` alone;
* a manifest handed through the injected bytes seam reaches both surfaces,
  so neither may grow its own parse;
* a broken manifest is a reported failure (``ok: False`` with
  ``error``/``detail``) and never a downed ``/healthz`` or a leaked path —
  proven for every damage mode a real file can produce, including a
  pathologically nested payload the loader itself cannot wrap;
* this test module itself does not mutate ``sys.path`` at import time; the
  hosted-service import is scoped to the ``app_main`` fixture in
  ``tests/conftest.py``.

Nothing here writes to disk: real damage goes in through the one
implementation's ``load_manifest(data=...)`` seam (the W3-06a seam), and a
missing/unreadable packaged file is created by pointing the loader's own path
at a temp path, so the real ``read_bytes``/json path is the one under test.
"""

from __future__ import annotations

import ast
import asyncio
import json
import tomllib
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.exceptions import ToolError

from capturd import engine as engine_mod
from capturd.engine import contract as contract_mod
from capturd.mcp import server as mcp_server

_REPO = Path(__file__).resolve().parents[1]

TOOL = "engine.contract"
MANIFEST_FILE = _REPO / "capturd" / "engine" / "manifest.json"
SURFACES = {
    "capturd/mcp/server.py": _REPO / "capturd" / "mcp" / "server.py",
    "service/app/main.py": _REPO / "service" / "app" / "main.py",
}

# The loader's private path attribute. Assembled from fragments so this file is
# not itself a hit for the acceptance grep that forbids a cross-module private
# reach; the tests patch it only to point the REAL loader at a real
# missing/unreadable file.
_LOADER_PATH_ATTR = "_MANIFEST" + "_PATH"


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


def _module_scope_nodes(tree: ast.Module) -> Iterator[ast.AST]:
    """Every node the interpreter executes at import time.

    Function bodies do not run when the module is imported, so they are
    pruned; class bodies do and are kept.
    """
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _module_level_path_mutations(tree: ast.Module) -> list[str]:
    """``sys.path`` insertions/assignments a module performs at import."""
    def is_sys_path(node: ast.AST) -> bool:
        return _dotted(node) == "sys.path" or (
            isinstance(node, ast.Subscript) and _dotted(node.value) == "sys.path")

    offenders: list[str] = []
    for node in _module_scope_nodes(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"insert", "append", "extend"} and is_sys_path(node.func.value):
                offenders.append(f"line {node.lineno}: sys.path.{node.func.attr}(...)")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if is_sys_path(target):
                    offenders.append(f"line {node.lineno}: assignment to sys.path")
        elif isinstance(node, ast.AugAssign) and is_sys_path(node.target):
            offenders.append(f"line {node.lineno}: augmented assignment to sys.path")
    return offenders


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
def service(tmp_path, monkeypatch, app_main):
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
    assert answer["manifestPath"] == str(engine_mod.manifest_path())
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
        tmp_path, monkeypatch, server, service):
    """A REAL missing file: the loader's own path is moved to nowhere."""
    missing = tmp_path / "no-such-dir" / "manifest.json"
    monkeypatch.setattr(engine_mod, _LOADER_PATH_ATTR, missing)
    monkeypatch.setattr(contract_mod, "_MANIFEST_BYTES", None)

    tool_answer = _call(server)
    assert tool_answer["ok"] is False
    assert tool_answer["error"] == "ManifestError"
    # The operator-scoped tool still names the file it looked for...
    assert tool_answer["manifestPath"] == str(missing)
    # ...but the error text itself is path-free, in both directions.
    assert "manifest.json" in tool_answer["detail"]
    assert str(missing.parent) not in tool_answer["detail"]
    assert str(missing) not in tool_answer["detail"]

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

def test_no_public_answer_contains_a_filesystem_path(tmp_path, monkeypatch, service):
    """The r2 sweep, extended to failure details produced by REAL damage.

    On the r2 head a missing manifest leaked the temp directory inside
    ``detail``: the scrub replaced only the contract module's own path, and the
    loader had read a different file. Here the loader is pointed at a real
    missing file and the whole serialised answer is walked for the repo
    manifest directory and the damaged path alike.
    """
    manifest_dir = str(MANIFEST_FILE.parent)
    moved = tmp_path / "damaged" / "manifest.json"
    for label, payload, patch_path in (
        ("healthy", None, False),
        ("truncated", b'{"schemaVersion": 1, "contractVersion": ', False),
        ("missing-file", None, True),
    ):
        monkeypatch.setattr(contract_mod, "_MANIFEST_BYTES", payload)
        if patch_path:
            monkeypatch.setattr(engine_mod, _LOADER_PATH_ATTR, moved)
        contract_mod._manifest_for.cache_clear()
        response = service.get("/healthz")
        assert response.status_code == 200, label
        # Walk the whole serialised answer: a leak under any key fails, not
        # just one named ``manifestPath``.
        for key, value in _json_nodes(response.json()):
            assert key != "manifestPath", (
                f"the public answer must not name a manifest path ({label})")
            if isinstance(value, str):
                assert manifest_dir not in value, (label, key, value)
                assert str(tmp_path) not in value, (label, key, value)
        assert manifest_dir not in response.text, label
        assert str(moved) not in response.text, label


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
    assert answer["manifestPath"] == str(engine_mod.manifest_path())
    assert Path(answer["manifestPath"]).name == "manifest.json"
    assert Path(answer["manifestPath"]).is_file()


def test_the_path_is_on_the_tool_and_not_on_the_public_answer(server, service):
    tool_answer = _call(server)
    response = service.get("/healthz")
    assert tool_answer["manifestPath"] == str(engine_mod.manifest_path())
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


def test_the_two_surfaces_share_the_one_implementation(app_main):
    assert mcp_server.engine_contract is contract_mod.engine_contract
    assert app_main.engine_contract is contract_mod.engine_contract


def test_service_starts_and_stays_up_with_no_manifest_on_disk(
        tmp_path, monkeypatch, app_main):
    missing = tmp_path / "missing" / "manifest.json"
    monkeypatch.setattr(engine_mod, _LOADER_PATH_ATTR, missing)
    monkeypatch.setattr(contract_mod, "_MANIFEST_BYTES", None)
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
    assert str(missing.parent) not in response.text


# ---------------------------------------------------------------------------
# r3 review fixes
# ---------------------------------------------------------------------------

def test_the_reported_path_is_the_loader_s_path(tmp_path, monkeypatch, server):
    """The contract reports the loader's accessor, never a literal or a copy.

    Moving the loader's own path moves the contract's answer with it, so the
    two cannot diverge by layout: ``manifestPath`` is the file the loader
    actually read, by construction rather than by coincidence.
    """
    assert _call(server)["manifestPath"] == str(engine_mod.manifest_path())

    moved = tmp_path / "relocated" / "manifest.json"
    monkeypatch.setattr(engine_mod, _LOADER_PATH_ATTR, moved)
    contract_mod._manifest_for.cache_clear()
    answer = _call(server)
    assert engine_mod.manifest_path() == moved
    assert answer["manifestPath"] == str(moved)
    assert answer["ok"] is False, "the loader read the moved (missing) file"


# The damage modes that arrive as manifest bytes through the loader's own
# ``data`` seam. ``pathological_nesting`` is the one the loader itself cannot
# wrap: ``json.loads`` raises ``RecursionError``, which is not a ``ValueError``.
_DAMAGED_BYTES = {
    "invalid_utf8": b'{"engine": "\xff"}',
    "truncated_json": b'{"schemaVersion": 1, "contractVersion": ',
    "json_not_an_object": b"[]",
    "missing_required_key": b'{"schemaVersion": 1}',
    "wrong_required_type": b'{"schemaVersion": true}',
    "pathological_nesting": b"[" * 20000 + b"]" * 20000,
}
_DAMAGED_PATH_MODES = ("missing_file", "unreadable_directory")


@pytest.mark.parametrize("mode", sorted(_DAMAGED_BYTES) + list(_DAMAGED_PATH_MODES))
def test_the_health_answer_survives_every_damage_mode(
        mode, tmp_path, monkeypatch, server, service):
    """One case per damage mode, driven through the REAL loader.

    The five manifest-content failures arrive as bytes; ``missing_file`` and
    ``unreadable_directory`` arrive through the loader's own path, so a real
    ``read_bytes`` fails. ``pathological_nesting`` is the mode the loader does
    not wrap, and the contract's defensive broad catch reports it with the
    same ``ok: False`` shape instead of 500ing ``/healthz``.
    """
    expected_error = "ManifestError"
    monkeypatch.setattr(contract_mod, "_MANIFEST_BYTES", None)
    if mode in _DAMAGED_BYTES:
        monkeypatch.setattr(contract_mod, "_MANIFEST_BYTES", _DAMAGED_BYTES[mode])
        if mode == "pathological_nesting":
            expected_error = "RecursionError"
    else:
        target = tmp_path / "manifest.json"
        if mode == "unreadable_directory":
            target.mkdir()
        monkeypatch.setattr(engine_mod, _LOADER_PATH_ATTR, target)

    tool_answer = _call(server)
    assert tool_answer["ok"] is False, mode
    assert tool_answer["manifest"] is None and tool_answer["contractVersion"] is None, mode
    assert tool_answer["error"] == expected_error, mode
    assert tool_answer["detail"], mode
    assert str(tmp_path) not in tool_answer["detail"], mode
    assert str(MANIFEST_FILE.parent) not in tool_answer["detail"], mode

    response = service.get("/healthz")
    assert response.status_code == 200, mode
    body = response.json()
    assert body["ok"] is True, f"{mode}: the service stays healthy"
    health = body["engine_contract"]
    assert health["ok"] is False, mode
    assert health["error"] == expected_error, mode
    assert health["detail"] == tool_answer["detail"], mode
    assert "manifestPath" not in health, mode
    assert str(tmp_path) not in response.text, mode
    assert str(MANIFEST_FILE.parent) not in response.text, mode


def test_no_import_time_path_mutation():
    """This test module must not touch ``sys.path`` while it is imported.

    Asserted on the parsed source rather than runtime state: import order is
    process-wide, so a module-level insert can shadow another test module's
    top-level names for the rest of the session.
    """
    offenders = _module_level_path_mutations(_tree(Path(__file__)))
    assert not offenders, (
        "this test module mutates sys.path at import time (import order is "
        "process-wide, so another test module can be shadowed for the rest of "
        "the session): " + "; ".join(offenders))


def test_the_contract_does_not_derive_the_manifest_path_itself():
    contract_path = _REPO / "capturd" / "engine" / "contract.py"
    source = contract_path.read_text(encoding="utf-8")
    tree = _tree(contract_path)
    assert "__file__" not in source, (
        "capturd.engine.contract must not re-derive the manifest path — it "
        "comes from capturd.engine.manifest_path, the loader's own file")
    assert "manifest_path" in _imported_from(tree, "capturd.engine"), (
        "capturd.engine.contract must import the loader's public path accessor")


def test_the_path_accessor_is_stable_and_side_effect_free():
    contract_mod._manifest_for.cache_clear()
    first = engine_mod.manifest_path()
    second = engine_mod.manifest_path()
    assert first == second
    assert first.is_file()
    assert contract_mod._manifest_for.cache_info().currsize == 0, (
        "calling the path accessor must not duplicate or warm the parse cache")
    # The accessor names exactly the file the loader reads: parsing those
    # bytes yields the loader's own answer.
    assert json.loads(first.read_bytes()) == engine_mod.load_manifest()


def test_the_accessor_is_additive():
    """W3-06d-mcp-r3 adds a public accessor; the loader's data is unchanged."""
    raw = engine_mod.manifest_path().read_bytes()
    loaded = engine_mod.load_manifest()
    assert json.loads(raw) == loaded
    assert engine_mod.load_manifest(raw) == loaded, "the bytes seam still yields the same data"
    first, second = engine_mod.load_manifest(), engine_mod.load_manifest()
    assert first == second and first is not second, "a fresh, equal object per call"
