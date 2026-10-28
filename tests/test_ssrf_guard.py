"""SSRF guard — the shared canonical predicate the MCP server now uses.

The hosted app and the ``demo.record`` MCP tool both drive a server-side
Playwright render from a user-supplied URL, so the predicate that classifies a
resolved IP as private/internal is a load-bearing security boundary. It must
BLOCK cloud-metadata, loopback, RFC 1918, link-local, and IPv6-mapped-IPv4
addresses, and it must fail CLOSED (treat as private) on anything it cannot
parse. These tests pin that behaviour against the single helper in
``capturd._net`` so the MCP copy cannot silently regress to a hand-rolled
version.
"""
import pytest

from capturd._net import (
    clear_test_fixture_origin,
    enable_test_fixture_allowance,
    is_private_ip,
    register_test_fixture_origin,
)


@pytest.fixture(autouse=True)
def _clean_fixture_registry():
    """Never leak a registered fixture origin across tests: cleanup runs even
    when an assertion fails mid-test (no reliance on a finally surviving).
    Also disarms on entry (clear) then arms, so tests that exercise the
    allowance behave like the explicitly-launched E2E wrapper process; the
    final clear returns the module to the production default (disarmed)."""
    clear_test_fixture_origin()
    enable_test_fixture_allowance()
    yield
    clear_test_fixture_origin()


# Every address here MUST be treated as private/internal (BLOCKED). Includes
# the headline cloud-metadata bypass (IPv6-mapped IPv4) that the old
# hand-rolled guard let through.
@pytest.mark.parametrize(
    "ip",
    [
        # IPv6-mapped IPv4 — cloud metadata / loopback / RFC1918. The old guard
        # routed any ":"-containing string down a weak branch that returned
        # False (allow), so ::ffff:169.254.169.254 slipped straight through.
        "::ffff:169.254.169.254",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        # IPv4 cloud metadata / link-local.
        "169.254.169.254",
        # IPv4 loopback + RFC 1918 (incl. both ends of the 172.16/12 range).
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        # IPv6 loopback, unique-local, link-local.
        "::1",
        "fc00::1",
        "fd12::1",
        "fe80::1",
        # Malformed input must fail CLOSED (treated as private), never allow.
        "999.999.999.999",
        "not-an-ip",
        "",
    ],
)
def test_is_private_ip_blocks_internal_and_malformed(ip):
    assert is_private_ip(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1"])
def test_is_private_ip_allows_public(ip):
    assert is_private_ip(ip) is False


# ---------------------------------------------------------------------------
# Direct public-call regression: the production MCP ``demo.record`` guard must
# keep failing on loopback/metadata/private literals even though the live-drive
# E2E harness (scripts/e2e_live_drive.py) needs a TEST-ONLY loopback allowance.
# IP-literal hosts resolve via getaddrinfo without any DNS traffic, so these
# are deterministic and non-destructive.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/signup-flow.html",
    "http://[::1]/signup-flow.html",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1/",
    "http://172.16.0.1/",
    "http://192.168.1.1/",
    "http://[fe80::1]/",
    # IPv6-mapped IPv4 metadata/loopback — the headline bypass class.
    "http://[::ffff:169.254.169.254]/",
    "http://[::ffff:127.0.0.1]/",
])
def test_production_url_guard_rejects_internal_literals(url):
    from capturd.mcp.server import _reject_private_url

    with pytest.raises(ValueError):
        _reject_private_url(url)


def test_production_url_guard_untouched_by_fixture_registration():
    """The test-fixture seam must be opt-in per process, never global.

    Arming + registering a loopback fixture origin (exactly what the live-drive
    E2E wrapper does in its own subprocess) must NOT relax the production
    ``capturd.mcp.server._reject_private_url`` — the wrapped guard only exists
    in the explicitly-launched test server process.
    """
    from capturd.mcp.server import _reject_private_url

    enable_test_fixture_allowance()
    register_test_fixture_origin("http://127.0.0.1:8123")
    # cleanup is guaranteed by the autouse _clean_fixture_registry fixture;
    # no finally needed (and none survives an assertion failure anyway).
    with pytest.raises(ValueError):
        _reject_private_url("http://127.0.0.1:8123/signup-flow.html")
    with pytest.raises(ValueError):
        _reject_private_url("http://127.0.0.1/signup-flow.html")


def test_mcp_server_delegates_to_shared_guard():
    """The MCP server must not keep a hand-rolled ``_is_private_ip`` copy.

    A local re-implementation is exactly how this bug class regressed before —
    the MCP copy drifted behind the service copy and started letting
    IPv6-mapped IPv4 metadata addresses through. Asserting the attribute is
    gone keeps future agents from silently reintroducing it.
    """
    import capturd.mcp.server

    assert not hasattr(capturd.mcp.server, "_is_private_ip")
    # And the shared predicate is what _reject_private_url now calls.
    assert capturd.mcp.server.is_private_ip is capturd._net.is_private_ip
