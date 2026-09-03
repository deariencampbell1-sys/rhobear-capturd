"""Record the marketing clips from the REAL Captur'd surfaces.

Sources (all real):
- the live recorder session video (Captur'd recording Captur'd — clip 02/03)
- the REAL exported walkthrough.mp4 (clips 08)
- the real viewer (export.html) played + interacted (01?/04/05/06/07/12/10)
- the real studio (01, stills)

Every clip keeps 2s pre-roll and 3s post-roll (we control start/stop).
30fps 1920x1080 (Playwright webm -> ffmpeg h264).
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[3]
EVID = REPO / "evidence" / "marketing-capture"
OUT = EVID
CAP = EVID / ".capture"
DEMOS = CAP / "demos"
SESSION_VID = CAP / "recorder-video"
STUDIO = "http://127.0.0.1:8099/"

# the self-demo built by build_demo.py
DEMO_ID = (CAP / "demo-id.txt").read_text(encoding="utf-8").strip()
VIEWER = DEMOS / DEMO_ID / "export.html"
EXPORTED_MP4 = DEMOS / DEMO_ID / "walkthrough.mp4"

TMP = CAP / "clip-tmp"
TMP.mkdir(parents=True, exist_ok=True)


def convert(src: Path, dst: Path) -> Path:
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(dst),
    ], check=True)
    return dst


def probe(path: Path) -> str:
    out = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-show_entries", "format=duration",
        "-of", "csv=p=0", str(path),
    ], capture_output=True, text=True).stdout.strip().replace("\n", " ")
    return out


def studio_cookie():
    import os, sys
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(REPO / "service"))
    os.environ["CAPTURD_DATA_DIR"] = str(EVID / ".svc-data")
    from app import store as svc_store
    svc_store.init()
    u = svc_store.upsert_user("capture@rhobear.ai")
    return svc_store.new_session(u["id"])


TOKEN = studio_cookie()


def new_ctx(browser, w=1920, h=1080, signed_in=False):
    ctx = browser.new_context(
        viewport={"width": w, "height": h},
        record_video_dir=str(TMP),
        record_video_size={"width": w, "height": h},
    )
    if signed_in:
        ctx.add_cookies([{"name": "capturd_session", "value": TOKEN,
                          "domain": "127.0.0.1", "path": "/"}])
    return ctx


def save_video(ctx, name: str, fps_note="") -> Path:
    video = ctx.pages[-1].video
    path = video.path()
    ctx.close()
    dst = OUT / name
    convert(path, dst)
    print(f"{name}: {probe(dst)}")
    return dst


def main() -> None:
    made: list[Path] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        # ---- CLIP 01 — clean product hero (the real studio, signed in) -----
        ctx = new_ctx(browser, signed_in=True)
        page = ctx.new_page()
        page.goto(STUDIO, wait_until="networkidle")
        try:
            page.click(".ctob-skip", timeout=3000)
        except Exception:
            pass
        page.wait_for_timeout(3000)                 # completely still
        page.click('button[data-tpl="ux-showcase"]')  # ONE subtle interaction
        page.wait_for_timeout(600)
        page.click('button[data-tpl="saas-walkthrough"]')  # back to clean state
        page.wait_for_timeout(4500)                 # hold still
        made.append(save_video(ctx, "01-capturd-clean-hero.mp4"))

        # ---- CLIP 04 — semantic camera (viewer zoom on real targets) -------
        ctx = new_ctx(browser)
        page = ctx.new_page()
        page.goto(VIEWER.as_uri(), wait_until="load")
        page.wait_for_function("window.__demoViewer")
        page.wait_for_timeout(2000)                 # handles
        page.evaluate("window.__demoViewer.play()")  # narration + camera
        page.wait_for_timeout(14000)                # zooms land + hold
        page.wait_for_timeout(3000)
        made.append(save_video(ctx, "04-semantic-camera.mp4"))

        # ---- CLIP 06 — interactive viewer ----------------------------------
        ctx = new_ctx(browser)
        page = ctx.new_page()
        page.goto(VIEWER.as_uri(), wait_until="load")
        page.wait_for_function("window.__demoViewer")
        page.wait_for_timeout(2000)
        # human interaction: click the hotspot to advance (real viewer behavior)
        page.click("#hotspot", position={"x": 20, "y": 20}, force=True)
        page.wait_for_timeout(2500)
        page.evaluate("window.__demoViewer.play()")  # auto-play the rest
        page.wait_for_timeout(13000)
        page.wait_for_timeout(3000)
        made.append(save_video(ctx, "06-interactive-viewer.mp4"))

        # ---- CLIP 07 — branching (V2 choice screen, genuinely working) -----
        ctx = new_ctx(browser)
        page = ctx.new_page()
        page.goto(VIEWER.as_uri(), wait_until="load")
        page.wait_for_function("window.__demoViewer")
        page.wait_for_timeout(2000)
        page.evaluate("window.__demoViewer.play()")
        page.wait_for_timeout(6500)                  # reach the choice step
        if page.locator("#choice-overlay.visible").count():
            page.wait_for_timeout(1200)              # let the viewer read it
            page.locator(".choice-btn").first.click()
        page.wait_for_timeout(9000)                   # path continues
        page.wait_for_timeout(3000)
        made.append(save_video(ctx, "07-branching.mp4"))

        # ---- CLIP 12 — dogfood: Captur'd demoing Captur'd ------------------
        # The demo itself was recorded FROM the Captur'd studio (clip 02's
        # session); playing it back is the recursive proof.
        ctx = new_ctx(browser)
        page = ctx.new_page()
        page.goto(VIEWER.as_uri(), wait_until="load")
        page.wait_for_function("window.__demoViewer")
        page.wait_for_timeout(2000)
        page.evaluate("window.__demoViewer.play()")
        page.wait_for_timeout(16000)
        page.wait_for_timeout(3000)
        made.append(save_video(ctx, "12-dogfood.mp4"))

        # ---- CLIP 10 — mobile viewer (390px) -------------------------------
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844},
            record_video_dir=str(TMP),
            record_video_size={"width": 390, "height": 844},
        )
        page = ctx.new_page()
        page.goto(VIEWER.as_uri(), wait_until="load")
        page.wait_for_function("window.__demoViewer")
        page.wait_for_timeout(2000)
        page.evaluate("window.__demoViewer.next()")
        page.wait_for_timeout(3500)
        page.evaluate("window.__demoViewer.play()")
        page.wait_for_timeout(9000)
        page.wait_for_timeout(3000)
        made.append(save_video(ctx, "10-mobile-viewer.mp4"))

        # ---- CLIP 05 — editing is the API (before -> real edit -> after) ---
        from capturd.walk.coordinator import DemoForge
        forge = DemoForge(demos_dir=DEMOS)
        before_annotation = forge.load_spec(DEMO_ID)["steps"][0]["annotation"]
        viewer_after = None
        ctx = new_ctx(browser)
        page = ctx.new_page()
        page.goto(VIEWER.as_uri(), wait_until="load")
        page.wait_for_function("window.__demoViewer")
        page.wait_for_timeout(3500)                   # BEFORE state visible
        made_before = page.video.path()
        ctx.close()
        # the REAL edit path (exactly what demo.edit runs on the draft)
        spec = forge.load_spec(DEMO_ID)
        spec["steps"][0]["annotation"] = (
            "Step one: point at your app. One URL — that's the whole setup.")
        forge.save_spec(DEMO_ID, spec)
        shutil.copy2(VIEWER, TMP / "viewer-after.html")
        ctx = new_ctx(browser)
        page = ctx.new_page()
        page.goto((TMP / "viewer-after.html").as_uri(), wait_until="load")
        page.wait_for_function("window.__demoViewer")
        page.wait_for_timeout(4000)                   # AFTER state visible
        page.wait_for_timeout(2000)
        after_path = page.video.path()
        ctx.close()
        # stitch before + after (concat at the action boundary — handles intact)
        b = TMP / "c05-before.webm"
        a = TMP / "c05-after.webm"
        shutil.copy2(made_before, b)
        shutil.copy2(after_path, a)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(b),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        str(TMP / "b.mp4")], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(a),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        str(TMP / "a.mp4")], check=True)
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(TMP / "b.mp4"), "-i", str(TMP / "a.mp4"),
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]",
            "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(OUT / "05-mcp-editing.mp4")], check=True)
        print("05-mcp-editing.mp4:", probe(OUT / "05-mcp-editing.mp4"))
        made.append(OUT / "05-mcp-editing.mp4")
        # restore the demo's real annotation (the edit was for the clip)
        spec["steps"][0]["annotation"] = before_annotation
        forge.save_spec(DEMO_ID, spec)

        # ---- CLIP 08 — voice-camera sync (the REAL exported mp4) -----------
        dst = OUT / "08-voice-camera-sync.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(EXPORTED_MP4),
            "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,"
                   "pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
            "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(dst)], check=True)
        print("08:", probe(dst))
        made.append(dst)

        # ---- CLIP 09 — export proof (demo -> real artifact playing) --------
        ctx = new_ctx(browser)
        page = ctx.new_page()
        page.goto(VIEWER.as_uri(), wait_until="load")
        page.wait_for_function("window.__demoViewer")
        page.wait_for_timeout(3500)                    # the demo itself
        seg1 = page.video.path()
        ctx.close()
        # play the REAL exported mp4 in a browser <video> (artifact proof)
        html = TMP / "play-export.html"
        html.write_text(
            "<body style='margin:0;background:#0d0f14;display:grid;"
            "place-items:center;height:100vh'>"
            "<video src='../.capture/demos/" + DEMO_ID + "/walkthrough.mp4' "
            "style='max-width:100%;max-height:100%' autoplay controls loop>"
            "</video></body>", encoding="utf-8")
        ctx = new_ctx(browser)
        page = ctx.new_page()
        page.goto(html.as_uri(), wait_until="load")
        page.wait_for_timeout(9000)
        page.wait_for_timeout(2500)
        seg2 = page.video.path()
        ctx.close()
        s1 = TMP / "s1.mp4"
        s2 = TMP / "s2.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(seg1),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(s1)],
                       check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(seg2),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        str(s2)], check=True)
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(s1), "-i", str(s2),
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]",
            "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(OUT / "09-export-proof.mp4")],
            check=True)
        print("09:", probe(OUT / "09-export-proof.mp4"))
        made.append(OUT / "09-export-proof.mp4")

        browser.close()

    # ---- CLIP 02/03 — the REAL recorder session (raw) ----------------------
    session_webms = sorted(SESSION_VID.glob("*.webm"), key=lambda p: p.stat().st_mtime)
    if session_webms:
        raw = session_webms[-1]
        dst = OUT / "02-record-real-product.mp4"
        convert(raw, dst)
        print("02:", probe(dst))
        made.append(dst)
        # 03 = the agent-driven segment (acts happen in the back half)
        dur = float(probe(dst).split()[-1])
        start = max(0.0, dur * 0.35)
        seg = OUT / "03-agent-directs-demo.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-ss", str(start),
            "-i", str(dst), "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(seg)], check=True)
        print("03:", probe(seg))
        made.append(seg)

    print("\n=== MADE ===")
    for m in made:
        print(m.name, probe(m))


if __name__ == "__main__":
    main()
