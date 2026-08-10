"""Frontend pack compliance tests for the Captur'd app.

Verifies the FIREFLY pack is correctly applied to the service web frontend
(service/web/index.html and service/web/m.html). Checks CSS tokens, font
contract, PWA scope, bear assets, and key class names. No network or browser
needed — these are static HTML analysis tests.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "service" / "web"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def index_html() -> str:
    p = WEB / "index.html"
    assert p.is_file(), f"Missing {p}"
    return p.read_text(encoding="utf-8")


@pytest.fixture
def mobile_html() -> str:
    p = WEB / "m.html"
    assert p.is_file(), f"Missing {p}"
    return p.read_text(encoding="utf-8")


@pytest.fixture
def manifest() -> dict:
    p = WEB / "manifest.webmanifest"
    assert p.is_file(), f"Missing {p}"
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.fixture
def sw_js() -> str:
    p = WEB / "sw.js"
    assert p.is_file(), f"Missing {p}"
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------

def test_index_html_exists(index_html):
    """The desktop studio entrypoint is served at GET /."""
    assert "<title>RHOBEAR Captur'd" in index_html
    assert "RHOBEAR <em>Captur'd</em>" in index_html


def test_mobile_html_exists(mobile_html):
    """The mobile studio entrypoint is served at GET /m."""
    assert "<title>RHOBEAR Captur'd" in mobile_html
    assert "RHOBEAR" in mobile_html
    assert "Captur'd" in mobile_html


# ---------------------------------------------------------------------------
# Pack tokens
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token", [
    "--capturd-bg:           #0A0F14",
    "--capturd-surface:      #131E2B",
    "--capturd-surface-2:    #1A2435",
    "--capturd-accent:       #4B7AC8",
    "--capturd-accent-dim:   rgba(75,122,200,0.12)",
    "--capturd-text-primary:   #E8F0F7",
    "--capturd-text-section:   #2A8FA8",
    "--capturd-scripting:   #22C55E",
    "--capturd-recording:   #F59E0B",
    "--capturd-danger:      #EF4444",
    "--capturd-font-display: 'rokkitt'",
    "--capturd-font-body:    'lato'",
    "--capturd-font-mono:    'droid-sans-mono'",
])
def test_index_tokens(index_html, token):
    """Every pack CSS token is present in the desktop app."""
    assert token in index_html


@pytest.mark.parametrize("token", [
    "--capturd-bg:#0A0F14",
    "--capturd-accent:#4B7AC8",
    "--capturd-text-section:#2A8FA8",
    "--capturd-font-display:'rokkitt'",
    "--capturd-font-body:'lato'",
    "--capturd-font-mono:'droid-sans-mono'",
    "--capturd-font-brand:'birch-std'",
])
def test_mobile_tokens(mobile_html, token):
    """Every pack CSS token is present in the mobile app."""
    # Remove whitespace for comparison since mobile has no spaces after colons
    condensed = mobile_html.replace(" ", "").replace("\n", "")
    check = token.replace(" ", "")
    assert check in condensed, f"Token '{token}' not found in mobile HTML"


# ---------------------------------------------------------------------------
# Font contract (Typekit sbv5bcv only)
# ---------------------------------------------------------------------------

def test_typekit_link(index_html):
    """The desktop app uses the Typekit font contract — no self-hosted fonts."""
    assert "use.typekit.net/sbv5bcv.css" in index_html
    # No Nacelle
    assert "nacelle" not in index_html.lower()
    # No Google Fonts
    assert "fonts.googleapis.com" not in index_html
    assert "fonts.gstatic.com" not in index_html


def test_mobile_typekit_link(mobile_html):
    """The mobile app uses the Typekit font contract — no self-hosted fonts."""
    assert "use.typekit.net/sbv5bcv.css" in mobile_html
    assert "nacelle" not in mobile_html.lower()
    assert "fonts.googleapis.com" not in mobile_html
    assert "fonts.gstatic.com" not in mobile_html


# ---------------------------------------------------------------------------
# Brand wordmark
# ---------------------------------------------------------------------------

def test_index_wordmark(index_html):
    """Wordmark uses rokkitt for RHOBEAR + birch-std for Captur'd."""
    assert "RHOBEAR <em>Captur'd</em>" in index_html
    assert "--capturd-font-brand" in index_html
    assert "birch-std" in index_html


def test_mobile_wordmark(mobile_html):
    """Mobile wordmark uses rokkitt for RHOBEAR + birch-std for Captur'd."""
    assert "RHOBEAR" in mobile_html
    assert "Captur'd" in mobile_html


