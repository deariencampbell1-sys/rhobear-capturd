"""The Pro gate is configured by environment — codes, pubkey, license all env-driven."""
import base64
import importlib
import json

import pytest

cryptography = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402


def _reload_pro(monkeypatch, **env):
    for k in ("CAPTURD_PRO_CODES", "CAPTURD_PRO_PUBKEY_B64", "RHOBEAR_CAPTURD_LICENSE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import capturd.pro
    return importlib.reload(capturd.pro)


def test_default_gate_fails_closed(monkeypatch):
    pro = _reload_pro(monkeypatch)
    assert pro.PRO_CONFIG["codes"] == []
    assert pro.PRO_CONFIG["pubkey_b64"] == ""
    assert not pro.is_pro()


def test_codes_from_env(monkeypatch):
    pro = _reload_pro(monkeypatch, CAPTURD_PRO_CODES="CAPTURD-LAUNCH-2026, SECOND-CODE",
                      RHOBEAR_CAPTURD_LICENSE="capturd-launch-2026")
    assert pro.PRO_CONFIG["codes"] == ["CAPTURD-LAUNCH-2026", "SECOND-CODE"]
    assert pro.is_pro()  # case-insensitive code match


def test_signed_license_from_env(monkeypatch):
    key = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        key.public_key().public_bytes_raw()).decode()

    def b64url(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    raw_payload = json.dumps({"v": 1, "product": "capturd"}).encode()
    payload = b64url(raw_payload)
    sig = b64url(key.sign(raw_payload))
    lic = f"{payload}.{sig}"

    pro = _reload_pro(monkeypatch, CAPTURD_PRO_PUBKEY_B64=pub_b64,
                      RHOBEAR_CAPTURD_LICENSE=lic)
    assert pro.is_pro()

    # Tampered payload fails
    monkeypatch.setenv("RHOBEAR_CAPTURD_LICENSE", f"{payload[:-2]}xx.{sig}")
    assert not pro.is_pro()
