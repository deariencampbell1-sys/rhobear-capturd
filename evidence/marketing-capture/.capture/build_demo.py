"""Marketing capture driver v2 — drives the REAL Captur'd live recorder
(forge.start_recording, the exact path demo.record takes) against the REAL
local studio. Cookies + camera injection happen on the recorder's own context.

Run from repo root: python evidence/marketing-capture/.capture/build_demo.py
"""
from __future__ import annotations

import asyncio
import base64
import time
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "service"))

EVID = REPO / "evidence" / "marketing-capture"
OUT = EVID / ".capture"
DEMOS = OUT / "demos"
DEMOS.mkdir(parents=True, exist_ok=True)

# Owner's own env-driven Pro unlock (capturd/pro.py) for this capture process.
os.environ.setdefault("CAPTURD_PRO_CODES", "CAPTURE-LOCAL-1")
os.environ.setdefault("RHOBEAR_CAPTURD_LICENSE", "CAPTURE-LOCAL-1")
# Same data dir the studio service runs with (session cookies must match).
os.environ["CAPTURD_DATA_DIR"] = str(EVID / ".svc-data")

STUDIO = "http://127.0.0.1:8099/"


async def main() -> None:
    from capturd.walk.coordinator import DemoForge
    from playwright.async_api import Browser

    forge = DemoForge(demos_dir=DEMOS)

    # ---- camera: record the recorder's own browser context -----------------
    video_dir = str(OUT / "recorder-video")
    Path(video_dir).mkdir(parents=True, exist_ok=True)
    _orig_ctx = Browser.new_context

    async def new_context_with_video(self, **kwargs):
        if kwargs.get("viewport", {}).get("width") == 1440:
            kwargs.setdefault("record_video_dir", video_dir)
            kwargs.setdefault("record_video_size", {"width": 1440, "height": 900})
        return await _orig_ctx(self, **kwargs)

    Browser.new_context = new_context_with_video

    # ---- 1. start the REAL live session ------------------------------------
    import threading
    last_err = None
    for attempt in range(3):
        recorder, sid, mode = forge.start_recording({
            "url": STUDIO,
            "name": "Captur'd — self demo",
            "mode": "live",
            "viewport": {"width": 1440, "height": 900},
        })
        print("session:", sid, mode, f"(attempt {attempt + 1})", flush=True)

        # EXACTLY what demo.record does for live mode: park a loop in a thread.
        def _spawn_session():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(recorder.start())
                loop.run_forever()
            except Exception:                        # noqa: BLE001
                import logging
                logging.getLogger("capture").exception("recorder thread crashed")
            finally:
                recorder.finished.set()
                try:
                    loop.close()
                except Exception:                    # noqa: BLE001
                    pass

        threading.Thread(target=_spawn_session, name=f"demo-recorder-{sid}",
                         daemon=True).start()
        t0 = time.time()
        while (recorder._page is None and not recorder.finished.is_set()
               and time.time() - t0 < 60):
            await asyncio.sleep(0.25)
        if recorder._page is not None and not recorder.finished.is_set():
            break
        last_err = RuntimeError(f"recorder attempt {attempt + 1} died (goto race?)")
        print("attempt died, retrying", flush=True)
        forge.discard_recorder(sid)
    else:
        raise last_err or RuntimeError("recorder never started")

    page = recorder._page

    # Mint a session in the real local service's own auth store.
    from app import store as svc_store
    svc_store.init()
    u = svc_store.upsert_user("capture@rhobear.ai")
    token = svc_store.new_session(u["id"])

    # Page-level ops MUST bridge onto the recorder's loop (run_on_session) —
    # awaiting its objects from this loop deadlocks forever. Bounded: 30s.
    async def _sign_in():
        if page.url in ("about:blank", ""):
            await page.goto(STUDIO, wait_until="load")
        await page.context.add_cookies([{
            "name": "capturd_session", "value": token,
            "domain": "127.0.0.1", "path": "/",
        }])
        await page.reload(wait_until="load")
        await page.reload(wait_until="load")
        # The onboarding overlay mounts ~1-2s AFTER load (post /api/me).
        # Give it its window, then dismiss it deterministically.
        seen = False
        for _ in range(16):                      # 4s watch window
            st = await page.evaluate(
                "() => ({onb: document.getElementById('ctOnb')"
                ".classList.contains('on')})")
            if st["onb"]:
                seen = True
                break
            await asyncio.sleep(0.25)
        if seen:
            await page.click("#ctobSkip", timeout=5000)
            await asyncio.sleep(0.5)
            gone = await page.evaluate(
                "() => !document.getElementById('ctOnb')"
                ".classList.contains('on')")
            assert gone, "onboarding overlay still up after Skip"
        await asyncio.sleep(0.5)
        # HARD VERIFICATION: the first mapped target must receive events.
        check = await page.evaluate("""() => {
          const tpl = document.querySelector('button[data-tpl="saas-walkthrough"]');
          if (!tpl) return {ok: false, why: 'no template button'};
          tpl.scrollIntoView({block: 'center'});
          const r = tpl.getBoundingClientRect();
          const top = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
          return {ok: !!(top && (top === tpl || tpl.contains(top) || top.closest('.capturd-step-3'))),
                  why: top ? String(top.className).slice(0, 60) : 'nothing at point'};
        }""")
        assert check["ok"], f"template button still intercepted: {check['why']}"
        await asyncio.sleep(0.5)

    await asyncio.to_thread(recorder.run_on_session, _sign_in(), 45.0)
    print("signed in + onboarding cleared + click-path verified")

    # ---- 3. the agent drives the real product ------------------------------
    steps = [
        ("input", "#url", "https://rhobear.ai",
         "Point the camera at your app — type the URL, that's the whole setup."),
        ("input", "#brief",
         "Show the landing page hero and end on the signup CTA.",
         "Brief the director in plain English — it extends the same voice."),
        ("click", 'button[data-tpl="saas-walkthrough"]', None,
         "Pick the SaaS walkthrough template — the classic product tour."),
        ("click", 'button[data-aspect="16:9"]', None,
         "Choose 16:9 for desktop-first demos."),
        ("click", '.voice[data-voice="polly:Ruth"]', None,
         "Pick the Ruth HD voice for the narration."),
        ("click", "#filmBtn", None,
         "Hit Film it — the crew takes it from here."),
    ]
    for i, (action, selector, value, note) in enumerate(steps):
        before = len(recorder.spec.steps)
        try:
            await asyncio.wait_for(
                asyncio.to_thread(recorder.act, action, selector, value, note),
                timeout=60)
        except Exception as exc:                      # noqa: BLE001
            print(f"act {i} ({selector}) FAILED:", type(exc).__name__, str(exc)[:160], flush=True)
            continue
        print(f"act {i} ok ({selector}) steps {before}->{len(recorder.spec.steps)}", flush=True)
        await asyncio.to_thread(recorder.narrate, note)
        await asyncio.sleep(1.4)

    # hold on the filming state (progress + chips + bear) ~5s, then stop
    await asyncio.sleep(5)

    look = await asyncio.to_thread(recorder.look)
    print("look ok:", type(look).__name__)

    # ---- 4. stop + persist the real DemoSpec -------------------------------
    spec = await asyncio.to_thread(recorder.stop)
    forge.discard_recorder(sid)
    demo_id = spec.id
    forge.save_spec(demo_id, spec.to_dict())
    print("demo saved:", demo_id, "steps:", len(spec.steps))

    # ---- 5. real agent edits via the SAME forge methods the MCP tools call --
    forge.append_animation_keyframe(
        demo_id, 0, "zoomTo", target='button[data-tpl="saas-walkthrough"]',
        zoom_level=2.2, duration=500, easing="ease-in-out")
    forge.append_animation_keyframe(
        demo_id, 1, "spotlightOn", target='button[data-aspect="16:9"]')
    forge.append_animation_keyframe(
        demo_id, 2, "zoomTo", target='.voice[data-voice="polly:Ruth"]',
        zoom_level=2.4, duration=500, easing="ease-in-out")
    br = forge.add_choices(
        demo_id, 0,
        [
            {"id": "template", "label": "Start with a template", "destination": 1,
             "analyticsName": "pick-template"},
            {"id": "voice", "label": "Choose the voice first", "destination": 2,
             "analyticsName": "pick-voice"},
        ],
        branch_id="entry-q",
    )
    print("branch:", br)

    # ---- 6. real per-step voiceover (product's own edge-tts path) ----------
    from capturd.walk.ai_pipeline import _synthesize_one

    spec = forge.load_spec(demo_id)
    for i, step in enumerate(spec.get("steps", [])):
        text = (step.get("annotation") or "").strip()
        if not text:
            print(f"step {i}: no annotation (narrate missing?)")
            continue
        audio, words = await _synthesize_one(text, "en-US-AriaNeural")
        step["voiceoverBase64"] = base64.b64encode(audio).decode("ascii")
        step["voiceoverWords"] = [
            {"word": w.word, "tStartMs": w.tStartMs, "tEndMs": w.tEndMs}
            for w in words
        ]
        print(f"voice step {i}: {len(audio)}B {len(words)}w")
    forge.save_spec(demo_id, spec)

    # ---- 7. real exports ----------------------------------------------------
    html_path = forge.export_demo(demo_id, fmt="html")
    print("viewer ->", html_path)
    try:
        mp4_path = forge.export_demo(demo_id, fmt="mp4")
        print("mp4 ->", mp4_path)
    except Exception as exc:                          # noqa: BLE001
        print("mp4 export failed:", exc)

    # ---- 8. copy deliverables ------------------------------------------------
    import shutil
    demo_dir = forge.demo_dir(demo_id)
    for name in ("viewer.html", "demo.json", "walkthrough.mp4"):
        src = demo_dir / name
        if src.is_file():
            shutil.copy2(src, OUT / f"selfdemo-{name}")
    (OUT / "demo-id.txt").write_text(demo_id, encoding="utf-8")
    print("DONE", demo_id)


asyncio.run(main())
