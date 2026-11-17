"""GAPS-NOW M10 (JOURNEY X7, brief 09 item 6): a secret never reaches a package.

The automated scan reads every text a demo carries (typed values, page URLs,
lines, voice transcripts, captions and transcript files) for credential
shapes, and a typed password is always a finding. A finding blocks the
package and names where it is and what kind, never the value. Pixels cannot
be scanned here, so an `approved` package also needs a person to confirm the
frames were checked (`privacy_reviewed`). Automated detection is an aid.
"""
from __future__ import annotations

import json

import pytest

from capturd.walk.privacy import scan_demo
from capturd.walk.publication import PackageError

SECRETS = {
    "aws": "AKIA" + "ABCDEFGHIJKLMNOP",
    "github": "ghp_" + "a" * 36,
    "stripe": "sk_live_" + "b" * 24,
    "slack": "xoxb-" + "1234567890-abcdef",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlLXZhbHVl",
    "private_key": "-----BEGIN PRIVATE KEY-----",
}


def _spec(**step):
    base = {"index": 0, "pageUrl": "https://app.example.com/home", "annotation": "Open it.",
            "interaction": {"type": "click", "target": {"selector": "#a", "text": "Go"},
                            "hotspot": {}, "value": None}}
    base.update(step)
    return {"steps": [base]}


def test_a_clean_demo_has_no_findings():
    assert scan_demo(_spec(), texts={}) == []


@pytest.mark.parametrize("kind", sorted(SECRETS))
def test_each_credential_shape_is_found_without_echoing_it(kind):
    spec = _spec(annotation=f"token is {SECRETS[kind]}")
    findings = scan_demo(spec, texts={})
    assert findings == [{"where": "step 0 line", "kind": kind}]
    assert SECRETS[kind] not in json.dumps(findings)


def test_a_typed_password_is_always_a_finding():
    spec = _spec(interaction={"type": "input", "target": {"selector": "#password", "type": "password"},
                              "hotspot": {}, "value": "hunter2"})
    assert {"where": "step 0 typed value", "kind": "password_field"} in scan_demo(spec, texts={})


def test_secret_url_parameters_and_userinfo_are_found():
    spec = _spec(pageUrl="https://user:pw@app.example.com/x?api_key=123&page=2")
    kinds = {f["kind"] for f in scan_demo(spec, texts={})}
    assert kinds == {"url_userinfo", "url_secret_parameter"}


def test_caption_and_transcript_files_are_scanned():
    findings = scan_demo(_spec(), texts={"transcript": "say " + SECRETS["github"]})
    assert findings == [{"where": "transcript", "kind": "github"}]


def test_voice_direction_is_scanned():
    findings = scan_demo(_spec(userDirection="my key " + SECRETS["aws"]), texts={})
    assert findings == [{"where": "step 0 voice", "kind": "aws"}]


# --- the package gate ---------------------------------------------------------

def test_a_finding_blocks_the_package_by_where_and_kind(tmp_path):
    from test_publication import _approved_demo, _build

    forge = _approved_demo(tmp_path)
    (forge.demo_dir("d") / "walkthrough.transcript.txt").write_text("key " + SECRETS["stripe"])
    with pytest.raises(PackageError) as exc:
        _build(forge)
    assert "transcript: stripe" in str(exc.value) and SECRETS["stripe"] not in str(exc.value)
    assert not (forge.demo_dir("d") / "package").exists()


def test_approval_needs_a_person_to_confirm_the_frames(tmp_path):
    from test_publication import _approved_demo, _build

    forge = _approved_demo(tmp_path)
    with pytest.raises(PackageError, match="frames"):
        _build(forge, approved_by="Neo", watched_complete=True, privacy_reviewed=False)
    manifest, _ = _build(forge, approved_by="Neo", watched_complete=True, privacy_reviewed=True)
    assert manifest["evidence"]["privacyCheck"] == {"automated": "clean", "framesReviewedBy": "Neo"}
