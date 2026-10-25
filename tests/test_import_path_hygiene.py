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
    it has leaked: fail loudly.
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
    """Names of the modules placed directly inside the pythonpath ini dirs."""
    names: set[str] = set()
    for d in ("scripts", "director-rig"):
        for py in sorted((REPO / d).glob("*.py")):
            names.add(py.stem)
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
def test_rig_modules_import_without_the_default_repo_path(mod):
    """director-rig/{film,scout,revoice,paid_boot}.py do no import-time
    filesystem work against ``/opt/sunsponge-capture``.

    Each module is imported in a fresh subprocess with ``CAPTURD_REPO`` unset
    and a sys.path where ``/opt/sunsponge-capture`` visibly does not exist
    (the subprocess asserts the premise itself). The default is only a
    ``pathlib`` constant plus a sys.path insert of a nonexistent directory in
    these modules — inserting a nonexistent path is a no-op for imports, so a
    clean import proves nothing at import time NEEDS that tree.

    Fallback contract (documented, not edited-around): if a rig module ever
    genuinely cannot import without the default path, its failure must be a
    clean ``SystemExit``/``ImportError`` whose output names the
    ``CAPTURD_REPO`` override — never a half-broken state. The rig modules are
    intentionally not edited to make this test pass.
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
    if proc.returncode == 0 and "IMPORT_OK" in proc.stdout:
        return  # success branch: no import-time work against the default path
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    assert "SystemExit" in combined or "ImportError" in combined, (
        f"{mod} cannot be imported without /opt/sunsponge-capture and the "
        f"failure is not a clean SystemExit/ImportError:\n{combined}")
    assert "CAPTURD_REPO" in combined, (
        f"{mod} cannot be imported without /opt/sunsponge-capture but its "
        f"failure does not name the CAPTURD_REPO override:\n{combined}")
