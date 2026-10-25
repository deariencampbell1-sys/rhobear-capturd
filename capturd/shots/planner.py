"""Deterministic capture planner: document -> ordered candidate interaction targets.

Pure planning only: this module never clicks, never screenshots, never touches
the network. Its canonical input is an HTML string (or a node tree produced by
:func:`parse_html`), parsed with a small ``html.parser.HTMLParser`` subclass —
stdlib only. Like ``capturd.shots.capture``, it is intentionally usable without
Playwright installed: there is no Playwright import anywhere in this module, not
even inside a function.

Honest limitation: nodes that only enter the DOM after interaction (accordion
panels, modal bodies, dynamically injected tabs) are not present in the input
document. The planner therefore plans from the *rested* document; the
live-clicking layer (brief 01d, ``capturd.shots.capture``) reuses this same pure
code against the live DOM rather than keeping a second implementation.

Everything here is deterministic: identical HTML in produces an identical
``CapturePlan`` out, byte for byte under ``repr``. All ordering comes from
document order and explicit sorts — never from set iteration.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from typing import Iterator, Optional

__all__ = [
    "ControlKind",
    "Candidate",
    "CapturePlan",
    "NAVIGATION_SELECTOR_HINTS",
    "STABLE_SELECTOR_ATTRIBUTES",
    "classify_control",
    "parse_html",
    "plan_candidates",
]


# ---------------------------------------------------------------------------
# Minimal node tree
# ---------------------------------------------------------------------------


class _Node:
    """One element of the parsed document."""

    __slots__ = ("tag", "attrs", "children", "parent", "text_chunks")

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node] = []
        self.parent: Optional[_Node] = None
        self.text_chunks: list[str] = []

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<_Node {self.tag} attrs={sorted(self.attrs.items())!r} children={len(self.children)}>"


class _NodeBuilder(HTMLParser):
    """Builds a minimal element tree; element children only, text kept as chunks."""

    _VOID = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {})
        self._stack = [self.root]

    def _add_element(self, tag: str, attrs: list[tuple[str, str | None]]) -> _Node:
        cleaned: dict[str, str] = {}
        for key, value in attrs:
            key = key.lower()
            if key not in cleaned:  # first occurrence wins, like getAttribute
                cleaned[key] = value if value is not None else ""
        node = _Node(tag.lower(), cleaned)
        top = self._stack[-1]
        node.parent = top
        top.children.append(node)
        return node

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = self._add_element(tag, attrs)
        if tag.lower() not in self._VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._add_element(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for depth in range(len(self._stack) - 1, 0, -1):
            if self._stack[depth].tag == tag:
                del self._stack[depth:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].text_chunks.append(data)


def parse_html(html: str) -> _Node:
    """Parse an HTML string into a tree; returns the synthetic ``#root`` node."""
    builder = _NodeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def _tree_root(node: _Node) -> _Node:
    while node.parent is not None:
        node = node.parent
    return node


def iter_elements(root: _Node) -> Iterator[_Node]:
    """Pre-order (document order) walk over element nodes, excluding the root."""
    for child in root.children:
        yield child
        yield from iter_elements(child)


# ---------------------------------------------------------------------------
# Text helpers (planner.js textOf / targetSummary)
# ---------------------------------------------------------------------------


def _full_text(node: _Node) -> str:
    parts = list(node.text_chunks)
    for child in node.children:
        parts.append(_full_text(child))
    return "".join(parts)


def _text_of(node: _Node) -> str:
    """aria-label / title / data-title / data-label / textContent, first non-empty."""
    for value in (
        node.attrs.get("aria-label"),
        node.attrs.get("title"),
        node.attrs.get("data-title"),
        node.attrs.get("data-label"),
        _full_text(node),
    ):
        if value and value.strip():
            return " ".join(value.split())[:80]
    return ""


def _classes(node: _Node) -> list[str]:
    return node.attrs.get("class", "").split()


# ---------------------------------------------------------------------------
# Unique selectors (planner.js uniqueSelector / queryCount)
# ---------------------------------------------------------------------------

