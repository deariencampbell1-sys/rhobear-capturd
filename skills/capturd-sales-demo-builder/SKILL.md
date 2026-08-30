---
name: capturd-sales-demo-builder
description: >-
  Build a sales demo from a product URL end to end (record -> enrich -> branch -> CTA -> publish).
---

# capturd-sales-demo-builder

Captur'd V2 expert workflow. MCP = capability (capture.*/demo.*/voice.*/analytics.*/share.*), this skill = the expert sequence.
Lane rules from the repo AGENTS.md apply. Never fabricate analytics. Never mutate a published version.

## Workflow

1. Record: `demo.record` the golden path - sign-in state, primary action, the wow moment, end on the natural next step.
2. Enrich: stop, wait for the AI pipeline, `demo.look` the annotations.
3. Narrate: ensure every step has annotation; apply brand voice (see capturd-brand-voice-editor).
4. Camera: `demo.zoom`/`demo.spotlight` on the thing that matters each step; `demo.hold` only where a human would pause.
5. Branch: `demo.branch` with choices for the 2-3 real buyer paths, each with an analyticsName.
6. CTA: `demo.edit` on the last step + spec.cta (e.g. Book a Build Call).
7. Gate: `demo.audit` >= 75, zero high. Then hosted: `demo.publish` -> `share.create`.
Never invent product claims. The screen is the truth.
