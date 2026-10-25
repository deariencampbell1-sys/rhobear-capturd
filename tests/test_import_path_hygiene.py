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
4. No tracked file writes a Pro-gate environment name: the gate compares a
   caller-supplied value against a caller-supplied allow-list, so any in-tree
   provisioning is a working licence bypass (issue #44 owns the gate design;
   this repo must ship no working example of it).
"""
from __future__ import annotations

import os
import re
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


# ---------------------------------------------------------------------------
# 4. No committed licence provisioning. The Pro gate (capturd/pro.py, issue
#    #44) reads its allow-list and licence from the caller-controlled
#    environment, so ANY tracked file that writes one of the two Pro-gate env
#    names is a working licence bypass checked into the repo. The scanner
#    below trips on (a) an assignment of a string literal to either name in
#    .py/.sh/.md, (b) any env-write call naming either name (setenv/setdefault/
#    putenv), and (c) any ``env[...] =`` / ``os.environ[...] =`` assignment into
#    either key with ANY right-hand side — pass-through to a child process must
#    happen by inheriting the operator's environment, never by writing the key.
#    The only allowed references are capturd/pro.py's own reads (a read never
#    matches the assignment patterns) and tests/test_pro_env_config.py's
#    monkeypatch-scoped licence fixtures.
# ---------------------------------------------------------------------------

# The two Pro-gate env names, assembled from fragments so this scanner file is
# not itself a text hit for `grep -rn CAPTURD_PRO_CODES` (the guard must be
# able to name what it forbids without ever spelling a provisioning line).
_LICENCE_ENV_VARS = ("CAPTURD_" "PRO_CODES", "RHOBEAR_" "CAPTURD_LICENSE")
_Q = "['\"]"  # either quote style, kept out of the raw f-strings below


def _tracked_licence_scan_files() -> list[Path]:
    """Every git-tracked .py/.sh/.md file (untracked dirs like .venv excluded
    automatically — git ls-files only knows committed paths)."""
    out = subprocess.run(
        ["git", "ls-files", "*.py", "*.sh", "*.md"],
        cwd=REPO, capture_output=True, text=True, check=True)
    return [REPO / line for line in out.stdout.splitlines() if line.strip()]


def _licence_assignment_violations(paths) -> list[str]:
    """Report each line in *paths* that writes a Pro-gate env name.

    A text scan is deliberately crude — that is what CI can enforce. It is a
    tripwire: it catches the literal-name leak (and the provisioning-by-alias
    shape r1 shipped), not every conceivable obfuscation.
    """
    var = "|".join(re.escape(v) for v in _LICENCE_ENV_VARS)
    # (a) an assignment whose right-hand side is a string literal:
    #     VAR = "..." / export VAR='...' / env["VAR"] = f"..."
    #     (unquoted shell right-hand sides are outside this tripwire's net —
    #     it catches the literal leak, it is not a proof)
    literal_assign = re.compile(
        rf"\b(?:export\s+)?(?:{var})\s*=\s*[fFrRbB]{{0,2}}{_Q}")
    # (b) env-write calls naming either key: .setenv(..) / .setdefault(..) /
    #     .putenv(..) — plus keyword-argument provisioning VAR="..." (caught by
    #     (a) anywhere in the line).
    env_write_call = re.compile(
        rf"(?:setenv|setdefault|putenv)\s*\(\s*{_Q}(?:{var}){_Q}")
    # (c) any assignment INTO either env key with any right-hand side — the
    #     harness must never write the gate's inputs, only inherit them.
    env_key_assign = re.compile(
        rf"(?:env|os\.environ)\s*\[\s*{_Q}(?:{var}){_Q}\s*\]\s*=")
    offenders: list[str] = []
    for p in paths:
        rel = (p.relative_to(REPO).as_posix()
               if p.is_relative_to(REPO) else str(p))
        if rel == "tests/test_pro_env_config.py":
            continue  # monkeypatch-scoped licence fixtures — test-only by design
        text = p.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if (literal_assign.search(line) or env_write_call.search(line)
                    or env_key_assign.search(line)):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    return offenders


def test_no_committed_literal_licence_assignment():
    """No tracked .py/.sh/.md may assign to CAPTURD_PRO_CODES or
    RHOBEAR_CAPTURD_LICENSE. The Pro gate compares a caller-supplied value
    against a caller-supplied allow-list, so in-tree provisioning is a working
    licence bypass — the exact hole this PR exists to close (issue #44 owns the
    gate design itself)."""
    offenders = _licence_assignment_violations(_tracked_licence_scan_files())
    assert not offenders, (
        "a tracked file writes a Pro-gate env name — that is a working licence "
        "bypass checked into the repo (take the licence from the operator's "
        "environment or skip loudly; never provision one):\n  "
        + "\n  ".join(offenders))


@pytest.mark.parametrize("kind, body", [
    # planted, out-of-tree violations — the tripwire must fire on the shapes a
    # leak actually takes (here: a .py module and a .sh script).
    ("py", 'import os\n{VAR}="x"\n'),
    ("sh", '#!/bin/sh\nexport {VAR}="y"\n'),
    ("md", '```sh\n{VAR}="z"  # documented provisioning is still provisioning\n```\n'),
])
def test_licence_tripwire_fires_on_planted_violations(tmp_path, kind, body):
    """Plant a literal assignment in a temp file (out of the tracked tree) and
    prove the scanner reports it — the tree-scan test above must never be able
    to go quietly vacuous."""
    planted = tmp_path / f"planted.{kind}"
    # The name is interpolated so this test's own source never contains a
    # literal assignment line for the scanner to trip over.
    planted.write_text(body.format(VAR=_LICENCE_ENV_VARS[0]), encoding="utf-8")
    offenders = _licence_assignment_violations([planted])
    assert len(offenders) == 1, offenders
    assert f"planted.{kind}:2" in offenders[0], offenders
