"""W0-01d-r1 — pathway_plan: URL resolution and target planning.

The seam layer: a parsed pathway map plus a base URL becomes capture targets.
Everything here reuses the frozen modules (pathway_map, planner) and never
re-implements parsing, planning, scrubbing or selectors.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import socket
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

import capturd.shots.pathway_plan as pathway_plan
from capturd.shots import capture as capture_mod
from capturd.shots.capture import (
    CaptureTarget,
    RestedCaptureError,
    build_capture_plan,
)
from capturd.shots.pathway_map import PathwayMapError, load_pathway_map
from capturd.shots.pathway_plan import (
    MAX_TARGETS,
    plan_targets_from_map,
    resolve_pathway_url,
)

FIXTURES = Path(__file__).parent / "fixtures" / "capture-foundations"
WIRING = Path(__file__).parent / "fixtures" / "capture-wiring"
TWO_CONTROL_HTML = (WIRING / "two-control-page.html").read_text(encoding="utf-8")

@pytest.fixture(autouse=True)
def _offline_test_dns(monkeypatch):
    # Only reserved test hosts and literal addresses; never a real DNS lookup.
    import ipaddress
    import socket

    def resolve(host, port=None, *args, **kwargs):
        aliases = {"base.test": "93.184.216.34", "example.com": "93.184.216.34",
                   "localhost": "127.0.0.1"}
        try:
            address = ipaddress.ip_address(aliases.get(host, host))
        except ValueError:
            raise socket.gaierror("unconfigured test host") from None
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        sockaddr = (str(address), port or 0, 0, 0) if address.version == 6 else (str(address), port or 0)
        return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]

    monkeypatch.setattr("capturd.shots.capture.socket.getaddrinfo", resolve)
    return resolve



def _load(name: str) -> dict:
    path = (WIRING / name) if (WIRING / name).is_file() else (FIXTURES / name)
    if name.endswith(".json"):
        return load_pathway_map(map_path=path)
    return load_pathway_map(manifest_path=path)


# ---------------------------------------------------------------------------
# 1. resolve_pathway_url — ported route resolution
# ---------------------------------------------------------------------------


def test_resolve_route_path_becomes_absolute():
    assert resolve_pathway_url("/settings", "http://base.test") == "http://base.test/settings"
    assert resolve_pathway_url("about", "http://base.test") == "http://base.test/about"


def test_absolute_route_is_not_rebased():
    # Edge 1: an already-absolute route is kept as-is, never doubled
    # against base_url.
    absolute = "https://example.com/deep/page?x=1"
    assert resolve_pathway_url(absolute, "http://127.0.0.1:8123") == absolute


def test_unresolvable_route_raises_with_the_pathway_named():
    # Work item 1: never a silent "" — a route that cannot be resolved
    # raises PathwayMapError naming the pathway.
    for bad in ("", "   ", "C:\\Users\\dev\\route", "mailto:someone@example.com"):
        with pytest.raises(PathwayMapError) as excinfo:
            resolve_pathway_url(bad, "http://base.test", pathway_id="p-broken-route")
        message = str(excinfo.value)
        assert "p-broken-route" in message
        # no host path may leak through the refusal
        assert "C:\\Users" not in message
        assert "Users\\dev" not in message


def test_resolve_names_no_pathway_when_unnamed():
    with pytest.raises(PathwayMapError):
        resolve_pathway_url("", "http://base.test")


# ---------------------------------------------------------------------------
# 2. plan_targets_from_map — one CaptureTarget per capturable pathway
# ---------------------------------------------------------------------------


def test_plan_targets_carry_pathway_fields():
    pathway_map = _load("named-control-manifest.md")

    def fake_pages(urls: list[str], **_policy) -> dict[str, str]:
        return {
            url: (
                "<html><body>"
                '<button id="open-settings" data-target="#settings-panel">Settings</button>'
                "</body></html>"
            )
            for url in urls
        }

    original = pathway_plan._load_pages_html
    pathway_plan._load_pages_html = fake_pages
    try:
        urls, targets = plan_targets_from_map(pathway_map, "http://base.test")
    finally:
        pathway_plan._load_pages_html = original

    assert len(targets) == 2
    assert all(isinstance(target, CaptureTarget) for target in targets)
    first, second = targets
    assert first.pathway_id == "open-settings"
    assert first.pathway_status == "WIRED"
    assert first.pathway_trigger.strip("`").startswith("click")
    assert first.url == "http://base.test/settings"
    # The map names the control `#open-settings`; the selector comes from
    # planner.py's candidate, not from the map string.
    assert first.trigger_selector == "#open-settings"
    assert second.pathway_id == "open-missing"
    assert second.pathway_status == "UNWIRED"
    assert second.url == "http://base.test/"
    # The map names `#missing-btn` but the page has no such control.
    assert second.trigger_selector == ""
    assert urls == ["http://base.test/settings", "http://base.test/"]


def test_named_selector_comes_from_planner():
    # "Do not re-implement planning, scrubbing or selectors — call it":
    # when the map names a control the page cannot answer with the map's own
    # string, the target's selector is the PLANNER's generated selector for
    # the matching control — never a hand-rolled one.
    import capturd.shots.planner as planner

    pathway_map = _load("named-control-manifest.md")
    page_without_ids = "<html><body><button>Settings</button></body></html>"

    def fake_pages(urls: list[str], **_policy) -> dict[str, str]:
        return {
            url: page_without_ids if url.endswith("/settings") else "<html><body></body></html>"
            for url in urls
        }

    original = pathway_plan._load_pages_html
    pathway_plan._load_pages_html = fake_pages
    try:
        _urls, targets = plan_targets_from_map(pathway_map, "http://base.test")
    finally:
        pathway_plan._load_pages_html = original

    plan = planner.plan_candidates(planner.parse_html(page_without_ids))
    assert len(plan.candidates) == 1
    generated = plan.candidates[0].selector
    # the map named `#open-settings`; the planner's selector is the one used
    assert targets[0].trigger_selector == generated
    assert targets[0].trigger_selector != "#open-settings"


def test_verifier_call_binds_to_control_target():
    # The verifier flavour names the control by the call it makes
    # (downstream "/api/state"); the fixture page has the control wired to
    # that call, and the planner's selector for it is what the target carries.
    pathway_map = _load("sample-verifier-map.json")

    original = pathway_plan._load_pages_html
    pathway_plan._load_pages_html = lambda urls, **_policy: {url: TWO_CONTROL_HTML for url in urls}
    try:
        urls, targets = plan_targets_from_map(pathway_map, "http://127.0.0.1:8123", allow_loopback=True)
    finally:
        pathway_plan._load_pages_html = original

    assert len(targets) == 2
    selectors = {target.pathway_id: target.trigger_selector for target in targets}
    assert selectors["route-not-found-_api_state"] == "#load-state"
    assert selectors["route-not-found-_api_control"] == "#send-control"
    assert urls == ["http://127.0.0.1:8123/?view=board"]


def test_no_host_path_in_plan_or_error():
    # Edge 4: a pasted map with a Windows path in its text parses, and the
    # plan or the error carries no host path.
    pathway_map = _load("windows-path-manifest.md")
    pasted = pathway_map["raw"]
    assert "C:\\Users\\dev" in pasted  # the path is in the map text

    original = pathway_plan._load_pages_html
    pathway_plan._load_pages_html = lambda urls, **_policy: {url: "<html><body></body></html>" for url in urls}
    try:
        urls, targets = plan_targets_from_map(pathway_map, "http://base.test")
    finally:
        pathway_plan._load_pages_html = original

    plan_text = json.dumps(
        {"urls": urls, "targets": [dataclasses.asdict(target) for target in targets]},
        sort_keys=True,
    )
    for host_fragment in ("C:\\Users", "Users\\dev", "project\\src", "project/src"):
        assert host_fragment not in plan_text

    with pytest.raises(PathwayMapError) as excinfo:
        resolve_pathway_url("C:\\Users\\dev\\broken", "http://base.test", pathway_id="p1")
    assert "C:\\Users" not in str(excinfo.value)


def test_max_targets_refusal_matches_the_service_limit():
    # Work item 2 / edge 3: the service's MAX_TARGETS refusal shape —
    # "too many capture targets; maximum is N" — nothing partially planned.
    assert MAX_TARGETS == 5000
    pathway_map = _load("named-control-manifest.md")

    with pytest.raises(RestedCaptureError) as excinfo:
        plan_targets_from_map(pathway_map, "http://base.test", max_targets=1)
    assert str(excinfo.value) == "too many capture targets; maximum is 1"


def test_plan_targets_require_a_base_url():
    pathway_map = _load("single-pathway-manifest.md")
    with pytest.raises(RestedCaptureError) as excinfo:
        plan_targets_from_map(pathway_map, "")
    assert "base_url" in str(excinfo.value)


def test_empty_map_has_no_targets():
    pathway_map = _load("single-pathway-manifest.md")
    with pytest.raises(RestedCaptureError) as excinfo:
        plan_targets_from_map({**pathway_map, "pathways": []}, "http://base.test")
    assert "no pathways" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Ported helpers
# ---------------------------------------------------------------------------


def test_view_inferred_from_location():
    from capturd.shots.pathway_plan import _infer_view_from_location

    assert _infer_view_from_location("hub/ui_kits/app/BoardView.jsx:188") == "board"
    assert _infer_view_from_location("C:\\Users\\dev\\src\\RestedCaptureView.jsx:1") == "capture"
    assert _infer_view_from_location("somewhere/unknown.txt") is None


def test_route_path_extracted():
    from capturd.shots.pathway_plan import _extract_route_path

    assert _extract_route_path("`GET /settings`") == "/settings"
    assert _extract_route_path('fetch(url + "/api/state")') == "/api/state"
    assert _extract_route_path("Home painted") is None


# ---------------------------------------------------------------------------
# W0-28-r1 — D94: plan-time navigation guarded like capture; no host paths
# ---------------------------------------------------------------------------


_POSIX_MANIFEST = """\
# Pathway Manifest — posix paths

