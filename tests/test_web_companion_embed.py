"""Rho companion wiring — both Captur'd surfaces, end to end.

Why these exist: PR #39 pointed ``service/web/index.html`` and ``service/web/m.html``
at ``https://builds.rhobear.ai/companion-embed-orb4.js`` / ``.../companion``. That
host answers **every** path with the 2,399-byte HTML "Coming soon" placeholder, so
the classic ``<script src>`` was MIME-blocked and the voice companion vanished from
both surfaces — silently, with no console error a user would report and nothing in
this repo that could have failed. The review gate ("smoke-test both surfaces against
the Builds host before merge") was never run.

So the fix is same-origin by construction, and these tests pin all four pieces of it:

1. both surfaces load the canonical orb4 embed from **this** docroot, never a
   third-party host (``test_*_same_origin*``);
2. the vendored asset is byte-identical to the canonical
   ``rhobear-builds-web/companion-embed-orb4.js`` (``test_embed_is_the_canonical_build``);
3. the endpoint is the same-origin ``/companion`` the embed documents, and the app
   really serves both halves — the JS as JavaScript, the endpoint as a live
   pass-through to the companion brain (``test_app_*``);
4. no unreferenced companion embed is left in the docroot (the 77 KB local embed
   PR #39 orphaned) — ``test_docroot_has_no_orphaned_companion_asset``.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))
sys.path.insert(0, str(ROOT))

WEB = ROOT / "service" / "web"
ASSETS = WEB / "assets"
EMBED_NAME = "companion-embed-orb4.js"
SURFACES = ("index.html", "m.html")          # "/" and "/m"
EMBED_REF = f"/assets/{EMBED_NAME}"

# The canonical build vendored from rhobear-builds-web. Pinned on purpose: a silent
# drift (or a downgrade back to the pre-orb4 embed) must fail here, not in prod.
# NOTE: updated after TTS race-condition fix (AbortController + signal).
CANONICAL_SHA256 = "aae14a24d9e7b3e28e8e1598c75b4ce7d6d926fa08d3aa2e27e3e64ced8c4846"
CANONICAL_BYTES = 106_989

# Hosts this repo must never depend on for the companion to render.
DEAD_COMPANION_HOSTS = ("builds.rhobear.ai", "workbench.rhobear.ai")


# --- helpers -----------------------------------------------------------------

def _page(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def _script_srcs(html: str) -> list[str]:
    """Script srcs, query string stripped (the ?v= release marker is not a path)."""
    return [s.split("?")[0] for s in re.findall(r"<script[^>]*\bsrc=\"([^\"]+)\"", html)]


def _companion_endpoint(html: str) -> str:
    block = re.search(r"window\.RHOBEAR_COMPANION\s*=\s*\{(.*?)\};", html, re.S)
    assert block, "surface no longer configures window.RHOBEAR_COMPANION"
    ep = re.search(r"endpoint\s*:\s*'([^']+)'", block.group(1))
    assert ep, "window.RHOBEAR_COMPANION has no endpoint"
    return ep.group(1)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- 1. the surfaces are same-origin -----------------------------------------

@pytest.mark.parametrize("page", SURFACES)
def test_surface_loads_the_embed_same_origin(page):
    html = _page(page)
    assert f'src="{EMBED_REF}?v=' in html, f"{page} does not load {EMBED_REF} same-origin"
    srcs = _script_srcs(html)
    companions = [s for s in srcs if "companion" in s]
    assert companions, f"{page} has no companion script tag at all"
    for src in companions:
        assert src.startswith("/"), f"{page} loads the companion cross-origin: {src}"


@pytest.mark.parametrize("page", SURFACES)
def test_surface_never_points_at_a_dead_companion_host(page):
    html = _page(page)
    loaded = _script_srcs(html) + re.findall(r"<link[^>]*\bhref=\"(https?://[^\"]+)\"", html)
    dead = [u for u in loaded
            if any(host in u for host in DEAD_COMPANION_HOSTS)]
    assert not dead, (
        f"{page} still loads {dead} from a host that answers every path with an HTML "
        "placeholder: a <script src> there is MIME-blocked and the companion silently "
        "disappears — the exact PR #39 regression"
    )
    assert not _companion_endpoint(html).startswith("http"), (
        f"{page} points the companion brain at a remote host instead of the same-origin "
        "/companion the app serves"
    )


@pytest.mark.parametrize("page", SURFACES)
def test_surface_endpoint_is_same_origin(page):
    assert _companion_endpoint(_page(page)) == "/companion"


# --- 2. the vendored asset is the canonical build ----------------------------

def test_embed_asset_exists():
    assert (ASSETS / EMBED_NAME).is_file(), f"missing vendored embed {EMBED_REF}"


def test_embed_is_the_canonical_build():
    raw = (ASSETS / EMBED_NAME).read_bytes()
    assert len(raw) == CANONICAL_BYTES, (
        f"vendored embed is {len(raw)} bytes, canonical is {CANONICAL_BYTES}"
    )
    assert hashlib.sha256(raw).hexdigest() == CANONICAL_SHA256, (
        "vendored embed drifted from rhobear-builds-web/companion-embed-orb4.js — "
        "re-vendor it verbatim and update this pin"
    )
    text = raw.decode("utf-8")
    # It must still be the orb4 embed and still know about this surface.
    assert "window.__rhoEmbedLoaded = '2.5'" in text
    assert "capturd:" in text, "canonical embed no longer declares the capturd surface"


def test_voice_follow_tts_is_abortable():
    """Race fix (PR #39 reviewer): voiceFollowStopAudio must abort an in-flight
    TTS fetch, not just pause the current Audio, so a stale reply can't resolve
    and play after a new turn starts."""
    text = (ASSETS / EMBED_NAME).read_text(encoding="utf-8")
    # the stop path cancels the controller
    assert "ttsAbort.abort()" in text
    # the fetch carries the controller's signal
    assert "signal: controller.signal" in text
    # the resolve path bails if the fetch was aborted mid-flight
    assert "controller.signal.aborted" in text


def test_docroot_has_no_orphaned_companion_asset():
    """Every companion embed in the docroot must be referenced by a surface."""
    referenced = set()
    for page in SURFACES:
        referenced |= {Path(src).name for src in _script_srcs(_page(page))}
    orphans = [p.name for p in sorted(ASSETS.glob("companion-embed*.js"))
               if p.name not in referenced]
    assert not orphans, f"unreferenced companion embed(s) in the docroot: {orphans}"


# --- 3. the app serves both halves -------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.config as config
    import app.store as store

    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "JOBS_DIR", data / "jobs")
    monkeypatch.setattr(config, "DB_PATH", data / "capturd.sqlite3")
    store.init()

    from app import main as app_main

    with TestClient(app_main.app) as c:
        yield c


@pytest.mark.parametrize("page,path", [("index.html", "/"), ("m.html", "/m")])
def test_app_serves_the_surface_with_the_embed(client, page, path):
    r = client.get(path)
    assert r.status_code == 200
    assert EMBED_REF in r.text


def test_app_serves_the_embed_as_javascript(client):
    r = client.get(f"{EMBED_REF}?v=r4-capturd-rho-20260921")
    assert r.status_code == 200
    ctype = r.headers.get("content-type", "")
    assert "javascript" in ctype, (
        f"embed served as {ctype!r} — a browser blocks a non-JS script at load, which is "
        "how the companion disappears without an error the user can report"
    )
    assert r.content == (ASSETS / EMBED_NAME).read_bytes()


# --- the brain pass-through ---------------------------------------------------

class _StubBrain(BaseHTTPRequestHandler):
    """Stand-in for rhobear-companion: JSON, SSE, an odd status, and an echo."""

    seen: list = []

    def log_message(self, fmt, *args):        # keep pytest output clean
        pass

    def _send(self, status, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).seen.append(("GET", self.path, dict(self.headers)))
        if self.path.startswith("/api/me"):
            self._send(200, json.dumps({"signedIn": True, "email": "stub@rhobear.ai"}).encode(),
                       "application/json")
        elif self.path.startswith("/assets/orb/"):
            self._send(200, b"\x89PNG\r\n\x1a\nstub-orb", "image/png")
        else:
            self._send(404, b'{"error":"nope"}', "application/json")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        type(self).seen.append(("POST", self.path, dict(self.headers), body))
        if self.path.startswith("/api/chat"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for ev in (b'event: session\ndata: {"sessionId":"s1"}\n\n',
                       b'event: delta\ndata: {"text":"hey"}\n\n',
                       b'event: done\ndata: {}\n\n'):
                self.wfile.write(ev)
                self.wfile.flush()
        elif self.path.startswith("/api/teapot"):
            self._send(418, b'{"error":"teapot"}', "application/json")
        else:
            self._send(404, b'{"error":"nope"}', "application/json")


@pytest.fixture
def stub_brain(monkeypatch):
    _StubBrain.seen = []
    port = _free_port()
    srv = HTTPServer(("127.0.0.1", port), _StubBrain)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    monkeypatch.setattr("app.main._COMPANION_UPSTREAM", f"http://127.0.0.1:{port}")
    try:
        yield port
    finally:
        srv.shutdown()
        srv.server_close()


def test_companion_route_forwards_json_and_identity(client, stub_brain):
    r = client.get("/companion/api/me", headers={"Authorization": "Bearer stub-token"})
    assert r.status_code == 200
    assert r.json() == {"signedIn": True, "email": "stub@rhobear.ai"}
    assert "application/json" in r.headers.get("content-type", "")
    seen = [s for s in _StubBrain.seen if s[1].startswith("/api/me")]
    assert seen, "the brain never saw /api/me — the route did not proxy"
    assert seen[-1][2].get("authorization") == "Bearer stub-token", "auth headers dropped"
    # Upstream's own date/server are dropped rather than stacked onto the host's
    # (TestClient adds neither, so a stacked copy shows up as 'BaseHTTP/…').
    assert "basehttp" not in (r.headers.get("server") or "").lower()
    assert r.headers.get("date") is None


def test_companion_route_streams_sse_unchanged(client, stub_brain):
    r = client.post("/companion/api/chat",
                    json={"text": "hello Rho", "sessionId": "s1", "mode": "chat"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    for ev in ("event: session", "event: delta", "event: done"):
        assert ev in r.text, f"SSE event {ev!r} did not survive the proxy"
    posted = [s for s in _StubBrain.seen if s[0] == "POST" and s[1].startswith("/api/chat")]
    assert posted, "the brain never saw /api/chat"
    assert json.loads(posted[-1][3].decode()) == {
        "text": "hello Rho", "sessionId": "s1", "mode": "chat"}, "request body not forwarded"


def test_companion_route_serves_orb_art(client, stub_brain):
    r = client.get("/companion/assets/orb/idle.png")
    assert r.status_code == 200
    assert r.headers.get("content-type") == "image/png"
    assert r.content.startswith(b"\x89PNG")


def test_companion_route_preserves_upstream_status(client, stub_brain):
    r = client.post("/companion/api/teapot", json={})
    assert r.status_code == 418


def test_companion_route_reports_a_dead_brain_honestly(client, monkeypatch):
    monkeypatch.setattr("app.main._COMPANION_UPSTREAM", f"http://127.0.0.1:{_free_port()}")
    r = client.get("/companion/api/me")
    assert r.status_code == 502
    assert "companion upstream unreachable" in r.json().get("error", "")


# --- 3b. the real brain, when it is running on this box ----------------------
# Not a gate (CI has no brain); it is the integration proof that the same-origin
# contract the HTML now declares actually terminates in the live companion.

def _brain_is_up() -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", 8787)) == 0


@pytest.mark.skipif(not _brain_is_up(), reason="rhobear-companion not running on this box")
def test_same_origin_companion_reaches_the_live_brain(client):
    r = client.get("/companion/api/me")
    assert r.status_code == 200
    assert "application/json" in r.headers.get("content-type", "")
