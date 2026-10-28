<p align="center">
  <img src="assets/capturd-logo-wide.png" alt="RHOBEAR Captur'd" width="420">
</p>

<h1 align="center">RHOBEAR Captur'd</h1>

<p align="center">
  <b>Post-work product capture. Free screenshots of every page state — plus, on Pro,
  agent-made AI walkthrough videos.</b><br>
  You built the thing. Now show it. Point Captur'd at a URL and get every rested-state
  screenshot of it — <b>free</b>, local, offline. Upgrade to <b>Pro</b> for a full
  agent-narrated walkthrough video, powered by our own AI. No clicking through, no manual editing.
</p>

<p align="center">
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-7dffd5"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-f5b942">
  <img alt="Playwright" src="https://img.shields.io/badge/engine-Playwright-b794f4">
  <img alt="MCP" src="https://img.shields.io/badge/agent--surface-MCP-6ea8fe">
</p>

---

## Two modes, one lane

### `capturd shots` — rested-state screenshots, in bulk &nbsp;·&nbsp; **free**
The free core, unchanged. Most screenshot tools fire too early — mid-animation,
before fonts load, while a carousel is sliding — and you get junk. Captur'd takes **rested** shots:
animations disabled, fonts awaited, motion stopped, scroll reset, then every page × every viewport ×
every color scheme, in one run.

- 🕸️ **Four ways in** — crawl a **site**, expand a **sitemap.xml**, paste **URLs**, point at **local HTML**.
- 📱 **Responsive matrix** — desktop (1440×1000), tablet (834×1112), mobile (390×844), in **light and dark**.
- 🧘 **Deterministic** — same input, same pixels. Diff-friendly.
- 📦 **Clean output** — `NNN-site-viewport-scheme.png` + `manifest.json`, ZIP or folder.

### `capturd walk` — agent-made AI walkthroughs &nbsp;·&nbsp; **Pro**
The paid mode, powered by our own AI (piped through the RHOBEAR gateway — **no bring-your-own-key**).
Unlock with `capturd activate <CODE>`. Prompt in, walkthrough out. Your agent (Plans, social manager, support, pi) says
"make me a UX-flow video of this site" and Captur'd walks the site itself and produces:

- **Interactive HTML viewer** (self-contained, zero deps, embeddable anywhere), plus
- **MP4 / GIF export** for social & sales,
- with **semantic zoom** (camera anchors to DOM elements, not pixels — survives your site's font/layout changes),
- **voice-synced camera** (TTS word timestamps align keyframes — camera lands on the noun),
- **spotlight mode** (rest of the page dims and blurs; attention is forced),
- **content-mode auto-detection** — canvas / WebGL / Three.js / HTML5 games fall back to video-mode automatically instead of silently breaking like every SaaS demo tool does.

Nothing else in the space is agent-made. Supademo / Arcade / Storylane all assume a human clicks
through. Screen Studio has the cinematography but no interactivity and no agent surface. Captur'd
does both. The walkthroughs run on **our own AI** and are the **Pro** tier — in the desktop app
and the hosted web service alike. **Screenshots are always free.**

### `capturd serve` — MCP surface for both modes
One MCP server exposes both modes to any RHOBEAR agent:

```
capture.crawl   capture.rested                            # stills
demo.record     demo.stop        demo.export              # walk basics
demo.zoom       demo.pan         demo.hold                # camera
demo.spotlight  demo.overlay                              # composition
demo.reorder    demo.trim        demo.branch              # structure
demo.stylize    demo.regenerate                           # restyle
```

Editing is the API. The agent IS the editor. No GUI editor as v1.

## Install

```bash
git clone https://github.com/rhobear-ai/rhobear-capturd
cd sunsponge-capture
pip install -e .
python -m playwright install chromium
```

## Usage

```bash
# Stills — crawl a whole site, every viewport + scheme, into a folder
capturd shots --site https://example.com --out ./shots

# Stills — a few URLs, mobile dark only, as JPEG
capturd shots --urls "https://example.com,https://example.com/pricing" \
  --viewports mobile --schemes dark --format jpeg

# Walkthrough — agent walks the site itself (headful Playwright + LLM step-picker)
capturd walk record --url https://example.com --goal "sign up for a free account" --name "onboarding"
capturd walk export --demo-id <id> --format mp4

# MCP server — expose everything to your RHOBEAR agents
capturd serve
```

Legacy CLI (`sunsponge-capture ...`) still works as an alias for `capturd shots ...` so existing
scripts don't break.

Map-driven stills accept `--pathway-map JSON` (verifier JSON), `--pathway-manifest MD`
(markdown), or `--map-text TEXT` (pasted verifier JSON). All require `--base-url URL`.
Use `--max-targets N` to bound the expanded capture matrix and `--dry-run` to print a plan
without browser or network activity. Repeat `--allow-private-host HOST` for an exact-host
RFC1918/ULA/loopback opt-in; metadata and other prohibited addresses remain refused.
Legacy direct URL capture keeps its exact-target loopback exception, but sitemap/crawl
discovery remains loopback-refused.

| Exit | Meaning |
| --- | --- |
| 0 | Capture completed successfully. |
| 1 | The job failed before capture. |
| 2 | Usage, input, or plan refusal. |
| 3 | Capture completed with one or more failed shots. |
| 4 | A completed map-driven run was refused by the quality gate. |

The quality gate and exit 4 apply only to map-driven runs. Legacy URL, sitemap, crawl, and
local runs retain their existing 0/1/2/3 behavior.

### As a library

```python
from capturd import RestedCaptureManager      # stills
from capturd.walk.coordinator import DemoForge  # walkthroughs
from capturd.walk.schema import DemoSpec       # the contract

mgr = RestedCaptureManager()
job = mgr.start({"crawl": True, "crawl_url": "https://example.com",
                 "viewports": ["desktop", "mobile"], "schemes": ["light", "dark"]})
```

For one page, the singular `url` payload key is an alternative to `urls`, for example
`mgr.start({"url": "https://example.com"})`. The exactly-one-source rule still applies:
do not combine `url`, `urls`, map, sitemap, crawl, or local inputs.

Captures run behind a default-deny navigation guard (HTTP and WebSocket):
loopback, private and link-local hosts are refused. To capture a local dev
server from code, pass the capability at the call site, e.g.
`mgr.start(payload, allow_loopback=True)` (the target's own host only) or
`allow_private_hosts=["172.17.0.1"]` (a list of exact hosts, never a string).
The guard is installed on the whole browser context before capture creates a page,
so popup requests receive the same policy; service workers are blocked. Each target's
exact loopback host is part of context reuse, preventing sibling-host permission leaks.
A start against a loopback/private target without the matching capability emits one
warning per job before the unchanged refusal. Outward plan, job, manifest, and ZIP URL
projections strip `user:password@` userinfo; live navigation targets are not rewritten.

Allowed navigation checks deliberately re-resolve DNS (there is no success cache or IP
pinning). A per-event-loop four-slot semaphore bounds resolver jobs queued to the
default executor, preventing an unbounded executor flood. Document checks have a
10-second bound and fail the target on timeout;
subresource checks have a 2-second bound, then abort only that request and warn. Both
paths remain fail-closed, and a deadline cannot cancel an OS resolver already running.

The real WebSocket and popup/service-worker proofs are marked `browser` and `network`.
They are required security acceptance, not optional skips: missing Playwright or Chromium
is a named test failure. Install Chromium with `python -m playwright install chromium`.

## Requirements

- Python **3.10+**
- **Playwright >=1.48** + Chromium (`python -m playwright install chromium`). `BrowserContext.route_web_socket` is required; capture refuses loudly when it is unavailable.
- For `walk` mode: a RHOBEAR Vertex Gateway token (`RHOBEAR_GW_API_KEY`) — walkthrough narration
  and camera choreography go through the family gateway. No API keys checked into source.
- For `walk` MP4 export: `ffmpeg` on PATH.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

[MIT](LICENSE) — free for anyone, forever. Fork it, ship it, rebrand it. Don't like the name?
Take it off; it's yours.

<p align="center"><sub>Made by SunSponge LLC · a thank-you to the open source we all build on.</sub></p>

## UX reference

The only UX reference for Captur'd is the North Star in rhobear-north-star. The old landing page (`web/`), the studio
web app (`service/web/`), its board renders and the pack rebuild doc were removed on 2026-10-24 (L0); the service is the
engine (API, jobs, MCP). Fonts follow the font contract (`docs/north-star/FONTS.md` there): Adobe kit `sbv5bcv`,
"RHOBEAR" in Rokkitt, the word "Captur'd" in **Birch**, body in Lato, mono in Droid Sans Mono. Nacelle is banned.
The app icons are the current Captur'd mark.
