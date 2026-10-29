"""Video export — deterministic frame rendering over the viewer's ``__demoExport``.

The viewer template compiles the whole demo (camera, cursor, crossfades,
spotlight, captions) into pure functions of time. This module drives that
renderer frame by frame in headless Chromium and encodes the result with
ffmpeg — every frame lands exactly where the timeline says, no wall-clock
jitter, any fps.

Voiceover audio (per-step Edge-TTS MP3s embedded in the spec) is muxed onto
the video at the offsets the compiled plan reports (``audioPlan``).

ffmpeg discovery: PATH first, then the ``imageio-ffmpeg`` wheel if installed.
No RHOBEAR credentials or founder identity anywhere — sellable by default.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_FPS = 30
GIF_FPS = 12
GIF_WIDTH = 960


class DemoForgeError(Exception):
    """Base error for DemoForge operations.

    Defined here (not in coordinator) so DemoExportError can subclass it;
    ``capturd.walk.coordinator`` re-exports this same class.
    """


class DemoExportError(DemoForgeError, RuntimeError):
    """Raised when the video export cannot complete.

    A DemoForgeError, so every caller that catches DemoForgeError (CLI,
    MCP server) also catches export failures; still a RuntimeError for
    callers that relied on that base.
    """


# ---------------------------------------------------------------------------
# Mechanical frame inspection — a blank export must never become a video
# ---------------------------------------------------------------------------


def _decode_png_rgb(png: bytes) -> tuple[int, int, bytes]:
    """Decode an 8-bit PNG into packed RGB bytes.

    Uses Pillow when it is installed (C-fast) and otherwise a self-contained
    stdlib decoder, so the blank-frame guard never depends on an optional
    imaging stack.
    """
    try:
        import io

        from PIL import Image  # type: ignore

        with Image.open(io.BytesIO(png)) as im:
            width, height = im.width, im.height
            rgb = im.convert("RGB").tobytes()
        return width, height, rgb
    except Exception:  # noqa: BLE001 - any failure falls back to stdlib
        pass
    return _decode_png_rgb_stdlib(png)


def _decode_png_rgb_stdlib(png: bytes) -> tuple[int, int, bytes]:
    """Minimal PNG reader: 8-bit grey/RGB/grey-alpha/RGBA, non-interlaced.

    Malformed input is normalized at this boundary: structural corruption
    (truncated chunks, bad IHDR size, invalid compressed data, short
    scanlines) raises ``DemoExportError("invalid PNG: ...")`` with the
    underlying cause chained. The documented format refusals (bad signature,
    missing IHDR, unsupported format, unknown filter) propagate unchanged.
    """
    import struct
    import zlib

    try:
        return _decode_png_rgb_stdlib_impl(png)
    except (struct.error, zlib.error, IndexError, ValueError) as exc:
        raise DemoExportError("invalid PNG: " + str(exc)) from exc


def _decode_png_rgb_stdlib_impl(png: bytes) -> tuple[int, int, bytes]:
    """PNG chunk parsing + unfilter math (implementation helper).

    New validation failures raise ``ValueError`` for
    ``_decode_png_rgb_stdlib`` to normalize with a cause; the unfilter math
    below is kept byte-identical to the original decoder.
    """
    import struct
    import zlib

    if not png or png[:8] != b"\x89PNG\r\n\x1a\n":
        raise DemoExportError("not a PNG")
    pos = 8
    width = height = 0
    bit_depth = color_type = interlace = -1
    idat = bytearray()
    while pos + 8 <= len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        ctype = png[pos + 4 : pos + 8]
        # Validate the chunk's complete payload + CRC extent before slicing.
        if pos + 12 + length > len(png):
            raise ValueError(f"truncated PNG chunk {ctype!r}")
        data = png[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            if length != 13:
                raise ValueError(f"IHDR payload must be 13 bytes, got {length}")
            width, height, bit_depth, color_type, _c, _f, interlace = struct.unpack(
                ">IIBBBBB", data
            )
        elif ctype == b"IDAT":
            idat += data
        elif ctype == b"IEND":
            break
    if not width or not height:
        raise DemoExportError("PNG missing IHDR")
    if bit_depth != 8 or interlace != 0 or color_type not in (0, 2, 4, 6):
        raise DemoExportError(
            f"unsupported PNG (bit depth {bit_depth}, color type {color_type})"
        )
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    # Short data must never return a silently shortened RGB buffer: the
    # decompressed payload has to hold exactly one filter byte per row plus
    # every scanline byte.
    expected = height * (1 + stride)
    if len(raw) != expected:
        raise ValueError(
            f"PNG pixel data must be {expected} bytes "
            f"({height} rows x {1 + stride}), got {len(raw)}"
        )
    out = bytearray(height * stride)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos : pos + stride])
        pos += stride
        if ftype == 1:  # Sub
            for x in range(channels, stride):
                line[x] = (line[x] + line[x - channels]) & 0xFF
        elif ftype == 2:  # Up
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif ftype == 3:  # Average
            for x in range(stride):
                a = line[x - channels] if x >= channels else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for x in range(stride):
                a = line[x - channels] if x >= channels else 0
                b = prev[x]
                cc = prev[x - channels] if x >= channels else 0
                p = a + b - cc
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - cc)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else cc)
                line[x] = (line[x] + pred) & 0xFF
        elif ftype != 0:
            raise DemoExportError(f"unknown PNG filter {ftype}")
        out[y * stride : (y + 1) * stride] = line
        prev = line

    rgb = bytearray(width * height * 3)
    if color_type == 2:
        rgb[:] = out
    else:
        i = j = 0
        for _ in range(width * height):
            if color_type == 0:  # grey
                g = out[i]
                rgb[j] = rgb[j + 1] = rgb[j + 2] = g
                i += 1
            elif color_type == 4:  # grey + alpha
                g = out[i]
                rgb[j] = rgb[j + 1] = rgb[j + 2] = g
                i += 2
            else:  # RGBA
                rgb[j] = out[i]
                rgb[j + 1] = out[i + 1]
                rgb[j + 2] = out[i + 2]
                i += 4
            j += 3
    return width, height, bytes(rgb)


def _assert_frame_not_blank(png: bytes, index: int) -> None:
    """Mechanical blank-frame guard — inspect pixels, not file existence.

    Samples a ≤ 64x64 grid of the captured PNG; when it holds ≤ 2 distinct
    RGB values AND a luma range of ≤ 2 the frame is blank or near-uniform
    and is refused with a named error instead of being handed to ffmpeg.
    """
    width, height, rgb = _decode_png_rgb(png)
    cols = min(64, max(1, width))
    rows = min(64, max(1, height))
    seen: set[tuple[int, int, int]] = set()
    luma_lo = luma_hi = 0
    for gy in range(rows):
        y = min(height - 1, int((gy + 0.5) * height / rows))
        row = y * width * 3
        for gx in range(cols):
            x = min(width - 1, int((gx + 0.5) * width / cols))
            i = row + x * 3
            px = (rgb[i], rgb[i + 1], rgb[i + 2])
            seen.add(px)
            luma = (77 * px[0] + 150 * px[1] + 29 * px[2]) >> 8
            if len(seen) == 1 and not (luma_lo or luma_hi):
                luma_lo = luma_hi = luma
            else:
                luma_lo = min(luma_lo, luma)
                luma_hi = max(luma_hi, luma)
            if len(seen) > 2 and (luma_hi - luma_lo) > 2:
                return  # visibly non-uniform content — good frame
    if len(seen) <= 2 and (luma_hi - luma_lo) <= 2:
        raise DemoExportError(f"blank or near-uniform export frame at index {index}")


def find_ffmpeg() -> str:
    """Locate an ffmpeg executable. PATH first, then the imageio-ffmpeg wheel."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - any failure means "not available"
        pass
    raise DemoExportError(
        "ffmpeg not found. Install it (winget install Gyan.FFmpeg / "
        "apt install ffmpeg) or `pip install imageio-ffmpeg`."
    )


