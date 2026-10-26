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

Navigation chrome and ``.nav``: ``NAVIGATION_SELECTOR_HINTS`` includes ``.nav``
for upstream parity (scout-src ``planner.js`` plans from the same hint list).
Accepted, on the record: a Bootstrap-style ``ul.nav.nav-tabs >
a[data-toggle="tab"]`` is chrome and is excluded from the plan, where upstream
planned such link tabs as ``page`` controls. Captur'd excludes chrome where
upstream planned it; a framework that hangs its tabs off ``.nav`` is captured
only if it also marks them up some other way outside navigation containers.

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
    "Node",
    "NonDocumentRootError",
    "NAVIGATION_SELECTOR_HINTS",
    "STABLE_SELECTOR_ATTRIBUTES",
    "classify_control",
    "iter_elements",
    "parse_html",
    "plan_candidates",
]


# ---------------------------------------------------------------------------
# Minimal node tree
# ---------------------------------------------------------------------------


class _Node:
    """One element of the parsed document."""

    __slots__ = ("tag", "attrs", "children", "parent", "text_chunks", "content")

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node] = []
        self.parent: Optional[_Node] = None
        self.text_chunks: list[str] = []
        #: Document-order stream of this node's own text chunks and child
        #: elements (D2): the builder fills it as the parser saw them, so
        #: :func:`_full_text` can interleave inline children with text.
        #: ``children`` and ``text_chunks`` stay as the element/text views.
        self.content: list[object] = []

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<_Node {self.tag} attrs={sorted(self.attrs.items())!r} children={len(self.children)}>"


#: Public alias for :class:`_Node` (D13): brief 01d consumes parsed trees and
#: imports this name; ``_Node`` stays as an alias for the same class.
Node = _Node


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
        top.content.append(node)
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
            self._stack[-1].content.append(data)


def parse_html(html: str) -> Node:
    """Parse an HTML string into a tree; returns the synthetic ``#root`` node."""
    builder = _NodeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def _tree_root(node: _Node) -> _Node:
    while node.parent is not None:
        node = node.parent
    return node


def iter_elements(root: Node) -> Iterator[Node]:
    """Pre-order (document order) walk over element nodes, excluding the root.

    Iterative (explicit stack), never recursion: a deeply nested document must
    not be able to raise RecursionError out of a walk.
    """
    stack: list[_Node] = list(reversed(root.children))
    while stack:
        node = stack.pop()
        yield node
        if node.children:
            stack.extend(reversed(node.children))


# ---------------------------------------------------------------------------
# Text helpers (planner.js textOf / targetSummary)
# ---------------------------------------------------------------------------


def _full_text(node: _Node) -> str:
    """Text of a subtree in document order (D2): the node's own text chunks
    interleave with its children as the parser saw them —
    ``<button><b>Save</b> now</button>`` yields ``"Save now"``, never
    ``"nowSave"``. The builder records that order in ``content``; a hand-built
    node without ``content`` falls back to the old fold (own chunks, then each
    child's text in document order). Iterative (explicit stack), never
    recursion."""
    parts: list[str] = []
    stack: list[object] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            parts.append(current)
            continue
        items = current.content or [*current.text_chunks, *current.children]
        stack.extend(reversed(items))
    return "".join(parts)


def _text_of(node: _Node) -> str:
    """aria-label / title / data-title / data-label / textContent / value, first non-empty.

    D1: a ``value`` attribute is a label too — ``<input type="submit"
    value="Sign up">`` renders "Sign up" as its only label — but only for
    ``input`` and ``button``, and only after every other source: a control
    with text keeps its text, and the fallback never rescues a non-control
    (a hidden input's value is data, not a label — it is not swept anyway).
    """
    for value in (
        node.attrs.get("aria-label"),
        node.attrs.get("title"),
        node.attrs.get("data-title"),
        node.attrs.get("data-label"),
        _full_text(node),
    ):
        if value and value.strip():
            return " ".join(value.split())[:80]
    if node.tag in ("input", "button"):
        value = node.attrs.get("value")
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


def _is_css_control(char: str) -> bool:
    """C0 controls and DEL (D5): raw inside a selector they are unparseable —
    backslash + newline is even a line continuation that deletes the newline —
    so escaping and unescaping treat them as hex escapes."""
    code = ord(char)
    return code < 0x20 or code == 0x7F


def _hex_escape(char: str) -> str:
    """CSS hex escape plus the one whitespace terminator the grammar allows
    (D5), so a following hex digit can never be swallowed into the escape."""
    return f"\\{format(ord(char), 'x')} "