# ---------------------------------------------------------------------------
# Bear asset
# ---------------------------------------------------------------------------

def test_index_bear_asset(index_html):
    """The blue constellation bear is referenced in the desktop app."""
    assert "/assets/capturd-bear.png" in index_html


def test_mobile_bear_asset(mobile_html):
    """The blue constellation bear is referenced in the mobile app."""
    assert "/assets/capturd-bear.png" in mobile_html


def test_bear_file_exists():
    """The bear asset file exists on disk."""
    assert (WEB / "assets" / "capturd-bear.png").is_file()


# ---------------------------------------------------------------------------
# Studio structure — class names
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls", [
    "capturd-studio",
    "capturd-studio-grid",
    "capturd-step__header",
    "capturd-step__badge",
    "capturd-step__label",
    "capturd-film-btn",
    "capturd-filming",
    "capturd-stage",
    "capturd-stage--done",
    "capturd-stage--active",
])
def test_index_studio_classes(index_html, cls):
    """Key pack class names are present in the desktop app."""
    assert cls in index_html


@pytest.mark.parametrize("cls", [
    "capturd-film-btn",
    "capturd-filming",
    "capturd-filming__bear",
    "capturd-filming__bar",
    "capturd-filming__chips",
])
def test_mobile_studio_classes(mobile_html, cls):
    """Key pack class names are present in the mobile app."""
    assert cls in mobile_html


# ---------------------------------------------------------------------------
# Studio features
# ---------------------------------------------------------------------------

def test_index_has_all_steps(index_html):
    """All 4 studio steps are present."""
    assert "Step 1" in index_html
    assert "Step 2" in index_html
    assert "Step 3" in index_html
    assert "Step 4" in index_html


def test_index_has_all_templates(index_html):
    """All 6 demo type templates are present."""
    templates = [
        "saas-walkthrough",
        "ux-showcase",
        "feature-spotlight",
        "tutorial-longform",
        "social-teaser",
        "login-flow",
    ]
    for t in templates:
        assert f'data-tpl="{t}"' in index_html


def test_index_has_all_voices(index_html):
    """All 6 voices (5 HD + Aria classic) are present."""
    assert "vertex:Charon:warm" in index_html
    assert "vertex:Kore:warm" in index_html
    assert "vertex:Aoede:warm" in index_html
    assert "vertex:Fenrir:trailer" in index_html
    assert "vertex:Zephyr:warm" in index_html
    # Aria classic (no HD)
    assert 'class="voice classic"' in index_html


def test_index_has_all_aspects(index_html):
    """All 3 aspect ratios are present."""
    assert 'data-aspect="16:9"' in index_html
    assert 'data-aspect="9:16"' in index_html
    assert 'data-aspect="1:1"' in index_html


def test_index_has_filming_chips(index_html):
    """Filming chip slots for type/voice/aspect are present."""
    assert "id=\"filmChips\"" in index_html


def test_index_has_four_stages(index_html):
    """All 4 filming stages are present."""
    for stage in ["Scripting", "Navigating", "Recording", "Rendering"]:
        assert stage in index_html


# ---------------------------------------------------------------------------
# Mobile-specific features
# ---------------------------------------------------------------------------

def test_mobile_has_surface_guard(mobile_html):
    """Mobile has a wrong-surface guard that redirects pointer users to /."""
    assert "surface" in mobile_html
    assert "matchMedia" in mobile_html


def test_mobile_has_plan_pill(mobile_html):
    """Mobile has plan pill and sign-out."""
    assert "planPill" in mobile_html
    assert "signout" in mobile_html


# ---------------------------------------------------------------------------
# PWA manifest
# ---------------------------------------------------------------------------

def test_manifest_properties(manifest):
    """PWA manifest has required fields."""
    assert manifest["name"] == "RHOBEAR Captur'd"
    assert manifest["short_name"] == "Captur'd"
    assert manifest["start_url"] == "/?src=pwa"
    assert manifest["display"] == "standalone"
    assert manifest["background_color"] == "#0A0F14"
    assert manifest["theme_color"] == "#4B7AC8"


def test_manifest_icons(manifest):
    """PWA manifest has all required icon sizes."""
    sizes = {"192x192", "512x512"}
    found = {i["sizes"] for i in manifest["icons"]}
    assert sizes.issubset(found), f"Missing icon sizes: {sizes - found}"


