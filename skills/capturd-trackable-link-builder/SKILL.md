---
name: capturd-trackable-link-builder
description: >-
  Mint Frontman-attributed Captur'd links for a send list.
---

# capturd-trackable-link-builder

Captur'd V2 expert workflow. MCP = capability (capture.*/demo.*/voice.*/analytics.*/share.*), this skill = the expert sequence.
Lane rules from the repo AGENTS.md apply. Never fabricate analytics. Never mutate a published version.

## Workflow

1. Confirm the demo is published (`demo.version.list`). Unpublished demos don't get links.
2. For each prospect: `share.trackable` with Frontman's contact_id. The opaque token is Frontman's - this tool is a facade, do not build your own tracking.
3. If the bridge is unconfigured you get an unattributed URL: tell the owner attribution silently degraded, don't fake it.
4. Hand the URLs to Frontman's send flow (it owns opens/taps/scoring).
5. After sends go out: demo-open/return/complete/cta/branch signals flow back to Frontman automatically. Verify in Frontman's dashboard, not by scraping Captur'd events.
Anti-enumeration rule: one prospect's token must never read another's anything.
