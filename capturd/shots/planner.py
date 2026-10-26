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

Navigation chrome, ``.nav`` and ``.menu``: ``NAVIGATION_SELECTOR_HINTS``
includes ``.nav`` for upstream parity (scout-src ``planner.js`` plans from the
same hint list) and ``.menu`` because dropdown menus are navigation in this
product — a ``ul.menu`` is chrome and is excluded from the plan, the same
accepted limitation as ``.nav``, and this paragraph names both.
Accepted, on the record: a Bootstrap-style ``ul.nav.nav-tabs >
a[data-toggle="tab"]`` is chrome and is excluded from the plan, where upstream
planned such link tabs as ``page`` controls. Captur'd excludes chrome where
upstream planned it; a framework that hangs its tabs off ``.nav`` or ``.menu``
is captured only if it also marks them up some other way outside navigation
containers.

Everything here is deterministic: identical HTML in produces an identical
``CapturePlan`` out, byte for byte under ``repr``. All ordering comes from
document order and explicit sorts — never from set iteration.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from typing import Iterator, Literal, Optional

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
        #: D43: per-tag index of open stack depths, maintained beside
        #: ``_stack`` — ``_open_depths[tag]`` is the sorted list of depths at
        #: which ``tag`` is open. An end tag is matched in constant time from
        #: this index instead of scanning the open stack, so an unmatched end
        #: tag returns immediately and the end-tag path never walks the whole
        #: stack per stray tag.
        self._open_depths: dict[str, list[int]] = {}
        self._elements = 0
        #: D44: deterministic cost counter for the end-tag path — incremented
        #: once per open-stack entry examined while matching an end tag, never
        #: a wall clock. The test suite bounds it; D28 forbids timing tests.
        self.stack_scans = 0

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
        self._elements += 1
        if self._elements > _MAX_NODES:
            # D27: abort the tree build the moment the node ceiling is
            # exceeded — the full tree is never materialised.
            raise _DocumentTooLarge(f"more than {_MAX_NODES} elements")
        return node

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = self._add_element(tag, attrs)
        if node.tag not in self._VOID:
            self._stack.append(node)
            self._open_depths.setdefault(node.tag, []).append(len(self._stack) - 1)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._add_element(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        depths = self._open_depths.get(tag)
        if not depths:
            # D43: nothing of this tag is open — a stray end tag returns
            # immediately, examining zero stack entries.
            return
        depth = depths[-1]  # topmost open element with this tag
        # D43: pop to the matched depth without scanning; the dropped suffix
        # is removed from the per-tag index one entry per dropped node, so the
        # whole end-tag path stays proportional to the nodes it drops.
        dropped = self._stack[depth:]
        self.stack_scans += len(dropped)  # stack entries examined (D44)
        for node in dropped:
            self._open_depths[node.tag].pop()
        del self._stack[depth:]

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].text_chunks.append(data)
            self._stack[-1].content.append(data)


def parse_html(html: str) -> Node:
    """Parse an HTML string into a tree; returns the synthetic ``#root`` node.

    D27: the ceiling bounds the parse, not just the plan's walk. A document
    whose serialized length exceeds ``_MAX_INPUT_CHARS`` is refused before a
    single node is built, and the builder aborts the tree build the moment
    more than ``_MAX_NODES`` elements exist — both raise
    :class:`_DocumentTooLarge` (a ``ValueError``) with the ceiling's reason
    text, before the full tree is materialised. A caller with a tree larger
    than the ceilings — brief 01d builds from the live DOM — hands it to
    :func:`plan_candidates`, which reports ``reason="too-large"`` instead of
    raising.
    """
    if len(html) > _MAX_INPUT_CHARS:
        raise _DocumentTooLarge(f"more than {_MAX_INPUT_CHARS} characters")
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


def _href_artifact(value: Optional[str]) -> str:
    """href -> plan-artifact form (D18): the query string never enters the plan.

    A ``?token=…&sig=…`` credential-bearing query must not be baked into the
    labels and targets that brief 01d writes to disk — "no credential, no
    signed URL". The path (or fragment) is kept; only the query is dropped.
    """
    return (value or "").split("?", 1)[0]


