"""DemoSpec — the canonical contract for a Captur'd walkthrough.

Everything downstream (recorder, AI pipeline, viewer, MCP surface, exporters)
speaks this schema. This is the ONE authoritative definition; nothing else
defines these types.

Aligned with ARCHITECTURE.md §1 (from the prev PR #17 salvage) with additions:

* ``ContentMode``       — DOM / video / hybrid, so canvas / Three.js / games get
                           a video-mode fallback instead of a silent break.
* ``ContentMetadata``   — per-step detection: canvas area %, video presence,
                           iframe presence, DOM mutation rate.
* ``ZoomTarget``        — semantic camera: anchor to a selector, not a pixel,
                           so demos survive site font/layout changes.
* ``WordTimestamp``     — for voice-synced camera (TTS word offsets align to
                           camera keyframes).
* ``AnimationKeyframe`` — the LLM-directed camera timeline (JSON the viewer
                           executes via panzoom).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# Content-mode detection — the "canvas / Three.js / game" answer
# ---------------------------------------------------------------------------


class ContentMode(str, Enum):
    DOM = "dom"        # rrweb + selectors — full stack works
    VIDEO = "video"    # MediaRecorder screen capture — cursor-anchored camera only
    HYBRID = "hybrid"  # rrweb around a canvas rect, video inside it


@dataclass
class ContentMetadata:
    """Per-step signals the recorder emits so the pipeline picks the right mode.

    Thresholds live in the pipeline, not here — this is raw evidence.
    """

    hasCanvas: bool = False
    canvasAreaPct: float = 0.0     # % of viewport occupied by the largest canvas
    hasVideo: bool = False          # <video> element present
    hasIframe: bool = False         # embedded iframe (YouTube etc.)
    mutationRate: float = 0.0       # DOM mutations / second at step time (rrweb)


# ---------------------------------------------------------------------------
# Semantic target metadata — optional fields on Interaction.target (W1-03-r1)
# ---------------------------------------------------------------------------

#: Where the target's pixels/coordinates live.
CAPTURE_SPACES = ("viewport", "full_page")

#: What the click target means for the semantic camera.
TARGET_REGIONS = (
    "meaningful",
    "result",
    "body",
    "full_viewport",
    "scrollbar_gutter",
    "degenerate",
)

#: Regions where zooming is meaningless — the camera holds wide instead.
SUPPRESSED_REGIONS = frozenset(
    {"body", "full_viewport", "scrollbar_gutter", "degenerate"}
)

#: ARIA roles that mark the step's outcome (modal/result states).
RESULT_ROLES = frozenset({"dialog", "alertdialog"})


def _finite_number(value: Any, default: float) -> float:
    try:
        n = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return n if math.isfinite(n) else default


def _target_rect(target: Mapping[str, Any] | None) -> dict[str, float] | None:
    """Finite, positive target rect in source pixels — else ``None``.

    Accepts ``boundingRect`` or a bare ``width``/``height`` pair (legacy
    fallback); malformed or zero-area rects are treated as degenerate.
    """
    if not isinstance(target, Mapping):
        return None
    raw = target.get("boundingRect")
    if isinstance(raw, Mapping):
        for key in ("x", "y", "width", "height"):
            value = raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if not math.isfinite(float(value)):
                    return None  # NaN / inf recorded geometry → degenerate
        x = _finite_number(raw.get("x"), 0.0)
        y = _finite_number(raw.get("y"), 0.0)
        w = _finite_number(raw.get("width"), 0.0)
        h = _finite_number(raw.get("height"), 0.0)
    else:
        x = y = 0.0
        w = _finite_number(target.get("width"), 0.0)
        h = _finite_number(target.get("height"), 0.0)
    if w <= 0 or h <= 0:
        return None
    return {"x": x, "y": y, "width": w, "height": h}


def _target_viewport(
    target: Mapping[str, Any] | None, viewport: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """The source viewport recorded with the target (or the caller's)."""
    for candidate in (
        target.get("viewport") if isinstance(target, Mapping) else None,
        viewport,
    ):
        if isinstance(candidate, Mapping):
            w = _finite_number(candidate.get("width"), 0.0)
            h = _finite_number(candidate.get("height"), 0.0)
            if w > 0 and h > 0:
                dsf = _finite_number(candidate.get("deviceScaleFactor"), 1.0)
                return {
                    "width": int(w) if w.is_integer() else w,
                    "height": int(h) if h.is_integer() else h,
                    "deviceScaleFactor": int(dsf) if dsf.is_integer() else dsf,
                }
    return None


def classify_target_region(
    target: Mapping[str, Any] | None, viewport: Mapping[str, Any] | None = None
) -> str:
    """Classify what a click target means for the semantic camera.

    Order: result roles → degenerate geometry → scrollbar gutter →
    full-viewport coverage → page body → meaningful content. Malformed
    values classify as ``degenerate`` — never an exception.
    """
    if not isinstance(target, Mapping):
        return "degenerate"
    role = str(target.get("role") or "").strip().lower()
    if role in RESULT_ROLES:
        return "result"
    rect = _target_rect(target)
    if rect is None:
        return "degenerate"
    vp = _target_viewport(target, viewport) or {"width": 1024, "height": 768}
    vw = float(vp["width"])
    vh = float(vp["height"])
    if (
        rect["x"] + rect["width"] >= vw - 18
        and rect["width"] <= 24
        and rect["height"] >= vh * 0.5
    ):
        return "scrollbar_gutter"
    if (
        rect["x"] <= vw * 0.02
        and rect["y"] <= vh * 0.02
        and rect["x"] + rect["width"] >= vw * 0.98
        and rect["y"] + rect["height"] >= vh * 0.98
    ):
        return "full_viewport"
    tag = str(target.get("tagName") or "").strip().lower()
    sel = str(target.get("selector") or "").strip().lower()
    if tag in ("body", "html") or sel in ("body", "html"):
        return "body"
    return "meaningful"


def normalize_target(
    target: Mapping[str, Any] | None,
    viewport: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a normalized copy of ``target`` (the input is never mutated).

    Legacy keys survive verbatim; the optional ``role`` / ``viewport`` /
    ``captureSpace`` / ``region`` keys are backfilled. The region follows the
    semantic classification (role/geometry) and only falls back to an
    explicit hint when the geometry is vacuous.
    """
    src: Mapping[str, Any] = target if isinstance(target, Mapping) else {}
    t = dict(src)
    t["role"] = str(src.get("role") or "")
    vp = _target_viewport(src, viewport)
    if vp is not None:
        t["viewport"] = vp
    t["captureSpace"] = (
        src.get("captureSpace") if src.get("captureSpace") in CAPTURE_SPACES else "viewport"
    )
    classified = classify_target_region(src, vp)
    explicit = src.get("region")
    if classified != "meaningful":
        t["region"] = classified
    elif explicit in TARGET_REGIONS:
        t["region"] = explicit
    else:
        t["region"] = "meaningful"
    return t


# ---------------------------------------------------------------------------
# Click / hotspot geometry
# ---------------------------------------------------------------------------


@dataclass
class Hotspot:
    """Percentage-based click point (survives viewport resize)."""

    xPct: float
    yPct: float


@dataclass
class BoundingRect:
    x: float
    y: float
    width: float
    height: float


@dataclass
class Interaction:
    """Single user (or agent) action on a page."""

    type: str  # 'click' | 'input' | 'submit' | 'navigate' | 'scroll'
    target: dict
    hotspot: dict
    value: str | None = None


# ---------------------------------------------------------------------------
# Cursor + voice timing
# ---------------------------------------------------------------------------


@dataclass
class CursorPoint:
    """Point on a smoothed cursor bezier — viewport-relative."""

    x: float
    y: float
    t: int  # ms offset within the step


@dataclass
class WordTimestamp:
    """One word of the narration + its ms window in the voiceover audio.

    Camera keyframes align to these so the camera arrives on 'Buy Now' at
    the moment the narrator says 'Buy Now'. This is the voice-synced-camera
    primitive nobody else in the space has.
    """

    word: str
    tStartMs: int
    tEndMs: int


# ---------------------------------------------------------------------------
# Camera choreography (LLM as director; viewer executes)
# ---------------------------------------------------------------------------


class CameraAction(str, Enum):
    ZOOM_TO = "zoomTo"
    PAN_TO = "panTo"
    ZOOM_TO_FIT = "zoomToFit"
    RESET = "reset"
    HOLD = "hold"
    SPOTLIGHT_ON = "spotlightOn"
    SPOTLIGHT_OFF = "spotlightOff"


@dataclass
class ZoomTarget:
    """Semantic camera target — an element selector + percentage inside it.

    Anchoring to the selector (not the pixel) is why our demos survive
    typography / layout / color changes on the underlying site. Screen
    Studio can't do this because it only has the mouse trail; Supademo
    can't do this because it's a static screenshot.
    """

    selector: str
    xPct: float = 50.0
    yPct: float = 50.0
    level: float = 1.5      # 1.0 = 100%, 1.5 = 150%
    duration: int = 500     # ms
    easing: str = "ease-in-out"


@dataclass
class AnimationKeyframe:
    """One camera move on the timeline. LLM writes these; viewer executes."""

    stepIndex: int
    action: str            # a CameraAction value (kept as str for JSON round-trip)
    target: str | None = None      # CSS selector when action needs one
    offset: dict | None = None     # {x, y} pct inside target
    zoomLevel: float | None = None
    duration: int = 500
    easing: str | None = "ease-in-out"
    tStartMs: int | None = None    # optional: absolute offset from step start (voice-sync)


# ---------------------------------------------------------------------------
# AI enrichment payload
# ---------------------------------------------------------------------------


@dataclass
class AIAnnotations:
    """Everything the AI pipeline adds to a raw recording."""

    summary: str = ""                 # 2-3 sentence flow summary
    style: str = "snappy"             # 'snappy' | 'smooth' | 'professional' | 'cinematic'
    generatedAt: str = ""
    animationTimeline: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Step + spec
# ---------------------------------------------------------------------------


@dataclass
class DemoStep:
    """One recorded interaction. Assets referenced by path, not inlined here."""

    index: int
    timestamp: int
    pageUrl: str
    pageTitle: str
    interaction: Interaction

    # Visual state — one of these is populated per step
    screenshotBase64: str | None = None   # deprecated — prefer screenshotPath
    screenshotPath: str | None = None     # local path (relative to demo dir)
    screenshotError: str | None = None    # capture failure surface

    # Content-mode detection (recorder fills in; pipeline picks DOM/video)
    contentMode: str = ContentMode.DOM.value
    contentMetadata: dict | None = None

    # rrweb event stream reference (for HYBRID / DOM modes)
    rrwebSnapshotRef: str | None = None

    # Video-mode assets (used when contentMode == video or hybrid)
    videoChunkPath: str | None = None
    videoChunkStartMs: int | None = None
    videoChunkEndMs: int | None = None

    # Voice direction (push-to-talk transcript for the LLM to consume)
    userDirection: str | None = None

    # Workflow-mode intent (agent asks "what are you illustrating?" after click)
    userIntent: str | None = None

    # AI enrichment
    annotation: str | None = None
    voiceoverPath: str | None = None      # local path to per-step audio
    voiceoverBase64: str | None = None    # deprecated — prefer voiceoverPath
    voiceoverWords: list[dict] | None = None  # WordTimestamp[] as dicts
    cursorPath: list[dict] | None = None  # CursorPoint[] as dicts


@dataclass
class DemoSpec:
    """The whole recording. Round-trips through JSON via ``to_dict`` / ``from_dict``."""

    version: int = 1
    id: str = ""
    name: str = ""
    goal: str = ""
    createdAt: str = ""
    viewport: dict = field(default_factory=lambda: {"width": 1440, "height": 900})
    startUrl: str = ""
    steps: list[DemoStep] = field(default_factory=list)
    aiAnnotations: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    # Content-mode
    "ContentMode",
    "ContentMetadata",
    # Geometry
    "Hotspot",
    "BoundingRect",
    "Interaction",
    # Semantic target metadata
    "CAPTURE_SPACES",
    "TARGET_REGIONS",
    "SUPPRESSED_REGIONS",
    "RESULT_ROLES",
    "classify_target_region",
    "normalize_target",
    # Cursor / voice
    "CursorPoint",
    "WordTimestamp",
    # Camera
    "CameraAction",
    "ZoomTarget",
    "AnimationKeyframe",
    # AI payload
    "AIAnnotations",
    # Step + spec
    "DemoStep",
    "DemoSpec",
]
