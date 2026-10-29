"""Visual regressions for the deterministic export path (real Chromium).

Fixtures in ``tests/fixtures/visual-regressions/`` are REAL captured frames
(see the README there): two consecutive scroll shots of a genuinely
scrollable public page plus the overlay-only frame from the old rejected
export.

The contract under test (W1-03-r1):

* The second scroll frame must DIFFER from the overlay-only frame and
  CONTAIN the expected source region pixels — a successful ffmpeg exit (or
  a file existing) never proves visible content.
* ``exportSeek(tMs)`` is paint-safe: it resolves a promise only after the
  live images it just showed are decoded and one animation frame has
  passed, so ``exporter._capture_frames`` can screenshot immediately after
  awaiting it and still capture painted pixels.
* No substitute screenshot: the second step's result frame shows the SECOND
  source frame's pixels, not the first's and not a placeholder.
"""

from __future__ import annotations

import base64
import math
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "visual-regressions"
SOURCE_0 = FIXTURES / "scroll-source-0.png"
SOURCE_1 = FIXTURES / "scroll-source-1.png"
OVERLAY_ONLY = FIXTURES / "overlay-only-frame.png"

VIEWPORT = {"width": 1024, "height": 768}
FPS = 8


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _decode_png(path: Path):
    """(width, height, rgb bytes) via the exporter's mechanical PNG reader."""
    from capturd.walk.exporter import _decode_png_rgb

    return _decode_png_rgb(path.read_bytes())


def _px(rgb: bytes, w: int, x: int, y: int) -> tuple[int, int, int]:
    i = (y * w + x) * 3
    return rgb[i], rgb[i + 1], rgb[i + 2]


def _scroll_target(index: int) -> dict:
    """A recorder-shaped synthetic scroll target (body / full viewport)."""
    return {
        "selector": "body",
        "tagName": "",
        "text": "down" if index == 0 else "up",
        "boundingRect": {"x": 0, "y": 0, "width": 1024, "height": 768},
        "role": "",
        "viewport": {"width": 1024, "height": 768, "deviceScaleFactor": 1},
        "captureSpace": "viewport",
        "region": "body",
    }


def _build_spec() -> dict:
    steps = []
    for i, (b64, note) in enumerate((
        (_b64(SOURCE_0), "Scroll through the real public page."),
        ((_b64(SOURCE_1)), "Continue down the page."),
    )):
        steps.append({
            "index": i,
            "timestamp": i * 2000,
            "pageUrl": "https://www.rfc-editor.org/rfc/rfc7231.html",
            "pageTitle": "RFC 7231",
            "interaction": {
                "type": "scroll",
                "target": _scroll_target(i),
                "hotspot": {"xPct": 50, "yPct": 50},
                "value": "down",
            },
            "annotation": note,
            "screenshotBase64": b64,
        })
    return {
        "version": 1,
        "id": "scroll-proof",
        "name": "scroll-proof",
        "goal": "Two real scroll shots, paint-safe export",
        "createdAt": "2026-10-29T00:00:00Z",
        "viewport": dict(VIEWPORT),
        "startUrl": "https://www.rfc-editor.org/rfc/rfc7231.html",
        "aiAnnotations": {"style": "snappy", "summary": "Two scroll steps.",
                          "animationTimeline": []},
        "steps": steps,
    }


def _render(tmp_path: Path, spec: dict) -> Path:
    from capturd.walk.viewer import render_viewer_to_file

    out = tmp_path / "export.html"
    render_viewer_to_file(spec, out)
    assert out.is_file()
    return out


