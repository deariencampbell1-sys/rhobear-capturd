"""Tests for the explicit TEST-ONLY loopback fixture allowance (capturd._net).

The live-drive E2E (scripts/e2e_live_drive.py) needs its local signup fixture
on 127.0.0.1, which the production SSRF guard correctly rejects. The seam here
lets the TEST HARNESS — and only the test harness — register exactly one
loopback origin in the spawned server process. These tests pin the invariants
the brief demands:

* nothing is allowed unless ``register_test_fixture_origin`` was called
  (production processes never call it; there is NO environment knob);
* only loopback IP-literal origins with an explicit port can be registered —
  metadata/RFC1918/link-local/hostnames are refused loudly;
* the allowance is an exact (scheme, host, port) match — no other port, host
  or scheme slips through, so environment alone can never turn arbitrary
  private URLs into allowed public input;
* every registration and every allowance hit logs a visible
  ``[capturd TEST FIXTURE]`` marker;
* the wrapped guard delegates everything else to the real fail-closed guard.
"""
from __future__ import annotations

import http.server
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
from pathlib import Path
from urllib.parse import urlparse

import pytest

import capturd._net as net
from capturd._net import (
    FixtureOriginError,
    clear_test_fixture_origin,
    guard_with_test_fixture_allowance,
    is_fixture_origin,
    registered_test_fixture_origin,
    register_test_fixture_origin,
)

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_fixture_registry():
    clear_test_fixture_origin()
    yield
    clear_test_fixture_origin()


# ---------------------------------------------------------------------------
# Production default: nothing registered, nothing allowed.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8123/signup-flow.html",
    "http://127.0.0.1/signup-flow.html",
    "http://[::1]:8123/",
    "not a url",
    "",
])
def test_nothing_registered_nothing_allowed(url):
    assert registered_test_fixture_origin() is None
    assert is_fixture_origin(url) is False


# ---------------------------------------------------------------------------
# Registration is loopback-only and loud.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("origin", [
    # cloud metadata / RFC1918 / link-local — never registrable.
    "http://169.254.169.254:80/",
    "http://10.0.0.1:8000",
    "http://172.16.0.1:8000",
    "http://192.168.1.1:8000",
    "http://100.64.0.1:8000",
    "http://[::ffff:169.254.169.254]:80/",
    "http://[fe80::1]:8000/",
    "http://[fd12::1]:8000/",
    # hostnames could resolve anywhere — IP literals only.
    "http://localhost:8123",
    "http://internal.svc:8123",
    # wrong scheme / missing port / malformed.
    "ftp://127.0.0.1:8123",
    "http://127.0.0.1",
    "http://127.0.0.1:0/",
    "not a url",
    "http://127.0.0.1:notaport/",
])
def test_registration_rejects_non_loopback_and_malformed(origin):
    with pytest.raises(FixtureOriginError):
        register_test_fixture_origin(origin)
    assert registered_test_fixture_origin() is None
    assert is_fixture_origin(origin) is False


def test_registration_refuses_metadata_even_with_explicit_port():
    with pytest.raises(FixtureOriginError):
        register_test_fixture_origin("http://169.254.169.254:8123")


@pytest.mark.parametrize("url", ["http://[::1:8123/", "http://[::1/"])
def test_unbalanced_ipv6_bracket_fails_closed(url):
    """A parse surprise (unbalanced IPv6 bracket) must raise FixtureOriginError
    on register and return False on match — never a raw ValueError."""
    with pytest.raises(FixtureOriginError):
        register_test_fixture_origin(url)
    assert registered_test_fixture_origin() is None
    assert is_fixture_origin(url) is False


@pytest.mark.parametrize("origin", [
    "http://127.0.0.1:8123",
    "http://127.0.0.1:8123/",
    "http://[::1]:8123",
    "http://127.1.2.3:9000",   # whole 127.0.0.0/8 is loopback
])
def test_registration_accepts_loopback_ip_literals(origin):
    assert register_test_fixture_origin(origin) == origin
    assert registered_test_fixture_origin() is not None