# ---------------------------------------------------------------------------
# Frame capture
# ---------------------------------------------------------------------------


async def _capture_frames(
    viewer_html: Path,
    viewport: dict[str, int],
    frames_dir: Path,
    fps: int,
) -> tuple[int, float, list[dict[str, Any]]]:
    """Drive ``__demoExport`` seek-by-seek and screenshot every frame.

    Returns ``(frame_count, duration_s, audio_plan)``.
    """
    from playwright.async_api import async_playwright

    vw = int(viewport.get("width", 1440))
    vh = int(viewport.get("height", 900))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(
            viewport={"width": vw, "height": vh},
            device_scale_factor=1,
        )
        await page.goto(viewer_html.resolve().as_uri(), wait_until="domcontentloaded")
        await page.wait_for_function(
            "() => window.__demoExport && window.__demoViewer && window.__demoViewer.STATE.spec",
            timeout=15000,
        )
        try:
            plan = await page.evaluate("() => window.__demoExport.prepare()")
        except Exception as exc:  # noqa: BLE001 - named source errors surface verbatim
            raise DemoExportError(str(exc)) from exc
        total_ms = float(plan["totalMs"])
        audio_plan = list(plan.get("audioPlan") or [])
        n_frames = max(1, int(total_ms / 1000.0 * fps))
        logger.info(
            "export: %d frames @ %dfps (%.1fs of video, %d voiceover clips)",
            n_frames, fps, total_ms / 1000.0, len(audio_plan),
        )

        for i in range(n_frames):
            t = i * 1000.0 / fps
            try:
                # exportSeek is async: it awaits decode() + one animation
                # frame after every source index change, so this immediate
                # screenshot can only capture painted pixels.
                await page.evaluate(
                    "async (t) => { await window.__demoExport.seek(t); }", t
                )
            except Exception as exc:  # noqa: BLE001 - named source errors surface
                raise DemoExportError(str(exc)) from exc
            png = await page.screenshot(type="png")
            _assert_frame_not_blank(png, i)
            (frames_dir / f"frame_{i:06d}.png").write_bytes(png)
            if i and i % (fps * 5) == 0:
                logger.info("export: %d/%d frames", i, n_frames)

        await browser.close()
    return n_frames, total_ms / 1000.0, audio_plan


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


