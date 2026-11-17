"""GAPS-NOW M7 (JOURNEY E10, brief 09 item 4): captions from the real narration.

Cues come only from each step's TTS word timings (`voiceoverWords`) placed at
the step's audio offset in the rendered timeline (`audio_plan` atMs, the same
offset the MP4's audio is muxed at). No words, no cue: nothing is invented.
Cues are ordered, never overlap, and escape WebVTT's special characters.
"""
from __future__ import annotations

import re

import pytest

from capturd.walk import exporter
from capturd.walk.captions import build_cues, to_transcript, to_webvtt


def _words(*triples):
    return [{"word": w, "tStartMs": a, "tEndMs": b} for w, a, b in triples]


STEPS = [
    {"index": 0, "voiceoverWords": _words(("Open", 0, 300), ("the", 300, 450), ("picker.", 450, 900))},
    {"index": 1, "voiceoverWords": None, "annotation": "no narration here"},
    {"index": 2, "voiceoverWords": _words(("Figurine", 0, 500), ("<is>", 500, 700),
                                          ("best", 700, 900), ("-->", 900, 1000), ("&", 1000, 1100))},
]
PLAN = [{"stepIndex": 0, "atMs": 1000}, {"stepIndex": 2, "atMs": 6000}]


def test_cues_sit_at_the_rendered_audio_offsets():
    cues = build_cues(STEPS, PLAN)
    assert cues[0]["startMs"] == 1000 and cues[0]["endMs"] == 1900
    assert cues[0]["text"] == "Open the picker."
    assert cues[-1]["endMs"] == 7100


def test_no_words_means_no_cue():
    cues = build_cues(STEPS, PLAN)
    assert not any("no narration" in c["text"] for c in cues)
    assert build_cues([{"index": 0, "annotation": "x"}], [{"stepIndex": 0, "atMs": 0}]) == []


def test_cues_are_ordered_and_never_overlap():
    many = [{"index": 0, "voiceoverWords": _words(*[(f"w{i}", i * 200, i * 200 + 250) for i in range(40)])}]
    cues = build_cues(many, [{"stepIndex": 0, "atMs": 0}])
    assert len(cues) > 1
    for a, b in zip(cues, cues[1:]):
        assert a["startMs"] < a["endMs"] <= b["startMs"] < b["endMs"]


def test_webvtt_is_well_formed_and_escaped():
    vtt = to_webvtt(build_cues(STEPS, PLAN))
    assert vtt.startswith("WEBVTT\n\n")
    assert "00:00:01.000 --> 00:00:01.900" in vtt
    body = vtt.split("\n\n", 1)[1]
    text_lines = [line for line in body.splitlines() if line and "-->" not in line or line.endswith("&gt;")]
    assert "&lt;is&gt;" in vtt and "--&gt;" in vtt and "&amp;" in vtt
    assert "<is>" not in vtt
    for line in body.splitlines():
        if "-->" in line and not line.startswith("0"):
            pytest.fail(f"an unescaped arrow sits in cue text: {line}")


def test_transcript_is_the_spoken_words_in_order():
    assert to_transcript(STEPS) == "Open the picker.\n\nFigurine <is> best --> &\n"


def test_mp4_export_writes_captions_and_transcript_beside_the_video(tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, "find_ffmpeg", lambda: "ffmpeg")

    async def fake_capture(viewer_html, viewport, frames_dir, fps):
        return 2, 8.0, PLAN

    def fake_encode(ffmpeg, frames_dir, fps, duration_s, out_path, clips):
        out_path.write_bytes(b"mp4")

    monkeypatch.setattr(exporter, "_capture_frames", fake_capture)
    monkeypatch.setattr(exporter, "_encode_mp4", fake_encode)
    html = tmp_path / "export.html"
    html.write_text("<html></html>")
    narrated = [dict(step, voiceoverBase64="YXVkaW8=") for step in STEPS]
    out = exporter.export_video({"steps": narrated}, html, tmp_path / "walkthrough.mp4")
    vtt = (tmp_path / "walkthrough.vtt").read_text()
    assert re.search(r"00:00:06\.000 --> ", vtt)
    assert (tmp_path / "walkthrough.transcript.txt").read_text() == to_transcript(STEPS)
    assert out.read_bytes() == b"mp4"


