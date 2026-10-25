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

import re
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
    return re.sub(r"[^a-zA-Z0-9_-]", lambda m: "\\" + m.group(0), value)


def _css_unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


_TAG_RE = re.compile(r"^(\*|[a-zA-Z][a-zA-Z0-9-]*)")
_ATTR_RE = re.compile(r'^([\w-]+)="(.*)"$', re.DOTALL)
_NTH_PREFIX = ":nth-of-type("


def _match_simple(node: _Node, part: str) -> bool:
    rest = part
    tag: Optional[str] = None
    match = _TAG_RE.match(rest)
    if match:
        tag = match.group(1)
        rest = rest[match.end():]

    if rest.startswith("#"):
        rest = rest[1:]
        end = len(rest)
        for index, char in enumerate(rest):
            if char in " [:":
                end = index
                break
        wanted = _css_unescape(rest[:end])
        rest = rest[end:]
        if node.attrs.get("id") != wanted:
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


def _match_chain(node: _Node, selector: str) -> bool:
    """Match a ``a > b > c`` direct-child chain; node must match the last part."""
    parts = selector.split(" > ")
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
    parts: list[str] = []
    current: Optional[_Node] = node
    while current is not None and current.tag not in ("#root", "html"):
        part = current.tag
        node_id = current.attrs.get("id")
        if node_id:
            parts.insert(0, f"{part}#{_escape_css(node_id)}")
            break
        parent = current.parent
        if parent is not None and parent.tag != "#root":
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
