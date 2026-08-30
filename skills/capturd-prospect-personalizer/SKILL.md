---
name: capturd-prospect-personalizer
description: >-
  Personalize a demo for a prospect using dynamic variables - server-side, no PII in URLs.
---

# capturd-prospect-personalizer

Captur'd V2 expert workflow. MCP = capability (capture.*/demo.*/voice.*/analytics.*/share.*), this skill = the expert sequence.
Lane rules from the repo AGENTS.md apply. Never fabricate analytics. Never mutate a published version.

## Workflow

1. Start from a published demo. NEVER mutate the published version.
2. Decide variables: business_name, use_case, viewer_role... allowed fields only (title, narration, branch intro, choice labels, CTA label).
3. Local preview: `demo.personalize` (preview mode) and READ the output - fallbacks must read naturally when empty.
4. Attribution: prospect identity lives in Frontman. Use `share.trackable` - context resolves server-side from the opaque token. NEVER put contact name/email/phone in a URL or a variable value.
5. If the demo needs a personal version saved, work on the DRAFT and publish as a new version.
6. Ship: send the trackable URL through Frontman's own send flow.