STABLE_SELECTOR_ATTRIBUTES: tuple[str, ...] = (
    "data-testid",
    "data-test",
    "data-cy",
    "data-qa",
    "data-tab",
    "data-page",
    "data-view",
    "data-section",
    "data-target",
    "data-bs-target",
    "data-controls",
    "aria-label",
    "aria-controls",
    "title",
    "href",
    "name",
)


def _escape_css(value: str) -> str:
    escaped = re.sub(r"[^a-zA-Z0-9_-]", lambda m: "\\" + m.group(0), value)
    # CSSOM: an identifier cannot start with a digit, nor with '-' followed by
    # a digit; such a digit is escaped as a hex code point plus a terminating
    # space, e.g. "1foo" -> "\\31 foo", so querySelectorAll accepts the selector.
    match = _LEADING_DIGIT_RE.match(escaped)
    if match:
        dash, digit = match.group(1), match.group(2)
        escaped = f"{dash}\\{format(ord(digit), 'x')} {escaped[match.end():]}"
    return escaped


_CSS_ESCAPE_RE = re.compile(r"\\([0-9a-fA-F]{1,6})(?:\r\n|[ \t\r\n\f])?|\\(.)", re.DOTALL)


def _css_unescape(value: str) -> str:
    def _replace(match):
        if match.group(1) is not None:
            return chr(int(match.group(1), 16))
        return match.group(2)

    return _CSS_ESCAPE_RE.sub(_replace, value)


#: Characters that terminate a bare (unescaped) CSS identifier fragment.
_CSS_IDENT_END = " [:"


def _scan_ident(text: str) -> tuple[str, str]:
    """Scan one identifier that may contain backslash escapes.

    Returns ``(raw_ident, remainder)`` with the identifier still escaped. A hex
    escape's terminating whitespace belongs to the escape, so ``\\31 foo`` scans
    as one identifier for the id ``1foo`` instead of stopping at the space.
    """
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            match = _CSS_ESCAPE_RE.match(text, index)
            index = match.end() if match else min(index + 2, len(text))
            continue
        if char in _CSS_IDENT_END:
            break
        index += 1
    return text[:index], text[index:]


_TAG_RE = re.compile(r"^(\*|[a-zA-Z][a-zA-Z0-9-]*)")
_ATTR_RE = re.compile(r'^([\w-]+)="(.*)"$', re.DOTALL)
_NTH_PREFIX = ":nth-of-type("
_LEADING_DIGIT_RE = re.compile(r"^(-?)([0-9])")
_LEADING_DIGIT_RE = re.compile(r"^(-?)([0-9])")


def _match_simple(node: _Node, part: str) -> bool:
    rest = part
    tag: Optional[str] = None
    match = _TAG_RE.match(rest)
    if match:
        tag = match.group(1)
        rest = rest[match.end():]

    if rest.startswith("#"):
        ident, rest = _scan_ident(rest[1:])
        if node.attrs.get("id") != _css_unescape(ident):
            return False

    while rest.startswith("["):
        close = rest.find("]")
        if close < 0:
            return False
        inner = rest[1:close]
        rest = rest[close + 1:]
        attr_match = _ATTR_RE.match(inner)
        if not attr_match:
            return False
        if node.attrs.get(attr_match.group(1)) != _css_unescape(attr_match.group(2)):
            return False

    nth: Optional[int] = None
    if rest.startswith(_NTH_PREFIX):
        if not rest.endswith(")"):
            return False
        try:
            nth = int(rest[len(_NTH_PREFIX):-1])
        except ValueError:
            return False
        rest = ""
    if rest:
        return False  # unparseable fragment -> no match, like querySelectorAll throwing

    if tag and tag != "*" and node.tag != tag:
        return False

    if nth is not None:
        parent = node.parent
        if parent is None:
            return False
        same_tag = [child for child in parent.children if child.tag == node.tag]
        if nth < 1 or node not in same_tag or same_tag.index(node) + 1 != nth:
            return False
    return True


