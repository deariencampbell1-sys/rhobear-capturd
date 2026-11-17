"""Captions and transcript from the narration that was actually spoken (GAPS-NOW M7).

Timing has exactly two sources: each step's TTS word timings
(`voiceoverWords`, ms within the step's clip) and the step's audio offset in
the rendered timeline (`audio_plan` `atMs`, the offset the MP4 mixes that clip
at). A step without word timings gets no cue; nothing is estimated.
"""
from __future__ import annotations

from typing import Any

_MAX_WORDS = 7
_MAX_SPAN_MS = 2500
_SENTENCE_END = (".", "!", "?")


def _step_words(step: dict[str, Any]) -> list[dict[str, Any]]:
    words = []
    for w in step.get("voiceoverWords") or []:
        if isinstance(w, dict) and isinstance(w.get("word"), str) and w["word"].strip() \
                and isinstance(w.get("tStartMs"), int) and isinstance(w.get("tEndMs"), int) \
                and 0 <= w["tStartMs"] < w["tEndMs"]:
            words.append(w)
    return sorted(words, key=lambda w: w["tStartMs"])


def build_cues(steps: list[dict[str, Any]], audio_plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ordered, non-overlapping cues `{startMs, endMs, text, stepIndex}`."""
    cues: list[dict[str, Any]] = []
    for entry in audio_plan:
        index, at = entry.get("stepIndex"), entry.get("atMs")
        if not isinstance(index, int) or not 0 <= index < len(steps) or not isinstance(at, int):
            continue
        group: list[dict[str, Any]] = []
        for word in _step_words(steps[index]) + [None]:
            if group and (word is None or len(group) >= _MAX_WORDS
                          or word["tEndMs"] - group[0]["tStartMs"] > _MAX_SPAN_MS
                          or group[-1]["word"].endswith(_SENTENCE_END)):
                cues.append({"startMs": at + group[0]["tStartMs"],
                             "endMs": at + group[-1]["tEndMs"],
                             "text": " ".join(w["word"].strip() for w in group),
                             "stepIndex": index})
                group = []
            if word is not None:
                group.append(word)
    cues.sort(key=lambda c: c["startMs"])
    ordered: list[dict[str, Any]] = []
    for cue in cues:
        if ordered and cue["startMs"] <= ordered[-1]["startMs"]:
            ordered[-1]["text"] += " " + cue["text"]
            ordered[-1]["endMs"] = max(ordered[-1]["endMs"], cue["endMs"])
            continue
        if ordered and ordered[-1]["endMs"] > cue["startMs"]:
            ordered[-1]["endMs"] = cue["startMs"]
        ordered.append(dict(cue))
    return ordered


def _stamp(ms: int) -> str:
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _escape(text: str) -> str:
    return " ".join(text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").split())


def to_webvtt(cues: list[dict[str, Any]]) -> str:
    blocks = [f"{_stamp(c['startMs'])} --> {_stamp(c['endMs'])}\n{_escape(c['text'])}" for c in cues]
    return "WEBVTT\n\n" + "\n\n".join(blocks) + ("\n" if blocks else "")


def to_transcript(steps: list[dict[str, Any]]) -> str:
    """The spoken words, one paragraph per narrated step, in step order."""
    paragraphs = [" ".join(w["word"].strip() for w in _step_words(step)) for step in steps]
    paragraphs = [p for p in paragraphs if p]
    return "\n\n".join(paragraphs) + ("\n" if paragraphs else "")
