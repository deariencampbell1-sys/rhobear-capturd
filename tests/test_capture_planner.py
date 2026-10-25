"""Tests for the deterministic capture planner (capturd.shots.planner)."""

from __future__ import annotations

from pathlib import Path

from capturd.shots.planner import (
    ControlKind,
    NAVIGATION_SELECTOR_HINTS,
    STABLE_SELECTOR_ATTRIBUTES,
    classify_control,
    iter_elements,
    parse_html,
    plan_candidates,
    unique_selector,
)

FIXTURES = Path(__file__).parent / "fixtures" / "capture-foundations"


def _load(name: str):
    return parse_html((FIXTURES / name).read_text(encoding="utf-8"))


def test_fixture_a_orders_tabs_accordion_modal():
    root = _load("planner_tabs_accordion_modal.html")
    plan = plan_candidates(root)

    assert plan.reason == "ok"
    assert [c.kind for c in plan.candidates] == ["tab", "tab", "accordion", "modal"]
    # D12: the tab loop uses the shared priority table, so a role=tab
    # carries the same priority (12) as a swept tab-like control.
    assert [c.priority for c in plan.candidates] == [12, 12, 25, 30]
    # Ties broken by label: "Billing" sorts before "Overview".
    assert plan.candidates[0].label == "Billing"
    assert plan.candidates[1].label == "Overview"
    assert [c.id for c in plan.candidates] == ["01-tab", "02-tab", "03-accordion", "04-modal"]


def test_fixture_a_selectors_and_strategies():
    root = _load("planner_tabs_accordion_modal.html")
    plan = plan_candidates(root)

    by_label = {c.label: c for c in plan.candidates}
    assert by_label["Billing"].selector == "#tab-billing"
    assert by_label["Billing"].selector_strategy == "id"
    assert by_label["Open sign-up"].selector_strategy == "attribute"
    assert by_label["Open sign-up"].selector == 'button[data-bs-target="#signup-modal"]'
    assert by_label["Billing FAQ"].selector_strategy == "structural"
    # The topbar link is navigation, never a candidate.
    assert all(c.label != "Home" for c in plan.candidates)
    assert plan.limits["navigation_excluded"] == 1
    # The modal target is statically hidden.
    assert by_label["Open sign-up"].target == "#signup-modal"
    assert by_label["Open sign-up"].target_visible is False


def test_no_candidate_is_ambiguity_prone():
    for name in (
        "planner_tabs_accordion_modal.html",
        "planner_duplicate_ids.html",
    ):
        root = _load(name)
        plan = plan_candidates(root)
        assert plan.candidates, name
        # Uniqueness from the public plan (D7): nothing was dropped as
        # ambiguous and every emitted selector is distinct.
        assert plan.limits["dropped_ambiguous"] == 0, name
        selectors = [c.selector for c in plan.candidates]
        assert len(set(selectors)) == len(selectors), name


def test_fixture_b_structural_fallback_repairs_duplicate_ids():
    root = _load("planner_duplicate_ids.html")
    plan = plan_candidates(root)

    assert plan.reason == "ok"
    # Duplicate #expand-btn / #action-btn ids and the repeated data-testid are
    # repaired by the positional structural strategy, never emitted ambiguous.
    assert plan.limits["dropped_ambiguous"] == 0
    # (priority, label) order: the two menus sort ahead of the accordions.
    assert [c.label for c in plan.candidates] == ["Open menu", "Open menu", "Expand A", "Expand B"]
    for candidate in plan.candidates:
        assert candidate.selector_strategy == "structural"
    assert ":nth-of-type(" in plan.candidates[2].selector
    assert plan.candidates[2].kind == "accordion"
    assert plan.candidates[0].kind == "menu"


def test_unrepairable_ambiguous_candidates_are_dropped():
    # Deliberate update (review round 1, D10): top-level siblings are now
    # repaired with :nth-of-type, so unrepairable ambiguity needs a shape no
    # strategy can split: identical subtrees under two <html> elements, where
    # the structural path refuses to cross <html> and the duplicated id cannot
    # anchor. Both buttons are dropped with a reason, never emitted ambiguous.
    html = (
        "<html><body><button id='x' aria-expanded='false'>Go</button></body></html>"
        "<html><body><button id='x' aria-expanded='false'>Go</button></body></html>"
    )
    root = parse_html(html)
    plan = plan_candidates(root)
    assert plan.candidates == []
    assert plan.reason == "no-candidates"
    assert plan.limits["dropped_ambiguous"] == 2