def _split_chain(selector: str) -> list[str]:
    """Split a ``a > b > c`` direct-child chain on its unquoted ``>`` separators.

    Quote-aware: an attribute value containing ``>`` — ``aria-label="Home >
    Docs"`` — stays inside its own part instead of splitting the chain into
    garbage.
    """
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    escaped = False
    index = 0
    while index < len(selector):
        char = selector[index]
        if escaped:
            current.append(char)
            escaped = False
            index += 1
        elif in_quotes and char == "\\":
            current.append(char)
            escaped = True
            index += 1
        elif char == '"':
            in_quotes = not in_quotes
            current.append(char)
            index += 1
        elif (
            not in_quotes
            and char == ">"
            and current
            and current[-1] == " "
            and index + 1 < len(selector)
            and selector[index + 1] == " "
        ):
            current.pop()  # the separator space before '>'
            parts.append("".join(current))
            current = []
            index += 2  # skip '>' and the separator space after it
        else:
            current.append(char)
            index += 1
    parts.append("".join(current))
    return parts


def _match_chain(node: _Node, selector: str) -> bool:
    """Match a ``a > b > c`` direct-child chain; node must match the last part."""
    parts = _split_chain(selector)
    if not parts or not _match_simple(node, parts[-1]):
        return False
    current = node
    for part in reversed(parts[:-1]):
        current = current.parent
        if current is None or current.tag == "#root" or not _match_simple(current, part):
            return False
    return True


def _count_matches(root: _Node, selector: str) -> int:
    """queryCount: 0 for invalid/empty selectors, else number of matching nodes."""
    if not selector:
        return 0
    return sum(1 for node in iter_elements(root) if _match_chain(node, selector))


def _is_unique(root: _Node, selector: str) -> bool:
    return _count_matches(root, selector) == 1


def _structural_path(node: _Node) -> str:
    """Positional path. A unique id anywhere on the chain anchors and shortens
    it; a *duplicate* id is omitted in favour of ``:nth-of-type`` so the path
    can still disambiguate (rule: never emit an ambiguous selector).
    ``:nth-of-type`` applies at every level including children of the synthetic
    ``#root`` — brief 01d feeds fragments with several top-level controls."""
    root = _tree_root(node)
    parts: list[str] = []
    current: Optional[_Node] = node
    while current is not None and current.tag not in ("#root", "html"):
        part = current.tag
        node_id = current.attrs.get("id")
        if node_id and sum(1 for n in iter_elements(root) if n.attrs.get("id") == node_id) == 1:
            parts.insert(0, f"{part}#{_escape_css(node_id)}")
            break
        parent = current.parent
        if parent is not None:
            same_tag = [child for child in parent.children if child.tag == current.tag]
            if len(same_tag) > 1:
                part += f"{_NTH_PREFIX}{same_tag.index(current) + 1})"
        parts.insert(0, part)
        current = parent
    return " > ".join(parts)


def unique_selector(node: _Node) -> tuple[Optional[str], str]:
    """Best selector for ``node``: ``#id``, then a stable attribute, then structure.

    Returns ``(selector, strategy)`` with strategy one of ``id`` / ``attribute`` /
    ``structural``. The ``structural`` fallback is not guaranteed unique on its
    own — callers verify with :func:`_is_unique` and drop ambiguous candidates.
    """
    root = _tree_root(node)
    node_id = node.attrs.get("id")
    if node_id:
        selector = f"#{_escape_css(node_id)}"
        if _count_matches(root, selector) == 1:
            return selector, "id"
    for attr in STABLE_SELECTOR_ATTRIBUTES:
        value = node.attrs.get(attr)
        if not value:
            continue
        selector = f'{node.tag}[{attr}="{value.replace(chr(34), chr(92) + chr(34))}"]'
        if _count_matches(root, selector) == 1:
            return selector, "attribute"
    path = _structural_path(node)
    if path:
        return path, "structural"
    return None, "structural"


# ---------------------------------------------------------------------------
# Static visibility / disabled approximation (planner.js isVisible / isDisabled)
# ---------------------------------------------------------------------------


def _style_declares(node: _Node, prop: str, value: str) -> bool:
    style = node.attrs.get("style", "")
    for declaration in style.split(";"):
        if ":" not in declaration:
            continue
        name, _, val = declaration.partition(":")
        if name.strip().lower() == prop:
            val = val.strip().lower()
            # A trailing "!important" is cascade priority, not a value: strip it
            # before comparing so "display: none !important" hides like "none".
            val = re.sub(r"!\s*important$", "", val).strip()
            if prop == "opacity":
                try:
                    return float(val) == 0.0
                except ValueError:
                    return False
            return val == value
    return False


