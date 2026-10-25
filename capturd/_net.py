"""SSRF guard — the single canonical helper shared by the hosted service and the
MCP server.

A submitted URL drives a server-side Playwright render (film.py). Without this
guard a user can point a generation at an internal address — ``169.254.169.254``
(cloud metadata), ``127.0.0.1`` (the service itself), or an RFC 1918 host on the
box's private network — and read whatever the render returns. We resolve the
host at submit time and reject private/internal ranges.

This is the ONE copy of the guard. Both ``service/app/main.py`` (the hosted app)
and ``capturd/mcp/server.py`` (the MCP ``demo.record`` tool) import
:func:`is_private_ip` from here so the two surfaces cannot drift apart — which
is exactly how the MCP copy fell behind the service copy and started letting
IPv6-mapped IPv4 metadata addresses through.

This module also hosts the explicit TEST-ONLY loopback fixture allowance
(:func:`register_test_fixture_origin` et al.) used by the live-drive E2E
harness — see the section comment below; :func:`is_private_ip` itself stays a
pure, environment-independent predicate.
"""
from __future__ import annotations

import ipaddress
import logging
from typing import Callable
from urllib.parse import urlparse


# ── explicit TEST-ONLY loopback fixture allowance ──────────────────────────
#
# The SSRF guard above correctly rejects 127.0.0.1 — which is exactly what the
# live-drive E2E harness (scripts/e2e_live_drive.py) needs to serve its local
# signup fixture on. Rather than weakening :func:`is_private_ip` (it stays a
# pure, fail-closed predicate), the harness launches the MCP server through an
# explicit test-only wrapper that registers ONE loopback origin here and wraps
# the server's ``_reject_private_url`` so that — and only that — origin is
# allowed. Properties (all pinned by tests/test_net_fixture.py):
#
# * opt-in: nothing is allowed until ``register_test_fixture_origin`` is
#   called; production processes (``python -m capturd.mcp.server``, the
#   service, the CLI) never call it and see an empty registry. There is NO
#   environment variable that enables this — env alone must never turn
#   private URLs into allowed public input.
# * narrowly bound: only an http(s) origin whose host is a LOOPBACK IP literal
#   with an explicit port can be registered (metadata/RFC1918/link-local/
#   hostnames are refused loudly), and ``is_fixture_origin`` matches exactly
#   that (scheme, host, port) triple — no other port, host or scheme.
# * fail-closed: registration and matching raise/return-False on any parse
#   surprise; the wrapped guard delegates everything else to the real one.
# * visible: every registration logs at WARNING and every allowance hit logs
#   with a ``[capturd TEST FIXTURE]`` marker — the FIRST allowance per origin
#   at WARNING (the visible signal a fixture run is running), subsequent hits
#   on the same origin at DEBUG so a long E2E run does not flood the log.

_FIXTURE_LOGGER = logging.getLogger("capturd._net.fixture")

# (scheme, host, port) of the single registered fixture origin, or None.
_fixture_origin: tuple[str, str, int] | None = None

# Origins whose first allowance hit has already been logged at WARNING; reset
# on every register/clear so each fixture session warns once.
_warned_allowances: set[tuple[str, str, int]] = set()


class FixtureOriginError(ValueError):
    """Raised when a non-loopback or malformed origin is registered as a test fixture."""