def test_fixture_c_navigation_only_yields_no_candidates():
    root = _load("planner_navigation_only.html")
    plan = plan_candidates(root)

    # A plan with no candidates is a failure of the plan, not an empty success.
    assert plan.candidates == []
    assert plan.reason == "no-candidates"
    assert plan.limits["navigation_excluded"] == 5


def test_max_states_truncates_deterministically():
    root = _load("planner_tabs_accordion_modal.html")
    plan = plan_candidates(root, max_states=2)

    assert plan.limits["max_states"] == 2
    assert plan.limits["truncated"] is True
    assert [c.id for c in plan.candidates] == ["01-tab", "02-tab"]


def test_capped_plan_reports_truncated():
    # D3: reason == "truncated" whenever limits["truncated"] is true —
    # including a capped plan that still has candidates. "ok" is only for an
    # untruncated plan.
    root = _load("planner_tabs_accordion_modal.html")
    plan = plan_candidates(root, max_states=2)

    assert plan.reason == "truncated"
    assert plan.limits["truncated"] is True
    assert [c.id for c in plan.candidates] == ["01-tab", "02-tab"]


def test_max_states_zero_is_a_failed_plan():
    # Deliberate update (review round 1, D4): a cap that truncates the plan to
    # zero is reason="truncated" — the operator must see that their max_states
    # did this, not that the document had nothing to capture.
    root = _load("planner_tabs_accordion_modal.html")
    plan = plan_candidates(root, max_states=0)

    assert plan.candidates == []
    assert plan.reason == "truncated"
    assert plan.limits["truncated"] is True
    assert plan.limits["max_states"] == 0
    assert plan.limits["discovered"] >= 1

    # A negative cap behaves like zero.
    negative = plan_candidates(root, max_states=-3)
    assert negative.reason == "truncated"
    assert negative.limits["max_states"] == 0


def test_plan_is_byte_deterministic():
    html = (FIXTURES / "planner_tabs_accordion_modal.html").read_text(encoding="utf-8")
    first = repr(plan_candidates(parse_html(html)))
    second = repr(plan_candidates(parse_html(html)))
    assert first == second


def test_classify_control_kinds():
    def one(html: str):
        root = parse_html(html)
        return root.children[0]

    assert classify_control(one('<button role="tab">A</button>')) == (ControlKind.TAB, False)
    assert classify_control(one("<summary>More</summary>")) == (ControlKind.ACCORDION, False)
    assert classify_control(one('<nav><a href="/x">X</a></nav>').children[0]) == (
        ControlKind.PAGE,
        True,
    )
    assert classify_control(one('<button aria-expanded="false">FAQ</button>')) == (
        ControlKind.ACCORDION,
        False,
    )
    assert classify_control(one("<button>Do it</button>")) == (ControlKind.CONTROL, False)
    assert classify_control(one("<button>Save</button>")) == (ControlKind.NONE, False)
    assert classify_control(one("<p>Nothing</p>")) == (ControlKind.NONE, False)


def test_hidden_and_disabled_controls_are_skipped():
    root = parse_html(
        "<body>"
        '<button hidden id="gone">Ghost</button>'
        '<button aria-hidden="true" id="veiled">Veiled</button>'
        '<button disabled id="stuck">Stuck</button>'
        '<button id="live" aria-expanded="false">Live</button>'
        "</body>"
    )
    plan = plan_candidates(root)
    assert [c.label for c in plan.candidates] == ["Live"]


# ---------------------------------------------------------------------------
# The inline-style visibility branches and both non-attribute disabled
# branches: the module's "never plan an unclickable control" guarantee.
# ---------------------------------------------------------------------------


def test_inline_display_none_is_skipped():
    root = parse_html(
        "<main>"
        '<button style="display:none" id="gone">Ghost</button>'
        '<button id="live" aria-expanded="false">Live</button>'
        "</main>"
    )
    plan = plan_candidates(root)
    assert [c.label for c in plan.candidates] == ["Live"]


def test_inline_visibility_hidden_is_skipped():
    root = parse_html(
        "<main>"
        '<button style="visibility:hidden" id="veiled">Veiled</button>'
        '<button id="live" aria-expanded="false">Live</button>'
        "</main>"
    )
    plan = plan_candidates(root)
    assert [c.label for c in plan.candidates] == ["Live"]


def test_inline_opacity_zero_is_skipped():
    root = parse_html(
        "<main>"
        '<button style="opacity:0" id="faint">Faint</button>'
        '<button id="live" aria-expanded="false">Live</button>'
        "</main>"
    )
    plan = plan_candidates(root)
    assert [c.label for c in plan.candidates] == ["Live"]


