---
name: capturd-demo-qa
description: >-
  QA a demo end to end before it ships: structural audit, keyframe sanity, CTA present, mobile crop, branch reachability.
---

# capturd-demo-qa

Captur'd V2 expert workflow. MCP = capability (capture.*/demo.*/voice.*/analytics.*/share.*), this skill = the expert sequence.
Lane rules from the repo AGENTS.md apply. Never fabricate analytics. Never mutate a published version.

## Workflow

1. Run `demo.audit` (pass live analytics via `analytics.demo` if published). Overall + category scores are your gate.
2. Read every `high` finding - fix before shipping. `warn` = judgment call. `info` = polish.
3. Walk the demo with `demo.list` + `demo.look`; confirm step 1 loads, narration matches the screen, hotspot lands on the real target.
4. If branches exist: `demo.branch` choices must all have destinations in range and no dead-end loops (audit flags these).
5. CTA check: a published sales demo with no cta and no ctaId-bearing choice is a fail.
6. Ship gate: overall >= 75 and zero high findings. Otherwise return the findings list and stop.
NEVER pass the audit by editing the audit - fix the demo.