def register_test_fixture_origin(origin: str) -> str:
    """Register the ONE loopback origin a test fixture may be fetched from.

    TEST-ONLY: production code must never call this. Accepts only an explicit
    ``http(s)://<loopback-ip-literal>:<port>`` origin (e.g. the live-drive E2E's
    ``http://127.0.0.1:<ephemeral>/signup-flow.html`` base). Anything else —
    RFC1918, cloud metadata, link-local, bare ``localhost`` (hostnames could
    resolve anywhere), missing port, malformed URL — raises
    :class:`FixtureOriginError`. Returns the origin as given.
    """
    global _fixture_origin
    try:
        parts = urlparse(origin)
        port = parts.port      # raises ValueError on malformed ports
        host = parts.hostname  # raises ValueError on malformed IPv6 literals
    except ValueError as exc:
        raise FixtureOriginError(f"malformed fixture origin {origin!r}: {exc}") from exc
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise FixtureOriginError(
            f"fixture origin {origin!r} must be http(s)://, got scheme {parts.scheme!r}")
    if not host:
        raise FixtureOriginError(f"fixture origin {origin!r} has no host")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        raise FixtureOriginError(
            f"fixture origin {origin!r}: host {host!r} is not an IP literal — "
            "only loopback IP literals (127.0.0.0/8, ::1) may be registered") from None
    if not addr.is_loopback:
        raise FixtureOriginError(
            f"fixture origin {origin!r}: {host} is not a loopback address — "
            "the test fixture seam may never allow private/metadata hosts")
    if port is None:
        raise FixtureOriginError(
            f"fixture origin {origin!r} must carry an explicit port")
    if port <= 0:
        raise FixtureOriginError(
            f"fixture origin {origin!r} has an unusable port {port}")
    _fixture_origin = (scheme, host.lower(), port)
    _warned_allowances.clear()
    _FIXTURE_LOGGER.warning(
        "[capturd TEST FIXTURE] loopback allowance registered for %s — "
        "valid ONLY inside this explicitly-launched test process", origin)
    return origin


def clear_test_fixture_origin() -> None:
    """Drop any registered fixture origin (back to production fail-closed state)."""
    global _fixture_origin
    _fixture_origin = None
    _warned_allowances.clear()


def registered_test_fixture_origin() -> str | None:
    """The registered fixture origin string, or None when no fixture is active."""
    if _fixture_origin is None:
        return None
    scheme, host, port = _fixture_origin
    # Bracket IPv6 literals so the rendered origin round-trips through
    # urlparse: "http://[::1]:8123", never the malformed "http://::1:8123".
    host_repr = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host_repr}:{port}"


def is_fixture_origin(url: str) -> bool:
    """True only when *url* IS the single registered loopback fixture origin.

    Exact (scheme, host, port) match against the registration; False for every
    other URL and always when nothing is registered (production default).
    Every True is logged with a ``TEST FIXTURE`` marker.
    """
    if _fixture_origin is None:
        return False
    try:
        parts = urlparse(url)
        port = parts.port      # raises ValueError on malformed ports
        host = parts.hostname  # raises ValueError on malformed IPv6 literals
    except ValueError:
        return False
    scheme = (parts.scheme or "").lower()
    if not host or port is None:
        return False
    if (scheme, host.lower(), port) != _fixture_origin:
        return False
    # First allowance for this origin: WARNING (the visible signal a fixture
    # run is running). Subsequent hits: DEBUG, same marker text, so the E2E
    # log is not flooded on every frame/caption request.
    key = (scheme, host.lower(), port)
    level = logging.WARNING if key not in _warned_allowances else logging.DEBUG
    _warned_allowances.add(key)
    _FIXTURE_LOGGER.log(
        level,
        "[capturd TEST FIXTURE] allowing %s — matches the registered loopback "
        "fixture origin; the SSRF guard is bypassed for THIS origin only in "
        "this test process", url)
    return True


def guard_with_test_fixture_allowance(
    reject_fn: Callable[[str], None],
) -> Callable[[str], None]:
    """Wrap a ``_reject_private_url``-style guard for the test fixture process.

    The returned guard allows ONLY the exact registered loopback fixture origin
    and delegates every other URL — unchanged — to *reject_fn*, so the real
    fail-closed guard still rules everywhere outside the one fixture origin.
    TEST-ONLY: called by the live-drive E2E's server wrapper, never by
    production code.
    """

    def guarded(url: str) -> None:
        if is_fixture_origin(url):
            return
        reject_fn(url)

    return guarded


def is_private_ip(ip_str: str) -> bool:
    """True when *ip_str* is private/internal in any address family.

    Uses the stdlib ``ipaddress`` classification (the earlier hand-rolled guard
    in the MCP server missed IPv6-mapped IPv4 like ``::ffff:169.254.169.254``,
    which slipped past cloud-metadata protection, and silently treated malformed
    IPv4 strings as public). An IPv6-mapped IPv4 address is unwrapped and
    re-checked as its IPv4 self. A string that doesn't parse as an IP at all is
    treated as PRIVATE — fail closed, since ``getaddrinfo`` should only ever
    hand us real IPs.
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True   # unparseable ⇒ fail closed
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)