def _keyword_value(node: _Node, name: str) -> str:
    """Value of a keyword attribute, folded to lowercase (D21).

    HTML says ``role``, ``type`` and every ``aria-*`` keyword value are
    ASCII-case-insensitive: ``<input TYPE="Submit">``, ``role="TAB"`` and
    ``aria-hidden="TRUE"`` must read exactly as their lowercase forms — an
    inversion here silently plans dead controls or drops live ones. Free-text
    values (``aria-label`` and friends) are labels, never keywords: they keep
    their case. A missing attribute reads as "".
    """
    value = node.attrs.get(name)
    return value.lower() if value is not None else ""


# ---------------------------------------------------------------------------
# Unique selectors (planner.js uniqueSelector / queryCount)
# ---------------------------------------------------------------------------

#: Attributes whose values name a control stably enough to select on.
#: Deliberately absent (D18): ``href`` — the structural path already
#: disambiguates a link, and ``a[href="https://…?token=…&sig=…"]`` bakes
#: signed URLs and credential-bearing query strings into plan artifacts that
#: brief 01d writes to disk.
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
            # D36: a 6-hex-digit escape can name code points beyond chr()'s
            # range (\ffffff is 0xFFFFFF, over the 0x10FFFF maximum). Mask
            # instead of raising ValueError out of the verifier — a bad escape
            # in a hand-written selector must count 0 matches, never abort
            # the plan.
            return chr(int(match.group(1), 16) & 0x10FFFF)
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


def _scan_bracketed(text: str) -> Optional[tuple[str, str]]:
    """Split ``[decl]rest`` at the bracket closing the attribute selector.

    Escape-aware (D11): a ``]`` inside the quoted value does not close the
    bracket, and a backslash escapes the next character inside the string —
    the same discipline as :func:`_scan_ident`. ``[attr="a]"]`` therefore
    splits after the final ``]``, where the old ``rest.find("]")`` split
    inside the value. ``None`` when the bracket is unterminated.
    """
    if not text.startswith("["):
        return None
    escaped = False
    in_quotes = False
    for index in range(1, len(text)):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            in_quotes = not in_quotes
        elif char == "]" and not in_quotes:
            return text[1:index], text[index + 1:]
    return None


def _parse_attr_decl(decl: str) -> Optional[tuple[str, str]]:
    """Parse ``name="escaped value"`` escape-aware (D11) -> ``(name, raw)``.

    The value runs to the first *unescaped* quote and nothing may follow it —
    never a greedy ``(.*)`` backtracking to the last quote. ``raw`` keeps its
    CSS escapes; :func:`_css_unescape` reads it back, so the round trip is
    exact. ``None`` for anything that is not exactly this shape.
    """
    match = re.match(r'^([\w-]+)="', decl)
    if match is None:
        return None
    raw = decl[match.end():]
    escaped = False
    for index, char in enumerate(raw):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            if index != len(raw) - 1:
                return None  # junk after the closing quote -> unparseable
            return match.group(1), raw[:index]
    return None  # unterminated value


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
        # D11: escape-aware bracket split and value parse — never
        # rest.find("]") (which splits inside a value) nor a greedy (.*).
        split = _scan_bracketed(rest)
        if split is None:
            return False
        decl, rest = split
        parsed = _parse_attr_decl(decl)
        if parsed is None:
            return False
        name, raw_value = parsed
        if node.attrs.get(name) != _css_unescape(raw_value):
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

    D52: the backslash escapes the next character EVERYWHERE, and a quote
    opens a string only when it is not escaped. The escape used to be honoured
    only inside quotes, so an escaped ``"`` in an unquoted segment —
    ``#say\"hi > button`` — flipped ``in_quotes`` and every later `` > `` was
    swallowed into one part.
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
        elif char == "\\":
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


def _parse_attr_part(part: str) -> Optional[tuple[str, str, str]]:
    """Parse a complete ``tag[attr="escaped value"]`` part, escape-aware (D16).

    Returns ``(tag, attr, raw_value)`` — or ``None`` when the part is not
    exactly that shape (``*`` tag, extra fragments, unterminated value). The
    index fast path delegates to this parser, the same scanner
    :func:`_match_simple` uses (D11), so a value containing ``"`` or ``]``
    can never be split one way on one path and another way on the other.
    Replaces the old ``_ATTR_SELECTOR_RE`` whose greedy ``(.*)`` was not
    escape-aware.
    """
    match = _TAG_RE.match(part)
    if match is None or match.group(1) == "*":
        return None
    split = _scan_bracketed(part[match.end():])
    if split is None:
        return None
    decl, rest = split
    if rest:
        return None
    parsed = _parse_attr_decl(decl)
    if parsed is None:
        return None
    name, raw_value = parsed
    return match.group(1), name, raw_value


