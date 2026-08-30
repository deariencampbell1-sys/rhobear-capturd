"""Frontman attribution adapter — the ONLY seam between Captur'd and Frontman.

Captur'd Core never knows Frontman exists. The hosted service resolves
attribution through this adapter and nothing else.

Hard rules (V2 directive):

* Frontman owns prospect identity and tracked-send attribution. We reuse its
  opaque send tokens — we do NOT mint a second prospect-token system.
* Only sales-significant signals cross this boundary:
  demo-open, demo-return, demo-complete, demo-cta, demo-branch.
  Step-level noise never reaches Frontman.
* The token stays opaque here. No public Captur'd endpoint may return contact
  name/email/phone based on a token, and unknown tokens must behave exactly
  like no attribution at all.
* The integration origin is server-side configuration only (FRONTMAN_BASE_URL,
  allowlisted by being THE configured origin). No client-supplied callback
  targets, ever.
* Every call is fail-open: telemetry/attribution failures are logged and
  swallowed. Prospect viewing takes priority over telemetry.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

import httpx

from . import config

log = logging.getLogger("capturd.frontman")

# The closed signal vocabulary Frontman understands. Anything else is dropped
# before it leaves this process.
SIGNALS = frozenset({
    "demo-open", "demo-return", "demo-complete", "demo-cta", "demo-branch",
})

_TIMEOUT = httpx.Timeout(6.0, connect=3.0)


def configured() -> bool:
    """True when the bridge origin + key are configured server-side."""
    return bool(config.FRONTMAN_BASE_URL and config.FRONTMAN_BRIDGE_KEY)


def _origin_ok() -> bool:
    try:
        u = urlparse(config.FRONTMAN_BASE_URL)
    except Exception:
        return False
    # https only in production; http allowed for explicit localhost dev.
    if u.scheme == "https":
        return True
    if u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost"):
        return True
    return False


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.FRONTMAN_BRIDGE_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "capturd-service/2.0 (+engagement-adapter)",
    }


def mint_tracked_share(target_url: str, *, contact_id: str = "",
                       name: str = "", channel: str = "capturd") -> dict | None:
    """Ask Frontman to mint an opaque tracked-send token for a Captur'd share.

    Returns {"token": ..., "send_url": ...} or None on any failure. The token
    is Frontman's own (secrets.token_urlsafe inside its sends ledger) — we never
    generate a competing one.
    """
    if not configured() or not _origin_ok():
        log.info("frontman bridge not configured; share will be unattributed")
        return None
    try:
        resp = httpx.post(
            f"{config.FRONTMAN_BASE_URL.rstrip('/')}/internal/capturd/mint",
            json={
                "target_url": target_url[:2048],
                "contact_id": str(contact_id or "")[:128],
                "name": str(name or "")[:120],
                "channel": str(channel or "capturd")[:24],
            },
            headers=_headers(), timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            log.warning("frontman mint failed: %s %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        token = str(data.get("token") or "")
        if not token or len(token) > 128:
            log.warning("frontman mint returned no usable token")
            return None
        return {"token": token, "send_url": str(data.get("send_url") or "")[:2048]}
    except Exception:                                   # noqa: BLE001 — fail-open
        log.exception("frontman mint unreachable; share will be unattributed")
        return None


def signal(token: str, signal_name: str, meta: dict | None = None) -> bool:
    """Report ONE sales-significant action for an opaque token. Fire-and-forget.

    Unknown/invalid tokens are Frontman's business: it answers without
    distinguishing signal, we log and move on. Nothing here may raise.
    """
    if not token or signal_name not in SIGNALS:
        return False
    if not configured() or not _origin_ok():
        return False
    try:
        resp = httpx.post(
            f"{config.FRONTMAN_BASE_URL.rstrip('/')}/internal/capturd/signal",
            json={"token": str(token)[:128], "signal": signal_name,
                  "meta": dict(meta or {})},
            headers=_headers(), timeout=_TIMEOUT,
        )
        return resp.status_code == 200
    except Exception:                                   # noqa: BLE001 — fail-open
        log.debug("frontman signal %s not delivered", signal_name)
        return False