A one-pathway map whose location and trigger carry a POSIX path from the
machine that produced the manifest. The path must parse fine and must never
reach the capture plan or any error text.

## Summary Counts

| Category | Count |
|---|---|
| **Total pathways** | 1 |
| **WIRED** | 1 |

## 1. Pathways Table

| id | location | trigger | declared_intent | handler | downstream_call | expected_side_effect | evidence | status |
|---|---|---|---|---|---|---|---|---|
| `home` | /home/dev/project/src/RestedCaptureView.jsx:1 | `click` (handler in /home/dev/project/src/app.js:88) | Render the home view | `render` |  | Home painted | /home/dev/project/src/RestedCaptureView.jsx:1 | WIRED |
"""


def test_plan_time_navigation_is_guarded_and_host_paths_are_redacted(monkeypatch):
    # D94: any navigation the planner performs goes through the same
    # private-IP / host guard the capture path uses — reused, not rewritten —
    # and _redact_host_paths redacts POSIX paths as well as Windows ones, so
    # no host path reaches plan-json or an error message on any platform.
    pathway_map = _load("named-control-manifest.md")
    with pytest.raises(RestedCaptureError) as plan_refusal:
        plan_targets_from_map(pathway_map, "http://169.254.169.254")
    with pytest.raises(RestedCaptureError) as capture_refusal:
        capture_mod._reject_private_url("http://169.254.169.254/")
    # the same error class — and the same error — the capture path raises
    assert type(plan_refusal.value) is type(capture_refusal.value)
    assert str(plan_refusal.value) == str(capture_refusal.value)

    monkeypatch.setattr(
        "capturd.shots.pathway_plan._load_pages_html",
        lambda urls, **_policy: {url: "<html><body></body></html>" for url in urls},
    )
    urls, targets, settings = build_capture_plan({
        "manifest_text": _POSIX_MANIFEST,
        "base_url": "http://base.test",
        "viewports": ["desktop"],
        "schemes": ["light"],
    })
    assert urls == ["http://base.test/?view=capture"]  # the manifest resolves
    plan_text = json.dumps(
        {"urls": urls, "targets": [dataclasses.asdict(t) for t in targets], "settings": settings},
        sort_keys=True,
    )
    for host_fragment in ("/home/dev", "home/dev", "project/src"):
        assert host_fragment not in plan_text


def test_posix_host_path_in_a_manifest_never_reaches_plan_or_error(monkeypatch):
    # Edge 5: a POSIX host path in a manifest resolves, and the path appears
    # in neither plan-json nor an error text.
    pathway_map = load_pathway_map(manifest_text=_POSIX_MANIFEST)
    monkeypatch.setattr(
        "capturd.shots.pathway_plan._load_pages_html",
        lambda urls, **_policy: {url: "<html><body></body></html>" for url in urls},
    )
    urls, targets = plan_targets_from_map(pathway_map, "http://base.test")
    assert urls and targets  # the manifest resolves
    plan_text = json.dumps(
        {"urls": urls, "targets": [dataclasses.asdict(t) for t in targets]},
        sort_keys=True,
    )
    for host_fragment in ("/home/dev", "home/dev", "project/src"):
        assert host_fragment not in plan_text

    with pytest.raises(PathwayMapError) as excinfo:
        resolve_pathway_url("", "http://base.test", pathway_id="/home/dev/project/src/broken")
    message = str(excinfo.value)
    assert "route is empty" in message
    for host_fragment in ("/home/dev", "home/dev", "project/src"):
        assert host_fragment not in message

# ---------------------------------------------------------------------------
# W0-01d-r3 — H1 loopback exception, one batch read, expanded limits,
# diagnostics. The fake Playwright stack below is driven by the real call
# signatures (_launch_browser, context.route, page.goto, page.content) — no
# network, no real browser, no sleeps.
# ---------------------------------------------------------------------------


def _one_control_page() -> str:
    return (
        "<html><body>"
        '<button id="open-settings" data-target="#settings-panel">Settings</button>'
        "</body></html>"
    )


def _single_named_control_map() -> dict:
    """A one-pathway verifier map that names a control needing the page."""
    return load_pathway_map(map_text=json.dumps({
        "checks": {
            "smoke-check": {
                "name": "smoke-check",
                "passed": False,
                "findings": [{
                    "id": "named-control",
                    "check": "smoke-check",
                    "severity": "high",
                    "code": "click `#panel-btn`",
                    "message": "named control finding",
                }],
            }
        }
    }))


class _FakeRoute:
    """A route.request / abort() / continue_() stand-in with the real shape."""

    def __init__(self, url: str, resource_type: str = "document") -> None:
        self.request = SimpleNamespace(url=url, resource_type=resource_type)
        self.aborted = False
        self.continued = False

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


class _FakePage:
    def __init__(self, fake: "_FakePlaywrightStack") -> None:
        self._fake = fake
        self.url = "about:blank"
        self.goto_calls: list[tuple[str, dict]] = []
        self.content_calls = 0
        self.closed = False

    async def goto(self, url: str, **kwargs) -> None:
        self.goto_calls.append((url, kwargs))
        self.url = url
        if self._fake.goto_hook is not None:
            await self._fake.goto_hook(self)

    async def content(self) -> str:
        self.content_calls += 1
        return self._fake.html_by_url.get(self.url, "")

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, fake: "_FakePlaywrightStack") -> None:
        self._fake = fake
        self.closed = False

    async def route(self, pattern: str, handler) -> None:
        self._fake.route_pattern = pattern
        self._fake.route_handler = handler

    async def new_page(self) -> _FakePage:
        self._fake.pages_created += 1
        page = _FakePage(self._fake)
        self._fake.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True
        self._fake.contexts_closed += 1


class _FakeBrowser:
    def __init__(self, fake: "_FakePlaywrightStack") -> None:
        self._fake = fake
        self.closed = False

    async def new_context(self) -> _FakeContext:
        self._fake.contexts_created += 1
        return _FakeContext(self._fake)

    async def close(self) -> None:
        self.closed = True
        self._fake.browsers_closed += 1


class _FakePlaywrightStack:
    """Fake `async_playwright()` stack — real call signatures only."""

    def __init__(
        self,
        html_by_url: dict[str, str] | None = None,
        goto_hook=None,
    ) -> None:
        self.html_by_url = dict(html_by_url or {})
        self.goto_hook = goto_hook
        self.entries = 0
        self.launches = 0
        self.contexts_created = 0
        self.contexts_closed = 0
        self.browsers_closed = 0
        self.pages_created = 0
        self.route_pattern: str | None = None
        self.route_handler = None
        self.pages: list[_FakePage] = []

    def factory(self):
        self.entries += 1
        fake = self

        class _Entry:
            async def __aenter__(self_inner):
                class _Chromium:
                    async def launch(self_ch, **kwargs):
                        fake.launches += 1
                        return _FakeBrowser(fake)

                return SimpleNamespace(chromium=_Chromium())

            async def __aexit__(self_inner, *exc) -> bool:
                return False

        return _Entry()

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr("playwright.async_api.async_playwright", self.factory)

    def assert_all_closed(self) -> None:
        assert self.contexts_closed == 1, "the context must close exactly once"
        assert self.browsers_closed == 1, "the browser must close exactly once"
        assert all(page.closed for page in self.pages), "every page must close"


# 1. H1: the plan-navigation guard's ONE named exception is loopback.
def test_r3_loopback_plan_read_is_allowed(monkeypatch, _offline_test_dns):
    # the real guard accepts loopback only with the named opt-in ...
    capture_mod._reject_private_url("http://127.0.0.1:9/x", allow_loopback=True)
    capture_mod._reject_private_url("http://[::1]:9/x", allow_loopback=True)
    capture_mod._reject_private_url("http://[::ffff:127.0.0.1]/x", allow_loopback=True)

    # ... and only loopback: a mixed public + loopback answer is fine, a mixed
    # public + RFC1918 answer is refused even with the opt-in.
    def _mixed_loopback(host, port=None):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr("capturd.shots.capture.socket.getaddrinfo", _mixed_loopback)
    capture_mod._reject_private_url("http://mixed.test/", allow_loopback=True)

    def _mixed_private(host, port=None):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0)),
        ]

    monkeypatch.setattr("capturd.shots.capture.socket.getaddrinfo", _mixed_private)
    with pytest.raises(RestedCaptureError):
        capture_mod._reject_private_url("http://mixed.test/", allow_loopback=True)

    # End mixed-answer scenarios before the real entry URL check.
    monkeypatch.setattr("capturd.shots.capture.socket.getaddrinfo", _offline_test_dns)
    # and a plan against a loopback base binds normally from a stubbed batch read
    monkeypatch.setattr(
        "capturd.shots.pathway_plan._load_pages_html",
        lambda urls, **_policy: {url: _one_control_page() for url in urls},
    )
    pathway_map = _load("named-control-manifest.md")
    _urls, targets = plan_targets_from_map(pathway_map, "http://127.0.0.1:9", allow_loopback=True)
    selectors = {target.pathway_id: target.trigger_selector for target in targets}
    assert selectors["open-settings"] == "#open-settings"
    assert selectors["open-missing"] == ""


# 2. The default capture guard still refuses loopback — H1 is plan-time only.
def test_r3_capture_guard_still_refuses_loopback_by_default():
    for ip in ("127.0.0.1", "::1"):
        url = f"http://[{ip}]:9/x" if ":" in ip else f"http://{ip}:9/x"
        with pytest.raises(RestedCaptureError) as excinfo:
            capture_mod._reject_private_url(url)
        assert str(excinfo.value) == f"url resolves to a private/internal IP ({ip}) — not allowed"


# 3. M-redirect-SSRF: every request URL is checked with the loopback-only policy.
def test_r3_redirect_requests_use_loopback_only_guard(monkeypatch):
    fake = _FakePlaywrightStack(html_by_url={"http://127.0.0.1:9/page": "<html></html>"})
    fake.install(monkeypatch)

    checked: list[tuple[str, bool]] = []
    real = capture_mod._reject_private_url

    def _spy(url, *, allow_loopback=False):
        checked.append((url, allow_loopback))
        return real(url, allow_loopback=allow_loopback)

    monkeypatch.setattr("capturd.shots.capture._reject_private_url", _spy)

    result = pathway_plan._load_pages_html(["http://127.0.0.1:9/page"], allow_loopback=True)
    assert result == {"http://127.0.0.1:9/page": "<html></html>"}
    assert fake.route_handler is not None
    assert fake.route_pattern == "**/*"

    # a redirect's request to a loopback subresource continues — the opt-in
    loopback = _FakeRoute("http://127.0.0.1:9/assets/app.js")
    asyncio.run(fake.route_handler(loopback))
    assert loopback.continued is True and loopback.aborted is False

    # a redirect's request to cloud metadata aborts — loopback-only, still
    metadata = _FakeRoute("http://169.254.169.254/latest/meta-data")
    asyncio.run(fake.route_handler(metadata))
    assert metadata.aborted is True and metadata.continued is False

    checked_urls = [url for url, _flag in checked]
    assert "http://169.254.169.254/latest/meta-data" in checked_urls
    # The same-host subresource reuses the entry check's cached allow verdict.
    assert "http://127.0.0.1:9/assets/app.js" not in checked_urls
    assert all(flag is True for url, flag in checked if url.startswith("http://127.0.0.1:9/"))
    assert ("http://169.254.169.254/latest/meta-data", False) in checked
    assert all(flag is False for url, flag in checked if "169.254.169.254" in url)


# 4. A guard refusal is never swallowed as reachability — it refuses the plan.
def test_r3_redirect_guard_refusal_is_not_swallowed(monkeypatch):
    pathway_map = _load("named-control-manifest.md")
    with pytest.raises(RestedCaptureError) as expected:
        capture_mod._reject_private_url("http://169.254.169.254/latest")
    guard_message = str(expected.value)

    # Scenario A: goto fires the route guard at a metadata redirect target and
    # returns — the recorded refusal must escape from the read.
    fake = _FakePlaywrightStack()

    async def redirect(page):
        await fake.route_handler(_FakeRoute("http://169.254.169.254/latest"))

    fake.goto_hook = redirect
    fake.install(monkeypatch)
    with pytest.raises(RestedCaptureError) as excinfo:
        plan_targets_from_map(pathway_map, "http://127.0.0.1:9", allow_loopback=True)
    assert str(excinfo.value) == guard_message, "the guard's own error must escape"
    fake.assert_all_closed()

    # Scenario B: goto records the refusal and then fails (aborted navigation) —
    # the refusal escapes, and it is never converted into an empty-page warning.
    fake_b = _FakePlaywrightStack()

    async def redirect_then_fail(page):
        await fake_b.route_handler(_FakeRoute("http://169.254.169.254/latest"))
        raise TimeoutError("navigation aborted")

    fake_b.goto_hook = redirect_then_fail
    fake_b.install(monkeypatch)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(RestedCaptureError) as excinfo:
            plan_targets_from_map(pathway_map, "http://127.0.0.1:9", allow_loopback=True)
    assert str(excinfo.value) == guard_message
    assert [w for w in caught if issubclass(w.category, RuntimeWarning)] == [], (
        "a guard refusal must not become a reachability diagnostic"
    )
    fake_b.assert_all_closed()


# 5. The final URL after goto is checked before any content is read.
def test_r3_final_url_is_checked_before_content(monkeypatch):
    fake = _FakePlaywrightStack()

    async def hijack(page):
        page.url = "http://169.254.169.254/latest"

    fake.goto_hook = hijack
    fake.install(monkeypatch)
    with pytest.raises(RestedCaptureError) as excinfo:
        pathway_plan._load_pages_html(["http://127.0.0.1:9/page"], allow_loopback=True)
    assert "169.254.169.254" in str(excinfo.value)
    assert fake.pages[0].content_calls == 0, "content must never be read for a refused final URL"
    fake.assert_all_closed()


# 6. M-browser-lifecycle: ONE driver/browser/context reads all named pages.
def test_r3_one_browser_reads_all_named_control_pages(monkeypatch):
    fake = _FakePlaywrightStack(html_by_url={
        "http://127.0.0.1:9/a": "<html><a></a></html>",
        "http://127.0.0.1:9/b": "<html><b></b></html>",
    })
    fake.install(monkeypatch)

    result = pathway_plan._load_pages_html(
        ["http://127.0.0.1:9/a", "http://127.0.0.1:9/b"], allow_loopback=True,
    )
    assert result == fake.html_by_url
    assert fake.entries == 1, "exactly one Playwright driver per plan"
    assert fake.launches == 1, "exactly one browser launch per plan"
    assert fake.contexts_created == 1, "exactly one context per plan"
    assert fake.pages_created == 2, "one page per URL, sequential in the context"
    for page in fake.pages:
        assert len(page.goto_calls) == 1
        _url, kwargs = page.goto_calls[0]
        assert kwargs == {"wait_until": "domcontentloaded", "timeout": pathway_plan.PLAN_NAV_TIMEOUT_MS}
    assert [page.goto_calls[0][0] for page in fake.pages] == [
        "http://127.0.0.1:9/a",
        "http://127.0.0.1:9/b",
    ]
    fake.assert_all_closed()


# 7. The synchronous contract: planning refuses an already-running event loop.
@pytest.mark.asyncio
async def test_r3_sync_plan_refuses_an_active_event_loop(monkeypatch):
    calls: list[list[str]] = []

    def _record(urls, **_policy):
        calls.append(list(urls))
        raise AssertionError("the batch reader must not run under an active event loop")

    monkeypatch.setattr("capturd.shots.pathway_plan._load_pages_html", _record)
    pathway_map = _load("named-control-manifest.md")

    with pytest.raises(RestedCaptureError) as excinfo:
        plan_targets_from_map(pathway_map, "http://127.0.0.1:9")
    assert str(excinfo.value) == (
        "pathway planning is synchronous; call it outside an active event loop"
    )
    assert calls == []

    # dry-run never performs this check (and launches nothing)
    _urls, targets = plan_targets_from_map(pathway_map, "http://127.0.0.1:9", dry_run=True)
    assert targets and calls == []


# 8. M-diagnostics: a swallowed read failure warns and keeps the target unbound.
def test_r3_page_read_failure_emits_diagnostic_and_keeps_unbound(monkeypatch):
    def _boom(urls, **_policy):
        raise RuntimeError("raw /home/dev/secret-path detail")

    monkeypatch.setattr("capturd.shots.pathway_plan._load_pages_html", _boom)
    pathway_map = _load("named-control-manifest.md")

    with pytest.warns(RuntimeWarning, match="pathway page read failed") as record:
        _urls, targets = plan_targets_from_map(pathway_map, "http://base.test")
    message = str(record[0].message)
    assert message == "pathway page read failed (RuntimeError); affected controls stay unbound"
    assert "/home/dev" not in message and "secret" not in message
    assert [target.trigger_selector for target in targets] == ["", ""], (
        "the targets stay planned and unbound"
    )


# 9. M-diagnostics: a planner failure warns — never raw text, never silent.
def test_r3_planner_failure_emits_diagnostic(monkeypatch):
    monkeypatch.setattr(
        "capturd.shots.pathway_plan._load_pages_html",
        lambda urls, **_policy: {url: TWO_CONTROL_HTML for url in urls},
    )

    def _boom(html):
        raise RuntimeError("raw secret hunter2 /home/dev/x")

    monkeypatch.setattr("capturd.shots.pathway_plan._plan_page", _boom)
    pathway_map = _load("named-control-manifest.md")

    with pytest.warns(RuntimeWarning, match="pathway page planning failed") as record:
        _urls, targets = plan_targets_from_map(pathway_map, "http://base.test")
    message = str(record[0].message)
    assert message == (
        "pathway page planning failed (RuntimeError); affected controls stay unbound"
    )
    assert "hunter2" not in message and "/home/dev" not in message
    assert [target.trigger_selector for target in targets] == ["", ""]


# 10. M-directory-base: a directory base is refused — never printed as a host path.
def test_r3_directory_base_is_refused_without_host_path_leak(monkeypatch):
    calls: list[list[str]] = []

    def _record(urls, **_policy):
        calls.append(list(urls))
        raise AssertionError(f"a page read happened for {urls}")

    monkeypatch.setattr("capturd.shots.pathway_plan._load_pages_html", _record)
    pathway_map = _load("named-control-manifest.md")
    refusal = (
        "base_url must be an absolute http(s) URL; local directory paths are not allowed"
    )

    for bad_base in ("/home/dev/build", "C:\\Users\\dev\\build", "file:///home/dev/build", "build"):
        with pytest.raises(RestedCaptureError) as excinfo:
            plan_targets_from_map(pathway_map, bad_base)
        message = str(excinfo.value)
        assert message == refusal
        assert "/home/dev" not in message
        assert "C:\\Users" not in message
    assert calls == [], "a refused base must trigger no read at all"


# 11. M-expanded-targets: the expanded matrix limit fails before navigation.
def test_r3_expanded_matrix_limit_fails_before_navigation(monkeypatch):
    calls: list[list[str]] = []

    def _record(urls, **_policy):
        calls.append(list(urls))
        raise AssertionError(f"a page read happened for {urls}")

    monkeypatch.setattr("capturd.shots.pathway_plan._load_pages_html", _record)
    pathway_map = _single_named_control_map()

    with pytest.raises(RestedCaptureError) as excinfo:
        plan_targets_from_map(
            pathway_map,
            "http://base.test",
            viewport_count=3,
            scheme_count=2,
            max_targets=5,
        )
    assert str(excinfo.value) == "too many capture targets; maximum is 5"
    assert calls == [], "the refusal must happen before any navigation"

    with pytest.raises(RestedCaptureError) as excinfo:
        plan_targets_from_map(pathway_map, "http://base.test", viewport_count=0)
    assert str(excinfo.value) == "viewport_count and scheme_count must be positive"
    with pytest.raises(RestedCaptureError) as excinfo:
        plan_targets_from_map(pathway_map, "http://base.test", scheme_count=-1)
    assert str(excinfo.value) == "viewport_count and scheme_count must be positive"
    assert calls == []


# W0-28-r4 exact base-host policy, off-loop resolver and bounded batch seams.
from unittest.mock import AsyncMock, Mock


def test_r5_substring_label_never_binds():
    # Use the real planner over HTML, never fabricated candidate responses.
    candidates = pathway_plan._plan_page(
        '<button id="open">Open settings panel</button><button id="reset" role="tab">Reset settings</button>'
    )
    assert pathway_plan._match_control(["settings"], candidates) is None
    assert pathway_plan._match_control(["Reset settings"], candidates).selector == "#reset"


@pytest.mark.parametrize("base_host, request_url, allowed", [
    ("example.com", "http://127.0.0.1:9/a", False),
    ("127.0.0.1", "http://localhost:9/a", False),
    ("127.0.0.1", "http://127.0.0.1:9/a", True),
    ("::1", "http://[::1]:9/a", True),
    ("::ffff:127.0.0.1", "http://[::ffff:127.0.0.1]:9/a", True),
    ("169.254.169.254", "http://169.254.169.254/latest", False),
    ("127.0.0.1", "http://10.0.0.5/x", False),
])
def test_r5_loopback_redirect_must_stay_on_base_host(base_host, request_url, allowed):
    context = SimpleNamespace(route=AsyncMock())
    errors = asyncio.run(capture_mod._install_navigation_guard(context, allow_loopback=True, loopback_host=base_host))
    handler = context.route.call_args.args[1]
    route = _FakeRoute(request_url)
    asyncio.run(handler(route))
    assert route.continued is allowed
    assert route.aborted is (not allowed)
    assert bool(errors) is (not allowed)
    if not allowed:
        assert "private/internal" in str(errors[0])
        first = errors[0]
        asyncio.run(handler(_FakeRoute("http://169.254.169.254/latest")))
        assert errors == [first]


def test_r5_capture_request_guard_aborts_private_redirect():
    context = SimpleNamespace(route=AsyncMock())
    errors = asyncio.run(capture_mod._install_navigation_guard(context, allow_loopback=False, loopback_host=None))
    route = SimpleNamespace(request=SimpleNamespace(url="http://169.254.169.254/latest"),
                            abort=AsyncMock(), continue_=AsyncMock())
    asyncio.run(context.route.call_args.args[1](route))
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()
    assert isinstance(errors[0], RestedCaptureError)


def test_r5_async_guard_resolves_off_loop(monkeypatch):
    real = asyncio.to_thread
    thread = AsyncMock(wraps=real)
    monkeypatch.setattr(capture_mod.asyncio, "to_thread", thread)
    context = SimpleNamespace(route=AsyncMock())
    errors = asyncio.run(capture_mod._install_navigation_guard(context, allow_loopback=True, loopback_host="127.0.0.1"))
    asyncio.run(context.route.call_args.args[1](_FakeRoute("http://127.0.0.1:9/a")))
    assert errors == []
    thread.assert_awaited_once_with(capture_mod._reject_navigation_url, "http://127.0.0.1:9/a",
                                    allow_loopback=True, loopback_host="127.0.0.1")


def test_r5_batch_deadline_is_120_seconds(monkeypatch):
    original = asyncio.wait_for
    seen = []
    async def wait_for(awaitable, *, timeout):
        seen.append(timeout)
        return await original(awaitable, timeout=0 if timeout == 120 else timeout)
    monkeypatch.setattr(pathway_plan.asyncio, "wait_for", wait_for)
    with pytest.raises(RestedCaptureError, match="^pathway page read timed out$"):
        pathway_plan._load_pages_html(["http://127.0.0.1:9"], allow_loopback=True)
    assert seen == [120]


def test_r5_dns_timeout_aborts_instead_of_continuing(monkeypatch):
    async def never(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(capture_mod.asyncio, "to_thread", never)
    monkeypatch.setattr(capture_mod, "NAV_GUARD_TIMEOUT_S", 0.001)
    context = SimpleNamespace(route=AsyncMock())
    errors = asyncio.run(capture_mod._install_navigation_guard(context, allow_loopback=True, loopback_host="127.0.0.1"))
    route = _FakeRoute("http://127.0.0.1:9")
    asyncio.run(context.route.call_args.args[1](route))
    assert route.aborted and not route.continued
    assert str(errors[0]) == "navigation URL check timed out"


def test_r5_dry_run_skips_dns_checks(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run resolved DNS")
    monkeypatch.setattr(capture_mod.socket, "getaddrinfo", forbidden)
    _urls, targets = plan_targets_from_map(_load("named-control-manifest.md"), "http://base.test", dry_run=True)
    assert len(targets) == 2


# W0-28-r5: subresource isolation, fail-closed errors, and safe defaults.
def test_r6_private_subresource_aborts_warns_and_is_not_document_error():
    context = SimpleNamespace(route=AsyncMock())
    guard_errors = asyncio.run(capture_mod._install_navigation_guard(
        context, allow_loopback=False, loopback_host=None,
    ))
    route = _FakeRoute("http://169.254.169.254/latest", "image")
    with pytest.warns(RuntimeWarning) as caught:
        asyncio.run(context.route.call_args.args[1](route))
    assert route.aborted is True and route.continued is False
    assert str(caught[0].message) == "capture subresource refused (RestedCaptureError); request aborted"
    assert guard_errors == []


def test_r6_document_refusal_is_fatal():
    context = SimpleNamespace(route=AsyncMock())
    guard_errors = asyncio.run(capture_mod._install_navigation_guard(
        context, allow_loopback=False, loopback_host=None,
    ))
    handler = context.route.call_args.args[1]
    route = _FakeRoute("http://169.254.169.254/latest")
    asyncio.run(handler(route))
    assert route.aborted is True and route.continued is False
    assert len(guard_errors) == 1 and "private/internal" in str(guard_errors[0])
    first = guard_errors[0]
    asyncio.run(handler(_FakeRoute("http://10.0.0.5/private")))
    assert guard_errors == [first]


def test_r6_non_guard_subresource_exception_fails_closed(monkeypatch):
    monkeypatch.setattr(capture_mod, "_check_navigation_url", AsyncMock(
        side_effect=socket.gaierror("private host detail"),
    ))
    context = SimpleNamespace(route=AsyncMock())
    guard_errors = asyncio.run(capture_mod._install_navigation_guard(
        context, allow_loopback=False, loopback_host=None,
    ))
    route = _FakeRoute("http://missing.test/x", "image")
    route.abort = AsyncMock()
    route.continue_ = AsyncMock()
    with pytest.warns(RuntimeWarning) as caught:
        asyncio.run(context.route.call_args.args[1](route))
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()
    message = str(caught[0].message)
    assert message == "capture subresource refused (gaierror); request aborted"
    assert "private host detail" not in message
    assert guard_errors == []


def test_r6_non_guard_document_exception_is_named_error(monkeypatch):
    monkeypatch.setattr(capture_mod, "_check_navigation_url", AsyncMock(
        side_effect=socket.gaierror("private host detail"),
    ))
    context = SimpleNamespace(route=AsyncMock())
    guard_errors = asyncio.run(capture_mod._install_navigation_guard(
        context, allow_loopback=False, loopback_host=None,
    ))
    route = _FakeRoute("http://missing.test/")
    route.abort = AsyncMock()
    route.continue_ = AsyncMock()
    asyncio.run(context.route.call_args.args[1](route))
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()
    assert len(guard_errors) == 1
    assert str(guard_errors[0]) == "navigation URL check failed (gaierror)"
    assert "private host detail" not in str(guard_errors[0])


def test_r6_direct_helpers_default_refuse_loopback(monkeypatch):
    import inspect

    assert inspect.signature(plan_targets_from_map).parameters["allow_loopback"].default is False
    assert inspect.signature(pathway_plan._load_pages_html).parameters["allow_loopback"].default is False
    launch = AsyncMock()
    monkeypatch.setattr(capture_mod, "_launch_browser", launch)
    pathway_map = _load("named-control-manifest.md")
    with pytest.raises(RestedCaptureError, match="private/internal"):
        plan_targets_from_map(pathway_map, "http://127.0.0.1:9")
    with pytest.raises(RestedCaptureError, match="private/internal"):
        pathway_plan._load_pages_html(["http://127.0.0.1:9/page"])
    launch.assert_not_called()


@pytest.mark.asyncio
async def test_r6_legacy_asyncio_timeout_is_named_refusal(monkeypatch):
    import inspect

    class LegacyTimeoutError(Exception):
        pass

    async def expire(awaitable, *args, **kwargs):
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        raise LegacyTimeoutError

    monkeypatch.setattr(capture_mod.asyncio, "wait_for", expire)
    monkeypatch.setattr(capture_mod.asyncio, "TimeoutError", LegacyTimeoutError)
    with pytest.raises(RestedCaptureError, match="^navigation URL check timed out$"):
        await capture_mod._check_navigation_url(
            "https://example.com", allow_loopback=False, loopback_host=None,
        )


def test_r6_legacy_asyncio_batch_timeouts_are_named_refusals(monkeypatch):
    import inspect

    class LegacyTimeoutError(Exception):
        pass

    async def expire(awaitable, *args, **kwargs):
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        raise LegacyTimeoutError

    monkeypatch.setattr(pathway_plan.asyncio, "wait_for", expire)
    monkeypatch.setattr(pathway_plan.asyncio, "TimeoutError", LegacyTimeoutError)
    with pytest.raises(RestedCaptureError, match="^pathway page read timed out$"):
        pathway_plan._load_pages_html(["http://127.0.0.1:9"], allow_loopback=True)
    with pytest.raises(RestedCaptureError, match="^pathway URL checks timed out$"):
        plan_targets_from_map(
            _load("named-control-manifest.md"), "http://127.0.0.1:9", allow_loopback=True,
        )


# W0-28-r7: same-head reachability, binding, redaction, and cache contracts.
def test_r7_page_read_reachability_failure_warns_and_preserves_sibling(monkeypatch):
    urls = ["https://example.com/a", "https://example.com/b"]
    fake = _FakePlaywrightStack(html_by_url={urls[1]: "<html>sibling</html>"})
    async def fail_first(page):
        if page.url == urls[0]:
            raise RuntimeError("raw /home/dev/secret detail")
    fake.goto_hook = fail_first
    fake.install(monkeypatch)
    with pytest.warns(RuntimeWarning) as caught:
        result = pathway_plan._load_pages_html(urls)
    assert result == {urls[0]: "", urls[1]: "<html>sibling</html>"}
    assert len(caught) == 1
    assert str(caught[0].message) == (
        "pathway page read failed (RuntimeError); affected controls stay unbound"
    )
    assert "/home/dev" not in str(caught[0].message)
    fake.assert_all_closed()


def test_r7_longest_exact_label_wins():
    candidates = pathway_plan._plan_page(
        '<button id="load">Load</button><button id="load-state">Load State</button>'
    )
    match = pathway_plan._match_control(["Load", "State", "button"], candidates)
    assert match.selector == "#load-state"


@pytest.mark.parametrize("text, expected", [
    ("file:/home/dev/x", "file:<path>"),
    ("(/home/dev/x)", "(<path>)"),
    (":/home/dev/x", ":<path>"),
])
def test_r7_posix_path_redaction_covers_prefixed_paths(text, expected):
    assert pathway_plan._redact_host_paths(text) == expected


def test_r7_plan_reader_reuses_navigation_cache(monkeypatch):
    url = "https://example.com/page"
    fake = _FakePlaywrightStack(html_by_url={url: "<html>ok</html>"})
    fake.install(monkeypatch)
    resolver = Mock(wraps=capture_mod._reject_navigation_url)
    monkeypatch.setattr(capture_mod, "_reject_navigation_url", resolver)
    cache = {}
    result = pathway_plan._load_pages_html([url], navigation_cache=cache)
    assert result == {url: "<html>ok</html>"}
    assert resolver.call_count == 1

    resolver.reset_mock()
    checked = []
    def reader(urls, **kwargs):
        checked.append(kwargs)
        return {item: "<html></html>" for item in urls}
    monkeypatch.setattr(pathway_plan, "_load_pages_html", reader)
    plan_targets_from_map(_single_named_control_map(), "https://example.com")
    assert resolver.call_count == 0
    assert isinstance(checked[0]["navigation_cache"], dict)
