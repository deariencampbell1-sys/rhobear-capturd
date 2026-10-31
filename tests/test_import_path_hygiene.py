"""Import-path hygiene guards for the test runtime configuration.

``pyproject.toml`` sets ``pythonpath = ["scripts", "director-rig"]`` (pytest ≥7
ini) so a clean clone collects and passes with no hand-set PYTHONPATH. That
puts two extra directories ahead of the stdlib fallbacks and site-packages for
every test run, which is exactly the shape of an import-shadowing foot-gun.
This file pins the properties that keep it safe, and keeps the TEST-ONLY SSRF
fixture seam from leaking into production code.

Sections:
1. The TEST-ONLY loopback fixture allowance in ``capturd/_net.py`` is a
   deliberate bypass primitive for the live-drive E2E harness. Production code
   must never import these; ``tests/test_import_path_hygiene.py`` enforces it.
2. No module directly inside ``scripts/`` or ``director-rig/`` may share a
   name with a stdlib module or a top-level site-packages import — the
   intersection is pinned to empty (and printed if it ever isn't).
3. ``director-rig/{film,scout,revoice,paid_boot}.py`` must do no import-time
   filesystem work against the ``/opt/sunsponge-capture`` default (real rigs
   override it via ``CAPTURD_REPO``).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# The two TEST-ONLY bypass primitives that live in capturd/_net.py.
SEAM_MARKERS = ("register_test_fixture_origin", "guard_with_test_fixture_allowance")

RIG_MODULES = ("film", "scout", "revoice", "paid_boot")


# ---------------------------------------------------------------------------
# 1. The TEST-ONLY SSRF fixture seam must stay out of production code.
#    Production code must never import these; tests/test_import_path_hygiene.py
#    enforces it. (The seam itself lives in capturd/_net.py, whose section
#    comment carries the same sentence — one rule, stated where the code is.)
# ---------------------------------------------------------------------------

def test_production_package_never_references_the_test_fixture_seam():
    """Text-scan every ``capturd/**.py`` except ``capturd/_net.py``.

    ``register_test_fixture_origin`` / ``guard_with_test_fixture_allowance``
    are the test-only bypass primitives. A text scan is deliberately crude —
    that is what CI can enforce, and unlike an import-assertion it cannot be
    dodged by a conditional import, a stringly-typed getattr, or a lazy
    ``importlib`` call. If any production module so much as mentions the seam,
    it has leaked: fail loudly. This is a tripwire on committed source, not a
    proof of the negative — the runtime gate in ``tests/test_net_fixture.py``
    is what actually keeps the seam inert in a process that never armed it.
    """
    net_py = REPO / "capturd" / "_net.py"
    offenders: list[str] = []
    for py in sorted((REPO / "capturd").rglob("*.py")):
        if py == net_py:
            continue  # the seam itself lives here, by design
        text = py.read_text(encoding="utf-8", errors="replace")
        for marker in SEAM_MARKERS:
            if marker in text:
                offenders.append(f"{py.relative_to(REPO)} mentions {marker!r}")
    assert not offenders, (
        "the TEST-ONLY fixture seam leaked into production code "
        "(production code must never import these):\n  "
        + "\n  ".join(offenders))


# ---------------------------------------------------------------------------
# 2. pythonpath = ["scripts", "director-rig"] must not shadow imports.
# ---------------------------------------------------------------------------

def _top_level_importable_names() -> set[str]:
    """Every stdlib module name, plus every top-level module/package name
    visible from site-packages — the things ``pythonpath = ["scripts",
    "director-rig"]`` could shadow for every test run."""
    import pkgutil
    import site

    names = set(sys.stdlib_module_names)
    for entry in site.getsitepackages() + [site.getusersitepackages()]:
        if entry and os.path.isdir(entry):
            for m in pkgutil.iter_modules([entry]):
                names.add(m.name)
    return names


def _pythonpath_dir_module_names() -> set[str]:
    """Names the pythonpath ini dirs put on sys.path's front: every module
    directly inside them AND every immediate subdirectory name — a plain
    (namespace) subpackage shadows just the same as a single-file module,
    which is how the first version of this scan missed them."""
    names: set[str] = set()
    for d in ("scripts", "director-rig"):
        base = REPO / d
        for py in sorted(base.glob("*.py")):
            names.add(py.stem)
        for child in sorted(base.iterdir()):
            if child.is_dir() and not child.name.startswith((".", "_")):
                names.add(child.name)
    return names


def test_pythonpath_dirs_do_not_shadow_importable_names():
    """No scripts/ or director-rig/ module may collide with a stdlib module or
    a top-level site-packages import — pytest's pythonpath ini puts those dirs
    ahead of both for every test run, so a collision would silently shadow it
    (and the shadowing would be order-dependent for every other consumer)."""
    collide = _pythonpath_dir_module_names() & _top_level_importable_names()
    assert not collide, (
        "modules directly inside scripts/ or director-rig/ shadow importable "
        f"top-level names: {sorted(collide)} — rename them (the pythonpath ini "
        "puts these dirs ahead of site-packages and the stdlib in every test run)")


# ---------------------------------------------------------------------------
# 3. director-rig modules must import without the default repo path.
# ---------------------------------------------------------------------------

def _subprocess_syspath_env() -> dict[str, str]:
    """Env for the rig-import subprocesses: CAPTURD_REPO unset, a fixed
    sys.path (director-rig, repo root, scripts) and no inherited PYTHONPATH,
    so the premise is fully controlled by this test."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("CAPTURD_REPO", "PYTHONPATH")}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO / "director-rig"), str(REPO), str(REPO / "scripts")])
    return env


@pytest.mark.parametrize("mod", RIG_MODULES)
def test_rig_modules_import_cleanly_without_the_default_repo_path(mod):
    """director-rig/{film,scout,revoice,paid_boot}.py do no import-time
    filesystem work against ``/opt/sunsponge-capture``.

    Each module is imported in a fresh subprocess with ``CAPTURD_REPO`` unset
    and a sys.path where ``/opt/sunsponge-capture`` visibly does not exist
    (the subprocess asserts the premise itself). The default is only a
    ``pathlib`` constant plus a sys.path insert of a nonexistent directory in
    these modules — inserting a nonexistent path is a no-op for imports, so a
    clean import proves nothing at import time NEEDS that tree.

    The success branch is MANDATORY: ``returncode == 0`` AND the IMPORT_OK
    marker. An earlier version of this test accepted a clean
    SystemExit/ImportError naming the ``CAPTURD_REPO`` override — which made
    it pass on exactly the failure it exists to catch. If a rig module ever
    genuinely needs the default path, that dependency must be split into a
    second, separately-named test that documents it — never folded in here.
    """
    if Path("/opt/sunsponge-capture").exists():
        pytest.skip(
            "/opt/sunsponge-capture exists on this host — the 'visibly does "
            "not exist' premise is unreachable here (this test is about the "
            "DEFAULT path being absent)")
    code = (
        "import importlib, os, sys\n"
        "assert not os.path.exists('/opt/sunsponge-capture'), (\n"
        "    'premise: /opt/sunsponge-capture must not exist for this check')\n"
        f"importlib.import_module({mod!r})\n"
        "print('IMPORT_OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=120,
        env=_subprocess_syspath_env(), cwd=tempfile.gettempdir())
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    assert proc.returncode == 0 and "IMPORT_OK" in proc.stdout, (
        f"{mod} must import CLEANLY without /opt/sunsponge-capture and with "
        f"CAPTURD_REPO unset — a clean SystemExit/ImportError naming the "
        f"override is no longer accepted (that would be a real dependency on "
        f"the default path and needs its own, separately-named test):\n"
        f"rc={proc.returncode}\n{combined}")


# ---------------------------------------------------------------------------
# 5. Runtime dependencies must be declared where the evidence puts them.
#    The four runtime deps used to be declared only in the `dev` extra while
#    production entry points need them at IMPORT time. Verdicts (each proven
#    experimentally — see the pyproject dependencies comment and the PR
#    comment):
#      fastapi      -> dependencies  (service/app/main.py imports it unguarded
#                                      at module import)
#      uvicorn      -> dependencies  (the `capturd serve` chain — fastmcp ->
#                                      mcp — fails to import without it; also
#                                      the service/mcp_service.py runner)
#      numpy        -> dependencies  (capturd/walk/voice.py imports it
#                                      unguarded at module import; on the
#                                      capturd.mcp.server / capturd walk chains)
#      cryptography -> dependencies  (mcp 2.3.0 imports it at module import
#                                      on the `capturd serve` chain)
# ---------------------------------------------------------------------------

# Declared exactly like pyproject.toml declares them.
_RUNTIME_DEPS = ("fastapi", "uvicorn[standard]", "numpy", "cryptography")
_ALL_FOUR = tuple(_RUNTIME_DEPS)


def _pyproject_main_dependencies_text() -> str:
    """The body of [project] dependencies = [...] (not the dev extra's).
    Scanned line-wise: a string-split on ']' would stop at the ']' inside
    \"uvicorn[standard]\"."""
    lines = (REPO / "pyproject.toml").read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "dependencies = [")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "]")
    return "\n".join(lines[start + 1:end])


# (entry module, blocked top-level names, expected, documented reason).
# blocked = _ALL_FOUR simulates a production install with NO dev extras.
_ENTRY_VERDICTS = (
    ("capturd.cli", _ALL_FOUR, "ok",
     "lazy dispatcher — imports none of the four at import time"),
    ("capturd.shots.cli", _ALL_FOUR, "ok",
     "screenshots CLI — stdlib + playwright only"),
    ("capturd.mcp.server", ("uvicorn",), "fail",
     "`capturd serve`: fastmcp -> mcp needs uvicorn at import time"),
    ("capturd.mcp.server", ("numpy",), "fail",
     "capturd/walk/voice.py imports numpy unguarded at import time"),
    ("capturd.mcp.server", ("cryptography",), "fail",
     "mcp 2.3.0 imports cryptography at module import (request_state)"),
    ("capturd.mcp.server", ("fastapi",), "ok",
     "the MCP server surface does not need fastapi"),
    ("app.main", ("fastapi",), "fail",
     "service/app/main.py imports fastapi unguarded at module import"),
    ("app.main", ("uvicorn",), "ok",
     "uvicorn only enters via service/mcp_service.py's __main__ runner"),
    ("app.main", ("numpy",), "ok",
     "the service app does not import the walk/voice chain"),
    ("app.main", ("cryptography",), "ok",
     "the service app never touches the signed-licence path"),
)


def _import_under_blocklist(module: str, blocked: tuple[str, ...]) -> subprocess.CompletedProcess:
    """Import *module* in a fresh subprocess with a sys.meta_path finder that
    raises ImportError for every blocked top-level name."""
    blocklist = ", ".join(repr(b.split("[")[0]) for b in blocked) or "set()"
    code = (
        "import sys\n"
        f"BLOCKED = {{{blocklist}}}\n"
        "class _Blocker:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in BLOCKED:\n"
        "            raise ImportError('blocked-by-test production dependency: ' + name)\n"
        "        return None\n"
        "    find_module = find_spec\n"
        "sys.meta_path.insert(0, _Blocker())\n"
        f"__import__({module!r})\n"
        "print('IMPORT_OK')\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    if module == "app.main":
        # the hosted service lays out `app/` under service/ and imports
        # capturd from the repo root
        env["PYTHONPATH"] = os.pathsep.join([str(REPO / "service"), str(REPO)])
    else:
        env["PYTHONPATH"] = str(REPO)
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        timeout=120, env=env, cwd=tempfile.gettempdir())


@pytest.mark.parametrize("module,blocked,expected,reason", _ENTRY_VERDICTS)
def test_production_entry_module_imports_per_dependency_verdict(
        module, blocked, expected, reason):
    """Pin the per-package verdict executably: 'ok' = the entry module imports
    with the listed names blocked; 'fail' = it fails with a CLEAN ImportError
    naming the missing production dependency (or fastmcp's documented
    server-support message) — which is exactly why that dependency is declared
    in [project] dependencies, not left in dev."""
    proc = _import_under_blocklist(module, blocked)
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if expected == "ok":
        assert proc.returncode == 0 and "IMPORT_OK" in proc.stdout, (
            f"{module} must import without {blocked} ({reason}):\n{combined}")
    else:
        assert proc.returncode != 0, (
            f"{module} was expected to REQUIRE {blocked} ({reason}) but imported "
            f"cleanly — revisit the dependency verdicts")
        assert "ImportError" in combined or "ModuleNotFoundError" in combined or \
            "FastMCP server support" in combined, (
            f"{module} failed without {blocked} but not with a clean import "
            f"error ({reason}):\n{combined}")


def test_production_entry_modules_import_without_optional_dev_deps():
    """The runtime deps the production entry points need at IMPORT time must be
    declared in [project] dependencies (not only in the dev extra) — and the
    console-script entry points that need NONE of them must import with all
    four blocked (a production install without dev extras)."""
    declared = _pyproject_main_dependencies_text()
    # every verdict dep is declared with a version specifier: "name>="
    missing = [d for d in _RUNTIME_DEPS if f'"{d}>=' not in declared]
    assert not missing, (
        f"import-time-required runtime deps missing from [project] dependencies: "
        f"{missing} — a production install (pip install .) would ship an entry "
        f"point that cannot import")
    for module in ("capturd.cli", "capturd.shots.cli"):
        proc = _import_under_blocklist(module, _ALL_FOUR)
        assert proc.returncode == 0 and "IMPORT_OK" in proc.stdout, (
            f"{module} must import with all four runtime deps blocked:\n"
            f"{proc.stderr}")
