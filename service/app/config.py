r"""Config + the credential boundary.

Everything the service needs is here. The OWNER-GATED credentials are read from
env (or the agent vault) and each has an honest "configured?" flag so the app
degrades gracefully and tells the truth instead of faking a broken flow.

The ONLY hard stops for going live (owner's words: "you can't install something
if my PayPal credentials aren't in there"):
  * PRO_CHECKOUT_URL      — the buy button target (Stripe Payment Link / PayPal subscribe URL)
  * BILLING_WEBHOOK_SECRET — verifies the "payment succeeded" callback that flips a user to Pro

Drop them in the environment (or D:\rhobear-agent-vault\ files) and the flow is live.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

# Platform-appropriate defaults. The Windows defaults keep the owner's box
# behavior bit-for-bit; on Linux (the VPS deploy) a Windows path would silently
# become a relative directory named ``D:\...`` — the old landmine. Env vars
# always win (the systemd unit sets CAPTURD_DATA_DIR=/var/lib/capturd).
#
# Kept as tiny pure functions (taking os.name) so both the nt and posix branches
# are directly unit-testable on any host — the reviewer's Windows-default branch
# can be asserted without a Windows CI lane.
def _default_data_dir(os_name: str) -> Path:
    if os_name == "nt":
        return Path(r"D:\capturd-service\data")
    return Path("/var/lib/capturd")


def _default_vault_dir(os_name: str) -> Path:
    if os_name == "nt":
        return Path(r"D:\rhobear-agent-vault")
    return Path("/var/lib/capturd-agent-vault")


_DEFAULT_DATA_DIR = _default_data_dir(os.name)
_DEFAULT_VAULT = _default_vault_dir(os.name)
# User-writable fallback for unprivileged/non-systemd Linux runs (see ensure_dirs).
_FALLBACK_DATA_DIR = Path.home() / ".local" / "share" / "capturd"

def _vault_dir() -> Path:
    v = os.environ.get("CAPTURD_VAULT_DIR", "").strip()
    return Path(v) if v else _DEFAULT_VAULT


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name, "").strip()
    if v:
        return v
    # optional vault fallback: a file named after the var (lowercased) holds the value
    f = _vault_dir() / f"{name.lower()}.txt"
    if f.is_file():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    return default


VAULT = _vault_dir()


# ---- service basics ----------------------------------------------------------
BASE_URL = _env("CAPTURD_BASE_URL", "http://127.0.0.1:8099")
DATA_DIR = Path(_env("CAPTURD_DATA_DIR", str(_DEFAULT_DATA_DIR)))
# Jobs live under the DATA_DIR root, so an overridden CAPTURD_DATA_DIR keeps DB
# and job artifacts in one tree rather than silently splitting them across two
# roots. CAPTURD_JOBS_DIR still wins when set explicitly.
JOBS_DIR = Path(_env("CAPTURD_JOBS_DIR", str(DATA_DIR / "jobs")))
DB_PATH = DATA_DIR / "capturd.sqlite3"
SESSION_SECRET = _env("CAPTURD_SESSION_SECRET") or secrets.token_hex(32)

# ---- plan limits (canon: Free = 1 generation + 20 shots) ---------------------
FREE_GENERATION_LIMIT = int(_env("CAPTURD_FREE_GEN_LIMIT", "1"))
FREE_SHOT_LIMIT = int(_env("CAPTURD_FREE_SHOT_LIMIT", "20"))
PRO_PRICE = _env("CAPTURD_PRO_PRICE", "$19")

# ---- render spend caps (paid Vertex TTS + Chromium is metered) ---------------
# Every account — Pro included — is bounded on how many renders it can start,
# so one account (or a runaway script holding a Pro key) cannot drain the
# Vertex/chromium budget. Free is already lifetime-capped to
# FREE_GENERATION_LIMIT; these add a per-account in-flight concurrency cap and
# a sliding hourly window, enforced on POST /api/generate with 429 + Retry-After.
RENDER_MAX_CONCURRENT = int(_env("CAPTURD_RENDER_MAX_CONCURRENT", "2"))
RENDER_MAX_PER_HOUR = int(_env("CAPTURD_RENDER_MAX_PER_HOUR", "10"))

# ---- OWNER-GATED credentials (honest flags) ----------------------------------
# Billing — two paths. The CANON path (Lane K) creates a Stripe Checkout Session
# server-side so the founder coupon auto-applies. The legacy path redirects to a
# static PRO_CHECKOUT_URL (Payment Link). Session-creation wins when configured.
STRIPE_SECRET_KEY = _env("STRIPE_SECRET_KEY")             # test: sk_test_...  (NEVER commit)
CAPTURD_PRICE_ID = _env("CAPTURD_PRICE_ID")               # price for lookup_key rhobear_capturd_pro
CAPTURD_COUPON_ID = _env("CAPTURD_COUPON_ID", "rhobear_capturd_founders_25")
PRO_CHECKOUT_URL = _env("PRO_CHECKOUT_URL")               # buy button target (legacy static link)
BILLING_WEBHOOK_SECRET = _env("BILLING_WEBHOOK_SECRET")

GW_API_KEY = _env("RHOBEAR_GW_API_KEY")               # optional: agent self-drive

# Central auth — identity comes from auth.rhobear.ai
RHOBEAR_AUTH_BASE = _env("RHOBEAR_AUTH_BASE", "https://auth.rhobear.ai")

# Billing is "configured" if EITHER checkout path is usable. The session-creation
# path (canon) needs the secret key + price id; legacy needs a static URL.
_BILLING_SESSION_READY = bool(STRIPE_SECRET_KEY and CAPTURD_PRICE_ID)
_BILLING_LEGACY_READY = bool(PRO_CHECKOUT_URL)

# Enterprise self-host: this whole service IS the enterprise edition when run on
# the customer's own box. No separate build — same code, self-hosted.
EDITION = _env("CAPTURD_EDITION", "hosted")           # hosted | enterprise


def status() -> dict:
    """Honest readiness — what's wired vs what waits on the owner's credentials."""
    return {
        "billing_configured": bool(_BILLING_SESSION_READY or _BILLING_LEGACY_READY),
        "billing_session_creation": _BILLING_SESSION_READY,
        "webhook_configured": bool(BILLING_WEBHOOK_SECRET),
        "gateway_configured": bool(GW_API_KEY),
        "edition": EDITION,
    }


def ensure_dirs() -> None:
    """Create the data/job dirs, falling back to a user-writable location.

    The Linux default (``/var/lib/capturd``) is root/systemd-oriented. When the
    service is run outside the unit (dev container, manual run, user namespace)
    that path is not writable and would raise ``PermissionError`` at startup;
    instead fall back to ``~/.local/share/capturd`` (and rebuild the dependent
    ``JOBS_DIR``/``DB_PATH``) so an unprivileged run still comes up. The systemd
    unit always sets ``CAPTURD_DATA_DIR`` so production never hits this path.
    """
    global DATA_DIR, JOBS_DIR, DB_PATH

    def _mkdir_ok(base: Path) -> bool:
        try:
            base.mkdir(parents=True, exist_ok=True)
            (base / "jobs").mkdir(parents=True, exist_ok=True)
            return True
        except PermissionError:
            return False

    if _mkdir_ok(DATA_DIR):
        return

    fallback = _FALLBACK_DATA_DIR
    if not _mkdir_ok(fallback):
        raise PermissionError(
            f"cannot create data dir {DATA_DIR} and cannot fall back to {fallback} "
            "(run under systemd, or set CAPTURD_DATA_DIR / CAPTURD_JOBS_DIR to a "
            "writable path)"
        )
    print(
        f"[capturd] data dir not writable ({DATA_DIR}); using {fallback} "
        "(set CAPTURD_DATA_DIR to silence)",
        file=sys.stderr,
    )
    DATA_DIR = fallback
    JOBS_DIR = fallback / "jobs"
    DB_PATH = DATA_DIR / "capturd.sqlite3"
