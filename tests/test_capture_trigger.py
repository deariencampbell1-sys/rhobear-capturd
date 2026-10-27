from __future__ import annotations

import json
import warnings
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
async def test_apply_trigger_returns_false_when_element_is_missing(monkeypatch):
    page = AsyncMock()
    page.query_selector.return_value = None
    settle = AsyncMock()
    monkeypatch.setattr(capture, "_settle_page", settle)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = await _apply_trigger(page, "#missing", 123)
    assert result is False
    assert caught == [], "a missing element is not a click failure"
    page.query_selector.assert_awaited_once_with("#missing")
    settle.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_trigger_clicks_then_settles_and_returns_true(monkeypatch):
    page = AsyncMock()
    element = AsyncMock()
    page.query_selector.return_value = element
    page.url = "https://example.com/"
    page.evaluate.side_effect = ["before", "after"]
    settle = AsyncMock()
    monkeypatch.setattr("capturd.shots.capture._settle_page", settle)

    assert await _apply_trigger(page, "  #open  ", 123) is True

    page.query_selector.assert_awaited_once_with("#open")
    element.click.assert_awaited_once_with(timeout=capture.TRIGGER_CLICK_TIMEOUT_MS)
    settle.assert_awaited_once_with(page, 123)


@pytest.mark.asyncio
async def test_apply_trigger_returns_false_when_click_fails(monkeypatch):
    page = AsyncMock()
    element = AsyncMock()
    element.click.side_effect = TimeoutError("click timed out")
    page.query_selector.return_value = element
    page.url = "https://example.com/"
    page.evaluate.side_effect = ["before", "after"]
    settle = AsyncMock()
    monkeypatch.setattr("capturd.shots.capture._settle_page", settle)

    with pytest.warns(RuntimeWarning, match=r"^capture trigger failed \(TimeoutError\); trigger_fired=False$"):
        assert await _apply_trigger(page, "#open", 123) is False

    settle.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_trigger_returns_false_when_query_fails():
    page = AsyncMock()
    page.query_selector.side_effect = RuntimeError("bad selector")

    with pytest.warns(RuntimeWarning, match=r"^capture trigger failed \(RuntimeError\); trigger_fired=False$"):
        assert await _apply_trigger(page, "#invalid", 123) is False


@pytest.mark.asyncio
async def test_apply_trigger_returns_false_when_settle_fails(monkeypatch):
    page = AsyncMock()
    page.query_selector.return_value = AsyncMock()
    monkeypatch.setattr(
        "capturd.shots.capture._settle_page",
        AsyncMock(side_effect=RuntimeError("page closed")),
    )

    with pytest.warns(RuntimeWarning, match=r"^capture trigger failed \(RuntimeError\); trigger_fired=False$"):
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


@pytest.mark.asyncio
async def test_r5_no_op_click_is_not_meaningful_state(monkeypatch):
    page = AsyncMock()
    page.url = "https://example.com/"
    page.evaluate.return_value = "same"
    settle = AsyncMock()
    monkeypatch.setattr(capture, "_settle_page", settle)
    clock = iter([0.0] + [step / 10 for step in range(26)])
    monkeypatch.setattr(capture.time, "monotonic", lambda: next(clock, 3.0))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert await _apply_trigger(page, "#load-state", 123) is False
    assert caught == [], "a genuine no-op must not enter the exception handler"
    assert page.wait_for_timeout.await_count == capture.TRIGGER_STATE_TIMEOUT_MS // 100 == 25
    page.wait_for_timeout.assert_awaited_with(100)
    page.query_selector.return_value.click.assert_awaited_once()
    settle.assert_awaited_once_with(page, 123)


@pytest.mark.asyncio
async def test_r5_url_change_is_meaningful_state(monkeypatch):
    page = AsyncMock()
    page.url = "https://example.com/"
    page.evaluate.side_effect = ["same", "same"]
    async def navigate(**kwargs):
        page.url = "https://example.com/settings"
    page.query_selector.return_value.click.side_effect = navigate
    monkeypatch.setattr(capture, "_settle_page", AsyncMock())
    assert await _apply_trigger(page, "#load-state", 123) is True


@pytest.mark.asyncio
async def test_r5_trigger_failure_warns_with_class_only():
    page = AsyncMock()
    page.url = "https://example.com/"
    page.evaluate.return_value = "same"
    page.query_selector.return_value.click.side_effect = TimeoutError("private selector detail")
    with pytest.warns(RuntimeWarning) as caught:
        assert await _apply_trigger(page, "#load-state", 123) is False
    assert str(caught[0].message) == "capture trigger failed (TimeoutError); trigger_fired=False"
    assert "private selector detail" not in str(caught[0].message)


def test_r5_plan_json_uses_secret_safe_allowlist(capsys):
    _, targets, settings = capture.build_capture_plan({"urls": ["example.com"]})
    settings.update({key: "test-only-canary" for key in ("cookies", "headers", "auth", "authorization")})
    cli._print_plan(["https://example.com/"], targets, settings)
    out = capsys.readouterr().out
    assert "test-only-canary" not in out
    plan = json.loads(next(line[11:] for line in out.splitlines() if line.startswith("plan-json: ")))
    assert not {"cookies", "headers", "auth", "authorization"} & plan["settings"].keys()
    assert plan["settings"]["format"] == "png"