_FFMPEG_TIMEOUT = 600  # max seconds for any single ffmpeg call


def _run_ffmpeg(args: list[str]) -> None:
    # -nostdin + DEVNULL: ffmpeg must never touch our stdin — when this runs
    # inside the MCP server, stdin is the JSON-RPC stdio channel and an
    # inherited handle makes ffmpeg block forever waiting for console input.
    try:
        result = subprocess.run(
            [args[0], "-nostdin", *args[1:]],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_FFMPEG_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        # A hung ffmpeg hitting the timeout raises TimeoutExpired, which none of
        # the callers (export_video → coordinator.export_demo) catch — it would
        # crash the export instead of surfacing a graceful error. Convert it.
        raise DemoExportError(
            f"ffmpeg timed out after {_FFMPEG_TIMEOUT}s"
        ) from exc
    if result.returncode != 0:
        raise DemoExportError(f"ffmpeg failed:\n{result.stderr[-3000:]}")


def _encode_mp4(
    ffmpeg: str,
    frames_dir: Path,
    fps: int,
    duration_s: float,
    out_path: Path,
    audio_clips: list[tuple[Path, int]],
) -> None:
    """Encode PNG frames to H.264, muxing voiceover clips at their offsets."""
    video_args = [
        ffmpeg, "-y",
        "-framerate", str(fps),
        "-i", str(frames_dir / "frame_%06d.png"),
    ]
    if audio_clips:
        filters = []
        mix_inputs = []
        for k, (_path, at_ms) in enumerate(audio_clips):
            video_args += ["-i", str(_path)]
            delay = max(0, int(at_ms))
            filters.append(f"[{k + 1}:a]adelay={delay}|{delay}[a{k}]")
            mix_inputs.append(f"[a{k}]")
        filters.append(
            "".join(mix_inputs)
            + f"amix=inputs={len(audio_clips)}:normalize=0:duration=longest[aout]"
        )
        video_args += [
            "-filter_complex", ";".join(filters),
            "-map", "0:v", "-map", "[aout]",
            "-c:a", "aac", "-b:a", "160k",
        ]
    video_args += [
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-t", f"{duration_s:.3f}",
        str(out_path),
    ]
    _run_ffmpeg(video_args)


def _encode_gif(ffmpeg: str, frames_dir: Path, fps: int, out_path: Path) -> None:
    """Two-pass palette GIF from the captured frames."""
    with tempfile.TemporaryDirectory(prefix="capturd-gif-") as td:
        palette = Path(td) / "palette.png"
        _run_ffmpeg([
            ffmpeg, "-y",
            "-framerate", str(fps),
            "-i", str(frames_dir / "frame_%06d.png"),
            "-vf", f"fps={GIF_FPS},scale={GIF_WIDTH}:-1:flags=lanczos,palettegen",
            str(palette),
        ])
        _run_ffmpeg([
            ffmpeg, "-y",
            "-framerate", str(fps),
            "-i", str(frames_dir / "frame_%06d.png"),
            "-i", str(palette),
            "-lavfi", f"fps={GIF_FPS},scale={GIF_WIDTH}:-1:flags=lanczos[x];[x][1:v]paletteuse",
            str(out_path),
        ])


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def export_video(
    spec: dict[str, Any],
    viewer_html: Path,
    out_path: Path,
    *,
    fmt: str = "mp4",
    fps: int = DEFAULT_FPS,
) -> Path:
    """Render ``viewer_html`` (a fully inlined export) to MP4 or GIF.

    ``spec`` must be the same enriched DemoSpec the viewer HTML embeds — the
    per-step ``voiceoverBase64`` clips are pulled from it for the audio track.
    """
    fmt = (fmt or "mp4").lower()
    if fmt not in {"mp4", "gif"}:
        raise DemoExportError(f"unsupported video format: {fmt!r}")
    if not viewer_html.is_file():
        raise DemoExportError(f"viewer html not found: {viewer_html}")
    if not (spec.get("steps") or []):
        raise DemoExportError("demo has no steps — record a flow before exporting video")

    ffmpeg = find_ffmpeg()
    viewport = spec.get("viewport") or {"width": 1440, "height": 900}

    with tempfile.TemporaryDirectory(prefix="capturd-frames-") as td:
        frames_dir = Path(td)
        from capturd.walk.recorder import run_async

        n_frames, duration_s, audio_plan = run_async(
            _capture_frames(viewer_html, viewport, frames_dir, fps)
        )
        if n_frames == 0:
            raise DemoExportError("no frames captured")

        if fmt == "gif":
            _encode_gif(ffmpeg, frames_dir, fps, out_path)
        else:
            audio_clips: list[tuple[Path, int]] = []
            steps = spec.get("steps") or []
            for entry in audio_plan:
                idx = entry.get("stepIndex")
                at_ms = entry.get("atMs", 0)
                if not isinstance(idx, int) or idx < 0 or idx >= len(steps):
                    continue
                b64 = steps[idx].get("voiceoverBase64")
                if not b64:
                    continue
                clip = frames_dir / f"voice_{idx:03d}.mp3"
                try:
                    clip.write_bytes(base64.b64decode(b64))
                except (ValueError, OSError) as exc:
                    logger.warning("skipping voiceover for step %d: %s", idx, exc)
                    continue
                audio_clips.append((clip, at_ms))
            _encode_mp4(ffmpeg, frames_dir, fps, duration_s, out_path, audio_clips)

    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise DemoExportError(f"export produced no output at {out_path}")
    logger.info("export complete: %s (%.1f KB)", out_path, out_path.stat().st_size / 1024)
    return out_path


__all__ = [
    "DEFAULT_FPS",
    "DemoExportError",
    "DemoForgeError",
    "export_video",
    "find_ffmpeg",
]
