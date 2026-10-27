"""URL resolution and target planning for map-driven capture — the seam.

This is the layer between the frozen map module (:mod:`capturd.shots.pathway_map`,
which parses a pathway map) and the capture engine (:mod:`capturd.shots.capture`,
which runs the plan). It does exactly two jobs, both ported from the old
service's ``pathway_map.py`` (``resolve_pathway_url`` :505, ``plan_targets_from_map``
:531 and the helpers they call) and never re-implements anything the frozen
modules already do:

* :func:`resolve_pathway_url` — a route path from the manifest becomes an
  absolute URL against ``base_url``; a route that is already absolute is kept
  as-is (never rebased); a route that cannot be resolved raises
  :class:`~capturd.shots.pathway_map.PathwayMapError` NAMING THE PATHWAY —
  never a silent ``""``.
* :func:`plan_targets_from_map` — one :class:`~capturd.shots.capture.CaptureTarget`
  per capturable pathway, carrying ``pathway_id``, ``pathway_status``,
  ``pathway_trigger``, the resolved URL, and the selector where the map names
  a control. The selector is never hand-rolled here: the named control is
  bound to a candidate from :mod:`capturd.shots.planner` planned over the
  target page, and the candidate's own selector is what the target carries.

Where the map names a control means, in priority order: the pathway's
``selector``/``css`` column (the explicit control reference), a quoted name or
selector-shaped token in its ``trigger`` for live planner binding, and the call the pathway names
(``downstreamCall``'s route path — a verifier finding names the control by the
call it makes). The named control is matched against the page's planner
candidates by exact selector, then exact target, then label.

In dry-run there is no page read at all: the plan is built without launching
a browser and without any network call. A target's ``trigger_selector`` then
comes only from the explicit ``selector``/``css`` column, and a named control that would
need the page to bind is reported unbound (``selector=-``) with a flag saying
so (:func:`named_control_needs_the_page`).

Determinism: the same map plus the same base URL produces the same plan, byte
for byte. Nothing here embeds host paths — ``pathway_trigger`` is carried with
host-path-shaped tokens (Windows or POSIX) redacted, errors name the
pathway id, never a path, and a directory-shaped ``base_url`` is refused
outright rather than joined into one.

Every resolved HTTP(S) URL is checked even if no control needs a page read.
The same request guard protects planning and capture contexts, including redirects,
subresources and final URLs. Only the exact base hostname can use the loopback
exception. Direct planner/reader calls retain local-tool defaults; build_capture_plan
passes False unless the CLI supplies its internal opt-in. Metadata and other private
ranges remain refused even on that host. Chromium resolves again at connect time:
this userspace check cannot pin its socket DNS, so rebinding remains a residual.
DNS checks run off-loop with a 10-second coroutine timeout; checks/read batches have
120-second coroutine deadlines. A stuck OS resolver thread cannot be terminated by
asyncio deadlines and may delay executor shutdown; do not claim an OS-level bound.

The read is best-effort for reachability: an unreachable page means no
selector, not a refused plan — reachability is a capture-time fact (a guard
refusal is never swallowed). Every swallowed read or planning failure emits
a named ``RuntimeWarning`` diagnostic — the exception class name only, never
raw exception text, URLs, credentials or host paths. All plan-time reading
is one batch (one Playwright driver, one browser, one context per
non-dry-run plan) behind a synchronous entry point: a call from an
already-running event loop is refused with a named error before anything
launches.
"""

from __future__ import annotations

import asyncio
import re
import warnings
from typing import Any
from urllib.parse import urljoin, urlparse

from .capture import CaptureTarget, RestedCaptureError, normalize_url
from .pathway_map import PathwayMapError

__all__ = [
    "MAX_TARGETS",
    "PathwayMapError",
    "named_control_needs_the_page",
    "plan_targets_from_map",
    "resolve_pathway_url",
]