def test_manifest_maskable_icons(manifest):
    """PWA manifest has maskable icons."""
    maskable = [i for i in manifest["icons"] if "maskable" in i.get("purpose", "")]
    assert len(maskable) >= 2, "Expected at least 2 maskable icons"


# ---------------------------------------------------------------------------
# Service worker
# ---------------------------------------------------------------------------

def test_sw_version(sw_js):
    """Service worker has a version string."""
    assert "VERSION" in sw_js


def test_sw_shell_assets(sw_js):
    """Service worker caches the shell assets."""
    for asset in ["/", "/m", "/manifest.webmanifest", "/assets/capturd-bear.png"]:
        assert asset in sw_js, f"SW missing shell asset: {asset}"


def test_sw_network_first_navigation(sw_js):
    """Service worker uses network-first for HTML navigations."""
    assert "request.mode === 'navigate'" in sw_js
    assert "fetch(request)" in sw_js


def test_sw_no_cache_api(sw_js):
    """Service worker never caches API/auth/billing paths."""
    assert "/api/" in sw_js
    assert "/auth/" in sw_js
    assert "/billing/" in sw_js


# ---------------------------------------------------------------------------
# No old residue
# ---------------------------------------------------------------------------

def test_no_old_accent_colors(index_html):
    """No old accent colors remain in the desktop app."""
    for old in ["#4a9eff", "#2e7fdd", "#080810", "#111120"]:
        assert old not in index_html, f"Old color '{old}' still present in index.html"


def test_mobile_no_old_accent_colors(mobile_html):
    """No old accent colors remain in the mobile app."""
    for old in ["#4a9eff", "#2e7fdd", "#080810", "#111120"]:
        assert old not in mobile_html, f"Old color '{old}' still present in m.html"


# ---------------------------------------------------------------------------
# Desktop surface guard
# ---------------------------------------------------------------------------

def test_index_surface_guard(index_html):
    """Desktop has a surface guard that redirects phones to /m."""
    assert "matchMedia" in index_html
    assert "screen.width" in index_html
    assert "location.replace('/m'" in index_html


# ---------------------------------------------------------------------------
# Companion embed
# ---------------------------------------------------------------------------

def test_index_companion_embed(index_html):
    """Rho companion embed script is present."""
    assert "RHOBEAR_COMPANION" in index_html
    assert "companion-embed.js" in index_html
    assert "#4B7AC8" in index_html  # accent passed to companion


def test_mobile_companion_embed(mobile_html):
    """Rho companion embed script is present in mobile."""
    assert "RHOBEAR_COMPANION" in mobile_html
    assert "companion-embed.js" in mobile_html


# ---------------------------------------------------------------------------
# Browser-based contrast assertion (requires server + Playwright)
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_mobile_shot_card_contrast():
    """Mobile shot card text is light on dark, not black.

    Starts the app, opens the mobile page in a mobile viewport, selects a
    .tpl card, and asserts the computed color of the <b> text is a light
    color (not black / default ButtonText). Uses the playwright sync API
    and the with_server context from capture_screenshots.
    """
    from playwright.sync_api import sync_playwright
    from scripts.capture_screenshots import with_server

    def check(port):
        base = f"http://127.0.0.1:{port}"
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                viewport={"width": 390, "height": 844},
                device_scale_factor=3,
                is_mobile=True,
                has_touch=True,
            )
            page = ctx.new_page()
            page.goto(base + "/m")
            page.wait_for_load_state("networkidle")

            # Pick the first .tpl card and get the <b> text's computed color
            color = page.evaluate("""
                () => {
                    const tpl = document.querySelector('.tpl b');
                    if (!tpl) return null;
                    return getComputedStyle(tpl).color;
                }
            """)
            assert color is not None, "Could not find .tpl b element"

            # Parse the rgb(a) value and verify it's light (not black)
            # Light colors have high R/G/B values; black = rgb(0,0,0)
            import re
            m = re.match(r'rgba?\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)', color)
            assert m, f"Could not parse color value: {color}"
            r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))

            # Dark text on dark bg would be < 100 per channel
            assert r > 100, f"Shot card text is too dark (red channel {r}): {color}"
            assert g > 100, f"Shot card text is too dark (green channel {g}): {color}"
            assert b > 100, f"Shot card text is too dark (blue channel {b}): {color}"

            # Verify no console errors
            errors = []
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
            page.goto(base + "/m")
            page.wait_for_load_state("networkidle")
            assert len(errors) == 0, f"Console errors on mobile page: {errors}"

            ctx.close()
            browser.close()

    with_server(check)