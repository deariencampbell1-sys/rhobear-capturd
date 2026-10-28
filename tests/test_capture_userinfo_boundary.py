"""Regression: every outward capture result must lose URL userinfo at the engine boundary.

This drives the documented library function and the real manager/export path.
Only the browser machinery and capture attempt are faked: the latter deliberately
returns unsafe Playwright-like results to exercise the final shared result guard.
"""
from __future__ import annotations

import json
import sys
import threading
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from capturd.shots import capture


@pytest.mark.usefixtures("_offline_test_dns")
def test_userinfo_is_absent_from_every_capture_result_sink(tmp_path, monkeypatch, capsys):
    raw_url = "https://UsEr:ToPsEcReT@example.com/private"
    payload = {
        "urls": [raw_url],
        "viewports": ["desktop"],
        "schemes": ["light"],
        "retries": 0,
    }
    _, targets, settings = capture.build_capture_plan(payload)

    # No external browser or network is needed. Emit the raw URL and an
    # unsanitized browser-style error, including one nested state failure.
    async def capture_one(_context, target, _settings, _path, **_kwargs):
        return {
            "url": target.url,
            "state_id": target.state_id,
            "status": "failed",
            "error": f'Page.goto failed at {target.url}; navigating to "{target.url}"',
            "file": "",
            "bytes": 0,
            "states": [{
                "url": target.url,
                "status": "failed",
                "error": f'State navigation failed at {target.url}',
            }],
        }

    context = SimpleNamespace(
        route=AsyncMock(),
        route_web_socket=AsyncMock(),
        close=AsyncMock(),
        add_cookies=AsyncMock(),
    )
    browser = SimpleNamespace(
        new_context=AsyncMock(return_value=context),
        close=AsyncMock(),
    )

    class FakePlaywright:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    playwright = ModuleType("playwright")
    playwright.__path__ = []
    async_api = ModuleType("playwright.async_api")
    async_api.async_playwright = FakePlaywright
    playwright.async_api = async_api
    monkeypatch.setitem(sys.modules, "playwright", playwright)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)
    monkeypatch.setattr(capture, "_launch_browser", AsyncMock(return_value=browser))
    monkeypatch.setattr(capture, "_capture_one", capture_one)

    progress_results = []
    library_results = capture.run_capture(
        targets, settings, tmp_path / "direct-shots", progress_results.append,
    )

    # Run manager.start synchronously so the test observes the *real* job
    # record, get(), manifest and ZIP without a polling race or background job.
    class InlineThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(
        capture, "threading", SimpleNamespace(Lock=threading.Lock, Thread=InlineThread),
    )
    manager = capture.RestedCaptureManager(tmp_path / "jobs")
    started = manager.start(payload)
    job_id = started["job_id"]
    finished = manager.get(job_id)
    raw_job_record = manager._jobs[job_id]
    manifest = (Path(finished["work_dir"]) / "manifest.json").read_text(encoding="utf-8")

    with zipfile.ZipFile(manager.zip_path(job_id)) as archive:
        zip_members = {
            name: archive.read(name)
            for name in archive.namelist()
        }

    assert library_results and progress_results
    assert finished["status"] == "done_with_errors"
    assert zip_members, "the export must contain a manifest"

    # What an ordinary library caller prints must also be safe.
    print(json.dumps(library_results))
    print(json.dumps(finished), file=sys.stderr)
    output = capsys.readouterr()

    text_sinks = {
        "run_capture return": json.dumps(library_results),
        "run_capture progress": json.dumps(progress_results),
        "manager job record": json.dumps(raw_job_record),
        "manager get": json.dumps(finished),
        "manager result errors": json.dumps([
            result.get("error") for result in finished["results"]
        ] + [
            state.get("error")
            for result in finished["results"]
            for state in result.get("states", [])
        ]),
        "manifest.json": manifest,
        "stdout": output.out,
        "stderr": output.err,
    }
    for name, body in zip_members.items():
        text_sinks[f"ZIP filename: {name}"] = name
        # ASCII userinfo must not occur even in a non-text ZIP member.
        assert b"topsecret" not in body.lower(), f"credential in ZIP member {name}"
        assert b"user:topsecret@" not in body.lower(), f"userinfo in ZIP member {name}"

    for sink, body in text_sinks.items():
        folded = body.casefold()
        assert "topsecret" not in folded, f"credential escaped via {sink}"
        assert "user:topsecret@" not in folded, f"userinfo escaped via {sink}"

    assert library_results[0]["url"] == "https://example.com/private"
    assert progress_results[0]["url"] == "https://example.com/private"
    assert library_results[0]["states"][0]["url"] == "https://example.com/private"