#: The service's capture-target ceiling (capture_service.py's ``MAX_TARGETS``).
#: The refusal text below is that service's, verbatim.
MAX_TARGETS = 5000
PLAN_NAV_TIMEOUT_MS = 15000
PLAN_BATCH_TIMEOUT_S = 120

_URL_RE = re.compile(r"https?://[^\s`'\"]+", re.IGNORECASE)

_VIEW_FROM_FILE: dict[str, str] = {
    "restedcaptureview": "capture",
    "hubview": "hub",
    "boardview": "board",
    "workbenchview": "workbench",
    "catalogview": "catalog",
    "skillsview": "skills",
    "settingsview": "settings",
    "vaultview": "vault",
    "cronview": "cron",
    "worldsview": "worlds",
    "cliview": "cli",
    "learnview": "learn",
    "app.jsx": "hub",
}

_ABS_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
_WINDOWS_PATH_RE = re.compile(r"^[a-zA-Z]:[\\/]|^\\\\")
_HOST_PATH_TOKEN_RE = re.compile(r"(?<!\w)[a-zA-Z]:[\\/][^\s\"'`]*|\\\\[^\s\"'`]*")
# D94: POSIX host paths redact too. A token is a host path when it is an
# absolute path of two or more segments that is rooted at a well-known host
# root, or is file-shaped (a dot extension, optional :line[:col]). Depth
# alone is never enough — a route path (/settings, /api/v1/state) is not a
# host path and stays readable. The lookbehind keeps URLs (http://x/y) from
# being read as paths.
_POSIX_PATH_TOKEN_RE = re.compile(r"(?<![\w./\\])/[^\s\"'`)\]]+")
_POSIX_HOST_ROOTS = frozenset({
    "home", "users", "user", "root", "tmp", "var", "opt", "mnt", "media",
    "srv", "etc", "usr", "data", "work", "dev", "private", "applications",
    "system", "library", "volumes", "proc", "run", "bin", "sbin", "boot",
    "lib", "lib64",
})
_POSIX_FILE_SUFFIX_RE = re.compile(r"\.\w{1,12}(?::\d+(?::\d+)?)?$")


def _posix_path_repl(match: re.Match[str]) -> str:
    token = match.group(0)
    if token.startswith("//"):
        return token
    segments = [seg for seg in token.strip("/").split("/") if seg]
    if len(segments) < 2:
        return token   # a single-segment path is a route, not a host path
    # Depth alone never makes a host path (a 3-segment route like
    # /api/v1/state stays readable) — a host root or a file suffix does.
    if (
        segments[0].lower() in _POSIX_HOST_ROOTS
        or _POSIX_FILE_SUFFIX_RE.search(segments[-1])
    ):
        return "<path>"
    return token


def _redact_host_paths(text: str) -> str:
    """Host-path-shaped tokens become ``<path>`` — the plan and its errors
    must carry no host paths (the determinism rule), on any platform: Windows
    drive-letter and UNC paths, and POSIX paths alike. A route path is not a
    host path and is kept."""
    redacted = _HOST_PATH_TOKEN_RE.sub("<path>", text or "")
    return _POSIX_PATH_TOKEN_RE.sub(_posix_path_repl, redacted)


def _pathway_label(pathway_id: str) -> str:
    pid = _redact_host_paths(str(pathway_id or "")).strip()
    return f"pathway '{pid}'" if pid else "pathway (unnamed)"


# ---------------------------------------------------------------------------
# Ported helpers (sunsponge pathway_map.py :487, :497)
# ---------------------------------------------------------------------------


def _infer_view_from_location(location: str) -> str | None:
    text = (location or "").replace("\\", "/")
    # Strip a trailing :line[:col] WITHOUT breaking a Windows drive letter
    # ("C:") — a manifest can name a host path and we must not echo or trip
    # over it.
    file_part = re.sub(r":\d+(?::\d+)?$", "", text).lower()
    base = file_part.rsplit("/", 1)[-1]
    compact = base.replace("_", "").replace("-", "")
    for key, view in _VIEW_FROM_FILE.items():
        if key.replace(".", "") in compact or key in base:
            return view
    return None


