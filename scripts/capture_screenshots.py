"""Capture screenshots of the Captur'd app for the compare board.

Usage: python scripts/capture_screenshots.py

Starts the FastAPI app, opens Playwright, captures each key screen,
saves to service/web/_board/renders/.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import urlencode

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "service"))

RENDERS = REPO / "service" / "web" / "_board" / "renders"
RENDERS.mkdir(parents=True, exist_ok=True)


def with_server(test_fn):
    """Context manager: start uvicorn in a thread, yield to the test."""
    # Ensure both service/ and repo root are on the path
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(REPO / "service"))
    from app.main import app
    import uvicorn
    import threading
    import socket

    def find_free_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    port = find_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(1.5)  # wait for server to start
    try:
        test_fn(port)
    finally:
        server.should_exit = True
        thread.join(timeout=3)


def main():
    from playwright.sync_api import sync_playwright

    def capture(port):
        base = f"http://127.0.0.1:{port}"

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            # ── Desktop 1280×800 ──────────────────────────────────────────
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 800},
                device_scale_factor=2,
            )
            page = ctx.new_page()

            # Studio empty (signed-out hero)
            page.goto(base + "/")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(RENDERS / "studio-empty.png"), full_page=False)
            print("[OK] studio-empty.png")

            # Sign in — we can't actually auth, but we can inject a session to
            # see the studio. The simplest approach: modify the page state.
            # For the filled studio, we'll use the TestClient to simulate a
            # signed-in state by injecting JS that triggers the studio view.
            page.evaluate("""
                // Simulate signed-in state for screenshots
                document.getElementById('signedOut').classList.remove('on');
                document.getElementById('dash').classList.add('on');
                document.getElementById('acct').classList.remove('hide');
                document.getElementById('acctEmail').textContent = 'demo@rhobear.ai';
                document.getElementById('planPill').textContent = 'free';
                document.getElementById('planPill').className = 'pill free';
            """)
            page.wait_for_timeout(300)
            page.screenshot(path=str(RENDERS / "studio-empty.png"), full_page=False)
            print("[OK] studio-empty.png (signed-in)")

            # Studio filled — inject values
            page.fill("#url", "https://myproduct.com")
            page.fill("#brief", "Show how easy onboarding is — end on the dashboard.")
            page.evaluate("syncFilmBtn()")
            page.wait_for_timeout(200)
            page.screenshot(path=str(RENDERS / "studio-filled.png"), full_page=False)
            print("[OK] studio-filled.png")

            # Filming overlay
            page.evaluate("""
                document.getElementById('studioForm').classList.add('hide');
                document.getElementById('prog').classList.add('on');
                document.getElementById('progFill').style.width = '65%';
                document.getElementById('progPct').textContent = '65%';
                document.getElementById('progRem').textContent = '35%';
                document.getElementById('filmChips').innerHTML =
                    '<span>SaaS walkthrough</span><span>Charon HD</span><span>16:9</span>';
                // Stage 2 active
                document.querySelectorAll('#stages .capturd-stage').forEach(el => {
                    const n = +el.dataset.stage;
                    el.classList.toggle('capturd-stage--done', n < 2);
                    el.classList.toggle('capturd-stage--active', n === 2);
                });
            """)
            page.wait_for_timeout(300)
            page.screenshot(path=str(RENDERS / "filming.png"), full_page=False)
            print("[OK] filming.png")

            # Demos gallery — inject some mock demos
            page.evaluate("""
                document.getElementById('prog').classList.remove('on');
                document.getElementById('studioForm').classList.remove('hide');
                // Inject mock gallery data
                const gal = document.getElementById('galgrid');
                gal.innerHTML = '';
                document.getElementById('galempty').classList.add('hide');
                // Create a mock demo
                const demo = document.createElement('div');
                demo.className = 'demo';
                demo.innerHTML = `
                    <div class="demo__thumb">
                        <span class="play"></span>
                    </div>
                    <div class="demo__body">
                        <div class="demo__title">SaaS walkthrough</div>
                        <div class="demo__url">https://myproduct.com</div>
                        <div class="demo__tags">
                            <span class="tg tg-tpl">SaaS walkthrough</span>
                            <span class="tg">Charon <span class="hd">HD</span></span>
                            <span class="tg">16:9</span>
                        </div>
                        <div class="demo__meta">
                            <span>8/9/2026</span>
                            <span>42s</span>
                            <span class="st done">done</span>
                        </div>
                    </div>
                    <div class="demo__actions">
                        <a class="demo__act" href="#">Download</a>
                        <button class="demo__act">Watch</button>
                        <button class="demo__act">Re-film</button>
                    </div>
                `;
                gal.appendChild(demo);
                // Second demo
                const demo2 = demo.cloneNode(true);
                demo2.querySelector('.demo__title').textContent = 'Feature spotlight';
                demo2.querySelector('.demo__url').textContent = 'https://myproduct.com/features';
                demo2.querySelector('.tg-tpl').textContent = 'Feature spotlight';
                gal.appendChild(demo2);
            """)
            page.wait_for_timeout(300)

            # Scroll to show the gallery
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(200)
            page.screenshot(path=str(RENDERS / "demos.png"), full_page=False)
            print("[OK] demos.png")

            # First-run onboarding overlay
            page.evaluate("""
                window.scrollTo(0, 0);
                // Reset localStorage so onboarding shows
                try { localStorage.removeItem('capturd_onboarded_v1'); } catch(e) {}
                window.maybeOnboard();
            """)
            page.wait_for_timeout(500)
            page.screenshot(path=str(RENDERS / "onboarding.png"), full_page=False)
            print("[OK] onboarding.png")

            ctx.close()

            # ── Mobile 390×844 (iPhone 14 Pro) ────────────────────────────
            mobile_ctx = browser.new_context(
                viewport={"width": 390, "height": 844},
                device_scale_factor=3,
                is_mobile=True,
                has_touch=True,
            )
            mobile = mobile_ctx.new_page()

            # Mobile studio — signed-in
            mobile.goto(base + "/m")
            mobile.wait_for_load_state("networkidle")

            # Inject signed-in state
            mobile.evaluate("""
                document.getElementById('signedOut').classList.add('hide');
                document.getElementById('dash').classList.remove('hide');
                document.getElementById('planPill').classList.remove('hide');
                document.getElementById('planPill').textContent = 'free';
                document.getElementById('planPill').className = 'pill free';
                document.getElementById('signout').classList.remove('hide');
            """)
            mobile.wait_for_timeout(300)
            mobile.screenshot(path=str(RENDERS / "m-studio.png"), full_page=False)
            print("[OK] m-studio.png")

            # Mobile demos
            mobile.evaluate("""
                const gal = document.getElementById('galgrid');
                gal.innerHTML = '';
                document.getElementById('galempty').classList.add('hide');
                const demo = document.createElement('div');
                demo.className = 'demo';
                demo.innerHTML = `
                    <div class="demo__thumb"><span class="play"></span></div>
                    <div class="demo__body">
                        <div class="demo__title">SaaS walkthrough</div>
                        <div class="demo__url">https://myproduct.com</div>
                        <div class="demo__tags">
                            <span class="tg tg-tpl">SaaS walkthrough</span>
                            <span class="tg">Charon</span>
                            <span class="tg">16:9</span>
                        </div>
                        <div class="demo__meta">
                            <span>8/9/2026</span>
                            <span>42s</span>
                            <span class="st done">done</span>
                        </div>
                    </div>
                    <div class="demo__actions">
                        <a class="demo__act" href="#">Download</a>
                        <button class="demo__act">Watch</button>
                    </div>
                `;
                gal.appendChild(demo);
            """)
            mobile.wait_for_timeout(200)
            mobile.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            mobile.wait_for_timeout(200)
            mobile.screenshot(path=str(RENDERS / "m-demos.png"), full_page=False)
            print("[OK] m-demos.png")

            mobile_ctx.close()
            browser.close()

    with_server(capture)
    print("Done — all screenshots saved to", RENDERS)


if __name__ == "__main__":
    main()