def _is_visible(node: _Node) -> bool:
    """Attribute-level approximation of computed-style visibility.

    Static HTML carries no computed styles or layout boxes, so this checks the
    node and its ancestors for ``hidden``, ``aria-hidden="true"`` and inline
    ``display:none`` / ``visibility:hidden`` / ``opacity:0``. Rect-size checks
    from planner.js are impossible without a browser and are intentionally
    omitted; the live-clicking layer re-checks visibility on the real DOM.
    """
    current: Optional[_Node] = node
    while current is not None and current.tag != "#root":
        if "hidden" in current.attrs:
            return False
        if current.attrs.get("aria-hidden") == "true":
            return False
        if _style_declares(current, "display", "none"):
            return False
        if _style_declares(current, "visibility", "hidden"):
            return False
        if _style_declares(current, "opacity", "0"):
            return False
        current = current.parent
    return True


def _is_disabled(node: _Node) -> bool:
    if node.tag in ("button", "input", "select", "textarea", "optgroup", "option", "fieldset"):
        if "disabled" in node.attrs:
            return True
    if node.attrs.get("aria-disabled") == "true":
        return True
    if "disabled" in _classes(node):
        return True
    return False


# ---------------------------------------------------------------------------
# Control classification (planner.js lines 160-215)
# ---------------------------------------------------------------------------


class ControlKind(enum.Enum):
    """Kinds of interaction targets the planner classifies. Never fabricated:
    every kind comes from a rule that matched the DOM."""

    TAB = "tab"
    PAGE = "page"
    MODAL = "modal"
    MENU = "menu"
    ACCORDION = "accordion"
    HASH_LINK = "hash-link"
    CAROUSEL = "carousel"
    CONTROL = "control"
    NONE = "none"


#: Containers whose links are navigation, not interaction states (planner.js
#: ``element.closest("nav, aside, header, [role='navigation'], .sidebar, ...")``;
#: brief 01b adds ``footer`` — site-footer links are navigation too).
NAVIGATION_SELECTOR_HINTS: tuple[str, ...] = (
    "nav",
    "aside",
    "header",
    "footer",
    "[role='navigation']",
    ".sidebar",
    ".side-bar",
    ".nav",
    ".navbar",
    ".topbar",
    ".top-bar",
    ".menu",
)

_TAB_RE = re.compile(r"\b(tab|tabs|pill)\b")
_NAV_RE = re.compile(r"\b(nav|sidebar|route|page|view|screen)\b")
_MODAL_RE = re.compile(r"\b(modal|dialog|popup)\b")
_MENU_RE = re.compile(r"\b(menu|dropdown|popover|drawer)\b")
_ACCORDION_RE = re.compile(r"\b(accordion|collapse|disclosure|details|faq|expand|toggle)\b")
_CAROUSEL_RE = re.compile(r"\b(next|previous|prev|slide|carousel)\b")
_ACTION_RE = re.compile(r"\b(close|cancel|delete|remove|clear|reset|submit|save|download|print)\b")


def _hint_match(node: _Node, hint: str) -> bool:
    if hint.startswith("."):
        return hint[1:] in _classes(node)
    if hint.startswith("[") and hint.endswith("]"):
        inner = hint[1:-1]
        name, _, value = inner.partition("=")
        value = value.strip().strip("'\"")
        return node.attrs.get(name.strip()) == value
    return node.tag == hint


def _in_navigation(node: _Node) -> bool:
    current: Optional[_Node] = node
    while current is not None and current.tag != "#root":
        for hint in NAVIGATION_SELECTOR_HINTS:
            if _hint_match(current, hint):
                return True
        current = current.parent
    return False