def _escape_css(value: str) -> str:
    def escape_one(match: re.Match) -> str:
        char = match.group(0)
        return _hex_escape(char) if _is_css_control(char) else "\\" + char

    escaped = re.sub(r"[^a-zA-Z0-9_-]", escape_one, value)
    # CSSOM: an identifier cannot start with a digit, nor with '-' followed by
    # a digit; such a digit is escaped as a hex code point plus a terminating
    # space, e.g. "1foo" -> "\\31 foo", so querySelectorAll accepts the selector.
    match = _LEADING_DIGIT_RE.match(escaped)
    if match:
        dash, digit = match.group(1), match.group(2)
        escaped = f"{dash}{_hex_escape(digit)}{escaped[match.end():]}"
    # D6: a leading '-' is an identifier start only before an identifier-start
    # character, another '-', or an escape (the digit rule above leaves an
    # escape behind, so '-1foo' stays '-\31 foo'). Anything else — a lone '-'
    # above all — hex-escapes the hyphen itself: '#\2d ', never '#-'.
    if escaped.startswith("-"):
        after = escaped[1:2]
        ident_start = bool(after) and (
            after in "-_" or after == "\\" or after.isalpha() or ord(after) > 0x7F
        )
        if not ident_start:
            escaped = _hex_escape("-") + escaped[1:]
    return escaped


def _escape_attr_value(value: str) -> str:
    """Escape an attribute value for a ``[attr="..."]`` selector (D3, D5).

    One helper for both directions: this escapes for generation and
    :func:`_css_unescape` reads it back in the uniqueness verifier, so a round
    trip is exact. A backslash is doubled and a quote is backslash-escaped —
    per character, so no ordering subtlety — and C0 controls and DEL become
    CSS hex escapes (D5): a raw newline inside the string is not valid CSS at
    all, and ``\\`` + newline would delete the newline instead of matching it.
    """
    out: list[str] = []
    for char in value:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif _is_css_control(char):
            out.append(_hex_escape(char))
        else:
            out.append(char)
    return "".join(out)


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
    """Match an ``a > b > c`` direct-child chain; node must match the last part.

    D2: every structural path starts at ``#root`` (or at ``html`` for full
    documents), so matching anchors there too — the topmost matched element
    must sit directly under that boundary. ``div > button`` therefore never
    matches a button under an unrelated ``div`` deeper in the document.
    D1 invariant (see :func:`_structural_path`): a path the generator emits
    always verifies against the same node — one shared boundary anchor.
    """
    parts = _split_chain(selector)
    if not parts or not _match_simple(node, parts[-1]):
        return False
    current = node
    for part in reversed(parts[:-1]):
        current = current.parent
        if current is None or current.tag == "#root" or not _match_simple(current, part):
            return False
    parent = current.parent
    return parent is not None and parent.tag in ("#root", "html")


def _count_matches(root: _Node, selector: str) -> int:
    """queryCount: 0 for invalid/empty selectors, else number of matching nodes."""
    return _PlanIndex(root).count_matches(selector)


#: A single-part ``tag[attr="value"]`` selector, for the index fast path.
_ATTR_SELECTOR_RE = re.compile(r'^([a-zA-Z][a-zA-Z0-9-]*)\[([\w-]+)="(.*)"\]$', re.DOTALL)


