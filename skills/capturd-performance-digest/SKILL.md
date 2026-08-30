---
name: capturd-performance-digest
description: >-
  Weekly digest of demo performance across the library.
---

# capturd-performance-digest

Captur'd V2 expert workflow. MCP = capability (capture.*/demo.*/voice.*/analytics.*/share.*), this skill = the expert sequence.
Lane rules from the repo AGENTS.md apply. Never fabricate analytics. Never mutate a published version.

## Workflow

1. List demos and their published versions.
2. `analytics.demo` per published demo. Build the table: sessions, completion_rate, avg engaged time, CTA conversion.
3. Rank: top performer, biggest mover, biggest dropper. Flag demos with < 5 starts as 'not enough data' - never rank them.
4. For the biggest dropper, hand off to capturd-dropoff-analyzer.
5. Output: 10-line digest, numbers first, no adjective without a number behind it.