def _in_control_sweep(node: _Node) -> bool:
    """The generic control sweep from planner.js, as a predicate."""
    tag = node.tag
    attrs = node.attrs
    if tag == "button":
        return True
    if tag == "input" and attrs.get("type") in ("button", "submit"):
        return True
    if tag == "a" and "href" in attrs:
        return True
    if tag in ("a", "button") and any(
        _hint_match(ancestor, container)
        for ancestor in _ancestors_inclusive(node)
        for container in ("nav", "aside", "header")
    ):
        return True
    if attrs.get("role") in ("button", "link"):
        return True
    for attr in (
        "aria-controls", "aria-expanded", "aria-current", "aria-haspopup",
        "data-tab", "data-page", "data-view", "data-section", "data-target",
        "data-bs-target", "data-toggle", "data-bs-toggle",
        "data-micromodal-trigger",
    ):
        if attr in attrs:
            return True
    classes = set(_classes(node))
    if classes & {"tab", "dropdown-toggle", "carousel-control-next", "carousel-control-prev"}:
        return True
    return False


def _ancestors_inclusive(node: _Node) -> Iterator[_Node]:
    current: Optional[_Node] = node
    while current is not None and current.tag != "#root":
        yield current
        current = current.parent


def _classify_sweep(node: _Node) -> Optional[ControlKind]:
    """Priority-ordered classification chain; first match wins (planner.js)."""
    attrs = node.attrs
    tag = node.tag
    role = attrs.get("role", "")
    aria_expanded = attrs.get("aria-expanded")
    aria_current = attrs.get("aria-current")
    aria_has_popup = attrs.get("aria-haspopup")
    data_toggle = (attrs.get("data-bs-toggle") or attrs.get("data-toggle") or "").lower()
    target = (
        attrs.get("data-bs-target") or attrs.get("data-target") or attrs.get("href") or ""
    ).lower()
    class_text = attrs.get("class", "").lower()
    label = _text_of(node).lower()
    combined = f"{role} {data_toggle} {target} {class_text} {label}"
    navigation = _in_navigation(node)

    if role == "tab" or "data-tab" in attrs or _TAB_RE.search(combined):
        return ControlKind.TAB
    if (
        navigation
        or aria_current is not None
        or "data-page" in attrs
        or "data-view" in attrs
        or "data-section" in attrs
        or _NAV_RE.search(combined)
    ):
        return ControlKind.PAGE
    if (
        "modal" in data_toggle
        or "modal" in target
        or "data-micromodal-trigger" in attrs
        or _MODAL_RE.search(combined)
    ):
        return ControlKind.MODAL
    if aria_has_popup is not None or "dropdown" in data_toggle or _MENU_RE.search(combined):
        return ControlKind.MENU
    if aria_expanded is not None or _ACCORDION_RE.search(combined):
        return ControlKind.ACCORDION
    if tag == "a" and target.startswith("#") and len(target) > 1:
        return ControlKind.HASH_LINK
    if _CAROUSEL_RE.search(combined):
        return ControlKind.CAROUSEL
    if len(label) >= 2 and not _ACTION_RE.search(combined):
        return ControlKind.CONTROL
    return None


def classify_control(node: Optional[_Node]) -> tuple[ControlKind, bool]:
    """Classify one node: ``(kind, is_navigation)``. ``NONE`` for non-controls."""
    if node is None or node.tag == "#root":
        return ControlKind.NONE, False
    if node.attrs.get("role") == "tab":
        return ControlKind.TAB, _in_navigation(node)
    if node.tag == "summary":
        return ControlKind.ACCORDION, _in_navigation(node)
    if not _in_control_sweep(node):
        return ControlKind.NONE, _in_navigation(node)
    kind = _classify_sweep(node)
    return (kind or ControlKind.NONE), _in_navigation(node)


# ---------------------------------------------------------------------------
# Planning (planner.js buildCapturePlan)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One candidate interaction target, ready for capture to click."""

    id: str
    selector: str
    selector_strategy: str
    kind: str
    label: str
    priority: int
    is_navigation: bool
    reason: str
    target: str = ""
    target_visible: Optional[bool] = None


@dataclass
class CapturePlan:
    """The planned capture states. ``candidates == []`` with a reason code is a
    *failure of the plan* — the planner never fabricates states it did not
    classify from the DOM."""

    candidates: list[Candidate] = field(default_factory=list)
    reason: str = "ok"
    limits: dict = field(default_factory=dict)