def _extract_route_path(text: str) -> str | None:
    for match in re.finditer(
        r"\b(GET|POST|PUT|DELETE|PATCH|WS)\s+(/[^\s`'\"]+)", text or "", re.IGNORECASE
    ):
        return match.group(2).strip()
    for match in re.finditer(r"(/api/[^\s`'\"]+)", text or ""):
        return match.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# 1. Route resolution
# ---------------------------------------------------------------------------


def resolve_pathway_url(route_path: str, base_url: str, *, pathway_id: str = "") -> str:
    """Resolve one route path to an absolute URL against ``base_url``.

    Ported behaviour: a route path from the manifest becomes an absolute URL
    against ``base_url``; a route that is already absolute is kept as-is (never
    rebased, never doubled against the base); a route that cannot be resolved
    raises :class:`PathwayMapError` naming the pathway — never a silent ``""``.
    A host path is never a route, and a non-http(s) scheme is never a route.
    """
    route = str(route_path or "").strip()
    base = str(base_url or "").strip()
    label = _pathway_label(pathway_id)
    if not route:
        raise PathwayMapError(
            f"{label}: route is empty — refusing to return a silent empty URL"
        )
    if _ABS_URL_RE.match(route):
        return route
    if _WINDOWS_PATH_RE.match(route) or "\\" in route:
        raise PathwayMapError(
            f"{label}: route is not a route path — a host path is never a route"
        )
    if _SCHEME_RE.match(route):
        raise PathwayMapError(
            f"{label}: route scheme is not http(s) — cannot resolve"
        )
    if not base:
        raise PathwayMapError(
            f"{label}: route cannot be resolved — no base URL to resolve against"
        )
    if _ABS_URL_RE.match(base) or base.startswith("file:"):
        joined = urljoin(base.rstrip("/") + "/", route.lstrip("/"))
    else:
        # A local built-HTML base (a directory path) joins as a string —
        # urljoin's URL semantics do not apply to a bare path.
        joined = base.rstrip("/") + "/" + route.lstrip("/")
    if not joined:
        raise PathwayMapError(
            f"{label}: route did not resolve to a URL against the base URL"
        )
    return joined


def _root_url(base_url: str, pathway_id: str) -> str:
    """The pathway's page is the base itself — the ported root fallback."""
    try:
        return normalize_url(base_url)
    except Exception:
        raise PathwayMapError(
            f"{_pathway_label(pathway_id)}: base URL cannot be resolved"
        ) from None


def _resolve_pathway(pathway: dict[str, Any], base_url: str, routes: list[dict[str, Any]]) -> str:
    """The ported per-pathway chain (sunsponge ``resolve_pathway_url`` :505):
    an absolute URL in the pathway's fields wins, then the UI view inferred
    from its location, then a GET/WS route from the routes table, then the
    root fallback."""
    pid = str(pathway.get("id") or "")
    for field in ("downstreamCall", "downstream_call", "trigger", "handler", "declaredIntent"):
        url_match = _URL_RE.search(str(pathway.get(field) or ""))
        if url_match:
            return resolve_pathway_url(url_match.group(0), base_url, pathway_id=pid)

    view = _infer_view_from_location(str(pathway.get("location") or ""))
    root = base_url.rstrip("/")
    if view and view != "hub":
        # Constructed from the base itself — not a route, so no route refusal.
        return f"{root}/?view={view}"

    route_path = _extract_route_path(
        str(pathway.get("downstreamCall") or pathway.get("downstream_call") or "")
    )
    if route_path and routes and not route_path.startswith("/api/"):
        for route in routes:
            if route.get("path") == route_path and str(route.get("method", "GET")).upper() in {"GET", "WS"}:
                return resolve_pathway_url(route_path, base_url, pathway_id=pid)

    return _root_url(base_url, pid)