def test_inline_opacity_half_stays_planned():
    # Only opacity 0 hides; 0.5 is a dimmed but visible, clickable control.
    root = parse_html('<main><button style="opacity:0.5" id="dim">Dimmed</button></main>')
    plan = plan_candidates(root)
    assert [c.label for c in plan.candidates] == ["Dimmed"]


def test_inline_display_none_important_is_skipped():
    root = parse_html(
        "<main>"
        '<button style="display: none !important" id="gone">Ghost</button>'
        '<button id="live" aria-expanded="false">Live</button>'
        "</main>"
    )
    plan = plan_candidates(root)
    assert [c.label for c in plan.candidates] == ["Live"]


def test_aria_disabled_true_is_skipped():
    root = parse_html(
        "<main>"
        '<button aria-disabled="true" id="stuck">Stuck</button>'
        '<button id="live" aria-expanded="false">Live</button>'
        "</main>"
    )
    plan = plan_candidates(root)
    assert [c.label for c in plan.candidates] == ["Live"]


def test_disabled_class_is_skipped():
    root = parse_html(
        "<main>"
        '<button class="btn disabled" id="stuck">Stuck</button>'
        '<button id="live" aria-expanded="false">Live</button>'
        "</main>"
    )
    plan = plan_candidates(root)
    assert [c.label for c in plan.candidates] == ["Live"]


# ---------------------------------------------------------------------------
# The action-word denylist: label text only, and every drop is counted.
# ---------------------------------------------------------------------------


def test_action_word_denylist_matches_label_only_and_counts_drops():
    # Deliberate scope (review round 1, D5): the denylist matches the control's
    # label text only - not role, not class text, not href - keeps the same
    # verb list, and every drop is counted for the operator. <a href="/submit">
    # with a neutral label is kept; <button>Save</button> is still dropped
    # (upstream parity) but now visibly counted.
    root = parse_html(
        "<main>"
        "<a href='/submit'>Docs</a>"
        "<button>Save</button>"
        "<button>Cancel changes</button>"
        "<a class='submit-button' href='/more'>Read more</a>"
        "</main>"
    )
    plan = plan_candidates(root)

    assert [c.label for c in plan.candidates] == ["Docs", "Read more"]
    assert plan.limits["dropped_action_labels"] == 2
    assert plan.reason == "ok"


# ---------------------------------------------------------------------------
# Selector robustness: CSSOM escaping and attribute values containing ">".
# ---------------------------------------------------------------------------


def test_leading_digit_id_is_css_escaped():
    # querySelectorAll('#1foo') throws SyntaxError; the CSSOM escape of the
    # leading digit is '\31 ', so the emitted selector must be '#\\31 foo'.
    root = parse_html("<main><button id='1foo' aria-expanded='false'>Open</button></main>")
    node = root.children[0].children[0]

    selector, strategy = unique_selector(node)
    assert strategy == "id"
    assert selector == "#\\31 foo"

    plan = plan_candidates(root)
    (candidate,) = plan.candidates
    assert candidate.selector == "#\\31 foo"
    assert candidate.selector_strategy == "id"


def test_leading_dash_digit_id_is_css_escaped():
    # An identifier may start with '-' but not with '-' followed by a digit.
    root = parse_html("<main><button id='-1foo' aria-expanded='false'>Open</button></main>")
    node = root.children[0].children[0]

    selector, strategy = unique_selector(node)
    assert strategy == "id"
    assert selector == "#-\\31 foo"

    # The public plan emits exactly that selector for exactly one candidate.
    plan = plan_candidates(root)
    (candidate,) = plan.candidates
    assert candidate.selector == "#-\\31 foo"
    assert candidate.selector_strategy == "id"


def test_attribute_value_backslash_round_trips():
    # D3: one escape helper for both directions — a backslash inside an
    # attribute value is doubled on generation and unescaped again by the
    # verifier, so the emitted selector resolves to the same node and the
    # control is planned, not silently dropped as ambiguous.
    root = parse_html("<main><button title=\"C:\\backups\">Restore</button></main>")
    plan = plan_candidates(root)

    (candidate,) = plan.candidates
    # title is a label source ahead of text, so the round-tripped value is the
    # label itself — the raw attribute value, backslash intact.
    assert candidate.label == "C:\\backups"
    assert candidate.selector_strategy == "attribute"
    assert candidate.selector == 'button[title="C:\\\\backups"]'
    assert plan.limits["dropped_ambiguous"] == 0


