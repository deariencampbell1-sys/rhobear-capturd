---
name: capturd-dropoff-analyzer
description: >-
  Find where viewers leave and why, step by step.
---

# capturd-dropoff-analyzer

Captur'd V2 expert workflow. MCP = capability (capture.*/demo.*/voice.*/analytics.*/share.*), this skill = the expert sequence.
Lane rules from the repo AGENTS.md apply. Never fabricate analytics. Never mutate a published version.

## Workflow

1. `analytics.dropoff` for the demo+version. Sort by dropoff_pct.
2. For each step with dropoff >= 35% and reach >= 10: `demo.look` that step. Common killers: narration ends before the click lands, hotspot on the wrong element, camera never arrives.
3. Cross-check `analytics.session` on 2-3 sessions that exited there - replay their trail before theorizing.
4. Propose fixes as `demo.optimize` plan items or targeted `demo.edit`s. One variable at a time; publish as a new version.
5. Prove the fix: `analytics.compare` old version vs new after ~20 sessions. No claims without that delta.
