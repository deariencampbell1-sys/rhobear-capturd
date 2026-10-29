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
import shutil
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


# ---------------------------------------------------------------------------
# W1-03-r2 — exercise the actual stdlib PNG path, not Pillow's fast path
# ---------------------------------------------------------------------------


def _filtered_png_fixture(color_type: int, filter_type: int) -> tuple[bytes, bytes]:
    """Encode 3x2 known pixels with one explicit PNG filter per row."""
    import struct
    import zlib

    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    rows = [
        bytes((13 + i * 31) % 256 for i in range(3 * channels)),
        bytes((83 + i * 47) % 256 for i in range(3 * channels)),
    ]
    encoded = bytearray()
    prior = bytes(3 * channels)
    for row in rows:
        encoded.append(filter_type)
        for i, sample in enumerate(row):
            left = row[i - channels] if i >= channels else 0
            above = prior[i]
            upper_left = prior[i - channels] if i >= channels else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            else:
                p = left + above - upper_left
                distances = (abs(p - left), abs(p - above), abs(p - upper_left))
                predictor = (left, above, upper_left)[distances.index(min(distances))]
            encoded.append((sample - predictor) & 255)
        prior = row

    def png_chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    image = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 3, 2, 8, color_type, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(bytes(encoded)))
        + png_chunk(b"IEND", b"")
    )
    expected = bytearray()
    for row in rows:
        for i in range(0, len(row), channels):
            if color_type in (0, 4):
                expected.extend([row[i]] * 3)
            else:
                expected.extend(row[i:i + 3])
    return image, bytes(expected)


@pytest.mark.parametrize("color_type", (0, 2, 4, 6))
@pytest.mark.parametrize("filter_type", (0, 1, 2, 3, 4))
def test_png_stdlib_direct_all_filters_and_colour_types(
    color_type: int, filter_type: int
) -> None:
    from capturd.walk.exporter import _decode_png_rgb_stdlib

    encoded, expected = _filtered_png_fixture(color_type, filter_type)
    assert _decode_png_rgb_stdlib(encoded) == (3, 2, expected)


@pytest.mark.parametrize("source", (SOURCE_0, SOURCE_1))
def test_png_stdlib_direct_real_scroll_fixtures(source: Path) -> None:
    from capturd.walk.exporter import _decode_png_rgb_stdlib

    png = source.read_bytes()
    width, height, rgb = _decode_png_rgb_stdlib(png)
    assert (width, height) == (VIEWPORT["width"], VIEWPORT["height"])
    assert len(rgb) == width * height * 3
    assert len({rgb[(y * width + x) * 3:(y * width + x) * 3 + 3]
                for y in range(20, height, 79)
                for x in range(20, width, 83)}) > 2

    try:
        from PIL import Image
    except ImportError:
        pass  # Direct decoder assertions above run without Pillow.
    else:
        import io
        with Image.open(io.BytesIO(png)) as image:
            assert rgb == image.convert("RGB").tobytes()


