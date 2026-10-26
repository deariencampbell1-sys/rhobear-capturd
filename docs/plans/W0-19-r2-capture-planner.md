# W0-19 round 2 — capture planner: D52–D58

Scope: `capturd/shots/planner.py` and `tests/test_capture_planner.py`, nothing
else. Red tests first (`test: <name> (red)` subjects), then one fix commit per
decision (`fix: <what> (D5x)` subjects). All seven decisions are verified by
tests in the test layer only.

Acceptance (measured):

- `env -u PYTHONPATH .venv/bin/python -m pytest tests/test_capture_planner.py -q`
  — 94 passed.
- `PYTHONPATH=scripts .venv/bin/python -m pytest -q`
  — 323 passed, 3 skipped, 13 deselected (316 passed + 7 new tests).
- `git status --porcelain` — clean.

## D52 — the escaped quote

`_split_chain` honoured the backslash escape only inside quotes, so an escaped
`"` in an unquoted segment (`#say\"hi > button`) flipped the quote state and
every later ` > ` was swallowed into one part — the chain never split, the
structural strategy failed to verify, and the node was dropped as ambiguous.
The escape now runs everywhere and a quote opens a string only when it is not
escaped. Edge cases: `div[k="v\"w"] > button` and `div[a="x > y"] > button`
both split into exactly two segments (regression asserts in the same test).

Red test: `test_escaped_quote_outside_quotes_does_not_open_a_string`.
Fix: `fix: an escaped quote outside quotes does not open a string (D52)`.

## D53 — `!important` beats a later normal declaration

`_style_declares` was plain last-wins over the whole block. Per property the
winning declaration is now the last `!important` one when any exists,
otherwise the last normal one — and only that winner decides:
`display:none!important;display:block` is hidden (not planned),
`display:block!important;display:none` is visible (planned). An empty value
(`display:`) is invalid CSS, is never a winner, and overrides nothing.

Red test: `test_important_beats_a_later_normal_declaration`.
Fix: `fix: !important beats a later normal declaration in one block (D53)`.

## D54 — nearest explicit `aria-disabled` wins

Inheritance (D34) stays, but the nearest explicit `aria-disabled` on the
ancestor chain now decides — an explicit `aria-disabled="false"` stops the
inheritance and re-enables the subtree. The disabled-`<fieldset>` check still
runs along the whole chain whatever the aria values say.

Red test: `test_explicit_aria_disabled_false_overrides_inherited_true`.
Fix: `fix: an explicit aria-disabled=false overrides an inherited true (D54)`.

## D55 — the `<details>` gate

Only the details BODY is conditional. A closed `<details>` renders itself and
its first `<summary>`; the gate judged the `<details>` invisible itself
(`_in_first_summary(details, details)` is False). The `node is not current`
guard passes the details element and its first summary and refuses only the
body; `<details open>` renders the body. A closed disclosure control and its
`<summary>` plan with selectors `main > details > summary` and
`main > details`; nothing else changes.

Red test: `test_closed_details_and_its_summary_are_visible`.
Fix: `fix: a closed <details> itself is rendered; only its body is conditional (D55)`.

Drift (reported, not built): planning the bare `<details>` ELEMENT itself as
a candidate. Discovery is role/attr/tag driven and a bare
`<details><summary>…` matches none of it; adding `<details>` elements to the
sweep would append candidates whose labels fold the whole details subtree,
which changes the exact list `["Gamma", "Zeta", "Eta"]` asserted by the
locked D35 test `test_ancestor_aria_disabled_and_unrendered_containers_are_not_planned`.
Locked tests may not be weakened, so the edge case is reported as drift. The
frozen fixture numbers are not affected either way: fixture A's `<details>`
("Billing FAQ") sits under the hidden `#tab-billing` panel and stays unplanned.

## D56 — `navigation_excluded` on a truncated plan

The cap stopped discovery early and took the counter with it. The count is
now taken up front over the FULL union of the discovery lists (deduplicated
per node, D6 unchanged), so a document whose navigation nodes lie partly past
the truncation point still reports every one of them.

Red test: `test_navigation_excluded_counts_on_a_truncated_plan` (3 early nav
links, 4 kept buttons under `max_states=2`, 4 late nav links — 7, where the
scan-stop counted 3).
Fix: `fix: navigation_excluded counts over the full discovery lists (D56)`.

## D57 — `aria-current="false"` is not current

`aria-current` is a value keyword (page/step/location/date/time/true, D21
folded lowercase). The check was presence-based, so `aria-current="false"`
was classified PAGE; it now plans as a normal action (`control`).

Red test: `test_aria_current_false_is_not_current`.
Fix: `fix: aria-current=false is not current (D57)`.

## D58 — `dropped_ambiguous` counts each node once

The counter incremented once per failing discovery call, so a node reached by
two discovery reasons (`<summary role="tab">` is both a tab and a summary)
was counted twice. Dropped nodes are recorded in a set — one entry per node,
whatever reached it — and the counter is the set's size (a node count, never
set iteration order).

Red test: `test_dropped_ambiguous_counts_each_node_once` (one dropped node →
1; two dropped nodes each reached twice → 2).
Fix: `fix: dropped_ambiguous counts each dropped node once (D58)`.

## Structure note

The red tests were committed as seven `test: <name> (red)` commits — the
ticket's exact subjects — as a red group ahead of the seven fix commits, then
one fix commit per decision in decision order. No fixture numbers, locked
tests, ceilings, or public names changed.