class _PlanIndex:
    """One-pass id/attribute index over a document, built once per plan (D7).

    Counting a candidate's selector through the index keeps planning roughly
    linear in the document size instead of rescanning the whole tree per
    candidate, and counts are memoised per selector because the tree is
    immutable during a plan. Structural chains still verify by scanning, but
    only for candidates that have no id or stable attribute.
    """

    def __init__(self, root: _Node) -> None:
        self.root = root
        self.nodes: list[_Node] = list(iter_elements(root))
        self.by_id: dict[str, list[_Node]] = {}
        self.by_attr: dict[tuple[str, str], list[_Node]] = {}
        for node in self.nodes:
            node_id = node.attrs.get("id")
            if node_id:
                self.by_id.setdefault(node_id, []).append(node)
            for name, value in node.attrs.items():
                if value:
                    self.by_attr.setdefault((name, value), []).append(node)
        self._counts: dict[str, int] = {}

    def count_matches(self, selector: str) -> int:
        """queryCount: 0 for invalid/empty selectors, else number of matches."""
        if not selector:
            return 0
        cached = self._counts.get(selector)
        if cached is None:
            cached = self._count_uncached(selector)
            self._counts[selector] = cached
        return cached

    def _count_uncached(self, selector: str) -> int:
        parts = _split_chain(selector)
        if len(parts) == 1:
            part = parts[0]
            if part.startswith("#"):
                ident, remainder = _scan_ident(part[1:])
                if remainder == "" and ident:
                    return len(self.by_id.get(_css_unescape(ident), ()))
            else:
                attr_match = _ATTR_SELECTOR_RE.fullmatch(part)
                if attr_match is not None:
                    tag, name, raw_value = attr_match.groups()
                    nodes = self.by_attr.get((name, _css_unescape(raw_value)), ())
                    return sum(1 for node in nodes if node.tag == tag)
        return sum(1 for node in self.nodes if _match_chain(node, selector))

    def unique_selector(self, node: _Node) -> tuple[Optional[str], str]:
        """Best selector for ``node``: ``#id``, then a stable attribute, then
        structure — the same strategies and verification as the module-level
        :func:`unique_selector`, but counting through this index."""
        node_id = node.attrs.get("id")
        if node_id:
            selector = f"#{_escape_css(node_id)}"
            if self.count_matches(selector) == 1:
                return selector, "id"
        for attr in STABLE_SELECTOR_ATTRIBUTES:
            value = node.attrs.get(attr)
            if not value:
                continue
            selector = f'{node.tag}[{attr}="{_escape_attr_value(value)}"]'
            if self.count_matches(selector) == 1:
                return selector, "attribute"
        path = _structural_path(node, self)
        if path:
            return path, "structural"
        return None, "structural"


def _structural_path(node: _Node, index: _PlanIndex) -> str:
    """Positional path, root-scoped (D2). The chain always starts at the
    synthetic ``#root`` (or at ``html`` for full documents): its first segment
    is a boundary child, emitted with ``:nth-of-type`` whenever its tag is not
    unique among the boundary's children — so ``div > button`` never collapses
    two wrappers at different depths. A unique id anywhere on the chain decorates
    its segment; a *duplicate* id is omitted in favour of ``:nth-of-type``
    so the path can still disambiguate (rule: never emit an ambiguous selector).

    D1 invariant: a path the generator emits always verifies against the same
    node. The walk therefore never stops early at a unique-id ancestor — the
    chain always runs to the boundary, the one anchor shared with the verifier
    (:func:`_match_chain`), so generation and verification can never disagree.
    """
    parts: list[str] = []
    current: Optional[_Node] = node
    while current is not None and current.tag not in ("#root", "html"):
        part = current.tag
        node_id = current.attrs.get("id")
        if node_id and len(index.by_id.get(node_id, ())) == 1:
            # The unique id alone disambiguates this segment; the walk still
            # continues to the boundary (D1) instead of shortening the chain.
            parts.insert(0, f"{part}#{_escape_css(node_id)}")
        else:
            parent = current.parent
            if parent is not None:
                same_tag = [child for child in parent.children if child.tag == current.tag]
                if len(same_tag) > 1:
                    part += f"{_NTH_PREFIX}{same_tag.index(current) + 1})"
            parts.insert(0, part)
        current = current.parent
    return " > ".join(parts)


def unique_selector(node: Node) -> tuple[Optional[str], str]:
    """Best selector for ``node``: ``#id``, then a stable attribute, then structure.

    Returns ``(selector, strategy)`` with strategy one of ``id`` / ``attribute`` /
    ``structural``. The ``structural`` fallback is not guaranteed unique on its
    own — :func:`plan_candidates` runs the uniqueness verifier and drops
    ambiguous candidates, so only the plan's selectors are verified-unique.
    """
    return _PlanIndex(_tree_root(node)).unique_selector(node)


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
    # Action-word denylist (D5): matches the control's label text only — not
    # role, not class text, not href — same verb list as upstream planner.js.
    # A non-empty label qualifies (D2): the root-scoping test's one-character
    # button labels are real controls, not noise.
    # plan_candidates counts every drop in limits["dropped_action_labels"].
    if label and not _ACTION_RE.search(label):
        return ControlKind.CONTROL
    return None


def classify_control(node: Optional[Node]) -> tuple[ControlKind, bool]:
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


#: Walk ceilings (review round 1, D6): a hostile or machine-generated document
#: fails the plan with reason="too-large" and a limits counter — never a raised
#: RecursionError, and never unbounded work.
_MAX_NODES = 10_000
_MAX_DEPTH = 256