def _probe(viewer: Path, seek_t: float | None = None) -> dict:
    """First plan pass: step windows + the camera geometry (optionally
    evaluated at a specific timeline time)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport=dict(VIEWPORT))
        page.goto(viewer.resolve().as_uri(), wait_until="domcontentloaded")
        page.wait_for_function(
            "() => window.__demoExport && window.__demoViewer && window.__demoViewer.STATE.spec",
            timeout=15000,
        )
        plan = page.evaluate("() => window.__demoExport.prepare()")
        if seek_t is not None:
            page.evaluate("(t) => window.__demoExport.seek(t)", seek_t)
        geo = page.evaluate(
            """() => {
                const cont = document.getElementById('screenshot-container').getBoundingClientRect();
                const m = new DOMMatrix(getComputedStyle(document.getElementById('panzoom-layer')).transform);
                const im = document.getElementById('screenshot');
                const layer = document.getElementById('panzoom-layer');
                return {cont: {x: cont.x, y: cont.y, w: cont.width, h: cont.height},
                        matrix: [m.a, m.b, m.c, m.d, m.e, m.f],
                        natural: [im.naturalWidth, im.naturalHeight],
                        layer: [layer.clientWidth, layer.clientHeight]};
            }"""
        )
        browser.close()
    return {"wins": plan["stepWindows"], "totalMs": plan["totalMs"], **geo}


def _src_to_screen(geo: dict, sx: float, sy: float) -> tuple[float, float]:
    """Map a source-image pixel to page screen coordinates through the
    aspect-fit box + the live camera matrix (kept general so resize /
    full-page captures stay correct)."""
    nat_w, nat_h = geo["natural"]
    lay_w, lay_h = geo["layer"]
    k = min(lay_w / nat_w, lay_h / nat_h)
    ox = (lay_w - nat_w * k) / 2
    oy = (lay_h - nat_h * k) / 2
    a, b, c, d, e, f = geo["matrix"]
    lx, ly = ox + sx * k, oy + sy * k
    return geo["cont"]["x"] + a * lx + c * ly + e, geo["cont"]["y"] + b * lx + d * ly + f


def _sample_points(w: int, h: int, rgb0: bytes, rgb1: bytes, count: int = 5):
    """Source pixels where the two scroll shots differ strongly, spread over
    the frame and away from the cursor/hotspot/caption zones."""
    candidates = []
    for y in range(60, h - 130, 16):
        for x in range(60, w - 60, 16):
            if math.hypot(x - w / 2, y - h / 2) < 210:
                continue  # cursor + hotspot ring live here
            p0, p1 = _px(rgb0, w, x, y), _px(rgb1, w, x, y)
            d = max(abs(a - b) for a, b in zip(p0, p1))
            if d >= 45:
                candidates.append((d, x, y))
    candidates.sort(reverse=True)
    chosen: list[tuple[int, int]] = []
    for _d, x, y in candidates:
        if all(math.hypot(x - cx, y - cy) >= 140 for cx, cy in chosen):
            chosen.append((x, y))
        if len(chosen) >= count:
            break
    assert len(chosen) >= count, "fixtures must contain enough differing regions"
    return chosen


def _downscaled_grid(rgb: bytes, w: int, h: int, gw: int = 32, gh: int = 20):
    grid = []
    for gy in range(gh):
        row = []
        for gx in range(gw):
            x0, x1 = int(gx * w / gw), max(int(gx * w / gw) + 1, int((gx + 1) * w / gw))
            y0, y1 = int(gy * h / gh), max(int(gy * h / gh) + 1, int((gy + 1) * h / gh))
            rs = gs = bs = n = 0
            for y in range(y0, y1, 3):
                for x in range(x0, x1, 3):
                    r, g, b = _px(rgb, w, x, y)
                    rs += r
                    gs += g
                    bs += b
                    n += 1
            row.append((rs / n, gs / n, bs / n))
        grid.append(row)
    return grid


def _grid_diff(g1, g2) -> float:
    total = 0.0
    n = 0
    for r1, r2 in zip(g1, g2):
        for p1, p2 in zip(r1, r2):
            total += sum(abs(a - b) for a, b in zip(p1, p2)) / 3
            n += 1
    return total / n


# ---------------------------------------------------------------------------
# Paint-safe contract — deterministic red at the broken base
# ---------------------------------------------------------------------------


def test_export_seek_is_paint_safe(tmp_path: Path) -> None:
    """seek() must return a promise that resolves only AFTER the images it
    just assigned are decoded and one animation frame has elapsed."""
    from playwright.sync_api import sync_playwright

    spec = _build_spec()
    viewer = _render(tmp_path, spec)
    wins = _probe(viewer)["wins"]
    t_change = wins[1]["tStart"] + 10  # first seek where source index changes

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport=dict(VIEWPORT))
        page.goto(viewer.resolve().as_uri(), wait_until="domcontentloaded")
        page.wait_for_function(
            "() => window.__demoExport && window.__demoViewer && window.__demoViewer.STATE.spec",
            timeout=15000,
        )
        page.evaluate("() => window.__demoExport.prepare()")
        result = page.evaluate(
            """async (t) => {
                let rafRan = false;
                requestAnimationFrame(() => { rafRan = true; });
                const r = window.__demoExport.seek(t);
                const isPromise = !!r && typeof r.then === 'function';
                if (isPromise) await r;
                const sh = document.getElementById('screenshot');
                const pv = document.getElementById('screenshot-prev');
                return {isPromise, rafRanAtResolve: rafRan,
                        shownReady: sh.complete && sh.naturalWidth > 0,
                        prevReady: pv.complete && pv.naturalWidth > 0};
            }""",
            t_change,
        )
        browser.close()

    assert result["isPromise"] is True, (
        "exportSeek must be async — the exporter awaits it before screenshotting"
    )
    assert result["rafRanAtResolve"] is True, (
        "exportSeek must yield at least one requestAnimationFrame after a "
        "source index change before it resolves"
    )
    assert result["shownReady"] is True and result["prevReady"] is True, (
        "live images must be decoded before exportSeek resolves"
    )


# ---------------------------------------------------------------------------
# The visual regression — second scroll frame contains source pixels
# ---------------------------------------------------------------------------


def test_second_scroll_frame_contains_source_pixels(tmp_path: Path) -> None:
    """Drive the REAL export capture path in Chromium and assert the frames
    around the second scroll step carry real source pixels."""
    from capturd.walk.exporter import _capture_frames
    from capturd.walk.recorder import run_async

    spec = _build_spec()
    viewer = _render(tmp_path, spec)
    probe = _probe(viewer)
    wins = probe["wins"]
    w0, h0, rgb0 = _decode_png(SOURCE_0)
    w1, h1, rgb1 = _decode_png(SOURCE_1)
    assert (w0, h0) == (w1, h1) == (VIEWPORT["width"], VIEWPORT["height"])

    frame_ms = 1000.0 / FPS
    # First frame of the second scroll step (its pre-click stage) ...
    i_stage = math.ceil((wins[1]["tStart"] + 10) / frame_ms)
    # ... and a frame after the pre-click → result crossfade has completed.
    fade_start = wins[1].get("fadeStart") or wins[1]["tStart"]
    i_result = math.ceil((fade_start + 260 + 150) / frame_ms)
    assert i_result * frame_ms < probe["totalMs"]

    # Camera geometry evaluated at the result frame time.
    probe = _probe(viewer, seek_t=i_result * frame_ms)

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    n_frames, _dur, _audio = run_async(
        _capture_frames(viewer, dict(VIEWPORT), frames_dir, FPS)
    )
    assert n_frames > i_result

    fw, fh, frame_stage = _decode_png(frames_dir / f"frame_{i_stage:06d}.png")
    fw2, fh2, frame_result = _decode_png(frames_dir / f"frame_{i_result:06d}.png")
    assert (fw, fh) == (fw2, fh2) == (VIEWPORT["width"], VIEWPORT["height"])

    ow, oh, overlay_rgb = _decode_png(OVERLAY_ONLY)
    g_overlay = _downscaled_grid(overlay_rgb, ow, oh)
    g_stage = _downscaled_grid(frame_stage, fw, fh)
    g_result = _downscaled_grid(frame_result, fw2, fh2)

    # 1. The frames are NOT overlay-only captures (the old failure mode).
    assert _grid_diff(g_stage, g_overlay) > 8, (
        "second step's opening frame is overlay-only — source image never painted"
    )
    assert _grid_diff(g_result, g_overlay) > 8, (
        "second step's result frame is overlay-only — source image never painted"
    )

    # 2. The opening frame of the second scroll step carries the PRE-CLICK
    #    stage pixels (source 0) at their expected screen positions.
    pts0 = _sample_points(w0, h0, rgb1, rgb0)
    for sx, sy in pts0:
        x, y = _src_to_screen(probe, sx, sy)
        got = _px(frame_stage, fw, int(round(x)), int(round(y)))
        want = _px(rgb0, w0, sx, sy)
        assert max(abs(a - b) for a, b in zip(got, want)) <= 26, (
            f"stage frame lost source pixels at ({sx},{sy}): got {got} want {want}"
        )

    # 3. The result frame contains the SECOND source frame's pixels —
    #    and those points differ from source 0, so no substitute shot can pass.
    pts1 = _sample_points(w1, h1, rgb0, rgb1)
    for sx, sy in pts1:
        x, y = _src_to_screen(probe, sx, sy)
        gx, gy = int(round(x)), int(round(y))
        got = _px(frame_result, fw2, gx, gy)
        want = _px(rgb1, w1, sx, sy)
        other = _px(rgb0, w0, sx, sy)
        assert max(abs(a - b) for a, b in zip(got, other)) > 26, (
            f"sample ({sx},{sy}) is not discriminative"
        )
        assert max(abs(a - b) for a, b in zip(got, want)) <= 26, (
            f"result frame lost second-source pixels at ({sx},{sy}): "
            f"got {got} want {want} (first source has {other})"
        )


# ---------------------------------------------------------------------------
# Blank-frame guard + missing/failed source rejection
# ---------------------------------------------------------------------------


def _encode_uniform_png(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    """Minimal PNG encoder for a constant-colour frame (test input only)."""
    import struct
    import zlib

    row = b"\x00" + bytes(rgb) * w
    raw = row * h

    def chunk(ctype: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + ctype + data
                + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _mean_colour(path: Path) -> tuple[int, int, int]:
    w, h, rgb = _decode_png(path)
    rs = gs = bs = 0
    n = w * h
    for i in range(0, len(rgb), 3):
        rs += rgb[i]
        gs += rgb[i + 1]
        bs += rgb[i + 2]
    return rs // n, gs // n, bs // n


def _blank_spec(mean_rgb: tuple[int, int, int], n_steps: int = 2) -> dict:
    """Uniform source frames whose export frames are near-uniform.

    The uniform colour is derived from a real captured frame's mean colour;
    hotspot percentages outside the viewport keep the synthetic cursor out
    of frame so nothing non-uniform is composited over the uniform source.
    """
    uniform = base64.b64encode(_encode_uniform_png(1024, 768, mean_rgb)).decode("ascii")
    steps = []
    for i in range(n_steps):
        t = _scroll_target(i)
        t["boundingRect"] = {"x": 0, "y": 0, "width": 1024, "height": 768}
        steps.append({
            "index": i, "timestamp": i * 2000,
            "pageUrl": "https://example.com", "pageTitle": "u",
            "interaction": {
                "type": "scroll", "target": t,
                "hotspot": {"xPct": 250, "yPct": 250},
                "value": "down",
            },
            "screenshotBase64": uniform,
        })
    return _spec_of(steps)


def _spec_of(steps: list[dict]) -> dict:
    return {
        "version": 1, "id": "guard", "name": "guard",
        "goal": "guard", "createdAt": "2026-10-29T00:00:00Z",
        "viewport": dict(VIEWPORT),
        "startUrl": "https://example.com",
        "aiAnnotations": {"style": "snappy", "summary": "", "animationTimeline": []},
        "steps": steps,
    }


def _step_with_source(index: int, b64: str | None, path: str | None = None) -> dict:
    step = _step_with(index)
    step["screenshotBase64"] = b64
    step["screenshotPath"] = path
    return step


def _step_with(index: int) -> dict:
    return {
        "index": index, "timestamp": index * 2000,
        "pageUrl": "https://example.com", "pageTitle": "t",
        "interaction": {
            "type": "click", "target": _scroll_target(index),
            "hotspot": {"xPct": 50, "yPct": 50},
        },
        "annotation": f"Step {index}.",
    }


def test_export_rejects_missing_source_with_named_error(tmp_path: Path) -> None:
    from capturd.walk.exporter import DemoExportError, export_video

    ok = _b64(SOURCE_0)
    spec = _spec_of([
        _step_with_source(0, ok),
        _step_with_source(1, None),          # required source is missing
    ])
    viewer = _render(tmp_path, spec)
    out = tmp_path / "out.mp4"

    with pytest.raises(DemoExportError) as excinfo:
        export_video(spec, viewer, out, fmt="mp4", fps=FPS)

    msg = str(excinfo.value)
    assert "DemoExportMissingSourceError" in msg, msg
    assert "step 1" in msg, f"named error must identify the missing step: {msg}"
    assert not out.exists(), "no successful MP4 when a required source is missing"


def test_export_rejects_failed_source_with_named_error(tmp_path: Path) -> None:
    from capturd.walk.exporter import DemoExportError, export_video

    spec = _spec_of([
        _step_with_source(0, _b64(SOURCE_0)),
        _step_with_source(1, "!!!not-a-png!!!"),  # decodes to nothing
    ])
    viewer = _render(tmp_path, spec)
    out = tmp_path / "out.mp4"

    with pytest.raises(DemoExportError) as excinfo:
        export_video(spec, viewer, out, fmt="mp4", fps=FPS)

    msg = str(excinfo.value)
    assert "DemoExportMissingSourceError" in msg, msg
    assert "step 1" in msg, f"named error must identify the failed step: {msg}"
    assert not out.exists(), "no successful MP4 when a required source fails"


def test_export_rejects_blank_frames_before_ffmpeg(tmp_path: Path) -> None:
    from capturd.walk.exporter import DemoExportError, export_video

    spec = _blank_spec(_mean_colour(SOURCE_0))
    viewer = _render(tmp_path, spec)
    out = tmp_path / "out.mp4"

    with pytest.raises(DemoExportError) as excinfo:
        export_video(spec, viewer, out, fmt="mp4", fps=FPS)

    assert str(excinfo.value) == "blank or near-uniform export frame at index 0", (
        str(excinfo.value)
    )
    assert not out.exists(), "blank frames must never reach a successful MP4"


def test_blank_guard_inspects_pixels_not_files(tmp_path: Path) -> None:
    """The mechanical guard looks at downsampled pixels — a frame merely
    existing is not enough, and real captured content never trips it."""
    from capturd.walk.exporter import _assert_frame_not_blank

    for real in (SOURCE_0, SOURCE_1, OVERLAY_ONLY):
        _assert_frame_not_blank(real.read_bytes(), 0)  # must not raise

    mean = _mean_colour(SOURCE_1)
    blank = _encode_uniform_png(1024, 768, mean)
    with pytest.raises(Exception) as excinfo:
        _assert_frame_not_blank(blank, 3)
    assert "blank or near-uniform export frame at index 3" in str(excinfo.value)