def test_png_public_decoder_forces_fallback_without_pillow(monkeypatch) -> None:
    import builtins
    from capturd.walk.exporter import _decode_png_rgb, _decode_png_rgb_stdlib

    png, _ = _filtered_png_fixture(6, 4)
    real_import = builtins.__import__

    def without_pillow(name, *args, **kwargs):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("Pillow is deliberately unavailable in this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_pillow)
    assert _decode_png_rgb(png) == _decode_png_rgb_stdlib(png)


# ---------------------------------------------------------------------------
# W1-03-r3 — malformed PNG normalizes to DemoExportError at the decoder
# boundary, and the coordinator's missing-source refusal contract
# ---------------------------------------------------------------------------


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    """The module's existing length/CRC chunk construction, reused."""
    import struct
    import zlib

    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def _split_png(png: bytes) -> list[tuple[bytes, bytes]]:
    """Split a tiny PNG into (chunk type, payload) pairs for surgery."""
    import struct

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    parts = []
    pos = 8
    while pos + 8 <= len(png):
        (length,) = struct.unpack(">I", png[pos:pos + 4])
        parts.append((png[pos + 4:pos + 8], png[pos + 8:pos + 8 + length]))
        pos += 12 + length
    return parts


def _join_png(parts: list[tuple[bytes, bytes]]) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"".join(_png_chunk(k, d) for k, d in parts)


@pytest.mark.parametrize("entry", ("stdlib", "public_no_pillow"))
@pytest.mark.parametrize("corrupt", ("short_ihdr", "bad_idat", "short_rows"))
def test_malformed_png_raises_export_error(corrupt, entry, monkeypatch) -> None:
    """Truncated IHDR, invalid IDAT and too-short decompressed rows all
    become DemoExportError('invalid PNG: …') with a chained cause — through
    the direct stdlib entry point and the public decoder with Pillow forced
    unavailable. These are explicit malformed-format inputs, not fabricated
    product responses."""
    import zlib

    from capturd.walk.exporter import (
        DemoExportError,
        _decode_png_rgb,
        _decode_png_rgb_stdlib,
    )

    png, _expected = _filtered_png_fixture(2, 0)  # 3x2 RGB: 20 scanline bytes
    parts = _split_png(png)
    if corrupt == "short_ihdr":
        # 12-byte IHDR payload where 13 bytes are required.
        bad = _join_png([(k, d[:12] if k == b"IHDR" else d) for k, d in parts])
    elif corrupt == "bad_idat":
        bad = _join_png([(k, b"bad" if k == b"IDAT" else d) for k, d in parts])
    else:  # short_rows: 9 valid decompressed bytes where 20 are required
        bad = _join_png([
            (k, zlib.compress(b"\x00" * 9) if k == b"IDAT" else d)
            for k, d in parts
        ])

    decode = _decode_png_rgb_stdlib
    if entry == "public_no_pillow":
        import builtins

        real_import = builtins.__import__

        def without_pillow(name, *args, **kwargs):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError("Pillow is deliberately unavailable in this test")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", without_pillow)
        decode = _decode_png_rgb

    with pytest.raises(DemoExportError, match="invalid PNG") as excinfo:
        decode(bad)
    assert excinfo.value.__cause__ is not None, (
        "normalized malformed-PNG errors must chain their cause"
    )


@pytest.mark.parametrize("fmt", ("mp4", "gif"))
@pytest.mark.parametrize("missing_kind", ("absent", "dangling"))
def test_coordinator_refuses_missing_export_sources(
    tmp_path: Path, monkeypatch, fmt, missing_kind
) -> None:
    """mp4/gif export refuses a missing step source BEFORE HTML/output
    creation or any export_video call — and the refusal is catchable as
    DemoForgeError AND DemoExportError. The missing step carries index 7.
    A truthy but unreadable screenshotPath is not proof of a source."""
    from capturd.walk.coordinator import DemoForge, DemoForgeError
    from capturd.walk.exporter import DemoExportError

    def fail_if_called(*args, **kwargs):
        raise AssertionError("export_video must not be called for a missing source")

    monkeypatch.setattr("capturd.walk.exporter.export_video", fail_if_called)

    demo_id = "missing-src"
    forge = DemoForge(demos_dir=tmp_path / "demos")
    if missing_kind == "absent":
        missing_step = _step_with_source(7, None)
    else:
        missing_step = _step_with_source(7, None, "missing.png")
    spec = _spec_of([_step_with_source(0, _b64(SOURCE_0)), missing_step])
    forge.save_spec(demo_id, spec)

    with pytest.raises(DemoForgeError) as excinfo:
        forge.export_demo(demo_id, fmt=fmt)

    err = excinfo.value
    assert isinstance(err, DemoExportError), (
        "missing-source refusal must also be catchable as DemoExportError"
    )
    msg = str(err)
    assert "DemoExportMissingSourceError" in msg, msg
    assert "step 7" in msg, msg
    d = forge.demo_dir(demo_id)
    assert not (d / "export.html").exists(), "refusal must precede HTML creation"
    assert not (d / f"walkthrough.{fmt}").exists(), "no output for a refused export"


@pytest.mark.parametrize("fmt", ("mp4", "gif"))
def test_coordinator_inlines_real_source_before_export(
    tmp_path: Path, monkeypatch, fmt
) -> None:
    """Real sources still pass the precheck: both fixtures are inlined to
    their exact bytes before the encoder boundary is reached. The encoder is
    instrumented to inspect its arguments — this does not claim a stub
    produced an accepted video."""
    from capturd.walk.coordinator import DemoForge

    calls: list[dict] = []

    def fake_export_video(spec, viewer_html, out_path, *, fmt="mp4", fps=30):
        calls.append({"spec": spec, "viewer_html": viewer_html,
                      "out_path": out_path, "fmt": fmt})
        return out_path

    monkeypatch.setattr("capturd.walk.exporter.export_video", fake_export_video)

    demo_id = "inline-src"
    forge = DemoForge(demos_dir=tmp_path / "demos")
    spec = _spec_of([
        _step_with_source(0, _b64(SOURCE_0)),
        _step_with_source(1, None, "scroll-source-1.png"),
    ])
    forge.save_spec(demo_id, spec)
    d = forge.demo_dir(demo_id)
    shutil.copy(SOURCE_1, d / "scroll-source-1.png")

    out = forge.export_demo(demo_id, fmt=fmt)

    assert len(calls) == 1, "the encoder boundary must be reached exactly once"
    call = calls[0]
    assert call["fmt"] == fmt
    expected_out = d / f"walkthrough.{fmt}"
    assert call["out_path"] == expected_out
    assert out == expected_out
    steps = call["spec"]["steps"]
    assert base64.b64decode(steps[0]["screenshotBase64"]) == SOURCE_0.read_bytes()
    assert base64.b64decode(steps[1]["screenshotBase64"]) == SOURCE_1.read_bytes()
    for s in steps:
        assert "screenshotError" not in s, s


# ---------------------------------------------------------------------------
# W1-03-r4 — export errors survive export_demo; the rAF paint wait is typed
# and bounded (1000 ms) so a stalled frame fails instead of deadlocking
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ("mp4", "gif"))
@pytest.mark.parametrize("kind", ("malformed_png", "blank_frame"))
def test_coordinator_preserves_export_error(tmp_path: Path, monkeypatch, fmt, kind) -> None:
    """A downstream DemoExportError keeps its exact object, message, subtype
    and cause through ``export_demo`` — the coordinator boundary must not
    re-type it as DemoForgeError. The error is CONSTRUCTED by really running
    the existing decoder on the short-IHDR fixture (or the real blank guard
    on a uniform PNG); only exporter.export_video is instrumented to raise
    that exact object once. This proves the coordinator boundary — it does
    not claim the instrumented encoder produced or inspected a video."""
    from capturd.walk.coordinator import DemoForge
    from capturd.walk.exporter import (
        DemoExportError,
        _assert_frame_not_blank,
        _decode_png_rgb_stdlib,
    )

    if kind == "malformed_png":
        png, _expected = _filtered_png_fixture(2, 0)  # 3x2 RGB
        parts = _split_png(png)
        bad = _join_png([(k, d[:12] if k == b"IHDR" else d) for k, d in parts])
        with pytest.raises(DemoExportError, match="invalid PNG") as built:
            _decode_png_rgb_stdlib(bad)
    else:
        with pytest.raises(DemoExportError, match="blank or near-uniform") as built:
            _assert_frame_not_blank(_encode_uniform_png(1024, 768, (12, 12, 12)), 0)
    err = built.value
    message = str(err)
    cause = err.__cause__

    calls: list[dict] = []

    def raising_export_video(spec, viewer_html, out_path, *, fmt="mp4", fps=30):
        calls.append({"out_path": out_path})
        raise err

    monkeypatch.setattr("capturd.walk.exporter.export_video", raising_export_video)

    demo_id = "preserve-err"
    forge = DemoForge(demos_dir=tmp_path / "demos")
    spec = _spec_of([
        _step_with_source(0, _b64(SOURCE_0)),
        _step_with_source(1, _b64(SOURCE_1)),
    ])
    forge.save_spec(demo_id, spec)

    with pytest.raises(DemoExportError) as excinfo:
        forge.export_demo(demo_id, fmt=fmt)

    got = excinfo.value
    assert got is err, "the original DemoExportError object must pass through"
    assert str(got) == message, "the original message must pass through"
    assert type(got) is type(err), "the original subtype must pass through"
    assert got.__cause__ is cause, "the original cause must pass through"
    if kind == "malformed_png":
        assert cause is not None, "the malformed decoder error keeps its cause"
    assert len(calls) == 1, "the encoder boundary must be reached exactly once"
    d = forge.demo_dir(demo_id)
    assert not (d / f"walkthrough.{fmt}").exists(), (
        "no video/GIF output follows a failed export"
    )