class _PlanIndex:
    """One-pass id/attribute index over a document, built once per plan (D7).

    Counting a candidate's selector through the index keeps planning roughly
    linear in the document size instead of rescanning the whole tree per
    candidate, and counts are memoised per selector (D28: for id/attr
    selectors only) because the tree is immutable during a plan. Structural
    chains verify against the target node's parent chain in O(depth) —
    :meth:`verify_structural` — never by scanning the document.
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
        # D28: the boundary nodes — children of ``#root`` and of every
        # ``<html>`` — are where a structural chain anchors (see
        # :func:`_match_chain`); verifying one needs them and nothing else.
        self._boundary_nodes: list[_Node] = list(root.children)
        for node in self.nodes:
            if node.tag == "html":
                self._boundary_nodes.extend(node.children)
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
                attr_part = _parse_attr_part(part)
                if attr_part is not None:
                    tag, name, raw_value = attr_part
                    nodes = self.by_attr.get((name, _css_unescape(raw_value)), ())
                    return sum(1 for node in nodes if node.tag == tag)
        return sum(1 for node in self.nodes if _match_chain(node, selector))

    def verify_structural(self, node: _Node, selector: str) -> bool:
        """Does ``selector`` name exactly this one node? O(depth), never O(nodes).

        D28: a structural path is verified by comparing the parent chain of
        the target node directly — :func:`_match_chain` — and by proving each
        segment selects exactly one node. At the boundary level the count
        spans every boundary container (``#root`` and every ``<html>``),
        which is where the old full-document scan saw cross-document
        ambiguity (two ``<html>`` roots, two ``body > button`` chains); at
        every deeper level exactly one child of the matched parent must match
        the segment. Together these prove the selector names exactly one
        node — this node — without visiting the rest of the document.
        """
        parts = _split_chain(selector)
        if not parts or not _match_chain(node, selector):
            return False
        if sum(1 for candidate in self._boundary_nodes if _match_simple(candidate, parts[0])) != 1:
            return False
        # Uniqueness, level by level: each segment must select exactly one
        # child of its matched parent. The top segment's parent is the
        # boundary — counted above across every container.
        current = node
        for part in reversed(parts[1:]):
            parent = current.parent
            if parent is None:
                return False
            if sum(1 for child in parent.children if _match_simple(child, part)) != 1:
                return False
            current = parent
        return True

    def unique_selector(self, node: _Node) -> tuple[Optional[str], str]:
        """Best selector for ``node``: ``#id``, then a stable attribute, then
        structure — the same strategies and verification as the module-level
        :func:`unique_selector`, but counting through this index. The
        structural fallback is verified by :meth:`verify_structural`, not
        counted (D28)."""
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
    """Does the node's inline style declare ``prop: value``?

    D17: last-wins, because CSS is — with D53 applying the cascade within one
    declaration block: an ``!important`` declaration beats a later normal
    one. Per property the winning declaration is the last ``!important`` one
    when any exists, otherwise the last normal one — and only that winning
    declaration decides, so ``display:none!important;display:block`` is
    hidden and ``display:block!important;display:none`` is visible. A
    trailing ``!important`` is cascade priority, not a value: it is stripped
    before comparing so "display: none !important" hides like "none". An
    empty declaration (``display:``) is invalid CSS and overrides nothing, so
    it is not a match at all and never wins.
    """
    style = node.attrs.get("style", "")
    normal: Optional[str] = None
    important: Optional[str] = None
    for declaration in style.split(";"):
        if ":" not in declaration:
            continue
        name, _, val = declaration.partition(":")
        if name.strip().lower() != prop:
            continue
        val = val.strip().lower()
        # A trailing "!important" is cascade priority, not a value: strip it
        # before comparing so "display: none !important" hides like "none".
        is_important = re.search(r"!\s*important$", val) is not None
        if is_important:
            val = re.sub(r"!\s*important$", "", val).strip()
        if not val:
            continue  # an empty declaration is invalid CSS: it overrides nothing
        if is_important:
            important = val
        else:
            normal = val
    val = important if important is not None else normal
    if val is None:
        return False
    if prop == "opacity":
        try:
            return float(val) == 0.0
        except ValueError:
            return False
    return val == value


def _in_first_summary(details: _Node, node: _Node) -> bool:
    """Is ``node`` inside the ``details``' first ``<summary>`` subtree (D35)?

    Per HTML, a ``<details>`` without ``open`` renders exactly its first
    ``<summary>`` child — the disclosure's own toggle — and nothing else in
    its body. The path from ``node`` up to the ``details`` must pass through
    that exact summary. Mirrors :func:`_in_first_legend`.
    """
    summary = next((child for child in details.children if child.tag == "summary"), None)
    if summary is None:
        return False
    current: Optional[_Node] = node
    while current is not None and current is not details:
        if current is summary:
            return True
        current = current.parent
    return False


def _is_visible(node: _Node) -> bool:
    """Attribute-level approximation of computed-style visibility.

    Static HTML carries no computed styles or layout boxes, so this checks the
    node and its ancestors for ``hidden``, ``aria-hidden="true"`` and inline
    ``display:none`` / ``visibility:hidden`` / ``opacity:0``. D35: it also
    refuses controls the browser never renders at all — anything under a
    ``<template>``, and anything in a ``<details>`` body whose ``<details>``
    has no ``open`` attribute. Both shapes appear verbatim in ``outerHTML``,
    so unlike computed layout they are attribute-detectable. D55: only the
    BODY is conditional — the closed ``<details>`` element itself is rendered
    (it holds the toggle) and its first ``<summary>`` is too (it IS the
    toggle); the old gate judged the ``<details>`` invisible itself because
    ``_in_first_summary(details, details)`` is False. ``<details open>``
    renders its body. Rect-size checks from planner.js are impossible without
    a browser and are intentionally omitted; the live-clicking layer
    re-checks visibility on the real DOM.
    """
    current: Optional[_Node] = node
    while current is not None and current.tag != "#root":
        if current.tag == "template":
            return False
        if current.tag == "details" and "open" not in current.attrs:
            if node is not current and not _in_first_summary(current, node):
                return False
        if "hidden" in current.attrs:
            return False
        if _keyword_value(current, "aria-hidden") == "true":
            return False
        if _style_declares(current, "display", "none"):
            return False
        if _style_declares(current, "visibility", "hidden"):
            return False
        if _style_declares(current, "opacity", "0"):
            return False
        current = current.parent
    return True


def _in_first_legend(fieldset: _Node, node: _Node) -> bool:
    """Is ``node`` inside the ``fieldset``'s first ``<legend>`` subtree (D20)?

    Per HTML, the first ``legend`` element child of a disabled ``fieldset``
    is exempt: its descendants are not disabled by that fieldset. The path
    from ``node`` up to the ``fieldset`` must pass through that exact legend.
    """
    legend = next((child for child in fieldset.children if child.tag == "legend"), None)
    if legend is None:
        return False
    current: Optional[_Node] = node
    while current is not None and current is not fieldset:
        if current is legend:
            return True
        current = current.parent
    return False


def _is_disabled(node: _Node) -> bool:
    """Disabled-ness, self and inherited (D10).

    A control is disabled by its own ``disabled`` attribute (form controls
    plus ``fieldset``), by ``aria-disabled="true"``, or by a ``disabled``
    class. D10: a disabled ancestor disables its descendants too — a
    ``<button>`` inside ``<fieldset disabled>`` is unclickable however deep
    it is nested, so it is never planned. D34: ``aria-disabled="true"`` is
    inherited the same way — the standard ARIA disabled-toolbar pattern
    ``<div aria-disabled="true"><button>Go</button></div>`` renders a control
    the browser will not activate, and planning it makes brief 01d click a
    no-op. The ancestor chain is walked via :func:`_ancestors_inclusive`,
    like :func:`_is_visible`.

    D20 exception, per HTML: the first ``<legend>`` subtree of a disabled
    ``<fieldset>`` is interactive — those controls are not disabled by that
    fieldset (a second legend's subtree, and everything else in the fieldset,
    still is).

    D54: the inheritance is decided by the NEAREST explicit ``aria-disabled``
    value on the ancestor chain — an explicit ``aria-disabled="false"``
    overrides an inherited ``true`` and re-enables the subtree, where the old
    walk dropped anything under any ancestor saying "true". The nearest
    value decides aria-disabled only; a disabled ``<fieldset>`` still
    disables the control whatever the aria values say.
    """
    if node.tag in ("button", "input", "select", "textarea", "optgroup", "option", "fieldset"):
        if "disabled" in node.attrs:
            return True
    if "disabled" in _classes(node):
        return True
    aria_decided = False
    for ancestor in _ancestors_inclusive(node):
        # D34: aria-disabled is inherited down the ancestor chain. D54: the
        # nearest explicit value wins — "false" stops the inheritance.
        if not aria_decided and "aria-disabled" in ancestor.attrs:
            aria_decided = True
            if _keyword_value(ancestor, "aria-disabled") == "true":
                return True
        if ancestor.tag == "fieldset" and "disabled" in ancestor.attrs:
            if not _in_first_legend(ancestor, node):
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
#: brief 01b adds ``footer`` — site-footer links are navigation too). ``.menu``
#: is here deliberately (D23): dropdown menus are navigation in this product —
#: the module docstring's accepted-limitation paragraph names both ``.nav``
#: and ``.menu``.
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
        # D21: keyword values compare case-insensitively ("role='navigation'"
        # reads role="NAVIGATION" too).
        return _keyword_value(node, name.strip()) == value.lower()
    return node.tag == hint


#: D73: deterministic cost counter for the navigation check — incremented
#: once per navigation-hint comparison made by :func:`_in_navigation`, never
#: a wall clock. The test suite bounds it against the node count; D28 forbids
#: timing tests. Cumulative, never reset: a test measures the delta around
#: one call.
_hint_comparisons = 0


def _in_navigation(node: _Node) -> bool:
    global _hint_comparisons
    current: Optional[_Node] = node
    while current is not None and current.tag != "#root":
        for hint in NAVIGATION_SELECTOR_HINTS:
            _hint_comparisons += 1  # one hint comparison (D73)
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
    if tag == "input" and _keyword_value(node, "type") in ("button", "submit"):
        return True
    if tag == "a" and "href" in attrs:
        return True
    if tag in ("a", "button") and any(
        _hint_match(ancestor, container)
        for ancestor in _ancestors_inclusive(node)
        for container in ("nav", "aside", "header")
    ):
        return True
    if _keyword_value(node, "role") in ("button", "link"):
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
    # D21: keyword values read folded to lowercase ("role="TAB"" is "tab").
    role = _keyword_value(node, "role")
    aria_expanded = attrs.get("aria-expanded")
    aria_current = _keyword_value(node, "aria-current")
    # D57: aria-current is a value keyword (page|step|location|date|time|true,
    # D21 folded lowercase) and "false" means NOT current — the old presence
    # check marked an aria-current="false" control current and classified it
    # PAGE; it plans as a normal action instead.
    current = aria_current in ("page", "step", "location", "date", "time", "true")
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
        or current
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
    if _keyword_value(node, "role") == "tab":
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
#: Serialized-input ceiling (D27): ``parse_html`` refuses a document whose
#: source is longer than this before a single node is built. The value
#: comfortably fits the 10 000-element ceiling (400 characters per element),
#: so a legal document is never refused here — this ceiling bounds the shapes
#: the node ceiling cannot see (huge text nodes, huge attribute values).
_MAX_INPUT_CHARS = 4_000_000


class _DocumentTooLarge(ValueError):
    """Internal: the document exceeds the planner's walk ceilings.

    A ``ValueError`` so a caller of :func:`parse_html` can catch the refusal
    without importing a private name (D27)."""


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
    """One candidate interaction target, ready for capture to click.

    ``kind_note`` (D22) is the per-candidate note about how the planner found
    and classified this control — it can never be confused with
    ``CapturePlan.reason``, whose domain is the four plan statuses.
    """

    id: str
    selector: str
    selector_strategy: str
    kind: str
    label: str
    priority: int
    is_navigation: bool
    kind_note: str
    target: str = ""
    target_visible: Optional[bool] = None


@dataclass
class CapturePlan:
    """The planned capture states. ``candidates == []`` with a reason code is a
    *failure of the plan* — the planner never fabricates states it did not
    classify from the DOM."""

    candidates: list[Candidate] = field(default_factory=list)
    reason: Literal["ok", "no-candidates", "truncated", "too-large"] = "ok"
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
                _href_artifact(node.attrs.get("href")),
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


def plan_candidates(root: Node, max_states: int = 24) -> CapturePlan:
    """Turn a parsed document into an ordered, deduped candidate list.

    Deterministic: document order drives discovery, the final order is
    ``(priority, label)``, ties keep insertion order, and everything is sorted —
    never set iteration. Dedupe is by selector (D8): one node is one candidate,
    however many discovery loops reach it (a ``<summary role="tab">``) — the
    second kind stays visible in the candidate's ``kind_note`` text (D22 —
    renamed so it cannot be confused with ``CapturePlan.reason``), never as a
    second row. There is no ``base_url`` parameter (D19): one that is
    accepted and ignored is a silent no-op that reads as a planner bug in the
    layer above — brief 01d gets it back when 01d actually resolves links.

    The reason is exactly one of ``"ok"`` (an untruncated plan with
    candidates), ``"no-candidates"`` (the document has nothing to interact
    with), ``"truncated"`` (``limits["truncated"]`` is true — a caller-imposed
    ``max_states`` cap cut the plan, to any size, including zero and including
    a plan that still holds candidates) and ``"too-large"`` (the document
    exceeds the walk ceilings: more than 10 000 elements or deeper than 256
    levels). A tree over the ceilings reaches this function only when the
    caller built it itself — :func:`parse_html` refuses such documents at the
    parse (D27) — and it is refused here with the same reason. D3 invariant: ``reason == "truncated"`` whenever
    ``limits["truncated"]`` is true; ``"ok"`` is only for an untruncated plan.
    On ``"too-large"`` the ``limits["too_large"]`` counter carries the reason
    text — the ceiling that was exceeded (D9): ``"more than 10000 elements"``
    or ``"deeper than 256 levels"``, never a bare ``True``.
    There is no live-versus-rested reason code here; that distinction is
    brief 01d's.

    ``limits`` keys (D12): ``max_states`` (the effective cap), ``kept`` (D26:
    the number of candidates the plan holds — ``len(plan.candidates)``. It
    used to report ``cap + 1``, the truncation sentinel, while
    ``candidates`` held ``cap``, under a name that says otherwise; the
    sentinel is internal bookkeeping and is never counted. D30: the one
    exception is a cap that truncates the plan to zero (``max_states=0``) —
    ``kept`` then reports the exact overshoot, 1, the one candidate discovery
    kept before the cap cut it, never a lower bound),
    ``truncated``, ``scan_stopped_at_cap``,
    ``dropped_ambiguous`` (D58: distinct dropped nodes — each node counted
    once however many discovery reasons reached it),
    ``navigation_excluded`` (D56: every excluded node counted once over the
    FULL discovery lists — the cap may stop discovery early but it never
    truncates this counter), ``dropped_action_labels``, and ``too_large``
    (the reason text, present only on reason="too-large").
    The cap bounds the plan, not the scan (round 3, D15): only kept
    candidates count against it — ``add()`` skips a node that is invisible,
    disabled or ambiguous before anything is added — so a document full of
    such controls is still scanned node by node to the end.
    ``limits["scan_stopped_at_cap"]`` (D24) is True exactly when
    ``truncated`` is True — it used to be set only when a discovery loop
    re-entered and broke, so a plan whose overflow node was the last element
    of its discovery list reported ``truncated=True`` with
    ``scan_stopped_at_cap=False``, the exact case the flag exists to
    disambiguate. The cap is overshot by one so the ``truncated`` flag stays
    exact.

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
        "kept": 0,
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
    tabs = [n for n in nodes if _keyword_value(n, "role") == "tab"]
    summaries = [n for n in nodes if n.tag == "summary"]
    swept = [n for n in nodes if _keyword_value(n, "role") != "tab" and n.tag != "summary" and _in_control_sweep(n)]

    candidates: list[Candidate] = []
    # D8: one candidate per node — the dedupe key is the selector (which the
    # verifier has just proved names exactly one node), and the value is that
    # candidate's index, so a second kind can fold into its reason text.
    seen: dict[str, int] = {}
    # D58: dropped_ambiguous counts DISTINCT dropped nodes — a node reached
    # by two discovery reasons (a <summary role="tab"> is both a tab and a
    # summary) that is ambiguous is one dropped node, not two. The value is a
    # node count, never set iteration order.
    dropped: set[_Node] = set()
    dropped_action_labels = 0
    scan_stopped_at_cap = False
    # D6: a node discoverable by two loops (a <summary role="tab">) is one
    # skipped node, so navigation_excluded counts it once across all loops.
    # D56: it counts every excluded node over the FULL discovery lists, taken
    # before the loops run — the cap stops discovery early and must not
    # truncate the counter, so a document whose navigation nodes lie partly
    # past the truncation point still reports all of them. The value is a
    # node count, never set iteration order.
    counted: set[_Node] = set()
    for node in [*tabs, *summaries, *swept]:
        if node not in counted and _in_navigation(node):
            counted.add(node)
    navigation_excluded = len(counted)

    def add(node: _Node, kind: ControlKind, note: str, priority: int) -> None:
        """Validate and append one candidate.

        D7: one cap mechanism only — the discovery loops check the cap at the
        top of every iteration and stop iterating once the plan holds more
        than ``cap`` kept candidates. The cap bounds kept candidates, not the
        node scan (round 3, D15): ``add()`` itself never consults it and its
        return value is gone (D7), and a node skipped here — invisible,
        disabled, ambiguous — does not count against it, so a document of
        only such nodes is scanned in full.
        The cap is overshot by one so the ``truncated`` flag stays exact: a
        document with exactly ``cap`` kept candidates is not reported as
        truncated.
        """
        # D58: a dropped node is recorded in the ``dropped`` set below — one
        # entry per node however many discovery reasons reached it.
        if not _is_visible(node) or _is_disabled(node):
            return
        selector, strategy = index.unique_selector(node)
        if selector is None:
            dropped.add(node)
            return
        if strategy == "structural":
            # D28: verify against the target's parent chain in O(depth) — the
            # verifier has just proved the selector names exactly one node.
            if not index.verify_structural(node, selector):
                dropped.add(node)
                return
        elif index.count_matches(selector) != 1:
            dropped.add(node)
            return
        label = _text_of(node) or node.attrs.get("aria-controls") or _href_artifact(node.attrs.get("href")) or kind.value
        target, target_visible = _target_summary(index, node)
        existing = seen.get(selector)
        if existing is not None:
            # D8: dedupe by selector — one node is one candidate. The second
            # kind stays visible in the kept candidate's kind_note.
            kept = candidates[existing]
            candidates[existing] = replace(
                kept,
                kind_note=f"{kept.kind_note}; also {kind.value}: {note}",
            )
            return
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
                kind_note=note,
                target=target,
                target_visible=target_visible,
            )
        )

    for index_no, node in enumerate(tabs):
        if len(candidates) > cap:
            scan_stopped_at_cap = True  # D4/D15: discovery stopped early at the cap.
            break
        # Uniform navigation exclusion (D2): every discovery loop skips nodes
        # inside navigation chrome — counted once per node over the full
        # discovery lists (D6, D56).
        if node in counted:
            continue
        # D12: one source of truth for priorities - the tab loop uses the same
        # table as the swept classification.
        add(node, ControlKind.TAB, f"role tab {index_no + 1}", _PRIORITY_BY_KIND[ControlKind.TAB])
    for index_no, node in enumerate(summaries):
        if len(candidates) > cap:
            scan_stopped_at_cap = True
            break
        if node in counted:
            continue
        add(node, ControlKind.ACCORDION, f"summary disclosure {index_no + 1}", PRIORITY_SUMMARY_DISCLOSURE)
    for index_no, node in enumerate(swept):
        if len(candidates) > cap:
            scan_stopped_at_cap = True
            break
        if node in counted:
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
    # D24: the flag tracks ``truncated`` exactly — a plan whose overflow node
    # was the last element of its discovery list never re-enters a loop to
    # break out of, but it was still cut by the cap.
    scan_stopped_at_cap = truncated or scan_stopped_at_cap
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
    # D26: kept is the true count — len(plan.candidates) — never the cap + 1
    # truncation sentinel. D30: a truncation to zero (max_states=0) reports
    # the exact overshoot instead — discovery kept exactly one candidate
    # (cap + 1) before the cap cut it.
    kept = 1 if (truncated and not final) else len(final)
    return CapturePlan(
        candidates=final,
        reason=reason,
        limits={
            "max_states": cap,
            "kept": kept,
            "truncated": truncated,
            "scan_stopped_at_cap": scan_stopped_at_cap,
            "dropped_ambiguous": len(dropped),
            "navigation_excluded": navigation_excluded,
            "dropped_action_labels": dropped_action_labels,
        },
    )