def test_no_narration_writes_no_caption_file(tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, "find_ffmpeg", lambda: "ffmpeg")

    async def fake_capture(viewer_html, viewport, frames_dir, fps):
        return 2, 2.0, []

    monkeypatch.setattr(exporter, "_capture_frames", fake_capture)
    monkeypatch.setattr(exporter, "_encode_mp4",
                        lambda *a: a[4].write_bytes(b"mp4"))
    html = tmp_path / "export.html"
    html.write_text("<html></html>")
    exporter.export_video({"steps": [{"index": 0}]}, html, tmp_path / "walkthrough.mp4")
    assert not (tmp_path / "walkthrough.vtt").exists()


def test_export_captions_only_include_clips_passed_to_encoder(tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, "find_ffmpeg", lambda: "ffmpeg")
    async def capture(*args):
        return 2, 5.0, [{"stepIndex": i, "atMs": i * 1000} for i in range(3)]
    monkeypatch.setattr(exporter, "_capture_frames", capture)
    received = []
    def encode(*args):
        received.extend(args[-1])
        args[4].write_bytes(b"mp4")
    monkeypatch.setattr(exporter, "_encode_mp4", encode)
    html = tmp_path / "viewer.html"
    html.write_text("viewer")
    steps = [
        {"voiceoverWords": _words(("Spoken", 0, 500)), "voiceoverBase64": "YXVkaW8="},
        {"voiceoverWords": _words(("Missing", 0, 500))},
        {"voiceoverWords": _words(("Invalid", 0, 500)), "voiceoverBase64": "!!!"},
    ]
    exporter.export_video({"steps": steps}, html, tmp_path / "walkthrough.mp4")
    assert len(received) == 1
    assert "Spoken" in (tmp_path / "walkthrough.vtt").read_text()
    assert "Missing" not in (tmp_path / "walkthrough.vtt").read_text()
    assert "Invalid" not in (tmp_path / "walkthrough.vtt").read_text()
    assert (tmp_path / "walkthrough.transcript.txt").read_text() == "Spoken\n"


def test_silent_reexport_removes_previous_caption_sidecars(tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, "find_ffmpeg", lambda: "ffmpeg")
    async def capture(*args):
        return 2, 2.0, [{"stepIndex": 0, "atMs": 0}]
    monkeypatch.setattr(exporter, "_capture_frames", capture)
    monkeypatch.setattr(exporter, "_encode_mp4", lambda *a: a[4].write_bytes(b"mp4"))
    html = tmp_path / "viewer.html"
    html.write_text("viewer")
    out = tmp_path / "walkthrough.mp4"
    narrated = {"steps": [{"voiceoverWords": _words(("Narrated", 0, 500)), "voiceoverBase64": "YXVkaW8="}]}
    exporter.export_video(narrated, html, out)
    assert out.with_suffix(".vtt").exists()
    assert out.with_name("walkthrough.transcript.txt").exists()
    exporter.export_video({"steps": [{"voiceoverWords": _words(("Narrated", 0, 500))}]}, html, out)
    assert not out.with_suffix(".vtt").exists()
    assert not out.with_name("walkthrough.transcript.txt").exists()


@pytest.mark.parametrize('name', ['walkthrough.vtt', 'walkthrough.transcript.txt'])
def test_caption_sidecars_refuse_symlinks(tmp_path, name):
    sentinel = tmp_path / 'sentinel'
    sentinel.write_text('unchanged')
    (tmp_path / name).symlink_to(sentinel)
    with pytest.raises(OSError):
        exporter._write_captions(STEPS, PLAN, tmp_path / 'walkthrough.mp4')
    assert sentinel.read_text() == 'unchanged'


def test_caption_sidecars_refuse_symlinked_parent(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'walkthrough.vtt'
    sentinel.write_text('unchanged')
    link = tmp_path / 'link'
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        exporter._write_captions(STEPS, PLAN, link / 'walkthrough.mp4')
    assert sentinel.read_text() == 'unchanged'
