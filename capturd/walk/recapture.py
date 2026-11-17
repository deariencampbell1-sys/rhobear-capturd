"""Record one shot again from its recorded page and target (GAPS-NOW M5).

The checkpoint a step carries is its page URL, viewport and click target.
`recapture_step` reloads that page in a fresh headless browser, requires the
recorded target to still exist, repeats the click, and screenshots the
viewport after it, the same moment the recorder captures. Anything that
does not hold (page unreachable, target gone) is a `RecaptureError` naming
the cause; no continuity is invented. Sign-in, form contents and earlier
steps are not replayed, so a step that depended on them fails here instead
of producing a plausible wrong frame. The caller checks the URL against the
SSRF guard first.
"""
from __future__ import annotations

from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from capturd.walk.recorder import _launch_demo_browser

_NAV_TIMEOUT_MS = 20_000
_TARGET_TIMEOUT_MS = 5_000
_SETTLE_MS = 400


class RecaptureError(RuntimeError):
    """The recorded checkpoint no longer reproduces; the reason is named."""


async def recapture_step(step: dict[str, Any], viewport: dict[str, int]) -> bytes:
    url = step.get("pageUrl")
    interaction = step.get("interaction") or {}
    selector = ((interaction.get("target") or {}).get("selector") or "").strip()
    if not url:
        raise RecaptureError("the step has no recorded page URL")
    async with async_playwright() as playwright:
        browser = await _launch_demo_browser(playwright, headless=True)
        try:
            context = await browser.new_context(
                viewport={"width": int(viewport["width"]), "height": int(viewport["height"])},
                device_scale_factor=1, locale="en-US")
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="load", timeout=_NAV_TIMEOUT_MS)
            except PlaywrightError as exc:
                raise RecaptureError(f"the page {url} did not load: {exc.message.splitlines()[0]}") from exc
            if interaction.get("type") == "click" and selector and selector != "body":
                try:
                    await page.locator(selector).first.click(timeout=_TARGET_TIMEOUT_MS)
                except PlaywrightError as exc:
                    raise RecaptureError(
                        f"the recorded target {selector} is no longer on the page") from exc
                await page.wait_for_timeout(_SETTLE_MS)
            return await page.screenshot(full_page=False, type="png")
        finally:
            await browser.close()