_PRIORITY_BY_KIND = {
    ControlKind.TAB: 12,
    ControlKind.PAGE: 20,
    ControlKind.MODAL: 30,
    ControlKind.MENU: 35,
    ControlKind.ACCORDION: 40,
    ControlKind.HASH_LINK: 50,
    ControlKind.CAROUSEL: 60,
    ControlKind.CONTROL: 80,
}


def _target_summary(root: _Node, node: _Node) -> tuple[str, Optional[bool]]:
    controls = next(
        (
            value
            for value in (
                node.attrs.get("aria-controls"),
                node.attrs.get("data-controls"),
                node.attrs.get("data-target"),
                node.attrs.get("data-bs-target"),
                node.attrs.get("href"),
            )
            if value
        ),
        "",
    )
    if not controls or not controls.startswith("#"):
        return controls, None
    wanted = controls[1:]
    for candidate in iter_elements(root):
        if candidate.attrs.get("id") == wanted:
            return controls, _is_visible(candidate)
    return controls, None


def plan_candidates(root: _Node, base_url: Optional[str] = None, max_states: int = 24) -> CapturePlan:
    """Turn a parsed document into an ordered, deduped candidate list.

    Deterministic: document order drives discovery, the final order is
    ``(priority, label)``, ties keep insertion order, and everything is sorted —
    never set iteration. ``base_url`` is accepted for the live-clicking layer
    (brief 01d) and does not affect the pure plan.
    """
    nodes = list(iter_elements(root))
    tabs = [n for n in nodes if n.attrs.get("role") == "tab"]
    summaries = [n for n in nodes if n.tag == "summary"]
    swept = [n for n in nodes if n.attrs.get("role") != "tab" and n.tag != "summary" and _in_control_sweep(n)]

    candidates: list[Candidate] = []
    seen: set[str] = set()
    dropped_ambiguous = 0
    navigation_excluded = 0

    def add(node: _Node, kind: ControlKind, reason: str, priority: int) -> None:
        nonlocal dropped_ambiguous, navigation_excluded
        if not _is_visible(node) or _is_disabled(node):
            return
        selector, strategy = unique_selector(node)
        if selector is None or not _is_unique(root, selector):
            dropped_ambiguous += 1
            return
        label = _text_of(node) or node.attrs.get("aria-controls") or node.attrs.get("href") or kind.value
        target, target_visible = _target_summary(root, node)
        key = f"{kind.value}|{selector}"
        if key in seen:
            return
        seen.add(key)
        candidates.append(
            Candidate(
                id="",
                selector=selector,
                selector_strategy=strategy,
                kind=kind.value,
                label=label,
                priority=priority,
                is_navigation=_in_navigation(node),
                reason=reason,
                target=target,
                target_visible=target_visible,
            )
        )

    for index_no, node in enumerate(tabs):
        # Uniform navigation exclusion (D2): every discovery loop skips nodes
        # inside navigation chrome and counts the skip.
        if _in_navigation(node):
            navigation_excluded += 1
            continue
        add(node, ControlKind.TAB, f"role tab {index_no + 1}", 10)
    for index_no, node in enumerate(summaries):
        if _in_navigation(node):
            navigation_excluded += 1
            continue
        add(node, ControlKind.ACCORDION, f"summary disclosure {index_no + 1}", 25)
    for index_no, node in enumerate(swept):
        if _in_navigation(node):
            navigation_excluded += 1
            continue
        kind = _classify_sweep(node)
        if kind is None:
            continue
        add(node, kind, f"{kind.value} control {index_no + 1}", _PRIORITY_BY_KIND[kind])

    candidates.sort(key=lambda c: (c.priority, c.label))
    cap = max(0, max_states)
    truncated = len(candidates) > cap
    final = [
        replace(candidate, id=f"{index + 1:02d}-{candidate.kind}")
        for index, candidate in enumerate(candidates[:cap])
    ]

    if final:
        reason = "ok"
    else:
        reason = "no-candidates"
    return CapturePlan(
        candidates=final,
        reason=reason,
        limits={
            "max_states": cap,
            "discovered": len(candidates),
            "truncated": truncated,
            "dropped_ambiguous": dropped_ambiguous,
            "navigation_excluded": navigation_excluded,
        },
    )
