# visual-regressions fixtures — provenance

Every PNG in this directory is a **real captured frame** (or a byte-for-byte
copy of one). Nothing here is synthesized, mocked or AI-generated.

| file | source | capture |
| --- | --- | --- |
| `scroll-source-0.png` | `evidence/W1-03-r1-scrollable-rfc7231/scroll_shot_0_y0.png` | real Chromium capture of `https://www.rfc-editor.org/rfc/rfc7231.html` at 1024x768, `scrollY=0`, `page.screenshot(full_page=False)`, 2026-10-29 |
| `scroll-source-1.png` | `evidence/W1-03-r1-scrollable-rfc7231/scroll_shot_1_y560.png` | same page/session, second consecutive scroll shot at `scrollY=560` |
| `overlay-only-frame.png` | `evidence/main-public-walk/frame-5.png` | real captured frame from the old rejected export — the viewer chrome/cursor/caption with **no source image painted** (the failure this work fixes) |

sha256:

```
8afca757d6d4166f48df7ee57e07f9501e6b5924bd33cb3c71bea08aa1100e7a  scroll-source-0.png
ce8ee818f7c37b96b983f55cac5afaaedac7d8d8cf70a4731a6784fb530f59c4  scroll-source-1.png
9c1c7b895ebe96a425ea311712713389d843d1e5e3a9f1a8160b264684063a8d  overlay-only-frame.png
```

The scroll target is a genuinely scrollable stable public page (RFC 7231,
frozen document, no JS): `scrollHeight=87601` at the 1024x768 capture
viewport, so the two consecutive scroll shots differ. The old
`example.com` capture (which cannot scroll at the recorded viewport) is
NOT reused here.

Capture tool: Playwright Chromium (headless), repo base
`39eb73c0a4db3347e011ac9046cdd063850fbe68`, venv
`/work/lanes/L8c-capturd/rhobear-capturd/.venv`.

---

## Export harness contract

The export harness in `capturd/walk/templates/viewer.html` exposes
`window.__demoExport.prepare()` and `window.__demoExport.seek(tMs)`:

* Both return Promises. `tMs` is milliseconds on the compiled timeline.
* Callers must await `prepare()` before any `seek()`, and must await each
  `seek()` before reading pixels or screenshotting the page.
* On source-index changes, `seek` awaits live image decoding and one
  animation frame before resolving.
* A synchronous call without awaiting can return before painting — a
  screenshot taken at that point may capture the previous frame or no source
  at all.

JavaScript usage:

```javascript
await window.__demoExport.prepare();
await window.__demoExport.seek(tMs);
```

Python (Playwright) usage:

```python
await page.evaluate("() => window.__demoExport.prepare()")
await page.evaluate("async (t) => { await window.__demoExport.seek(t); }", t_ms)
png = await page.screenshot(type="png")
```

Refusal contracts (recorder and coordinator):

* Wheel no-movement refusal: a WHEEL scroll whose target does not move by at
  least one pixel is refused (`DemoRecorderError`, zero recorded steps) —
  including an outward wheel at the target's own edge. The explicit
  normalized `top`/`bottom` edge commands are the documented exception: at
  their requested edge they remain recorded no-op steps.
* Coordinator missing-source refusal: when a required step screenshot is
  missing or unreadable, mp4/gif export refuses before HTML/output creation
  with `DemoExportMissingSourceError`, which is catchable as BOTH
  `DemoForgeError` and `DemoExportError`. A downstream `DemoExportError`
  raised by the export pipeline itself keeps its exact object, message,
  subtype and cause through `export_demo` (it is not re-typed).

Bounded waits (W1-03-r4) — errors, never deadlocks or guessed readiness:

* Paint wait: on source-index changes the awaited animation frame is bounded
  at **1000 ms**. A stalled frame rejects with a JS
  `DemoExportPaintTimeoutError` (a real `Error` subclass with
  `err.name === 'DemoExportPaintTimeoutError'` and a message naming that type
  and the source index), which the exporter surfaces as `DemoExportError`
  with the JS failure chained. The outstanding frame is cancelled on timeout
  and a late callback cannot turn failure into success; the shown/previous
  source indices are invalidated so a repeat seek must paint again. No
  screenshot or frame file is written after a failed seek.
* Scroll settling (recorder): wheel steps observe the selected element's
  `scrollTop` instead of assuming elapsed time — a **2000 ms** movement
  window polled every **50 ms** retaining the ≥ 1 pixel movement threshold,
  then stable scrollTop (variation < 1 pixel across three 50 ms samples —
  each sample records the scroll range observed over its 50 ms window) under
  a separate **2000 ms** ceiling before the capture is recorded. No
  movement raises the existing refusal above with zero steps; movement that
  never settles raises `DemoRecorderError` naming scroll-settle timeout, also
  with zero steps. Explicit `top`/`bottom` keep instant scrolling and remain
  valid recorded edge no-ops (the documented exception).

The uniform-frame histogram (blank guard) does not detect overlay-only
images; the separate source-pixel regression
(`test_second_scroll_frame_contains_source_pixels`) provides that proof.
