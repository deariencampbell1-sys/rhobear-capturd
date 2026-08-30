---
name: capturd-stale-demo-detector
description: >-
  Find demos that are old, broken, or underperforming and need attention.
---

# capturd-stale-demo-detector

Captur'd V2 expert workflow. MCP = capability (capture.*/demo.*/voice.*/analytics.*/share.*), this skill = the expert sequence.
Lane rules from the repo AGENTS.md apply. Never fabricate analytics. Never mutate a published version.

## Workflow

1. Age check: last published_at older than ~90 days = review candidate (age is a hint, not the verdict).
2. Selector check: capturd-product-change-mapper's live-selector diff is the real staleness test.
3. Performance check: `analytics.demo` - completion_rate < 25% with >= 20 starts = underperformer.
4. Verdict per demo: KEEP / REFRESH (list steps) / RETIRE. RETIRE needs owner approval - never delete demos yourself.
5. Deliver the verdict list with evidence (age, broken selectors, numbers).