def test_attribute_value_quote_round_trips():
    # D3: the same helper escapes the quote, so [attr="..."] survives a value
    # containing double quotes and round-trips through the verifier exactly.
    root = parse_html("<main><button aria-label='Say \"hi\"'>Greet</button></main>")
    plan = plan_candidates(root)

    (candidate,) = plan.candidates
    assert candidate.label == 'Say "hi"'
    assert candidate.selector_strategy == "attribute"
    assert candidate.selector == 'button[aria-label="Say \\\"hi\\\""]'
    assert plan.limits["dropped_ambiguous"] == 0


def test_attribute_value_with_gt_does_not_break_the_chain():
    # The chain matcher splits on " > "; an attribute value containing ">"
    # used to split its own selector and the candidate was silently dropped.
    html = (
        "<div id='host'><button aria-label='Home > Docs'>Docs</button></div>"
        "<div id='host'><button aria-label='Home > Blog'>Blog</button></div>"
    )
    root = parse_html(html)
    plan = plan_candidates(root)

    by_label = {c.label: c for c in plan.candidates}
    assert set(by_label) == {"Home > Docs", "Home > Blog"}
    assert by_label["Home > Docs"].selector_strategy == "attribute"
    assert by_label["Home > Docs"].selector == 'button[aria-label="Home > Docs"]'
    assert by_label["Home > Blog"].selector == 'button[aria-label="Home > Blog"]'
    assert plan.limits["dropped_ambiguous"] == 0


# ---------------------------------------------------------------------------
# Fragments: #root children get :nth-of-type too, and navigation exclusion is
# uniform across every discovery loop.
# ---------------------------------------------------------------------------


def test_root_siblings_are_disambiguated():
    # Brief 01d feeds fragments (synthetic #root, several top-level controls);
    # two top-level buttons must both plan via :nth-of-type, never both drop.
    root = parse_html("<button>One</button><button>Two</button>")
    plan = plan_candidates(root)

    assert plan.reason == "ok"
    assert [c.label for c in plan.candidates] == ["One", "Two"]
    assert [c.selector for c in plan.candidates] == [
        "button:nth-of-type(1)",
        "button:nth-of-type(2)",
    ]
    assert plan.limits["dropped_ambiguous"] == 0


def test_structural_path_with_a_unique_id_ancestor_is_planned():
    # D1 (round 3): generation and verification share one anchor. The
    # generator used to stop the structural chain at a unique-id ancestor
    # ("div#host > button") while the verifier only accepts chains anchored at
    # the document boundary — so the emitted path never verified against its
    # own node and the button was dropped as ambiguous. Invariant (D1): a path
    # the generator emits always verifies against the same node.
    root = parse_html("<main><div id='host'><button>Go</button></div></main>")
    plan = plan_candidates(root)

    assert plan.reason == "ok"
    (candidate,) = plan.candidates
    assert candidate.label == "Go"
    assert candidate.selector_strategy == "structural"
    assert candidate.selector == "main > div#host > button"
    assert plan.limits["dropped_ambiguous"] == 0
    # The generator's own selector is exactly the selector the plan verified.
    button = root.children[0].children[0].children[0]
    assert unique_selector(button)[0] == candidate.selector


def test_structural_path_is_root_scoped():
    # D2 (round 2, Apollo): every structural path starts at #root, and the
    # verifier anchors the chain there — "div > button" must never match a
    # button under an unrelated div, so the top div's button and the nested
    # div inside <main> both plan, none dropped as ambiguous.
    root = parse_html(
        "<div><button>A</button></div><main><div><button>B</button></div></main>"
    )
    plan = plan_candidates(root)

    assert [c.label for c in plan.candidates] == ["A", "B"]
    assert plan.limits["dropped_ambiguous"] == 0
    by_label = {c.label: c for c in plan.candidates}
    assert by_label["A"].selector == "div > button"
    assert by_label["B"].selector == "main > div > button"


# ---------------------------------------------------------------------------
# Document-order text: label text interleaves children and own text.
# ---------------------------------------------------------------------------


def test_text_after_an_inline_child_keeps_document_order():
    # D2: _full_text interleaves children and text in document order.
    # <button><b>Save</b> now</button> yields "Save now", not "nowSave" —
    # so the action-word rule sees "save" and counts the drop; and the
    # neutral sibling's label is "Retry now", not "nowRetry".
    root = parse_html(
        "<main>"
        "<button><b>Save</b> now</button>"
        "<button><b>Retry</b> now</button>"
        "</main>"
    )
    plan = plan_candidates(root)

    assert plan.limits["dropped_action_labels"] == 1
    (candidate,) = plan.candidates
    assert candidate.label == "Retry now"


