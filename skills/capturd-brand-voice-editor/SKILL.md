---
name: capturd-brand-voice-editor
description: >-
  Rewrite demo narration/annotations to a brand voice, then re-sync timing.
---

# capturd-brand-voice-editor

Captur'd V2 expert workflow. MCP = capability (capture.*/demo.*/voice.*/analytics.*/share.*), this skill = the expert sequence.
Lane rules from the repo AGENTS.md apply. Never fabricate analytics. Never mutate a published version.

## Workflow

1. Ask for (or look up) the voice spec: person, tone, 3 banned words, sentence-length target.
2. Pull current copy: `demo.list` -> `demo.look`. Dump every `annotation`.
3. Rewrite per step: lead with the viewer's outcome, one idea per step, <= 140 chars, keep recorded product copy sacred (never claim features the screen doesn't show).
4. Apply: `demo.edit` per step with the new annotation.
5. Re-run `demo.audit` - narration category must not regress.
6. If voiceover audio is baked in, `demo.regenerate` narration for edited steps so words stay synced.
