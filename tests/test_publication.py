"""GAPS-NOW M9 (JOURNEY U2/E10, brief 09 items 1-2, L9 CONTRACT §4):
an immutable `rhobear.demo-package.v1` package from the approved takes.

* Only from a review whose selected takes are all approved (§6).
* Media facts are measured (ffprobe), chapters are the render timeline's step
  windows, captions and transcript are the narration files; nothing invented.
* The folder is content-addressed: same inputs, same revision; a change is a
  new revision and an old one never mutates.
* `approved` needs a named approver, a time, `watchedComplete: true`, captions
  and a transcript (L9's hard rules); otherwise the package is `pending`.
* URLs are https, revision-scoped and carry no signing parameters.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess

import pytest

from capturd.engine.take_store import TakeStore
from capturd.walk import exporter
from capturd.walk.publication import PackageError, build_package

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                reason="needs ffmpeg + ffprobe")

REV = "a" * 40
BASE = "https://media.example.com/demos/capturd-making-of"


def _approved_demo(tmp_path):
    from test_poster import _spec  # same two-step demo
    from capturd.walk.coordinator import DemoForge

    forge = DemoForge(demos_dir=tmp_path / "demos")
    forge.save_spec("d", _spec(b"\x89PNG first"))
    store = TakeStore(forge)
    for take in store.open_review("d")["takes"]:
        store.approve("d", take["shotIndex"], 0, by="Neo", expected_revision=take["sourceRevision"])
    folder = forge.demo_dir("d")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x48:d=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(folder / "walkthrough.mp4")],
                   check=True)
    (folder / "poster.png").write_bytes(b"\x89PNG first")
    (folder / "walkthrough.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHi\n")
    (folder / "walkthrough.transcript.txt").write_text("Hi\n")
    (folder / "walkthrough.chapters.json").write_text(json.dumps(
        [{"label": "Open", "startSeconds": 0.0}, {"label": "Pick", "startSeconds": 1.2}]))
    (folder / "export.html").write_text("<html>viewer</html>")
    return forge


def _build(forge, **kw):
    args = dict(scenario_id="capturd-making-of", engine_or_product="capturd",
                source_app="capturd", source_revision=REV, base_url=BASE)
    args.update(kw)
    return build_package(forge, "d", **args)


def test_a_pending_package_has_the_wire_shape_and_measured_media(tmp_path):
    forge = _approved_demo(tmp_path)
    manifest, folder = _build(forge)
    assert manifest["schema"] == "rhobear.demo-package.v1"
    assert manifest["publicationState"] == "pending"
    assert manifest["sourceRevision"] == REV and manifest["immutableUrls"] is True
    rendition = manifest["renditions"][0]
    assert (rendition["mime"], rendition["width"], rendition["height"]) == ("video/mp4", 64, 48)
    assert abs(rendition["durationSeconds"] - 2.0) < 0.1
    assert rendition["chapters"] == [{"label": "Open", "startSeconds": 0.0},
                                     {"label": "Pick", "startSeconds": 1.2}]
    rev = manifest["packageRevision"]
    for key in ("posterUrl", "captionsUrl", "transcriptUrl"):
        assert manifest[key].startswith(f"{BASE}/{rev}/")
    assert manifest["interactive"]["url"] == f"{BASE}/{rev}/export.html"
    assert (folder / "walkthrough.mp4").read_bytes() == \
        (forge.demo_dir("d") / "walkthrough.mp4").read_bytes()
    on_disk = json.loads((folder / "manifest.json").read_text())
    assert on_disk == manifest


def test_the_content_hash_covers_the_manifest_without_itself(tmp_path):
    manifest, _ = _build(_approved_demo(tmp_path))
    body = {k: v for k, v in manifest.items() if k != "contentHash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    assert manifest["contentHash"] == "sha256:" + hashlib.sha256(canonical).hexdigest()
    hashes = manifest["evidence"]["artifactHashes"]
    assert len(hashes) == 6 and all(h.startswith("sha256:") for h in hashes)


def test_the_approved_takes_are_the_evidence(tmp_path):
    manifest, _ = _build(_approved_demo(tmp_path))
    takes = manifest["evidence"]["inputs"]["takes"]
    assert [(t["shotIndex"], t["takeIndex"], t["state"]) for t in takes] == \
        [(0, 0, "approved"), (1, 0, "approved")]
    assert manifest["evidence"]["runIds"] == ["d"]


def test_same_inputs_same_revision_and_a_change_is_a_new_revision(tmp_path):
    forge = _approved_demo(tmp_path)
    first, folder = _build(forge)
    again, _ = _build(forge)
    assert again == first
    (forge.demo_dir("d") / "walkthrough.transcript.txt").write_text("Hi there\n")
    changed, new_folder = _build(forge)
    assert changed["packageRevision"] != first["packageRevision"]
    assert (folder / "walkthrough.transcript.txt").read_text() == "Hi\n", "old revision untouched"


def test_approved_needs_the_whole_approval(tmp_path):
    forge = _approved_demo(tmp_path)
    manifest, _ = _build(forge, approved_by="Neo", watched_complete=True, privacy_reviewed=True)
    assert manifest["publicationState"] == "approved"
    assert manifest["approval"]["approvedBy"] == "Neo" and manifest["approval"]["watchedComplete"] is True
    with pytest.raises(PackageError, match="watched"):
        _build(forge, approved_by="Neo", watched_complete=False, privacy_reviewed=True)


def test_approved_needs_captions_and_transcript(tmp_path):
    forge = _approved_demo(tmp_path)
    (forge.demo_dir("d") / "walkthrough.vtt").unlink()
    with pytest.raises(PackageError, match="captions"):
        _build(forge, approved_by="Neo", watched_complete=True, privacy_reviewed=True)


def test_unapproved_takes_block_the_package(tmp_path):
    forge = _approved_demo(tmp_path)
    store = TakeStore(forge)
    store.revoke("d", 1, 0, by="Neo", reason="wrong page")
    with pytest.raises(PackageError, match="shot 1"):
        _build(forge)


def test_without_a_review_there_is_no_package(tmp_path):
    forge = _approved_demo(tmp_path)
    (forge.demo_dir("d") / "takes.json").unlink()
    with pytest.raises(PackageError, match="review"):
        _build(forge)


@pytest.mark.parametrize("base, revision", [
    ("http://media.example.com/x", REV),
    ("https://media.example.com/x?X-Amz-Expires=60", REV),
    ("https://media.example.com/x?token=1", REV),
    (BASE, "abc"),
])
def test_bad_urls_and_revisions_are_refused(tmp_path, base, revision):
    with pytest.raises(PackageError):
        _build(_approved_demo(tmp_path), base_url=base, source_revision=revision)


def test_the_mp4_export_writes_real_chapter_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, "find_ffmpeg", lambda: "ffmpeg")

    async def fake_capture(viewer_html, viewport, frames_dir, fps):
        (frames_dir / exporter.PLAN_FILE).write_text(json.dumps(
            {"stepWindows": [{"index": 0, "tStart": 0, "tEnd": 1500},
                             {"index": 1, "tStart": 1500, "tEnd": 4000}]}))
        return 2, 4.4, []

    monkeypatch.setattr(exporter, "_capture_frames", fake_capture)
    monkeypatch.setattr(exporter, "_encode_mp4", lambda *a: a[4].write_bytes(b"mp4"))
    html = tmp_path / "export.html"
    html.write_text("<html></html>")
    steps = [{"index": 0, "annotation": "Open the picker."}, {"index": 1, "annotation": None}]
    exporter.export_video({"steps": steps}, html, tmp_path / "walkthrough.mp4")
    assert json.loads((tmp_path / "walkthrough.chapters.json").read_text()) == [
        {"label": "Open the picker.", "startSeconds": 0.0},
        {"label": "Step 2", "startSeconds": 1.5}]


def test_the_package_tool(tmp_path):
    import asyncio

    from capturd.mcp.server import _build_server

    forge = _approved_demo(tmp_path)
    server = _build_server(forge)

    def call(args):
        result = asyncio.run(server.call_tool("demo.package", args))
        return result.structured_content.get("result", result.structured_content)

    args = {"demo_id": "d", "scenario_id": "capturd-making-of", "engine_or_product": "capturd",
            "source_app": "capturd", "source_revision": REV, "base_url": BASE}
    ok = call(args)
    assert ok["ok"] is True and ok["manifest"]["publicationState"] == "pending"
    assert ok["path"].endswith(ok["manifest"]["packageRevision"])
    refused = call({**args, "base_url": "http://x.example.com"})
    assert refused["ok"] is False and "https" in refused["detail"]
