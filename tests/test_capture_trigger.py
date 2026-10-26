from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock

import pytest

from capturd.shots import cli, capture
from capturd.shots.capture import _apply_trigger


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", [None, "", "   "])
async def test_apply_trigger_rejects_empty_selector_without_page_access(selector):
    page = AsyncMock()

    assert await _apply_trigger(page, selector, 123) is False

    page.query_selector.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_trigger_returns_false_when_element_is_missing():
    page = AsyncMock()
    page.query_selector.return_value = None
    internal_exceptions = []

    def trace(frame, event, arg):
        if (
            event == "exception"
            and frame.f_code is capture._apply_trigger.__code__
            and arg[0] is not StopIteration
        ):
            internal_exceptions.append(arg[0])
        return trace

    previous_trace = sys.gettrace()
    sys.settrace(trace)
    try:
        result = await _apply_trigger(page, "#missing", 123)
    finally:
        sys.settrace(previous_trace)

    assert result is False
    assert internal_exceptions == [], "a missing element is not a click failure"
    page.query_selector.assert_awaited_once_with("#missing")


@pytest.mark.asyncio
async def test_apply_trigger_clicks_then_settles_and_returns_true(monkeypatch):
    page = AsyncMock()
    element = AsyncMock()
    page.query_selector.return_value = element
    settle = AsyncMock()
    monkeypatch.setattr("capturd.shots.capture._settle_page", settle)

    assert await _apply_trigger(page, "  #open  ", 123) is True

    page.query_selector.assert_awaited_once_with("#open")
    element.click.assert_awaited_once_with(timeout=2500)
    settle.assert_awaited_once_with(page, 123)


@pytest.mark.asyncio
async def test_apply_trigger_returns_false_when_click_fails(monkeypatch):
    page = AsyncMock()
    element = AsyncMock()
    element.click.side_effect = TimeoutError("click timed out")
    page.query_selector.return_value = element
    settle = AsyncMock()
    monkeypatch.setattr("capturd.shots.capture._settle_page", settle)

    assert await _apply_trigger(page, "#open", 123) is False

    settle.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_trigger_returns_false_when_query_fails():
    page = AsyncMock()
    page.query_selector.side_effect = RuntimeError("bad selector")

    assert await _apply_trigger(page, "#invalid", 123) is False


@pytest.mark.asyncio
async def test_apply_trigger_returns_false_when_settle_fails(monkeypatch):
    page = AsyncMock()
    page.query_selector.return_value = AsyncMock()
    monkeypatch.setattr(
        "capturd.shots.capture._settle_page",
        AsyncMock(side_effect=RuntimeError("page closed")),
    )

    assert await _apply_trigger(page, "#open", 123) is False


def test_dry_run_plan_json_omits_cookie_credentials(capsys):
    secret = "session-value-that-must-not-leak"
    exit_code = cli.main([
        "--urls", "example.com",
        "--cookie", f"session={secret};example.com",
        "--dry-run",
    ])

    assert exit_code == 0
    output = capsys.readouterr().out
    plan = json.loads(next(
        line[len("plan-json: "):]
        for line in output.splitlines()
        if line.startswith("plan-json: ")
    ))
    assert "cookies" not in plan["settings"]
    assert secret not in output
