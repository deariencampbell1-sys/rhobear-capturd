---
name: capturd-top-performer-extractor
description: >-
  Extract what the top-performing demos do, as repeatable patterns.
---

# capturd-top-performer-extractor

Captur'd V2 expert workflow. MCP = capability (capture.*/demo.*/voice.*/analytics.*/share.*), this skill = the expert sequence.
Lane rules from the repo AGENTS.md apply. Never fabricate analytics. Never mutate a published version.

## Workflow

1. Rank demos by completion_rate (>= 20 starts) and CTA conversion.
2. For the top 2: read every annotation, timeline keyframe (`demo.look`), branch structure, CTA copy.
3. Extract PATTERNS not accidents: how many steps to first zoom? narration length per step? where the branch sits? CTA timing relative to the wow moment?
4. Write each pattern as a checkable rule ('zoom lands within 1.2s of the step start').
5. Propose applying the top 3 rules to the worst performer as `demo.optimize`/`demo.edit` plans. Measure with `analytics.compare`.
