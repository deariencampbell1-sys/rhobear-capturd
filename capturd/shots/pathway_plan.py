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
selector-shaped token in its ``trigger``, and the call the pathway names
(``downstreamCall``'s route path — a verifier finding names the control by the
call it makes). The named control is matched against the page's planner
candidates by exact selector, then exact target, then label.

In dry-run there is no page read at all: the plan is built without launching
a browser and without any network call. A target's ``trigger_selector`` then
comes only from what the map itself names (the ``selector``/``css`` column, or
a selector-shaped token in its ``trigger``), and a named control that would
need the page to bind is reported unbound (``selector=-``) with a flag saying
so (:func:`named_control_needs_the_page`).

Determinism: the same map plus the same base URL produces the same plan, byte
for byte. Nothing here embeds host paths — ``pathway_trigger`` is carried with
host-path-shaped tokens redacted, and errors name the pathway id, never a path.

The page read for selector binding goes through the real capture path (a
Playwright navigation to the target the user pointed at — the same trust model
``run_capture`` already has); it adds no new fetch surface and no SSRF-guard
exception. The read is best-effort: an unreachable page means no selector, not
a refused plan — reachability is a capture-time fact.
"""

from __future__ import annotations

import re
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
_HOST_PATH_TOKEN_RE = re.compile(r"[a-zA-Z]:[\\/][^\s\"'`]*|\\\\[^\s\"'`]*")


def _redact_host_paths(text: str) -> str:
    """Host-path-shaped tokens become ``<path>`` — the plan and its errors
    must carry no host paths (the determinism rule)."""
    return _HOST_PATH_TOKEN_RE.sub("<path>", text or "")


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

    D92: exactly two sources, in priority order — the pathway's
    ``selector``/``css`` column (the explicit control reference) and a
    selector-shaped token in its ``trigger``. Anything else (a quoted label,
    a bare word, the call the pathway names) names a control that would need
    the page to bind, and yields no explicit selector.
    """
    explicit = str(pathway.get("selector") or pathway.get("css") or "").strip().strip("`").strip()
    if explicit:
        return explicit
    match = _SELECTORISH_RE.search(str(pathway.get("trigger") or ""))
    return match.group(1) if match else ""


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
    for token in tokens:
        wanted = _norm_label(token)
        if len(wanted) >= 3:
            for candidate in candidates:
                if wanted == _norm_label(candidate.label):
                    return candidate
    for token in tokens:
        wanted = _norm_label(token)
        if len(wanted) >= 4:
            for candidate in candidates:
                if wanted in _norm_label(candidate.label):
                    return candidate
    return None


def _load_page_html(url: str) -> str:
    """Read the target page's HTML through the real capture path.

    A Playwright navigation to the same target ``run_capture`` will shoot —
    the local tool pointing at the user's own built HTML. Monkeypatchable
    seam for tests; best-effort at the call site.
    """
    import asyncio

    from .capture import _launch_browser

    async def _read() -> str:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - engine dependency
            raise RuntimeError(
                "Playwright is not installed. Install engine requirements, then run "
                "`python -m playwright install chromium` if system Edge is unavailable."
            ) from exc
        async with async_playwright() as p:
            browser = await _launch_browser(p, allow_file_access=urlparse(url).scheme == "file")
            try:
                page = await browser.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                return await page.content()
            finally:
                await browser.close()

    return asyncio.run(_read())


def _plan_page(html: str) -> list[Any]:
    from . import planner

    try:
        return planner.plan_candidates(planner.parse_html(html or "")).candidates
    except Exception:
        # A page we cannot plan is a capture-time fact, not a plan refusal.
        return []


# ---------------------------------------------------------------------------
# 2. Target planning
# ---------------------------------------------------------------------------


def plan_targets_from_map(
    pathway_map: dict[str, Any],
    base_url: str,
    *,
    max_targets: int = MAX_TARGETS,
    dry_run: bool = False,
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

    More than ``max_targets`` named pathways is refused before anything is
    planned, with the service's MAX_TARGETS refusal text verbatim.
    """
    base = str(base_url or "").strip()
    if not base:
        raise RestedCaptureError("base_url is required when feeding a pathway map")

    pathways = pathway_map.get("pathways") or []
    if not pathways:
        raise RestedCaptureError("pathway map contains no pathways")
    if len(pathways) > max_targets:
        raise RestedCaptureError(f"too many capture targets; maximum is {max_targets}")

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

    def candidates_for(url: str) -> list[Any]:
        if url not in plans_by_url:
            if url not in html_by_url:
                try:
                    html_by_url[url] = _load_page_html(url)
                except Exception:
                    html_by_url[url] = ""
            plans_by_url[url] = _plan_page(html_by_url[url])
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