# ---------------------------------------------------------------------------
# Named controls — bound to planner candidates, never hand-rolled
# ---------------------------------------------------------------------------

_TRIGGER_VERBS = frozenset({
    "load", "click", "fetch", "submit", "navigate", "render", "hover", "press",
    "open", "select", "input", "change", "post", "get", "put", "delete",
    "patch", "ws", "start", "poll", "setinterval",
})

_QUOTED_RE = re.compile(r"[\"'`]([^\"'`\n]+)[\"'`]")
_WORD_RE = re.compile(r"[A-Za-z][\w\-]*")
# A selector-shaped token is a standalone ``#id`` / ``.class`` token — the
# lookbehind keeps a method access (``el.querySelector``) from reading as one.
# The alternation is group(1) so its matches read as the token itself.
_SELECTORISH_RE = re.compile(r"(?<![\w])(#[A-Za-z][\w\-]*|\.[A-Za-z][\w\-]*)")


def _named_control_tokens(pathway: dict[str, Any]) -> list[str]:
    """The control names the map carries for one pathway, in priority order."""
    tokens: list[str] = []
    seen: set[str] = set()

    def add(value: str, min_len: int = 1) -> None:
        value = (value or "").strip().strip("`").strip()
        if not value or len(value) < min_len:
            return
        if value.lower() in _TRIGGER_VERBS or value in seen:
            return
        seen.add(value)
        tokens.append(value)

    add(str(pathway.get("selector") or pathway.get("css") or ""))

    trigger = str(pathway.get("trigger") or "")
    for match in _QUOTED_RE.finditer(trigger):
        add(match.group(1))
    for match in _SELECTORISH_RE.finditer(trigger):
        add(match.group(1))
    unquoted = _QUOTED_RE.sub(" ", trigger)
    for match in _WORD_RE.finditer(unquoted):
        add(match.group(0), min_len=4)

    call = str(pathway.get("downstreamCall") or pathway.get("downstream_call") or "")
    route = _extract_route_path(call) or (call.strip() if call.strip().startswith("/") else "")
    if route:
        add(route)
        add(route.rstrip("/").rsplit("/", 1)[-1], min_len=3)
    return tokens


def _explicit_selector(pathway: dict[str, Any]) -> str:
    """The selector the map itself names — never read from a page.

    D92: only the explicit ``selector``/``css`` column qualifies. Trigger
    prose, quoted labels, bare words, and downstream calls require live
    planner binding and yield no explicit dry-run selector.
    """
    explicit = str(pathway.get("selector") or pathway.get("css") or "").strip().strip("`").strip()
    if explicit:
        return explicit
    return ""


def named_control_needs_the_page(pathway: dict[str, Any]) -> bool:
    """True when the map names a control only in a way that needs the page.

    D92: a named control the map hands no selector for can only be bound by
    reading the page, so a plan that reads no page (dry-run) reports it as
    unbound (``selector=-``) with a flag saying so.
    """
    return not _explicit_selector(pathway) and bool(_named_control_tokens(pathway))


def _norm_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _match_control(tokens: list[str], candidates: list[Any]) -> Any | None:
    """Bind a named control to a planner candidate: exact selector, then
    exact target, then label. The candidate's own scrubbed selector is the
    selector — this module never builds one."""
    for token in tokens:
        for candidate in candidates:
            if candidate.selector and token == candidate.selector:
                return candidate
    for token in tokens:
        for candidate in candidates:
            if candidate.target and token == candidate.target:
                return candidate
    label_matches: list[tuple[int, int, Any]] = []
    for token_index in range(len(tokens)):
        for end in range(token_index + 1, len(tokens) + 1):
            wanted = _norm_label(" ".join(tokens[token_index:end]))
            if len(wanted) < 3:
                continue
            for candidate_index, candidate in enumerate(candidates):
                if wanted == _norm_label(candidate.label):
                    label_matches.append((token_index, candidate_index, candidate))
    if label_matches:
        return max(
            label_matches,
            key=lambda item: (len(_norm_label(item[2].label)), -item[0], -item[1]),
        )[2]
    return None