def test_summary_in_navigation_is_excluded_and_counted():
    # A <summary> inside navigation chrome is excluded like any other control:
    # the summary loop must apply the same navigation skip and count it.
    root = parse_html(
        "<header class='navbar'><details><summary>Menu</summary></details></header>"
        "<main><summary>Content disclosure</summary></main>"
    )
    plan = plan_candidates(root)

    assert [c.label for c in plan.candidates] == ["Content disclosure"]
    assert all(c.is_navigation is False for c in plan.candidates)
    assert plan.limits["navigation_excluded"] == 1


def test_dual_role_navigation_node_is_counted_once():
    # D6 (round 2): <summary role="tab"> is discovered by both the tab loop
    # and the summary loop; inside navigation it must be excluded — and
    # counted — exactly once, not once per discovery loop.
    root = parse_html(
        "<header class='navbar'><details><summary role='tab'>Menu</summary></details></header>"
        "<main><summary role='tab'>Content</summary></main>"
    )
    plan = plan_candidates(root)

    assert sorted(c.label for c in plan.candidates) == ["Content", "Content"]
    assert all(c.is_navigation is False for c in plan.candidates)
    assert plan.limits["navigation_excluded"] == 1


def test_role_tab_in_navigation_is_excluded_and_counted():
    # The role=tab loop must skip nodes inside navigation exactly like the
    # sweep does; no candidate may ever come out with is_navigation=True.
    root = parse_html(
        "<header><button role='tab'>Home tab</button></header>"
        "<main><button role='tab'>Main tab</button></main>"
    )
    plan = plan_candidates(root)

    assert [c.label for c in plan.candidates] == ["Main tab"]
    assert all(c.is_navigation is False for c in plan.candidates)
    assert plan.limits["navigation_excluded"] == 1


def test_bootstrap_nav_tabs_are_excluded_as_chrome():
    # Architect decision D1 (review round 1): ".nav" stays in
    # NAVIGATION_SELECTOR_HINTS for upstream parity. Accepted consequence,
    # locked here: a Bootstrap ul.nav.nav-tabs > a[data-toggle=tab] link tab is
    # chrome and is excluded, where upstream planned it as a page control.
    assert ".nav" in NAVIGATION_SELECTOR_HINTS
    root = parse_html(
        "<ul class='nav nav-tabs'><li><a data-toggle='tab' href='#a'>A</a></li></ul>"
    )
    plan = plan_candidates(root)

    assert plan.candidates == []
    assert plan.reason == "no-candidates"
    assert plan.limits["navigation_excluded"] == 1


# ---------------------------------------------------------------------------
# Hostile input: the walk ceilings fail the plan with a reason, never a crash.
# ---------------------------------------------------------------------------


def test_deep_document_fails_with_a_reason_not_a_crash():
    # 400 nested levels: the recursive walks used to raise RecursionError out
    # of plan_candidates; the plan must fail with reason="too-large" instead.
    html = "<div>" * 400 + "<button>Bottom</button>" + "</div>" * 400
    plan = plan_candidates(parse_html(html))

    assert plan.candidates == []
    assert plan.reason == "too-large"
    assert plan.limits["too_large"] is True


def test_oversized_document_fails_with_a_reason_not_a_crash():
    # More than 10 000 elements exceeds the node ceiling -> too-large.
    html = "<span></span>" * 10_001
    plan = plan_candidates(parse_html(html))

    assert plan.candidates == []
    assert plan.reason == "too-large"
    assert plan.limits["too_large"] is True


def test_documents_within_the_ceilings_still_plan():
    # Boundary: 256 levels are allowed - the button sits at depth 256.
    html = "<div>" * 255 + "<button>Bottom</button>" + "</div>" * 255
    plan = plan_candidates(parse_html(html))

    assert plan.reason == "ok"
    assert [c.label for c in plan.candidates] == ["Bottom"]


def test_reason_taxonomy_is_exactly_four_codes():
    # D3/D4: the reason is exactly one of ok / no-candidates / truncated /
    # too-large. There is no "base-only" code in this module: live versus
    # rested is brief 01d's business, not the planner's.
    ok = plan_candidates(parse_html("<main><button>Go</button></main>"))
    nothing = plan_candidates(parse_html(""))
    capped = plan_candidates(parse_html("<main><button>Go</button></main>"), max_states=0)
    deep = plan_candidates(parse_html("<div>" * 300 + "<button>Go</button>" + "</div>" * 300))

    assert ok.reason == "ok"
    assert nothing.reason == "no-candidates"
    assert capped.reason == "truncated"
    assert deep.reason == "too-large"
    assert {ok.reason, nothing.reason, capped.reason, deep.reason} == {
        "ok",
        "no-candidates",
        "truncated",
        "too-large",
    }


