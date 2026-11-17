<p align="center">
  <img src="assets/capturd-logo-wide.png" alt="RHOBEAR Captur'd" width="420">
</p>

<h1 align="center">RHOBEAR Captur'd</h1>

<p align="center">
  <b>The engine that records product demos: free rested-state stills, and agent-made
  interactive walkthroughs you review shot by shot.</b><br>
  Captur'd is the studio inside RHOBEAR Builds (Engines → Captur'd) and the <code>@capturd</code> MCP
  for other harnesses. Every plan gets every feature; stills also run free and offline on your own machine.
</p>

<p align="center">
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-7dffd5"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-3aa7ff">
  <img alt="Playwright" src="https://img.shields.io/badge/engine-Playwright-3aa7ff">
  <img alt="MCP" src="https://img.shields.io/badge/agent--surface-MCP-3aa7ff">
</p>

---

## What it does today

### `capturd shots`: rested-state stills, in bulk, free and local
Most screenshot tools fire too early (mid-animation, before fonts load, while a carousel slides).
Captur'd takes **rested** shots: animations disabled, fonts awaited, motion stopped, scroll reset,
then every page × every viewport × every color scheme, in one run.

- **Four ways in**: crawl a **site**, expand a **sitemap.xml**, paste **URLs**, point at **local HTML**,
  or drive it from a pathway map (below).
- **Responsive matrix**: desktop (1440×1000), tablet (834×1112), mobile (390×844), light and dark.
- **Deterministic**: same input, same pixels.
- **Clean output**: `NNN-site-viewport-scheme.png` + `manifest.json`, ZIP or folder.

### `capturd walk`: agent-made walkthroughs
An agent records a product flow (on its own toward a goal, turn by turn from chat, or while a person
clicks), Captur'd enriches it (annotations, narration, camera timeline) and exports:

- a self-contained **interactive HTML viewer** with choices and local viewer events, and
- **MP4 / GIF** export,
- with a **semantic camera** (anchored to DOM elements, not pixels; meaningless zoom is suppressed),
- **voice-synced** camera keyframes from TTS word timings, and **spotlight**.

Every edit makes a new immutable revision (`demo.revision`), and `demo.audit` / `demo.optimize` check and
tidy a spec without changing an approved one.

**Not supported yet, stated plainly:** canvas / WebGL / game pages are *detected and labelled*
(`contentMode`), but there is no video-mode playback for them in the viewer or the export. Hosted
metering, retakes and publication are being built in this repo; see `GAPS-NOW.md`.

### `capturd serve`: the `@capturd` MCP

```text
# @capturd tools
capture.crawl   capture.rested                                  # stills
demo.record     demo.act      demo.narrate    demo.poll_voice   # record
demo.look       demo.stop     demo.cancel                       # session
demo.status     demo.list                                       # library
demo.edit       demo.delete   demo.export                       # edit and export
demo.zoom       demo.pan      demo.hold                         # camera
demo.spotlight  demo.overlay                                    # composition
demo.reorder    demo.trim     demo.branch                       # structure
demo.stylize    demo.regenerate                                 # restyle
demo.audit      demo.optimize demo.revision                     # check, tidy, revisions
take.list       take.approve  take.reject     take.revoke       # shot review; export waits for approval
engine.contract                                                 # manifest + contract version (also /healthz)
```

Editing is the API: the agent is the editor.

## Install

```bash
git clone https://github.com/rhobear75/rhobear-capturd
cd rhobear-capturd
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
Direct `--urls`/`--local` capture keeps its exact-target loopback exception. Sitemap and
crawl discoveries have no blanket loopback permission: a discovered loopback target is
refused unless its exact host is named with `--allow-private-host`; other hosts and metadata
remain refused.

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
A default local `pytest` deselects these optional tests, the pre-existing `slow` Chromium
viewer tests, and `e2e`, so an unprovisioned workstation can run the unrelated suite. For explicit local selection,
install the project dependencies and run `python -m playwright install chromium`, then use
`pytest -m 'browser or network'`. `RB_REQUIRE_BROWSER=1` or `CI` restores the full `not e2e`
selection for required security acceptance. In those runs the proofs never skip: missing
Playwright or Chromium is a named required-security failure.

## Requirements

- Python **3.10+**
- **Playwright >=1.48** + Chromium (`python -m playwright install chromium`). `BrowserContext.route_web_socket` is required; capture refuses loudly when it is unavailable.
- For `walk` agent mode: a RHOBEAR gateway token (`RHOBEAR_GW_API_KEY`, base URL `RHOBEAR_GW_BASE_URL`);
  narration and camera choreography go through the family gateway. No keys in source.
- For `walk` MP4 export: `ffmpeg` on PATH.

## Development

```bash
pip install -e ".[dev]"
pytest                                      # local: not e2e/browser/network/slow
python -m playwright install chromium       # required for security acceptance
RB_REQUIRE_BROWSER=1 pytest                 # required: not e2e, zero security skips
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