# ---------------------------------------------------------------------------
# Allowance is an exact (scheme, host, port) match.
# ---------------------------------------------------------------------------

def test_exact_origin_matches_and_nothing_else_does():
    register_test_fixture_origin("http://127.0.0.1:8123")
    assert is_fixture_origin("http://127.0.0.1:8123/signup-flow.html") is True
    assert is_fixture_origin("http://127.0.0.1:8123/") is True
    # wrong port / default port
    assert is_fixture_origin("http://127.0.0.1:9999/signup-flow.html") is False
    assert is_fixture_origin("http://127.0.0.1/signup-flow.html") is False
    # wrong scheme
    assert is_fixture_origin("https://127.0.0.1:8123/signup-flow.html") is False
    # wrong loopback host
    assert is_fixture_origin("http://[::1]:8123/signup-flow.html") is False
    assert is_fixture_origin("http://127.0.0.2:8123/signup-flow.html") is False
    # private/metadata hosts never match, registration or not
    assert is_fixture_origin("http://169.254.169.254:8123/") is False
    assert is_fixture_origin("http://10.0.0.1:8123/") is False
    # userinfo tricks still fail closed
    assert is_fixture_origin("http://127.0.0.1:8123@evil.example/") is False


def test_reregistration_replaces_the_single_origin():
    register_test_fixture_origin("http://127.0.0.1:8123")
    register_test_fixture_origin("http://127.0.0.1:9001")
    assert is_fixture_origin("http://127.0.0.1:8123/") is False
    assert is_fixture_origin("http://127.0.0.1:9001/") is True
    assert registered_test_fixture_origin() == "http://127.0.0.1:9001"


def test_ipv6_origin_round_trips_bracketed():
    """registered_test_fixture_origin() must bracket IPv6 literals so the
    rendered origin is itself a valid URL that can be registered again."""
    assert register_test_fixture_origin("http://[::1]:8123") == "http://[::1]:8123"
    rendered = registered_test_fixture_origin()
    assert rendered == "http://[::1]:8123"  # bracketed — NOT "http://::1:8123"
    # idempotent: the rendered form registers again, unchanged
    assert register_test_fixture_origin(rendered) == rendered
    assert registered_test_fixture_origin() == rendered
    # matching still exact: right host+port matches, other port does not
    assert is_fixture_origin("http://[::1]:8123/x") is True
    assert is_fixture_origin("http://[::1]:9999/x") is False


def test_clear_restores_production_default():
    register_test_fixture_origin("http://127.0.0.1:8123")
    clear_test_fixture_origin()
    assert is_fixture_origin("http://127.0.0.1:8123/") is False
    assert registered_test_fixture_origin() is None


# ---------------------------------------------------------------------------
# Every allowance is visibly marked as a fixture in the logs.
# ---------------------------------------------------------------------------

def test_registration_and_allowance_are_logged_as_fixture(caplog):
    with caplog.at_level(logging.WARNING, logger="capturd._net.fixture"):
        register_test_fixture_origin("http://127.0.0.1:8123")
        assert is_fixture_origin("http://127.0.0.1:8123/signup-flow.html") is True
        assert is_fixture_origin("http://127.0.0.1") is False  # no explicit port

    fixture_lines = [r.message for r in caplog.records if "TEST FIXTURE" in r.message]
    assert any("registered" in m for m in fixture_lines), fixture_lines
    assert any("allowing" in m and "8123" in m for m in fixture_lines), fixture_lines


# ---------------------------------------------------------------------------
# The wrapped guard: fixture origin allowed, everything else delegated to the
# real (fail-closed) guard.
# ---------------------------------------------------------------------------

def _real_guard(url: str) -> None:
    raise ValueError(f"real guard rejects: {url}")