def test_export_seek_stalled_raf_is_typed_and_bounded(tmp_path: Path) -> None:
    """A stalled requestAnimationFrame must REJECT the seek with a typed
    ``DemoExportPaintTimeoutError`` within the 1000 ms bound (observed under
    an independent 3000 ms watchdog) — never deadlock — cancelling the
    outstanding frame; and after restoring real rAF the SAME seek must paint
    a real, decoded source before resolving."""
    import time

    from playwright.sync_api import sync_playwright

    spec = _build_spec()
    viewer = _render(tmp_path, spec)

    install_js = """() => {
        window.__rafTest = {handles: [], cancels: [], next: 990000};
        window.__rafTest.realRaf = window.requestAnimationFrame;
        window.__rafTest.realCaf = window.cancelAnimationFrame;
        window.requestAnimationFrame = function () {
            const handle = ++window.__rafTest.next;
            window.__rafTest.handles.push(handle);
            return handle;  // never invoke the callback
        };
        window.cancelAnimationFrame = function (h) {
            window.__rafTest.cancels.push(h);
        };
    }"""
    restore_js = """() => {
        if (window.__rafTest) {
            window.requestAnimationFrame = window.__rafTest.realRaf;
            window.cancelAnimationFrame = window.__rafTest.realCaf;
        }
    }"""
    stalled_seek_js = """async (t) => {
        const outcome = await Promise.race([
            window.__demoExport.seek(t).then(
                () => ({kind: 'resolved'}),
                (err) => ({kind: 'rejected',
                           name: err && err.name,
                           message: String(err && err.message || err)})),
            new Promise((resolve) => setTimeout(
                () => resolve({kind: 'watchdog'}), 3000)),
        ]);
        const wins = window.__demoExport.plan().wins;
        let win = wins[0];
        for (const w of wins) { if (t >= w.tStart) win = w; else break; }
        return Object.assign({}, outcome, {
            index: win.index,
            handles: window.__rafTest.handles.slice(),
            cancels: window.__rafTest.cancels.slice(),
        });
    }"""
    retry_seek_js = """async (t) => {
        window.requestAnimationFrame = window.__rafTest.realRaf;
        window.cancelAnimationFrame = window.__rafTest.realCaf;
        let rafRan = false;
        requestAnimationFrame(() => { rafRan = true; });
        const outcome = await Promise.race([
            window.__demoExport.seek(t).then(
                () => ({kind: 'resolved'}),
                (err) => ({kind: 'rejected',
                           name: err && err.name,
                           message: String(err && err.message || err)})),
            new Promise((resolve) => setTimeout(
                () => resolve({kind: 'watchdog'}), 3000)),
        ]);
        const sh = document.getElementById('screenshot');
        const pv = document.getElementById('screenshot-prev');
        return Object.assign({}, outcome, {
            rafRanAtResolve: rafRan,
            shownReady: sh.complete && sh.naturalWidth > 0,
            prevReady: pv.complete && pv.naturalWidth > 0,
        });
    }"""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport=dict(VIEWPORT))
            page.goto(viewer.resolve().as_uri(), wait_until="domcontentloaded")
            page.wait_for_function(
                "() => window.__demoExport && window.__demoViewer && window.__demoViewer.STATE.spec",
                timeout=15000,
            )
            plan = page.evaluate("() => window.__demoExport.prepare()")
            wins = plan["stepWindows"]
            t_change = wins[1]["tStart"] + 10  # a real source-index change

            page.evaluate(install_js)
            try:
                started = time.monotonic()
                result = page.evaluate(stalled_seek_js, t_change)
                elapsed = time.monotonic() - started
                assert result["kind"] == "rejected", (
                    f"a stalled rAF must reject, not resolve or deadlock: {result}"
                )
                assert result["name"] == "DemoExportPaintTimeoutError", result
                assert "DemoExportPaintTimeoutError" in result["message"], result
                assert f"source index {result['index']}" in result["message"], result
                assert elapsed <= 3.0, f"paint timeout must be bounded: {elapsed:.2f}s"
                assert result["handles"], "the stalled rAF handle must be recorded"
                stalled_handle = result["handles"][0]
                assert stalled_handle in result["cancels"], (
                    "the outstanding rAF must be cancelled on timeout: "
                    f"{result['cancels']} vs {stalled_handle}"
                )

                # Restoring the real rAF lets the SAME seek paint for real.
                retry = page.evaluate(retry_seek_js, t_change)
                assert retry["kind"] == "resolved", retry
                assert retry["rafRanAtResolve"] is True, retry
                assert retry["shownReady"] is True and retry["prevReady"] is True, retry
            finally:
                page.evaluate(restore_js)
        finally:
            browser.close()


