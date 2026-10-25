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

import logging
import textwrap

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
# The live-drive E2E wrapper stays wired to this seam (compile + tripwire).
# ---------------------------------------------------------------------------

def test_e2e_wrapper_uses_the_canonical_seam():
    """The e2e server wrapper must compile and route through capturd._net."""
    from e2e_live_drive import SERVER_FIXTURE_WRAPPER

    compile(textwrap.dedent(SERVER_FIXTURE_WRAPPER), "<server-fixture-wrapper>", "exec")
    assert "register_test_fixture_origin" in SERVER_FIXTURE_WRAPPER
    assert "guard_with_test_fixture_allowance" in SERVER_FIXTURE_WRAPPER
    assert "capturd.mcp.server" in SERVER_FIXTURE_WRAPPER
    # The production entry point (`python -m capturd.mcp.server`) stays untouched;
    # the wrapper exists only for the explicitly-launched test server process.
    assert net.registered_test_fixture_origin() is None