def test_discovery_stops_at_the_cap():
    # D4 (round 2): the cap really bounds the scan — discovery stops as soon
    # as the plan holds more than max_states validated candidates, and
    # limits["scan_stopped_at_cap"] says so. discovered reports what was
    # actually validated (cap + 1, the truncation sentinel), never a
    # full-document count.
    links = "".join(f'<a href="/p/{index}">Item {index}</a>' for index in range(2_000))
    root = parse_html(f"<main>{links}</main>")
    plan = plan_candidates(root, max_states=5)

    assert len(plan.candidates) == 5
    # Deliberate update (review round 3, D3): reason == "truncated" whenever
    # limits["truncated"] is true, even when the capped plan still has
    # candidates — "ok" is only for an untruncated plan.
    assert plan.reason == "truncated"
    assert plan.limits["truncated"] is True
    assert plan.limits["scan_stopped_at_cap"] is True
    assert plan.limits["discovered"] == 6


def test_large_document_planning_is_bounded_by_max_states():
    # D7: the id/attribute index is built once, and add() short-circuits once
    # the plan holds one candidate more than the cap (so the truncated flag
    # stays exact). Discovery stops at cap + 1 validated candidates instead of
    # verifying all 2 000 links.
    links = "".join(f'<a href="/p/{index}">Item {index}</a>' for index in range(2_000))
    root = parse_html(f"<main>{links}</main>")
    plan = plan_candidates(root, max_states=5)

    assert len(plan.candidates) == 5
    # Deliberate update (review round 3, D3): a truncated plan is
    # reason="truncated", whatever its size — "ok" is untruncated only.
    assert plan.reason == "truncated"
    assert plan.limits["truncated"] is True
    assert plan.limits["discovered"] == 6

    # Exactly cap candidates in the document is NOT a truncation.
    exact = plan_candidates(parse_html("<main><button>One</button><button>Two</button></main>"), max_states=2)
    assert exact.reason == "ok"
    assert exact.limits["truncated"] is False
    assert exact.limits["discovered"] == 2


# ---------------------------------------------------------------------------
# One priority table, and a public surface brief 01d can import.
# ---------------------------------------------------------------------------


def test_role_tab_priority_uses_the_shared_table():
    # D12: one source of truth for priorities - a role=tab from the tab loop
    # and a swept class="tab" control carry the same priority.
    root = parse_html(
        "<main>"
        "<button role='tab' id='r'>Role tab</button>"
        "<button class='tab' id='s'>Swept tab</button>"
        "</main>"
    )
    plan = plan_candidates(root)

    priorities = {c.label: c.priority for c in plan.candidates}
    # Behavioural (D7): one shared table means the two tab shapes carry the
    # same priority; the fixture test pins the literal value.
    assert priorities["Role tab"] == priorities["Swept tab"]


def test_summary_disclosure_uses_the_named_priority():
    # D5 (round 2): the <summary> priority is the named constant
    # PRIORITY_SUMMARY_DISCLOSURE, not a bare literal, and a native disclosure
    # is ordered ahead of a swept accordion control (40).
    import capturd.shots.planner as planner_module

    root = parse_html(
        "<main>"
        "<summary>Contents</summary>"
        '<button aria-expanded="false">More details</button>'
        "</main>"
    )
    plan = plan_candidates(root)

    by_label = {c.label: c for c in plan.candidates}
    assert by_label["Contents"].priority == planner_module.PRIORITY_SUMMARY_DISCLOSURE
    assert by_label["Contents"].priority < by_label["More details"].priority
    assert [c.label for c in plan.candidates] == ["Contents", "More details"]


def test_public_surface_exports_node_and_iter_elements():
    # D13: brief 01d consumes parsed trees; it must not have to import a
    # private name or an un-exported walker.
    import capturd.shots.planner as planner_module

    assert "Node" in planner_module.__all__
    assert "iter_elements" in planner_module.__all__
    root = planner_module.parse_html("<main><button>Go</button></main>")
    assert isinstance(root, planner_module.Node)
    assert [node.tag for node in planner_module.iter_elements(root)] == ["main", "button"]


# ---------------------------------------------------------------------------
# One test per control kind (brief 01b), all against inline HTML strings.
# ---------------------------------------------------------------------------