@pytest.mark.asyncio
async def test_capture_frames_stalled_raf_raises_export_error(tmp_path: Path) -> None:
    """The real _capture_frames path turns a stalled paint into a named
    DemoExportError (chained cause) and writes NO frame file and no output.
    In the TEMPORARY rendered HTML only, prepare() is wrapped so the real
    plan is produced first and only then requestAnimationFrame is replaced
    with a never-calling stub. seek, the compiled plan, the screenshot path,
    the decoder and _capture_frames are untouched."""
    import asyncio as _asyncio

    from capturd.walk.exporter import DemoExportError, _capture_frames

    spec = _build_spec()
    viewer = _render(tmp_path, spec)
    html = viewer.read_text(encoding="utf-8")
    assert html.count("</body>") == 1, "expected one closing body tag"
    viewer.write_text(
        html.replace(
            "</body>",
            "<script>\n"
            "(function () {\n"
            "  const realPrepare = window.__demoExport.prepare;\n"
            "  window.__demoExport.prepare = async function () {\n"
            "    const plan = await realPrepare();\n"
            "    window.requestAnimationFrame = function () {\n"
            "      return 424242;  // never invoke the callback\n"
            "    };\n"
            "    return plan;\n"
            "  };\n"
            "})();\n"
            "</script>\n</body>",
        ),
        encoding="utf-8",
    )

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    with pytest.raises(DemoExportError) as excinfo:
        try:
            await _asyncio.wait_for(
                _capture_frames(viewer, dict(VIEWPORT), frames_dir, FPS),
                timeout=5,
            )
        except _asyncio.TimeoutError:
            pytest.fail("a stalled rAF must raise a bounded error, not deadlock")
    msg = str(excinfo.value)
    assert "DemoExportPaintTimeoutError" in msg, msg
    assert excinfo.value.__cause__ is not None, (
        "the DemoExportError must chain the JS paint failure"
    )
    assert not list(frames_dir.glob("frame_*.png")), (
        "no frame file may follow a failed seek"
    )
    assert not list(frames_dir.iterdir()), "no output may follow a failed seek"