def test_wrapped_guard_allows_only_exact_fixture_origin():
    register_test_fixture_origin("http://127.0.0.1:8123")
    guard = guard_with_test_fixture_allowance(_real_guard)
    guard("http://127.0.0.1:8123/signup-flow.html")  # must NOT raise
    with pytest.raises(ValueError):
        guard("http://127.0.0.1:9999/signup-flow.html")
    with pytest.raises(ValueError):
        guard("http://169.254.169.254:8123/")
    with pytest.raises(ValueError):
        guard("http://127.0.0.1:8123@evil.example/")


def test_wrapped_guard_with_no_registration_delegates_everything():
    guard = guard_with_test_fixture_allowance(_real_guard)
    with pytest.raises(ValueError):
        guard("http://127.0.0.1:8123/signup-flow.html")


# ---------------------------------------------------------------------------
# The live-drive E2E wrapper stays wired to this seam (compile + tripwire),
# and the rebind it performs is proven BEHAVIOURALLY below.
# ---------------------------------------------------------------------------

def test_e2e_wrapper_uses_the_canonical_seam():
    """The e2e server wrapper must compile and route through capturd._net."""
    # Import ONLY the small constants module — never the e2e_live_drive driver
    # (which drags in the MCP stdio client and, transitively, playwright).
    from _e2e_fixture_wrapper import SERVER_FIXTURE_WRAPPER

    compile(textwrap.dedent(SERVER_FIXTURE_WRAPPER), "<server-fixture-wrapper>", "exec")
    assert "register_test_fixture_origin" in SERVER_FIXTURE_WRAPPER
    assert "guard_with_test_fixture_allowance" in SERVER_FIXTURE_WRAPPER
    assert "capturd.mcp.server" in SERVER_FIXTURE_WRAPPER
    # The production entry point (`python -m capturd.mcp.server`) stays untouched;
    # the wrapper exists only for the explicitly-launched test server process.
    assert net.registered_test_fixture_origin() is None


def test_wrapper_module_import_does_not_import_the_e2e_driver():
    """Importing _e2e_fixture_wrapper must stay cheap: no e2e_live_drive (and
    no playwright) may land in sys.modules. Runs in a fresh subprocess so the
    assertion cannot be skewed by other tests importing the driver earlier."""
    code = (
        "import sys, _e2e_fixture_wrapper\n"
        "assert 'e2e_live_drive' not in sys.modules, 'e2e_live_drive got imported'\n"
        "assert 'playwright' not in sys.modules, 'playwright got imported'\n"
        "print('CLEAN')\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "scripts")
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        env=env, timeout=30, cwd=tempfile.gettempdir())
    assert proc.returncode == 0, proc.stderr
    assert "CLEAN" in proc.stdout


def test_e2e_dev_license_code_is_the_documented_fixture_constant():
    """The dev license code lives in one documented TEST-ONLY constant, not as
    a bare string in the driver (review: hardcoded code, undeclared)."""
    from _e2e_fixture_wrapper import E2E_DEV_LICENSE_CODE

    assert E2E_DEV_LICENSE_CODE == "capturd-e2e-dev-code"
    driver = (REPO / "scripts" / "e2e_live_drive.py").read_text(encoding="utf-8")
    assert 'env["CAPTURD_PRO_CODES"] = E2E_DEV_LICENSE_CODE' in driver
    assert 'env["RHOBEAR_CAPTURD_LICENSE"] = E2E_DEV_LICENSE_CODE' in driver
    assert '"capturd-e2e-dev-code"' not in driver  # no bare literal left behind


class _LiveFixtureHandler(http.server.BaseHTTPRequestHandler):
    """One tiny always-200 origin, so the wrapper test proves a LIVE allow."""

    def do_GET(self):
        body = b"captured-e2e-fixture-ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep test output clean
        pass


