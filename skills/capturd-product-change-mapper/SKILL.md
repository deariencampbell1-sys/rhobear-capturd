---
name: capturd-product-change-mapper
description: >-
  Product UI changed - map which demos are stale and what to re-record.
---

# capturd-product-change-mapper

Captur'd V2 expert workflow. MCP = capability (capture.*/demo.*/voice.*/analytics.*/share.*), this skill = the expert sequence.
Lane rules from the repo AGENTS.md apply. Never fabricate analytics. Never mutate a published version.

## Workflow

1. For each demo: `demo.list` + `demo.look` each step's target selector.
2. Crawl the live product (`capture.rested` on the product URL) and diff: does each recorded selector still exist? Does the flow still end at the same CTA?
3. Classify per demo: VALID (all selectors live), STALE-STEP (n specific steps), STRUCTURAL (flow changed - re-record).
4. For STALE-STEP: re-record just those steps (`demo.record` append + `demo.trim`), re-audit, publish new version.
5. Report the map as a table. Don't re-record anything structural without the owner's go.