def test_link_control_is_planned_with_attribute_selector():
    root = parse_html("<main><a href=\"/pricing\">Pricing</a></main>")
    plan = plan_candidates(root)

    assert plan.reason == "ok"
    (candidate,) = plan.candidates
    assert candidate.kind == "control"
    assert candidate.label == "Pricing"
    assert candidate.selector == 'a[href="/pricing"]'
    assert candidate.selector_strategy == "attribute"
    assert candidate.priority == 80
    assert candidate.is_navigation is False


def test_button_control_is_planned_with_structural_selector():
    root = parse_html("<main><button>Do it</button></main>")
    plan = plan_candidates(root)

    (candidate,) = plan.candidates
    assert candidate.kind == "control"
    assert candidate.label == "Do it"
    assert candidate.selector == "main > button"
    assert candidate.selector_strategy == "structural"


def test_form_submit_input_is_planned():
    root = parse_html(
        "<form action=\"/signup\" method=\"post\">"
        '<input type="submit" aria-label="Create account" value="Go">'
        "</form>"
    )
    plan = plan_candidates(root)

    (candidate,) = plan.candidates
    assert candidate.kind == "control"
    assert candidate.label == "Create account"
    assert candidate.selector == 'input[aria-label="Create account"]'
    assert candidate.selector_strategy == "attribute"
    assert candidate.priority == 80
    assert candidate.target == ""


def test_value_only_submit_input_is_planned():
    # D1 (round 2): a value attribute is a label. <input type="submit"
    # value="Sign up"> with no aria-label has no text content, was classified
    # NONE and silently dropped — a main-path control must never be dropped
    # for want of a label it renders itself.
    root = parse_html(
        '<form action="/signup"><input type="submit" value="Sign up"></form>'
    )
    plan = plan_candidates(root)

    (candidate,) = plan.candidates
    assert candidate.kind == "control"
    assert candidate.label == "Sign up"
    assert candidate.is_navigation is False
    assert classify_control(root.children[0].children[0]) == (ControlKind.CONTROL, False)


def test_value_only_button_input_is_planned():
    # D1: same fallback for <input type="button"> — value labels it.
    root = parse_html('<form><input type="button" value="Retry"></form>')
    plan = plan_candidates(root)

    (candidate,) = plan.candidates
    assert candidate.kind == "control"
    assert candidate.label == "Retry"


def test_value_only_hidden_input_is_not_planned():
    # D1's boundary: the value fallback applies to input/button labels only —
    # a hidden input is not a control at all, whatever its value says.
    root = parse_html('<form><input type="hidden" value="session-token"></form>')
    plan = plan_candidates(root)

    assert plan.candidates == []
    assert plan.reason == "no-candidates"


def test_input_with_stable_attribute_uses_it_for_the_selector():
    root = parse_html(
        "<form>"
        '<input type="button" data-testid="pick-file" aria-label="Choose file">'
        "</form>"
    )
    plan = plan_candidates(root)

    (candidate,) = plan.candidates
    assert candidate.kind == "control"
    # data-testid precedes aria-label in STABLE_SELECTOR_ATTRIBUTES.
    assert candidate.selector == 'input[data-testid="pick-file"]'
    assert candidate.selector_strategy == "attribute"
    assert candidate.label == "Choose file"


def test_element_matched_only_by_structure():
    root = parse_html(
        "<main><section>"
        '<div role="button" tabindex="0">Share document</div>'
        "</section></main>"
    )
    plan = plan_candidates(root)

    (candidate,) = plan.candidates
    assert candidate.kind == "control"
    assert candidate.selector_strategy == "structural"
    assert "#" not in candidate.selector and "[" not in candidate.selector
    assert candidate.selector == "main > section > div"


# ---------------------------------------------------------------------------
# Navigation exclusion, dedupe, preference order, ordering, aria-only, empty.
# ---------------------------------------------------------------------------


def test_nav_header_footer_links_are_excluded():
    assert "footer" in NAVIGATION_SELECTOR_HINTS
    root = parse_html(
        "<nav><a href=\"/nav-link\">Nav link</a></nav>"
        "<header><a href=\"/header-link\">Header link</a></header>"
        "<footer><a href=\"/footer-link\">Footer link</a></footer>"
        "<main><a href=\"/pricing\">Pricing</a></main>"
    )
    plan = plan_candidates(root)

    assert [c.label for c in plan.candidates] == ["Pricing"]
    assert plan.candidates[0].is_navigation is False
    assert plan.limits["navigation_excluded"] == 3

    footer_link = root.children[2].children[0]
    assert classify_control(footer_link) == (ControlKind.PAGE, True)


