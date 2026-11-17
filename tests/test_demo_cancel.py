"""GAPS-NOW M1 (JOURNEY E3, brief 06 item 4): cancel a running capture.

A real headless live session on a loopback fixture: cancel stops the
recorder, the whole browser process tree exits, captured steps stay on disk
labelled partial, and nothing is enriched. A cancel after the session has
ended is reported as too late and changes nothing.
"""
from __future__ import annotations

import asyncio
import functools
import http.server
import os
import sys
import threading
import time
from pathlib import Path

import pytest

import capturd.mcp.server as server_mod
from capturd.walk.coordinator import DemoForge

FIXTURES = Path(__file__).resolve().parent / "fixtures"
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="process-tree proof reads /proc")


def _descendants() -> set[int]:
    parents: dict[int, int] = {}
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                stat = Path(f"/proc/{entry}/stat").read_text().rsplit(")", 1)[1].split()
            except OSError:
                continue
            parents[int(entry)] = int(stat[1])
    tree, grew = {os.getpid()}, True
    while grew:
        grew = False
        for pid, ppid in parents.items():
            if ppid in tree and pid not in tree:
                tree.add(pid)
                grew = True
    return tree - {os.getpid()}


def _call(server, name, args):
    result = asyncio.run(server.call_tool(name, args))
    content = result.structured_content
    return content.get("result", content)


@pytest.fixture
def fixture_url(monkeypatch):
    # Test-only: the SSRF guard refuses loopback; this test serves its own page.
    monkeypatch.setattr(server_mod, "_reject_private_url", lambda url: None)
    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(_QuietHandler, directory=str(FIXTURES)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/signup-flow.html"
    httpd.shutdown()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@linux_only
def test_cancel_ends_the_browser_tree_and_keeps_a_partial_demo(tmp_path, fixture_url):
    forge = DemoForge(demos_dir=tmp_path / "demos")
    server = server_mod._build_server(forge)
    before = _descendants()
    started = _call(server, "demo.record", {"url": fixture_url, "name": "c", "mode": "live",
                                            "visible": False, "voice": False})
    session = started["sessionId"]
    assert _descendants() - before, "the live session launched a browser"
    _call(server, "demo.act", {"session_id": session, "action": "click", "selector": "body"})

    cancelled = _call(server, "demo.cancel", {"session_id": session})
    assert cancelled["ok"] is True and cancelled["status"] == "cancelled"
    assert cancelled["partial"] is True and cancelled["stepCount"] >= 1

    deadline = time.monotonic() + 10
    while _descendants() - before and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not (_descendants() - before), "browser processes outlived the cancel"

    status = forge.get_status(session)
    assert status["cancelled"] is True and status["partial"] is True
    assert status["status"] != "enriched"


def test_cancel_after_the_session_ended_is_too_late(tmp_path):
    forge = DemoForge(demos_dir=tmp_path / "demos")
    forge.save_spec("done1", {
        "version": 1, "id": "done1", "name": "done1", "goal": "g",
        "createdAt": "2026-11-17T00:00:00Z", "viewport": {"width": 16, "height": 16},
        "startUrl": "https://example.com",
        "aiAnnotations": {"style": "snappy", "summary": "", "animationTimeline": []},
        "steps": []})
    server = server_mod._build_server(forge)
    before = forge.get_status("done1")
    answer = _call(server, "demo.cancel", {"session_id": "done1"})
    assert answer["ok"] is False and answer["code"] == "too_late"
    assert forge.get_status("done1") == before


def test_cancel_of_an_unknown_session_is_refused(tmp_path):
    server = server_mod._build_server(DemoForge(demos_dir=tmp_path / "demos"))
    with pytest.raises(Exception, match="unknown recording session"):
        asyncio.run(server.call_tool("demo.cancel", {"session_id": "nope"}))


@linux_only
def test_cancel_interrupts_blocked_agent_and_preserves_partial(tmp_path, fixture_url, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class BlockedGateway(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            entered.set()
            release.wait(20)
        def log_message(self, *args):
            pass

    gateway = http.server.ThreadingHTTPServer(('127.0.0.1', 0), BlockedGateway)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    monkeypatch.setenv('RHOBEAR_GW_API_KEY', 'test-only')
    monkeypatch.setenv('RHOBEAR_GW_BASE_URL', f'http://127.0.0.1:{gateway.server_port}/v1')
    import signal
    signals = []
    killpg = os.killpg
    def ignore_term(pid, sig):
        signals.append(sig)
        if sig != signal.SIGTERM:
            killpg(pid, sig)
    monkeypatch.setattr(server_mod.os, 'killpg', ignore_term)
    forge = DemoForge(demos_dir=tmp_path / 'demos')
    server = server_mod._build_server(forge)
    before = _descendants()
    try:
        started = _call(server, 'demo.record', {'url': fixture_url, 'name': 'c', 'goal': 'g', 'mode': 'agent', 'visible': False})
        session = started['sessionId']
        assert entered.wait(15), 'agent did not enter blocked turn'
        assert _descendants() - before, 'agent did not start a browser'
        cancelled = _call(server, 'demo.cancel', {'session_id': session})
        assert cancelled['partial'] is True
        assert signal.SIGTERM in signals and signal.SIGKILL in signals
        deadline = time.monotonic() + 5
        while _descendants() - before and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _descendants() - before
        status = forge.get_status(session)
        assert status['partial'] and status['status'] != 'enriched'
        with pytest.raises(Exception, match='(?i)(cancel|partial)'):
            forge.export_demo(session)
    finally:
        release.set()
        gateway.shutdown()


@linux_only
def test_agent_process_can_finish_and_stop_normally(tmp_path, fixture_url, monkeypatch):
    import json
    class Gateway(http.server.BaseHTTPRequestHandler):
        count = 0
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            type(self).count += 1
            reply = {'action': 'click', 'selector': '#signup-btn'} if self.count == 1 else {'action': 'done'}
            body = json.dumps({'choices': [{'message': {'content': json.dumps(reply)}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    gateway = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    monkeypatch.setenv('RHOBEAR_GW_API_KEY', 'test-only')
    monkeypatch.setenv('RHOBEAR_GW_BASE_URL', f'http://127.0.0.1:{gateway.server_port}/v1')
    forge = DemoForge(demos_dir=tmp_path / 'demos')
    monkeypatch.setattr(forge, 'enrich_demo', lambda demo_id: {'status': 'pending', 'jobId': 'j'})
    server = server_mod._build_server(forge)
    try:
        started = _call(server, 'demo.record', {'url': fixture_url, 'name': 'c', 'goal': 'g', 'mode': 'agent', 'visible': False})
        stopped = _call(server, 'demo.stop', {'session_id': started['sessionId']})
        assert stopped['stepCount'] >= 1
        assert stopped['status'] != 'failed'
        assert not (forge.demo_dir(started['sessionId']) / 'recording-incomplete').exists()
    finally:
        gateway.shutdown()
