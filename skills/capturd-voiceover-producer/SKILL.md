---
name: capturd-voiceover-producer
description: >-
  Produce and quality-check narration audio for a demo.
---

# capturd-voiceover-producer

Captur'd V2 expert workflow. MCP = capability (capture.*/demo.*/voice.*/analytics.*/share.*), this skill = the expert sequence.
Lane rules from the repo AGENTS.md apply. Never fabricate analytics. Never mutate a published version.

## Workflow

1. Pick the voice: `voice.list` (HD Polly voices carry word timings for camera sync; edge fallbacks don't count as HD).
2. `voice.preview` 2-3 candidates; listen for tone match with the brand voice.
3. Narrate: `voice.synthesize` per step annotation (NOT the whole script - per-step keeps word timing aligned to clicks).
4. Check timings: first word tStartMs near 0; last tEndMs < dwell. If audio outruns the step, shorten copy (capturd-pacing-optimizer) - never speed the audio.
5. Attach via `demo.edit`/`demo.regenerate` so voiceoverWords land on the right steps.
Switcher rule: one voice per demo. Two voices in one demo is a defect.
