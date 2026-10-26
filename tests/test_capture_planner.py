"""Tests for the deterministic capture planner (capturd.shots.planner)."""

from __future__ import annotations

from pathlib import Path

import pytest

from capturd.shots.planner import (
    CapturePlan,
    Candidate,
    ControlKind,
    NAVIGATION_SELECTOR_HINTS,
    STABLE_SELECTOR_ATTRIBUTES,
    Node,
    NonDocumentRootError,
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
    # D30: the exact overshoot — discovery kept exactly one candidate (cap+1).
    assert plan.limits["kept"] == 1

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


def test_visibility_and_opacity_important_are_skipped():
    # D13: "!important" is cascade priority, not a value — visibility:hidden
    # and opacity:0 hide exactly like their plain forms (three assertions:
    # the two !important forms here, the non-numeric opacity below).
    root = parse_html(
        "<main>"
        '<button style="visibility: hidden !important" id="veiled">Veiled</button>'
        '<button style="opacity: 0 !important" id="faint">Faint</button>'
        '<button id="live" aria-expanded="false">Live</button>'
        "</main>"
    )
    plan = plan_candidates(root)
    assert [c.label for c in plan.candidates] == ["Live"]


def test_non_numeric_opacity_is_not_hidden():
    # D13: opacity:abc is not a computed 0 — and parsing it must not raise.
    root = parse_html(
        "<main>"
        '<button style="opacity: abc" id="odd">Odd</button>'
        '<button id="live" aria-expanded="false">Live</button>'
        "</main>"
    )
    plan = plan_candidates(root)
    assert sorted(c.label for c in plan.candidates) == ["Live", "Odd"]


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


def test_button_inside_disabled_fieldset_is_not_planned():
    # D10: a disabled <fieldset> disables its descendant controls — the
    # "never plan an unclickable control" guarantee must cover the normal
    # form shape, not just a self-disabled control. The ancestor chain is
    # walked, so a button nested below the fieldset is disabled too.
    root = parse_html(
        "<main>"
        "<fieldset disabled><button>Go</button></fieldset>"
        "<fieldset disabled><div><button>Deep</button></div></fieldset>"
        "</main>"
    )
    plan = plan_candidates(root)
    assert plan.candidates == []
    assert plan.reason == "no-candidates"

    # The skip is fieldset-scoped: a sibling outside it is unaffected.
    outside = parse_html(
        "<main>"
        "<fieldset disabled><button>Go</button></fieldset>"
        '<button aria-expanded="false">Live</button>'
        "</main>"
    )
    live_plan = plan_candidates(outside)
    assert [c.label for c in live_plan.candidates] == ["Live"]


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


def test_lone_hyphen_id_is_escaped():
    # D6: a leading '-' is only an identifier start before an identifier-start
    # character (or '-' or an escape). A lone '-' is not a CSS identifier at
    # all — '#-' is unparseable — so the hyphen is hex-escaped: '#\\2d '
    # unescapes back to the id '-'.
    root = parse_html("<main><button id='-' aria-expanded='false'>Open</button></main>")
    plan = plan_candidates(root)

    (candidate,) = plan.candidates
    assert candidate.selector_strategy == "id"
    assert candidate.selector == "#\\2d "
    assert plan.limits["dropped_ambiguous"] == 0


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


def test_attribute_value_with_a_newline_round_trips():
    # D5: control characters in attribute values become CSS hex escapes — a
    # raw newline inside [attr="..."] is unparseable CSS. aria-label="a\nb"
    # must emit one escaped selector that verifies and matches exactly one
    # node (hex escape '\\a ' unescapes back to the newline).
    root = parse_html('<main><button aria-label="a\nb">Tap</button></main>')
    plan = plan_candidates(root)

    (candidate,) = plan.candidates
    assert candidate.selector_strategy == "attribute"
    assert candidate.selector == 'button[aria-label="a\\a b"]'
    assert plan.limits["dropped_ambiguous"] == 0


def test_id_with_a_newline_round_trips():
    # D5: the same hex escaping for ids — backslash + newline is a CSS line
    # continuation that deletes the newline, so '#a\\<newline>b' would select
    # the wrong node in a browser. '#a\\a b' round-trips exactly.
    root = parse_html('<main><button id="a\nb" aria-expanded="false">Open</button></main>')
    plan = plan_candidates(root)

    (candidate,) = plan.candidates
    assert candidate.selector_strategy == "id"
    assert candidate.selector == "#a\\a b"
    assert plan.limits["dropped_ambiguous"] == 0


def test_attribute_value_with_quote_and_bracket_round_trips():
    # D11: aria-label='a"]' — a value containing both an escaped quote and a
    # closing bracket. The generated selector must verify and match exactly
    # one node. The old parse split the value at the first "]" and read a
    # greedy (.*) that is not escape-aware, so the module generated a
    # selector its own matcher rejected.
    root = parse_html("<main><button aria-label='a\"]'>Tap</button></main>")
    plan = plan_candidates(root)

    (candidate,) = plan.candidates
    assert candidate.selector_strategy == "attribute"
    assert candidate.selector == 'button[aria-label="a\\\"]"]'
    assert plan.limits["dropped_ambiguous"] == 0

    button = root.children[0].children[0]
    assert unique_selector(button)[0] == candidate.selector
    # "matches exactly one node" — through the module's own matcher, not
    # just the index fast path.
    import capturd.shots.planner as planner_module

    assert planner_module._count_matches(root, candidate.selector) == 1
    matching = [
        node for node in iter_elements(root)
        if planner_module._match_simple(node, candidate.selector)
    ]
    assert matching == [button]


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


def test_attribute_value_with_closing_bracket_matches_on_both_paths():
    # D16: a single-part attribute selector whose value contains "]" —
    # _match_simple (per node) and _count_uncached (index) must agree on
    # which node it names. _match_simple used to find the closing bracket
    # with rest.find("]"), splitting inside the value, so the two paths
    # disagreed on exactly this shape.
    import capturd.shots.planner as planner_module

    root = parse_html(
        "<main><button aria-label='a]b'>Tap</button></main>"
        "<main><button>Other</button></main>"
    )
    tap = root.children[0].children[0]
    selector, strategy = unique_selector(tap)
    assert strategy == "attribute"
    assert selector == 'button[aria-label="a]b"]'

    index_count = planner_module._count_matches(root, selector)
    matcher_count = sum(
        1 for node in iter_elements(root) if planner_module._match_simple(node, selector)
    )
    assert index_count == 1
    assert matcher_count == index_count  # both paths agree
    assert planner_module._match_simple(tap, selector) is True


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


def test_non_document_root_is_refused():
    # D4: plan_candidates requires the document root from parse_html
    # (root.parent is None, root.tag == "#root"). A subtree must be refused
    # with a named error — never silently planned as an empty plan, which is
    # indistinguishable from "the document has nothing to interact with".
    root = parse_html("<main><div id='host'><button>Go</button></div></main>")

    with pytest.raises(NonDocumentRootError):
        plan_candidates(root.children[0])  # a subtree with a parent
    with pytest.raises(NonDocumentRootError):
        plan_candidates(Node("main", {}))  # detached, but not the synthetic root


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


def test_summary_tab_yields_one_candidate():
    # D8: a <summary role="tab"> is discoverable by both the tab loop and the
    # summary loop, but it is one node — dedupe is by selector, so it yields
    # one candidate, and the second kind stays visible in its reason text.
    root = parse_html("<main><summary role='tab' id='both'>Overview</summary></main>")
    plan = plan_candidates(root)

    (candidate,) = plan.candidates
    assert candidate.kind == "tab"
    assert "role tab" in candidate.kind_note
    assert "accordion" in candidate.kind_note
    assert "summary disclosure" in candidate.kind_note
    assert plan.limits["dropped_ambiguous"] == 0


def test_dual_role_navigation_node_is_counted_once():
    # D6 (round 2): <summary role="tab"> is discovered by both the tab loop
    # and the summary loop; inside navigation it must be excluded — and
    # counted — exactly once, not once per discovery loop. Deliberate update
    # (review round 3, D8): outside navigation the same dual-role node yields
    # ONE candidate (dedupe by selector), not one per loop.
    root = parse_html(
        "<header class='navbar'><details><summary role='tab'>Menu</summary></details></header>"
        "<main><summary role='tab'>Content</summary></main>"
    )
    plan = plan_candidates(root)

    assert [c.label for c in plan.candidates] == ["Content"]
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
    # Deliberate update (review round 3, D9): limits["too_large"] names the
    # ceiling that was exceeded instead of a bare True.
    assert plan.limits["too_large"] == "deeper than 256 levels"


def _spans_tree(count: int) -> Node:
    """Hand-built tree of ``count`` ``<span>`` elements.

    Deliberate update (review round 7, D27): ``parse_html`` refuses documents
    over the walk ceilings at the parse now (see
    test_parse_refuses_oversized_input_before_building), so the plan-level
    "too-large" path — what brief 01d hits with a live-DOM tree it built
    itself — is exercised with a tree built directly from ``Node``. Every
    assertion is kept at equal strength.
    """
    root = Node("#root", {})
    for _ in range(count):
        node = Node("span", {})
        node.parent = root
        root.children.append(node)
    return root


def test_parse_refuses_oversized_input_before_building(monkeypatch):
    # D27: the ceiling bounds the parse, not just the walk. parse_html refuses
    # a document whose serialized length exceeds _MAX_INPUT_CHARS before a
    # single node is built, and aborts the tree build once _MAX_NODES is
    # exceeded — raising the too-large error before the full tree is ever
    # materialised. Nodes built are counted, never timed.
    import capturd.shots.planner as planner_module

    built = []
    original_add = planner_module._NodeBuilder._add_element

    def counting_add(self, tag, attrs):
        built.append(tag)
        return original_add(self, tag, attrs)

    monkeypatch.setattr(planner_module._NodeBuilder, "_add_element", counting_add)

    # 100 000 elements: refused mid-build, never fully materialised.
    with pytest.raises(ValueError):
        planner_module.parse_html("<span></span>" * 100_000)
    assert len(built) <= planner_module._MAX_NODES + 1

    # A serialized document over the character ceiling is refused before a
    # single node is built at all.
    with pytest.raises(ValueError):
        planner_module.parse_html("x" * (planner_module._MAX_INPUT_CHARS + 1))
    assert len(built) <= planner_module._MAX_NODES + 1


def test_structural_verification_is_linear_in_depth(monkeypatch):
    # D28: verifying a structural path must compare the parent chain of the
    # target node directly (O(depth)) — never re-scan the document per
    # candidate (O(nodes) per selector). Counted, not timed: on a ~2 000-node
    # document with no stable attributes at all, the number of _match_chain
    # calls must stay under a bound proportional to nodes x depth. A
    # scan-everything implementation makes one call per node per candidate
    # and blows the bound immediately.
    import capturd.shots.planner as planner_module

    groups = "".join(
        f"<div class='g'><span><p>Text {n}</p><button>Go</button></span></div>"
        for n in range(500)
    )
    root = parse_html(f"<main>{groups}</main>")
    nodes = list(iter_elements(root))
    assert len(nodes) == 2_001

    depth = 0
    stack = [(root, 0)]
    while stack:
        node, level = stack.pop()
        depth = max(depth, level)
        stack.extend((child, level + 1) for child in node.children)
    assert depth == 4

    calls = 0
    original = planner_module._match_chain

    def counting_match_chain(node, selector):
        nonlocal calls
        calls += 1
        return original(node, selector)

    monkeypatch.setattr(planner_module, "_match_chain", counting_match_chain)
    # No cap: every one of the 500 controls is discovered and verified.
    plan = plan_candidates(root, max_states=len(nodes))
    assert plan.reason == "ok"
    assert len(plan.candidates) == 500
    assert calls <= len(nodes) * depth


def test_oversized_document_fails_with_a_reason_not_a_crash():
    # More than 10 000 elements exceeds the node ceiling -> too-large.
    plan = plan_candidates(_spans_tree(10_001))

    assert plan.candidates == []
    assert plan.reason == "too-large"
    # Deliberate update (review round 3, D9): the counter names the ceiling.
    assert plan.limits["too_large"] == "more than 10000 elements"


def test_too_large_reports_which_ceiling():
    # D9: limits["too_large"] carries the reason text — the ceiling that was
    # exceeded — so the operator can tell "too big" from "too deep".
    deep = plan_candidates(parse_html("<div>" * 300 + "<button>Go</button>" + "</div>" * 300))
    assert deep.reason == "too-large"
    assert deep.limits["too_large"] == "deeper than 256 levels"

    big = plan_candidates(_spans_tree(10_001))
    assert big.reason == "too-large"
    assert big.limits["too_large"] == "more than 10000 elements"


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
    # D4 (round 2), D15: the cap bounds the plan — discovery stops as soon
    # as the plan holds more than max_states kept candidates, and
    # limits["scan_stopped_at_cap"] says so. D26: kept reports the candidates
    # the plan holds (len(plan.candidates)), never the cap + 1 truncation
    # sentinel and never a full-document count.
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
    assert plan.limits["kept"] == 5


def test_large_document_planning_is_bounded_by_max_states():
    # D7: the id/attribute index is built once, and the discovery loops stop
    # once the plan holds one candidate more than the cap (so the truncated
    # flag stays exact). Discovery stops at cap + 1 kept candidates instead of
    # verifying all 2 000 links.
    links = "".join(f'<a href="/p/{index}">Item {index}</a>' for index in range(2_000))
    root = parse_html(f"<main>{links}</main>")
    plan = plan_candidates(root, max_states=5)

    assert len(plan.candidates) == 5
    # Deliberate update (review round 3, D3): a truncated plan is
    # reason="truncated", whatever its size — "ok" is untruncated only.
    assert plan.reason == "truncated"
    assert plan.limits["truncated"] is True
    # D26: kept is the true count — len(plan.candidates) — not cap + 1.
    assert plan.limits["kept"] == 5

    # Exactly cap candidates in the document is NOT a truncation.
    exact = plan_candidates(parse_html("<main><button>One</button><button>Two</button></main>"), max_states=2)
    assert exact.reason == "ok"
    assert exact.limits["truncated"] is False
    assert exact.limits["kept"] == 2


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


def test_link_control_is_planned_with_structural_selector():
    # Retargeted under locked D18 (not weakened): href left
    # STABLE_SELECTOR_ATTRIBUTES, so a link is named by structure. Every
    # assertion is kept with the same strength on the new truth — an
    # a[href="..."] selector baked signed URLs into plan artifacts.
    root = parse_html("<main><a href=\"/pricing\">Pricing</a></main>")
    plan = plan_candidates(root)

    assert plan.reason == "ok"
    (candidate,) = plan.candidates
    assert candidate.kind == "control"
    assert candidate.label == "Pricing"
    assert candidate.selector == "main > a"
    assert candidate.selector_strategy == "structural"
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


def test_carousel_control_is_classified():
    # D14: CAROUSEL is reachable (carousel-control-next is in the control
    # sweep's class set and _CAROUSEL_RE matches carousel) but nothing
    # exercised it — one test per kind makes the PR claim true.
    root = parse_html('<main><button class="carousel-control-next">Next</button></main>')
    node = root.children[0].children[0]

    assert classify_control(node) == (ControlKind.CAROUSEL, False)
    plan = plan_candidates(root)
    (candidate,) = plan.candidates
    assert candidate.kind == "carousel"
    assert candidate.priority == 60


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

    # Deliberate update (review round 3, D8): one node discoverable as two
    # kinds emits ONE candidate — the dedupe key is the selector — and the
    # second kind stays visible in its reason text, never as a second row.
    plan2 = plan_candidates(parse_html('<summary role="tab" id="both">Overview</summary>'))
    (only,) = plan2.candidates
    assert only.selector == "#both"
    assert "accordion" in only.kind_note
    selectors2 = [c.selector for c in plan2.candidates]
    assert len(selectors2) == len(set(selectors2)) == 1


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
    assert plan.limits["kept"] == 0
    assert plan.limits["navigation_excluded"] == 0

    whitespace = plan_candidates(parse_html("   \n "))
    assert whitespace.candidates == []
    assert whitespace.reason == "no-candidates"


# ---------------------------------------------------------------------------
# Review round 6: the truth layer — D17 (inline style is last-wins) and D18
# (href is not a stable selector attribute; no credential-bearing URL ever
# enters a plan artifact).
# ---------------------------------------------------------------------------


def test_style_declares_is_last_wins():
    # D17: _style_declares scans every declaration for the property and
    # compares the LAST match, because CSS is last-wins. This sits in the gate
    # that decides what gets planned: an inversion silently plans dead controls
    # or drops live ones. Both directions:
    #   style="display:none;display:block" -> visible  -> planned
    #   style="display:block;display:none" -> hidden   -> not planned
    root = parse_html(
        "<main>"
        '<button style="display:none;display:block" id="reshown">Reshown</button>'
        '<button style="display:block;display:none" id="rehidden">Rehidden</button>'
        "</main>"
    )
    plan = plan_candidates(root)
    assert [c.label for c in plan.candidates] == ["Reshown"]


def test_href_is_not_a_stable_attribute():
    # D18: href leaves STABLE_SELECTOR_ATTRIBUTES — the structural path
    # already disambiguates a link, and a[href="...?token=...&sig=..."] bakes
    # signed URLs and credential-bearing query strings into plan artifacts
    # that brief 01d writes to disk. A link with ?token=secret produces a
    # selector containing no token, and neither does the serialized plan
    # (selector, label fallback and target all included).
    assert "href" not in STABLE_SELECTOR_ATTRIBUTES
    root = parse_html(
        "<main>"
        '<a href="/x?token=secret">Open</a>'
        '<a role="tab" href="/y?token=secret"></a>'
        "</main>"
    )
    plan = plan_candidates(root)

    assert plan.reason == "ok"
    assert len(plan.candidates) == 2
    for candidate in plan.candidates:
        assert "token" not in candidate.selector
        assert candidate.selector_strategy == "structural"
        assert plan.limits["dropped_ambiguous"] == 0
    assert "token" not in repr(plan)


# ---------------------------------------------------------------------------
# Review round 6: D19 (base_url is deleted — a parameter that is accepted and
# ignored is a silent no-op) and D20 (the first <legend> of a disabled
# <fieldset> stays interactive, per HTML).
# ---------------------------------------------------------------------------


def test_base_url_is_gone():
    # D19: base_url was accepted and ignored — a silent no-op that reads as a
    # planner bug in the layer above. It is deleted from the signature; a
    # caller passing it fails loudly with TypeError instead of being ignored.
    # Brief 01d gets it back when 01d actually resolves links.
    import inspect

    assert "base_url" not in inspect.signature(plan_candidates).parameters
    root = parse_html("<main><button>Go</button></main>")
    with pytest.raises(TypeError):
        plan_candidates(root, base_url="https://example.com")


def test_legend_controls_are_planned():
    # D20: per HTML, controls inside the first <legend> subtree of a disabled
    # <fieldset> are NOT disabled — the fieldset does not disable them. A
    # control anywhere else in the same fieldset (including a second legend)
    # is disabled and never planned.
    root = parse_html(
        "<main>"
        "<fieldset disabled>"
        "<legend><button>Legend top</button><div><button>Legend deep</button></div></legend>"
        "<legend><button>Second legend</button></legend>"
        "<button>Outside legend</button>"
        "</fieldset>"
        "</main>"
    )
    plan = plan_candidates(root)

    assert sorted(c.label for c in plan.candidates) == ["Legend deep", "Legend top"]
    assert all(c.kind == "control" for c in plan.candidates)
    assert plan.reason == "ok"


# ---------------------------------------------------------------------------
# Review round 6: D21 — attribute values are compared case-insensitively
# where HTML says so (role, type, aria-* keywords).
# ---------------------------------------------------------------------------


def test_attribute_values_are_case_insensitive():
    # D21: <input TYPE="Submit">, role="TAB" and aria-hidden="TRUE" must read
    # exactly as their lowercase forms — HTML makes these keyword values
    # ASCII-case-insensitive. All three directions:
    root = parse_html(
        "<main>"
        '<input TYPE="Submit" value="Continue">'
        '<button role="TAB" id="t">Tabs</button>'
        '<button aria-hidden="TRUE" id="h">Ghost</button>'
        "</main>"
    )
    plan = plan_candidates(root)

    # TYPE="Submit" reads as type="submit" -> swept and planned;
    # aria-hidden="TRUE" reads as aria-hidden="true" -> hidden, not planned.
    assert [c.label for c in plan.candidates] == ["Tabs", "Continue"]
    by_label = {c.label: c for c in plan.candidates}
    assert by_label["Continue"].kind == "control"
    assert by_label["Tabs"].kind == "tab"

    # role="TAB" reads as role="tab" in the classifier too.
    tab_node = next(n for n in iter_elements(root) if n.attrs.get("id") == "t")
    assert classify_control(tab_node) == (ControlKind.TAB, False)


# ---------------------------------------------------------------------------
# Review round 6: D22 — the per-candidate note is kind_note, so it can never
# be confused with CapturePlan.reason, whose domain is exactly four codes.
# ---------------------------------------------------------------------------


def test_candidate_note_is_not_a_plan_reason():
    # D22: the per-candidate field is renamed kind_note; CapturePlan.reason is
    # typed as a Literal over ok | no-candidates | truncated | too-large. The
    # note is never read as a plan status.
    import dataclasses
    import typing

    statuses = typing.get_args(typing.get_type_hints(CapturePlan)["reason"])
    assert set(statuses) == {"ok", "no-candidates", "truncated", "too-large"}

    field_names = {f.name for f in dataclasses.fields(Candidate)}
    assert "kind_note" in field_names
    assert "reason" not in field_names

    root = parse_html("<main><summary role='tab' id='both'>Overview</summary></main>")
    plan = plan_candidates(root)
    (candidate,) = plan.candidates
    assert plan.reason == "ok"
    assert plan.reason in statuses
    # The note folds two discovery kinds into one line of text — it is not
    # one of the four statuses and can never be read as one.
    assert candidate.kind_note not in statuses
    assert "role tab" in candidate.kind_note
    assert "accordion" in candidate.kind_note


# ---------------------------------------------------------------------------
# Review round 6: D23 — .menu is a navigation exclusion and the module
# docstring says so in the same paragraph as .nav.
# ---------------------------------------------------------------------------


def test_menu_is_a_documented_navigation_exclusion():
    # D23: dropdown menus are navigation in this product, so excluding
    # ul.menu is right — but the accepted-limitation paragraph must name
    # .menu alongside .nav, not understate the exclusion.
    import capturd.shots.planner as planner_module

    assert ".menu" in NAVIGATION_SELECTOR_HINTS
    nav_paragraph = next(
        part for part in (planner_module.__doc__ or "").split("\n\n") if ".nav" in part
    )
    assert ".menu" in nav_paragraph

    root = parse_html("<ul class='menu'><li><a href='/settings'>Settings</a></li></ul>")
    plan = plan_candidates(root)
    assert plan.candidates == []
    assert plan.reason == "no-candidates"
    assert plan.limits["navigation_excluded"] == 1


# ---------------------------------------------------------------------------
# Review round 7: the limits layer — the ceilings must bound what they claim.
# ---------------------------------------------------------------------------


def test_scan_stopped_at_cap_tracks_truncated_exactly():
    # D24: scan_stopped_at_cap is true exactly when truncated is true. It used
    # to be set only when a discovery loop re-entered and broke, so a plan
    # whose overflow node was the LAST element of its discovery list reported
    # truncated=True with scan_stopped_at_cap=False — the exact case the flag
    # exists to disambiguate.

    # 1. Overshoot mid-list: discovery stops early — True.
    links = "".join(f'<a href="/p/{index}">Item {index}</a>' for index in range(200))
    mid = plan_candidates(parse_html(f"<main>{links}</main>"), max_states=5)
    assert mid.limits["truncated"] is True
    assert mid.limits["scan_stopped_at_cap"] is True

    # 2. Overshoot on the LAST node of a discovery list: the overflow node is
    #    the swept list's last element, so no loop re-enters and breaks — the
    #    flag must still be True.
    last = plan_candidates(
        parse_html("<main><button>Alpha</button><button>Beta</button><button>Gamma</button></main>"),
        max_states=2,
    )
    assert last.limits["truncated"] is True
    assert last.limits["scan_stopped_at_cap"] is True

    # 3. Exactly cap kept: not truncated — False.
    exact = plan_candidates(
        parse_html("<main><button>Alpha</button><button>Beta</button></main>"),
        max_states=2,
    )
    assert exact.limits["truncated"] is False
    assert exact.limits["scan_stopped_at_cap"] is False


def test_exactly_cap_is_not_truncated():
    # D25: a document with exactly N eligible candidates and max_states=N is
    # NOT truncated — reason "ok". This is the whole rationale for overshooting
    # the cap by one; an off-by-one from '>' to '>=' in the truncation test
    # must fail this test.
    html = "<main>" + "".join(f"<button>Item {n}</button>" for n in range(1, 4)) + "</main>"
    plan = plan_candidates(parse_html(html), max_states=3)

    assert plan.reason == "ok"
    assert plan.limits["truncated"] is False
    assert plan.limits["scan_stopped_at_cap"] is False
    assert len(plan.candidates) == 3


def test_kept_equals_len_candidates():
    # D26: limits["kept"] is the true count — len(plan.candidates) — on a
    # capped and an uncapped plan. It used to report cap + 1 (the truncation
    # sentinel) while plan.candidates held cap, under a name that says
    # otherwise; the sentinel is internal bookkeeping and is never counted.
    root = _load("planner_tabs_accordion_modal.html")

    capped = plan_candidates(root, max_states=2)
    assert capped.limits["kept"] == len(capped.candidates) == 2
    uncapped = plan_candidates(root)
    assert uncapped.limits["kept"] == len(uncapped.candidates) == 4

    # D30: max_states=0 asserts the exact overshoot — the discovery kept
    # exactly one candidate (cap + 1) — not a lower bound.
    zero = plan_candidates(root, max_states=0)
    assert zero.limits["kept"] == 1