def _reject_running_loop() -> None:
    """The synchronous contract: planning drives its own one-shot event loop.

    ``asyncio.run`` cannot be called from a running loop, so a plan invoked
    from async code is refused with a named error BEFORE anything launches.
    Dry-run never performs this check.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RestedCaptureError(
        "pathway planning is synchronous; call it outside an active event loop"
    )


def _load_pages_html(
    urls: list[str], *, allow_loopback: bool = False, loopback_host: str | None = None,
    allow_private_hosts: tuple[str, ...] = (),
    navigation_cache: dict[tuple[Any, ...], RestedCaptureError | None] | None = None,
) -> dict[str, str]:
    """Read the target pages' HTML through the real capture path — one batch.

    Every HTTP(S) URL, request and final URL uses capture's exact-base-host
    loopback policy; metadata remains refused. The FIRST refusal is retained.
    See module documentation for DNS rebinding and OS resolver limitations.

    M-browser-lifecycle: ONE batch read per non-dry-run plan — one Playwright
    driver, one browser, one context, one page per URL sequential inside it,
    exactly one ``asyncio.run`` around the whole batch. Pages, context and
    browser close on every path. A call from an already-running event loop is
    refused up front (see :func:`_reject_running_loop`).

    Monkeypatchable seam for tests; best-effort per URL for reachability — a
    per-URL read failure yields ``""`` plus a named ``RuntimeWarning`` (the
    exception class name only — never raw text, URLs or host paths) — but a
    guard refusal is never swallowed.
    """
    _reject_running_loop()
    from .capture import _launch_browser, _check_navigation_url, _install_navigation_guard

    # Direct reader compatibility: only its first URL supplies the base host.
    if loopback_host is None and urls:
        loopback_host = urlparse(urls[0]).hostname
    if navigation_cache is None:
        navigation_cache = {}

    async def _read_all() -> dict[str, str]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - engine dependency
            raise RuntimeError(
                "Playwright is not installed. Install engine requirements, then run "
                "`python -m playwright install chromium` if system Edge is unavailable."
            ) from exc

        html_by_url: dict[str, str] = {}
        for url in urls:
            await _check_navigation_url(
                url, allow_loopback=allow_loopback, loopback_host=loopback_host,
                allow_private_hosts=allow_private_hosts, cache=navigation_cache,
            )

        async with async_playwright() as p:
            # allow_file_access is false: `file:` bases are refused at
            # plan_targets_from_map entry, so this reader never needs them.
            browser = await _launch_browser(p, allow_file_access=False)
            context = None
            try:
                context = await browser.new_context()
                # Before any page exists: every request, redirects included.
                guard_errors = await _install_navigation_guard(
                    context, allow_loopback=allow_loopback, loopback_host=loopback_host,
                    allow_private_hosts=allow_private_hosts, navigation_cache=navigation_cache,
                )
                for url in urls:
                    guard_errors.clear()
                    page = await context.new_page()
                    try:
                        try:
                            await page.goto(url, wait_until="domcontentloaded", timeout=PLAN_NAV_TIMEOUT_MS)
                            if guard_errors:
                                raise guard_errors[0]
                            await _check_navigation_url(
                                page.url, allow_loopback=allow_loopback, loopback_host=loopback_host,
                                allow_private_hosts=allow_private_hosts, cache=navigation_cache,
                            )
                            html_by_url[url] = await page.content()
                        except RestedCaptureError:
                            raise
                        except Exception as exc:
                            if guard_errors:
                                # A guard refusal is a security fact — never
                                # the reachability best-effort below.
                                raise guard_errors[0]
                            warnings.warn(
                                "pathway page read failed "
                                f"({type(exc).__name__}); affected controls stay unbound",
                                RuntimeWarning,
                                stacklevel=2,
                            )
                            html_by_url[url] = ""
                    finally:
                        await page.close()
                if guard_errors:
                    raise guard_errors[0]
            finally:
                try:
                    if context is not None:
                        await context.close()
                finally:
                    await browser.close()
        return html_by_url

    async def _bounded() -> dict[str, str]:
        try:
            return await asyncio.wait_for(_read_all(), timeout=PLAN_BATCH_TIMEOUT_S)
        except (asyncio.TimeoutError, TimeoutError):
            raise RestedCaptureError("pathway page read timed out") from None

    return asyncio.run(_bounded())


def _plan_page(html: str) -> list[Any]:
    from . import planner

    return planner.plan_candidates(planner.parse_html(html or "")).candidates


# ---------------------------------------------------------------------------
# 2. Target planning
# ---------------------------------------------------------------------------


def _require_http_base(base: str) -> None:
    """M-directory-base: the base is an absolute http(s) URL with a hostname.

    A bare directory (POSIX or Windows), a ``file:`` URL, a relative path or
    any other scheme is a HOST PATH base — it would join into a host path and
    reach shareable plan output verbatim. It is refused with one named error
    BEFORE any resolution or navigation; the URL itself is never echoed (the
    "no host paths" promise covers refusals too).
    """
    parsed = urlparse(base)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise RestedCaptureError(
            "base_url must be an absolute http(s) URL; local directory paths are not allowed"
        )


def plan_targets_from_map(
    pathway_map: dict[str, Any],
    base_url: str,
    *,
    max_targets: int = MAX_TARGETS,
    viewport_count: int = 1,
    scheme_count: int = 1,
    dry_run: bool = False,
    allow_loopback: bool = False,
    allow_private_hosts: tuple[str, ...] = (),
) -> tuple[list[str], list[CaptureTarget]]:
    """Expand a parsed map into unique URLs and one target per capturable
    pathway.

    Each target carries ``pathway_id``, ``pathway_status``, ``pathway_trigger``
    (host paths redacted), the resolved URL and — where the map names a
    control — the selector from the planner's candidate for that control.

    With ``dry_run`` the plan is built without launching a browser and
    without any network call: ``trigger_selector`` comes only from what the
    map itself names (:func:`_explicit_selector`), and a named control that
    would need the page to bind is left unbound (``selector=-``) — whether it
    needs the page is :func:`named_control_needs_the_page`.

    ``viewport_count`` x ``scheme_count`` is the capture matrix every pathway
    expands across; ``max_targets`` bounds the EXPANDED target count
    (pathways x viewports x schemes) and the refusal fires before any
    navigation. The 1x1 defaults preserve direct-call behavior.

    More than ``max_targets`` expanded targets is refused before anything is
    planned or navigated, with the service's MAX_TARGETS refusal text verbatim.
    """
    base = str(base_url or "").strip()
    if not base:
        raise RestedCaptureError("base_url is required when feeding a pathway map")
    _require_http_base(base)

    pathways = pathway_map.get("pathways") or []
    if not pathways:
        raise RestedCaptureError("pathway map contains no pathways")
    if viewport_count < 1 or scheme_count < 1:
        raise RestedCaptureError("viewport_count and scheme_count must be positive")
    if len(pathways) * viewport_count * scheme_count > max_targets:
        raise RestedCaptureError(f"too many capture targets; maximum is {max_targets}")
    if not dry_run:
        # The synchronous contract — dry-run never performs this check.
        _reject_running_loop()

    routes = pathway_map.get("routes") or []
    resolved: list[tuple[str, dict[str, Any], list[str]]] = []
    for pathway in pathways:
        url = _resolve_pathway(pathway, base, routes)
        resolved.append((url, pathway, _named_control_tokens(pathway)))

    urls: list[str] = []
    seen: set[str] = set()
    for url, _pathway, _tokens in resolved:
        if url not in seen:
            seen.add(url)
            urls.append(url)

    html_by_url: dict[str, str] = {}
    plans_by_url: dict[str, list[Any]] = {}
    navigation_cache: dict[tuple[Any, ...], RestedCaptureError | None] = {}

    if not dry_run:
        from .capture import _check_navigation_url, _unwrapped_address, is_private_ip

        # Reader entry checks own these URLs; preflight only the others. A
        # private literal is still checked here so a reader seam cannot bypass
        # the plan's security invariant; the shared cache prevents a re-resolve.
        needs_page = {url for url, _pathway, tokens in resolved if tokens}
        read_urls = [url for url in urls if url in needs_page]

        async def _check_all() -> None:
            for url in urls:
                host = urlparse(url).hostname or ""
                private_literal = _unwrapped_address(host) is not None and is_private_ip(host)
                if url in read_urls and not private_literal:
                    continue
                await _check_navigation_url(
                    url, allow_loopback=allow_loopback,
                    loopback_host=urlparse(base).hostname,
                    allow_private_hosts=allow_private_hosts, cache=navigation_cache,
                )

        async def _bounded_checks() -> None:
            try:
                await asyncio.wait_for(_check_all(), timeout=PLAN_BATCH_TIMEOUT_S)
            except (asyncio.TimeoutError, TimeoutError):
                raise RestedCaptureError("pathway URL checks timed out") from None

        asyncio.run(_bounded_checks())
        # M-browser-lifecycle: ONE batch read for the whole plan — the unique
        # URLs that actually have named-control tokens.
        if read_urls:
            try:
                html_by_url.update(_load_pages_html(
                    read_urls, allow_loopback=allow_loopback,
                    loopback_host=urlparse(base).hostname,
                    allow_private_hosts=allow_private_hosts,
                    navigation_cache=navigation_cache,
                ))
            except RestedCaptureError:
                # D94: a guard refusal is a security fact and refuses the
                # plan — it is never the reachability best-effort below.
                raise
            except Exception as exc:
                # M-diagnostics: the batch itself failed (no browser, no
                # Playwright, ...) — warn and keep every read URL unbound.
                warnings.warn(
                    "pathway page read failed "
                    f"({type(exc).__name__}); affected controls stay unbound",
                    RuntimeWarning,
                    stacklevel=2,
                )
                html_by_url = {url: "" for url in read_urls}

    def candidates_for(url: str) -> list[Any]:
        if url not in plans_by_url:
            try:
                plans_by_url[url] = _plan_page(html_by_url.get(url, ""))
            except RestedCaptureError:
                raise
            except Exception as exc:
                # M-diagnostics: a page we cannot plan is a capture-time fact,
                # not a plan refusal — but never a silent un-binding. The
                # exception class name only: never raw text or host paths.
                warnings.warn(
                    "pathway page planning failed "
                    f"({type(exc).__name__}); affected controls stay unbound",
                    RuntimeWarning,
                    stacklevel=2,
                )
                plans_by_url[url] = []
        return plans_by_url[url]

    targets: list[CaptureTarget] = []
    for index, (url, pathway, tokens) in enumerate(resolved, start=1):
        trigger_selector = ""
        if dry_run:
            # D92: dry-run navigates nothing — the selector comes only from
            # what the map itself names, never from a page.
            trigger_selector = _explicit_selector(pathway)
        elif tokens:
            match = _match_control(tokens, candidates_for(url))
            if match is not None:
                trigger_selector = match.selector
        targets.append(
            CaptureTarget(
                index=index,
                url=url,
                # Matrix fields are unset at the pathway layer —
                # build_capture_plan fills them in the viewport x scheme
                # expansion.
                viewport_id="",
                width=0,
                height=0,
                scheme="",
                pathway_id=str(pathway.get("id") or f"pathway-{index}"),
                pathway_status=str(pathway.get("status") or "UNKNOWN"),
                pathway_trigger=_redact_host_paths(str(pathway.get("trigger") or "")),
                trigger_selector=trigger_selector,
            )
        )
    return urls, targets
