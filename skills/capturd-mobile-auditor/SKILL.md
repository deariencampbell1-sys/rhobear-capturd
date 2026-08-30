---
name: capturd-mobile-auditor
description: >-
  Audit the mobile experience of a demo (~390px).
---

# capturd-mobile-auditor

Captur'd V2 expert workflow. MCP = capability (capture.*/demo.*/voice.*/analytics.*/share.*), this skill = the expert sequence.
Lane rules from the repo AGENTS.md apply. Never fabricate analytics. Never mutate a published version.

## Workflow

1. `demo.audit` - mobile_experience findings list targets outside the mobile-safe crop.
2. Playwright pass at 390x844: load the viewer, verify controls reachable, captions not clipped, choice buttons thumb-sized (>= 44px height), CTA visible.
3. Hotspot math: any interaction target with x-fraction < 10% or > 90% gets re-framed via `demo.zoom` centering (zoom re-centers the crop).
4. Test `prefers-reduced-motion` + keyboard nav while you're in there.
5. Fix via edit/zoom (never by resizing the recorded viewport), re-render, re-verify at 390px.