class _DocumentTooLarge(Exception):
    """Internal: the document exceeds the planner's walk ceilings."""


def _check_size(root: _Node) -> None:
    """Raise :class:`_DocumentTooLarge` when the tree exceeds the ceilings."""
    count = 0
    stack: list[tuple[_Node, int]] = [(child, 1) for child in reversed(root.children)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > _MAX_NODES:
            raise _DocumentTooLarge(f"more than {_MAX_NODES} elements")
        if depth > _MAX_DEPTH:
            raise _DocumentTooLarge(f"deeper than {_MAX_DEPTH} levels")
        for child in reversed(node.children):
            stack.append((child, depth + 1))


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

# A native <summary> disclosure is ordered before a swept accordion control
# (40): its toggle and its label are one native element, not an accordion
# inferred from aria-expanded or wording.
PRIORITY_SUMMARY_DISCLOSURE = 25


def _target_summary(index: _PlanIndex, node: _Node) -> tuple[str, Optional[bool]]:
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
    matches = index.by_id.get(controls[1:])
    if matches:
        return controls, _is_visible(matches[0])
    return controls, None


class NonDocumentRootError(ValueError):
    """Raised when :func:`plan_candidates` is handed a subtree instead of the
    document root (D4). The precondition — ``root.parent is None`` and
    ``root.tag == "#root"`` — is refused with this named error, so a caller
    passing a subtree can never mistake the refusal for an empty plan."""


def plan_candidates(root: Node, base_url: Optional[str] = None, max_states: int = 24) -> CapturePlan:
    """Turn a parsed document into an ordered, deduped candidate list.

    Deterministic: document order drives discovery, the final order is
    ``(priority, label)``, ties keep insertion order, and everything is sorted —
    never set iteration. Dedupe is by selector (D8): one node is one candidate,
    however many discovery loops reach it (a ``<summary role="tab">``) — the
    second kind stays visible in the candidate's ``reason`` text, never as a
    second row. ``base_url`` is accepted for the live-clicking layer
    (brief 01d) and does not affect the pure plan.

    The reason is exactly one of ``"ok"`` (an untruncated plan with
    candidates), ``"no-candidates"`` (the document has nothing to interact
    with), ``"truncated"`` (``limits["truncated"]`` is true — a caller-imposed
    ``max_states`` cap cut the plan, to any size, including zero and including
    a plan that still holds candidates) and ``"too-large"`` (the document
    exceeds the walk ceilings: more than 10 000 elements or deeper than 256
    levels). D3 invariant: ``reason == "truncated"`` whenever
    ``limits["truncated"]`` is true; ``"ok"`` is only for an untruncated plan.
    On ``"too-large"`` the ``limits["too_large"]`` counter carries the reason
    text — the ceiling that was exceeded (D9): ``"more than 10000 elements"``
    or ``"deeper than 256 levels"``, never a bare ``True``.
    There is no live-versus-rested reason code here; that distinction is
    brief 01d's.

    ``limits["discovered"]`` counts the candidates actually validated into
    the plan. The cap really bounds the scan (round 2, D4): discovery stops
    as soon as the plan holds more than ``cap`` validated candidates — it does
    not run to the end of the document — and ``limits["scan_stopped_at_cap"]``
    is True exactly when it stopped early. The cap is overshot by one so the
    ``truncated`` flag stays exact.

    Raises :class:`NonDocumentRootError` (D4) unless ``root`` is the document
    root produced by :func:`parse_html` — ``root.parent is None`` and
    ``root.tag == "#root"``. A subtree is refused with that named error, never
    silently planned as an empty plan.
    """
    if root.parent is not None or root.tag != "#root":
        raise NonDocumentRootError(
            "plan_candidates requires the parse_html document root "
            "(root.parent is None and root.tag == '#root'); got "
            f"tag={root.tag!r} with parent={root.parent!r}"
        )
    cap = max(0, max_states)
    limits: dict = {
        "max_states": cap,
        "discovered": 0,
        "truncated": False,
        "scan_stopped_at_cap": False,
        "dropped_ambiguous": 0,
        "navigation_excluded": 0,
        "dropped_action_labels": 0,
    }
    try:
        _check_size(root)
    except _DocumentTooLarge as exc:
        # D9: the counter names the ceiling that was exceeded.
        limits["too_large"] = str(exc)
        return CapturePlan(candidates=[], reason="too-large", limits=limits)

    index = _PlanIndex(root)
    nodes = index.nodes
    tabs = [n for n in nodes if n.attrs.get("role") == "tab"]
    summaries = [n for n in nodes if n.tag == "summary"]
    swept = [n for n in nodes if n.attrs.get("role") != "tab" and n.tag != "summary" and _in_control_sweep(n)]

    candidates: list[Candidate] = []
    # D8: one candidate per node — the dedupe key is the selector (which the
    # verifier has just proved names exactly one node), and the value is that
    # candidate's index, so a second kind can fold into its reason text.
    seen: dict[str, int] = {}
    dropped_ambiguous = 0
    navigation_excluded = 0
    dropped_action_labels = 0
    scan_stopped_at_cap = False
    # D6: a node discoverable by two loops (a <summary role="tab">) is one
    # skipped node, so navigation_excluded counts it once across all loops.
    counted: set[_Node] = set()

    def add(node: _Node, kind: ControlKind, reason: str, priority: int) -> bool:
        """Validate and append one candidate.

        Returns False once the plan holds more than ``cap`` candidates. The
        discovery loops check the same condition at the top of every iteration
        and stop scanning (round 2, D4): the cap bounds the whole scan, not
        just validation, so the counters reflect the nodes actually visited.
        The cap is overshot by one so the ``truncated`` flag stays exact: a
        document with exactly ``cap`` valid candidates is not reported as
        truncated.
        """
        nonlocal dropped_ambiguous
        if len(candidates) > cap:
            return False
        if not _is_visible(node) or _is_disabled(node):
            return True
        selector, strategy = index.unique_selector(node)
        if selector is None or index.count_matches(selector) != 1:
            dropped_ambiguous += 1
            return True
        label = _text_of(node) or node.attrs.get("aria-controls") or node.attrs.get("href") or kind.value
        target, target_visible = _target_summary(index, node)
        existing = seen.get(selector)
        if existing is not None:
            # D8: dedupe by selector — one node is one candidate. The second
            # kind stays visible in the kept candidate's reason text.
            kept = candidates[existing]
            candidates[existing] = replace(
                kept,
                reason=f"{kept.reason}; also {kind.value}: {reason}",
            )
            return True
        seen[selector] = len(candidates)
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
        return True

    for index_no, node in enumerate(tabs):
        if len(candidates) > cap:
            scan_stopped_at_cap = True  # D4: the cap bounds the scan mid-document.
            break
        # Uniform navigation exclusion (D2): every discovery loop skips nodes
        # inside navigation chrome and counts the skip — once per node (D6).
        if _in_navigation(node):
            if node not in counted:
                counted.add(node)
                navigation_excluded += 1
            continue
        # D12: one source of truth for priorities - the tab loop uses the same
        # table as the swept classification.
        add(node, ControlKind.TAB, f"role tab {index_no + 1}", _PRIORITY_BY_KIND[ControlKind.TAB])
    for index_no, node in enumerate(summaries):
        if len(candidates) > cap:
            scan_stopped_at_cap = True
            break
        if _in_navigation(node):
            if node not in counted:
                counted.add(node)
                navigation_excluded += 1
            continue
        add(node, ControlKind.ACCORDION, f"summary disclosure {index_no + 1}", PRIORITY_SUMMARY_DISCLOSURE)
    for index_no, node in enumerate(swept):
        if len(candidates) > cap:
            scan_stopped_at_cap = True
            break
        if _in_navigation(node):
            if node not in counted:
                counted.add(node)
                navigation_excluded += 1
            continue
        kind = _classify_sweep(node)
        if kind is None:
            # D5: every action-word drop is counted and visible to the operator.
            if _ACTION_RE.search(_text_of(node).lower()):
                dropped_action_labels += 1
            continue
        add(node, kind, f"{kind.value} control {index_no + 1}", _PRIORITY_BY_KIND[kind])

    candidates.sort(key=lambda c: (c.priority, c.label))
    truncated = len(candidates) > cap
    final = [
        replace(candidate, id=f"{index_no + 1:02d}-{candidate.kind}")
        for index_no, candidate in enumerate(candidates[:cap])
    ]

    if truncated:
        # D3: a cap that truncates the plan — to any size, including zero,
        # and including a plan that still holds candidates — is "truncated",
        # never "ok", never "no-candidates".
        reason = "truncated"
    elif final:
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
            "scan_stopped_at_cap": scan_stopped_at_cap,
            "dropped_ambiguous": dropped_ambiguous,
            "navigation_excluded": navigation_excluded,
            "dropped_action_labels": dropped_action_labels,
        },
    )
