"""Tests for the deterministic capture planner (capturd.shots.planner)."""

from __future__ import annotations

from pathlib import Path

from capturd.shots.planner import (
    ControlKind,
    NAVIGATION_SELECTOR_HINTS,
    STABLE_SELECTOR_ATTRIBUTES,
    _count_matches,
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
    assert [c.priority for c in plan.candidates] == [10, 10, 25, 30]
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
        for candidate in plan.candidates:
            assert _count_matches(root, candidate.selector) == 1, candidate.selector


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
        assert _count_matches(root, candidate.selector) == 1
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


def test_max_states_zero_is_a_failed_plan():
    root = _load("planner_tabs_accordion_modal.html")
    plan = plan_candidates(root, max_states=0)

    assert plan.candidates == []
    assert plan.reason == "no-candidates"


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
    assert _count_matches(root, candidate.selector) == 1


def test_leading_dash_digit_id_is_css_escaped():
    # An identifier may start with '-' but not with '-' followed by a digit.
    root = parse_html("<main><button id='-1foo' aria-expanded='false'>Open</button></main>")
    node = root.children[0].children[0]

    selector, strategy = unique_selector(node)
    assert strategy == "id"
    assert selector == "#-\\31 foo"
    assert _count_matches(root, selector) == 1


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
    for candidate in plan.candidates:
        assert _count_matches(root, candidate.selector) == 1
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
    for candidate in plan.candidates:
        assert _count_matches(root, candidate.selector) == 1


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
    assert _count_matches(root, candidate.selector) == 1


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
    assert _count_matches(root, candidate.selector) == 1


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
    for candidate in plan.candidates:
        assert _count_matches(root, candidate.selector) == 1

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
        (10, "Alpha tab"),
        (10, "Zulu tab"),
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