def _run_wrapper_against_fake_server(origin: str) -> dict:
    """Run the REAL SERVER_FIXTURE_WRAPPER body in a fresh subprocess against a
    fake ``capturd.mcp.server`` and return its verdict dict.

    The fake package on sys.path ships a byte-for-byte copy of the real
    ``capturd/_net.py`` (copied at test time, so the wrapper runs the real seam
    logic); only ``capturd.mcp.server`` is faked, with a fail-closed
    ``_reject_private_url`` that rejects EVERYTHING — so any ALLOWED result can
    only come from the wrapper's registration + rebind actually taking effect.
    """
    from _e2e_fixture_wrapper import SERVER_FIXTURE_WRAPPER

    with tempfile.TemporaryDirectory(prefix="wrapper-check-") as tmpname:
        tmp = Path(tmpname)
        pkg = tmp / "capturd" / "mcp"
        pkg.mkdir(parents=True)
        (tmp / "capturd" / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        shutil.copyfile(REPO / "capturd" / "_net.py", tmp / "capturd" / "_net.py")
        port = urlparse(origin).port
        server_py = textwrap.dedent(f"""\
            import json
            import urllib.request

            REGISTERED = {origin!r}
            UNMET_PORT = "http://127.0.0.1:{port + 1}/x"
            METADATA = "http://169.254.169.254/latest/meta-data/"
            RFC1918 = "http://10.1.2.3/x"

            def _reject_private_url(url):
                # Stand-in for the production fail-closed guard: rejects
                # EVERYTHING, so any ALLOWED result below can only come from
                # the wrapper's rebind.
                raise ValueError("fail-closed stand-in rejected: " + url)

            def main():
                results = {{}}
                # (a) the registered loopback origin must be allowed by the
                # wrapped guard AND actually served by the live http.server.
                try:
                    _reject_private_url(REGISTERED)
                    with urllib.request.urlopen(REGISTERED, timeout=5) as resp:
                        body = resp.read()
                    results["registered"] = "ALLOWED %d" % len(body)
                except Exception as exc:
                    results["registered"] = "FAILED %r" % (exc,)
                # (b)-(d) everything else must still hit the fail-closed stand-in.
                for name, url in (("unmet_port", UNMET_PORT),
                                  ("metadata", METADATA), ("rfc1918", RFC1918)):
                    try:
                        _reject_private_url(url)
                        results[name] = "ALLOWED (BAD)"
                    except ValueError:
                        results[name] = "REJECTED"
                print("WRAPPER_CHECK " + json.dumps(results))
                return 0
            """)
        (pkg / "server.py").write_text(server_py, encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(tmp)  # fake capturd wins; real one must not load
        proc = subprocess.run(
            [sys.executable, "-c", SERVER_FIXTURE_WRAPPER, origin],
            capture_output=True, text=True, timeout=60, env=env, cwd=tmp)
    assert proc.returncode == 0, (
        f"wrapper subprocess failed rc={proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("WRAPPER_CHECK ")]
    assert len(lines) == 1, (
        f"expected exactly one WRAPPER_CHECK line, got {lines}\nstderr:\n{proc.stderr}")
    return json.loads(lines[0].split(" ", 1)[1])


def test_wrapper_rebind_allows_only_the_registered_live_origin():
    """Behavioural proof of the E2E wrapper's rebind (the old test only
    compile-checked the source string). Runs the real wrapper body in a fresh
    subprocess against a fake server module and a REAL http.server on
    127.0.0.1 with an ephemeral port, asserting:
      (a) the registered loopback origin is allowed (and actually served),
      (b) an unmet loopback port is rejected,
      (c) a cloud-metadata URL is rejected,
      (d) an RFC1918 URL is rejected.
    """
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LiveFixtureHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        origin = f"http://127.0.0.1:{port}/signup-flow.html"
        results = _run_wrapper_against_fake_server(origin)
        assert results["registered"].startswith("ALLOWED "), results
        assert results["unmet_port"] == "REJECTED", results
        assert results["metadata"] == "REJECTED", results
        assert results["rfc1918"] == "REJECTED", results
    finally:
        srv.shutdown()
        srv.server_close()
