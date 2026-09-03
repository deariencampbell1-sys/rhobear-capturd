"""Screenshot the REAL studio (signed in) — desktop 1920x1080 + mobile 390 —
after the bear fix, so the owner can SEE the corrected UX."""
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "service"))

# MUST match the service process's data dir (set identically there).
import os
os.environ["CAPTURD_DATA_DIR"] = str(REPO / "evidence" / "marketing-capture" / ".svc-data")

OUT = REPO / "evidence" / "marketing-capture"
STUDIO = "http://127.0.0.1:8099/"


async def main():
    from playwright.async_api import async_playwright
    from app import store as svc_store

    svc_store.init()
    u = svc_store.upsert_user("capture@rhobear.ai")
    token = svc_store.new_session(u["id"])

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()

        # desktop
        ctx = await browser.new_context(
            viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
        await ctx.add_cookies([{"name": "capturd_session", "value": token,
                                "domain": "127.0.0.1", "path": "/"}])
        page = await ctx.new_page()
        await page.goto(STUDIO, wait_until="networkidle")
        await page.wait_for_timeout(1500)
        # skip onboarding if present
        try:
            await page.click(".ctob-skip", timeout=2500)
            await page.wait_for_timeout(600)
        except Exception:
            pass
        await page.screenshot(path=str(OUT / "UX-CHECK-desktop-studio.png"))
        print("desktop shot done")

        # filming state can't be shown without a render; skip. Mobile next.
        mctx = await browser.new_context(
            viewport={"width": 390, "height": 844}, device_scale_factor=2,
            is_mobile=True)
        await mctx.add_cookies([{"name": "capturd_session", "value": token,
                                 "domain": "127.0.0.1", "path": "/"}])
        mpage = await mctx.new_page()
        await mpage.goto(STUDIO + "m", wait_until="networkidle")
        await mpage.wait_for_timeout(1500)
        try:
            await mpage.click(".ctob-skip", timeout=2500)
            await mpage.wait_for_timeout(600)
        except Exception:
            pass
        await mpage.screenshot(path=str(OUT / "UX-CHECK-mobile-studio.png"))
        print("mobile shot done")

        await browser.close()


asyncio.run(main())
