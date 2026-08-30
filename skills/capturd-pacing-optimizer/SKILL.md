---
name: capturd-pacing-optimizer
description: >-
  Tighten demo pacing: long holds, static steps, dwell vs narration.
---

# capturd-pacing-optimizer

Captur'd V2 expert workflow. MCP = capability (capture.*/demo.*/voice.*/analytics.*/share.*), this skill = the expert sequence.
Lane rules from the repo AGENTS.md apply. Never fabricate analytics. Never mutate a published version.

## Workflow

1. `demo.audit` - pacing findings list long holds / static steps with evidence.
2. `demo.optimize` (plan-only) and review the pause_trim proposals. Apply safe ones with apply=true.
3. For structural pacing (step order, merging), plan with `demo.reorder`/`demo.trim` but get owner approval before deleting anything.
4. Re-audit. Pacing score should rise without dropping narration score (a trimmed hold that cuts narration mid-word is a regression).
5. Publish a NEW version - never mutate a live one.