def test_duplicate_candidates_are_deduped():
    # Deliberate update (review round 1, D10): the two identical top-level
    # subtrees no longer collapse into one ambiguous selector — each button
    # plans as its own control via a positional structural selector. Dedupe
    # still holds: every emitted (kind, selector) pair is unique.
    root = parse_html(
        "<main><button id=\"dup\" data-testid=\"dup\">Pick me</button></main>"
        "<main><button id=\"dup\" data-testid=\"dup\">Pick me</button></main>"
        "<div id=\"host\"><button id=\"solo\">Only one</button></div>"
    )
    plan = plan_candidates(root)

    selectors = [c.selector for c in plan.candidates]
    assert len(selectors) == len(set(selectors))
    assert len({(c.kind, c.selector) for c in plan.candidates}) == len(plan.candidates)
    assert plan.limits["dropped_ambiguous"] == 0
    assert sorted(c.label for c in plan.candidates) == ["Only one", "Pick me", "Pick me"]

    # One node discoverable as two kinds emits one candidate per kind: the
    # dedupe key is (kind, selector), matching planner.js.
    plan2 = plan_candidates(parse_html('<summary role="tab" id="both">Overview</summary>'))
    assert sorted(c.kind for c in plan2.candidates) == ["accordion", "tab"]
    keys2 = [(c.kind, c.selector) for c in plan2.candidates]
    assert len(keys2) == len(set(keys2)) == 2


def test_selector_preference_id_then_stable_attribute_then_structure():
    assert STABLE_SELECTOR_ATTRIBUTES.index("data-testid") < STABLE_SELECTOR_ATTRIBUTES.index(
        "aria-label"
    )
    root = parse_html(
        "<main>"
        '<button id="save" data-testid="save-ctrl" title="Keep edits">Keep</button>'
        '<input type="button" data-testid="pick-file" aria-label="Choose file">'
        "<button>Press me</button>"
        "</main>"
    )
    buttons = [n for n in iter_elements(root) if n.tag == "button"]
    inputs = [n for n in iter_elements(root) if n.tag == "input"]

    assert unique_selector(buttons[0]) == ("#save", "id")
    assert unique_selector(inputs[0]) == ('input[data-testid="pick-file"]', "attribute")
    assert unique_selector(buttons[1]) == ("main > button:nth-of-type(2)", "structural")

    plan = plan_candidates(root)
    by_label = {c.label: c for c in plan.candidates}
    # Labels come from aria-label/title before textContent; note the planner
    # skips action-word labels like "Save", so these use neutral wording.
    assert by_label["Keep edits"].selector_strategy == "id"
    assert by_label["Choose file"].selector_strategy == "attribute"
    assert by_label["Press me"].selector_strategy == "structural"


def test_plan_orders_by_priority_then_label():
    root = parse_html(
        "<main>"
        '<button id="z-ctl">Zulu act</button>'
        '<button role="tab" id="t2">Zulu tab</button>'
        '<button role="tab" id="t1">Alpha tab</button>'
        '<button aria-haspopup="true" id="m1">Open menu</button>'
        '<button aria-expanded="false" id="a1">More details</button>'
        '<a href="#footnote" id="h1">Jump</a>'
        "</main>"
    )
    plan = plan_candidates(root)

    assert [(c.priority, c.label) for c in plan.candidates] == [
        (12, "Alpha tab"),
        (12, "Zulu tab"),
        (35, "Open menu"),
        (40, "More details"),
        (50, "Jump"),
        (80, "Zulu act"),
    ]
    assert [c.id for c in plan.candidates] == [
        "01-tab",
        "02-tab",
        "03-menu",
        "04-accordion",
        "05-hash-link",
        "06-control",
    ]


def test_aria_and_role_only_controls():
    root = parse_html(
        '<div role="button" tabindex="0" aria-label="Share document"></div>'
        '<span role="button">Push</span>'
    )
    plan = plan_candidates(root)

    by_label = {c.label: c for c in plan.candidates}
    assert by_label["Share document"].kind == "control"
    assert by_label["Share document"].selector == 'div[aria-label="Share document"]'
    assert by_label["Share document"].selector_strategy == "attribute"
    assert by_label["Push"].kind == "control"
    assert by_label["Push"].selector == "span"
    assert by_label["Push"].selector_strategy == "structural"


def test_empty_document_yields_empty_plan():
    plan = plan_candidates(parse_html(""))

    assert plan.candidates == []
    assert plan.reason == "no-candidates"
    assert plan.limits["discovered"] == 0
    assert plan.limits["navigation_excluded"] == 0

    whitespace = plan_candidates(parse_html("   \n "))
    assert whitespace.candidates == []
    assert whitespace.reason == "no-candidates"
