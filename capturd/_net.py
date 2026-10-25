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
import threading
from typing import Callable
from urllib.parse import urlparse


# ── explicit TEST-ONLY loopback fixture allowance ──────────────────────────
#
# Production code must never import these; tests/test_import_path_hygiene.py
# enforces it (text scan over capturd/**.py, exempting only _net.py itself).
# That scan is a TRIPWIRE on committed source, not a proof: it catches the
# literal-name leak, but a conditional import, a stringly-typed getattr or a
# lazy importlib call built at runtime could dodge it — which is why the
# runtime gate below exists too. The two halves are documented together: the
# scan stops a leak from being COMMITTED; the enable-gate keeps the bypass
# INERT in any process that did not deliberately arm it.
#
# The SSRF guard above correctly rejects 127.0.0.1 — which is exactly what the
# live-drive E2E harness (scripts/e2e_live_drive.py) needs to serve its local
# signup fixture on. Rather than weakening :func:`is_private_ip` (it stays a
# pure, fail-closed predicate), the harness launches the MCP server through an
# explicit test-only wrapper that ARMS the seam
# (``enable_test_fixture_allowance``), registers ONE loopback origin here and
# wraps the server's ``_reject_private_url`` so that — and only that — origin
# is allowed. Properties (all pinned by tests/test_net_fixture.py):
#
# * disarmed by default AND opt-in twice over: nothing is allowed until the
#   process explicitly calls ``enable_test_fixture_allowance`` and then
#   ``register_test_fixture_origin``; production processes (``python -m
#   capturd.mcp.server``, the service, the CLI) never call either and stay
#   fail-closed. There is NO environment variable that enables this — env
#   alone must never turn private URLs into allowed public input.
# * narrowly bound: only an http(s) origin whose host is a LOOPBACK IP literal
#   with an explicit port can be registered (metadata/RFC1918/link-local/
#   hostnames are refused loudly), and ``is_fixture_origin`` matches exactly
#   that (scheme, host, port) triple — no other port, host or scheme.
# * fail-closed: registration and matching raise/return-False on any parse
#   surprise; a reset (clear) disarms the seam again; the wrapped guard
#   delegates everything else to the real one.
# * visible: every arm/registration logs at WARNING and every allowance hit logs
#   with a ``[capturd TEST FIXTURE]`` marker — the FIRST allowance per origin
#   at WARNING (the visible signal a fixture run is running), subsequent hits
#   on the same origin at DEBUG so a long E2E run does not flood the log.

_FIXTURE_LOGGER = logging.getLogger("capturd._net.fixture")

# (scheme, host, port) of the single registered fixture origin, or None.
_fixture_origin: tuple[str, str, int] | None = None

# Module-private arm switch: the allowance is INERT until the test process
# explicitly enables it. Production never calls the enable function, so even
# a leaked register call fails closed. A reset (clear) disarms it again.
_allowance_enabled = False

# Serialises the "warn once, then debug" transition on the set below. The
# allowance CHECK itself is lock-free (a single module-global read); only the
# warned-set mutation is locked, so concurrent allowance hits can't both log
# WARNING or race the set mutation.
_warned_allowances_lock = threading.Lock()

# Origins whose first allowance hit has already been logged at WARNING; reset
# on every register/clear so each fixture session warns once.
_warned_allowances: set[tuple[str, str, int]] = set()


class FixtureOriginError(ValueError):
    """Raised when the fixture seam is used without being enabled, or when a
    non-loopback or malformed origin is registered as a test fixture."""


def enable_test_fixture_allowance() -> None:
    """Arm the fixture seam in THIS process (TEST-ONLY).

    Until this is called, :func:`register_test_fixture_origin` raises and
    :func:`is_fixture_origin` returns False — the seam is inert by default in
    every fresh process. There is deliberately no environment variable and no
    disable counterpart: a reset (:func:`clear_test_fixture_origin`) disarms
    it again. Logs at WARNING so an armed process is visible in any log.
    """
    global _allowance_enabled
    _allowance_enabled = True
    _FIXTURE_LOGGER.warning(
        "[capturd TEST FIXTURE] allowance ARMED in this process — test-only; "
        "production processes never arm the fixture seam")


def register_test_fixture_origin(origin: str) -> str:
    """Register the ONE loopback origin a test fixture may be fetched from.

    TEST-ONLY: production code must never call this. The seam must be armed
    first (:func:`enable_test_fixture_allowance`) or this raises
    :class:`FixtureOriginError` — registration alone enables nothing. Accepts
    only an explicit ``http(s)://<loopback-ip-literal>:<port>`` origin (e.g.
    the live-drive E2E's ``http://127.0.0.1:<ephemeral>/signup-flow.html``
    base). Anything else — RFC1918, cloud metadata, link-local, bare
    ``localhost`` (hostnames could resolve anywhere), missing port, malformed
    URL — raises :class:`FixtureOriginError`. Returns the origin as given.
    """
    global _fixture_origin
    if not _allowance_enabled:
        raise FixtureOriginError(
            "the test-fixture seam is disarmed: call enable_test_fixture_allowance() "
            "in the test process first (there is no environment knob; production "
            "never arms it)")
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
    with _warned_allowances_lock:
        _warned_allowances.clear()
    _FIXTURE_LOGGER.warning(
        "[capturd TEST FIXTURE] loopback allowance registered for %s — "
        "valid ONLY inside this explicitly-launched test process", origin)
    return origin


def clear_test_fixture_origin() -> None:
    """Drop any registered fixture origin and DISARM the seam (back to the
    production fail-closed state — registration raises again until re-enabled)."""
    global _fixture_origin, _allowance_enabled
    _fixture_origin = None
    _allowance_enabled = False
    with _warned_allowances_lock:
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
    other URL and ALWAYS when the seam is disarmed or nothing is registered
    (production default). Every True is logged with a ``TEST FIXTURE`` marker.
    """
    if _fixture_origin is None or not _allowance_enabled:
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
    # log is not flooded on every frame/caption request. The allowance check
    # above is lock-free (one module-global read); only this warned-set
    # transition is serialised.
    key = (scheme, host.lower(), port)
    with _warned_allowances_lock:
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
