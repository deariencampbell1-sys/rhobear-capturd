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
