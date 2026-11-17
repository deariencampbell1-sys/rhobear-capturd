"""The `rhobear.demo-package.v1` package (GAPS-NOW M9, L9 CONTRACT §4).

`build_package` turns an exported, fully approved demo into an immutable,
content-addressed folder `<demo>/package/<packageRevision>/` holding the MP4,
poster, WebVTT, transcript, interactive HTML and `manifest.json`. Uploading
that folder under `base_url` is L9's (GAPS-NOW B-L9-1); this module never
publishes anything.

* Gate: a review must be open and its selected takes exportable and approved
  (`TakeStore.export_blocks`, contract §6).
* Facts: width/height/duration from ffprobe; chapters from the export's
  `walkthrough.chapters.json` (render-timeline step starts); captions and
  transcript are the narration files.
* Revision: SHA-256 over every file hash, the manifest inputs and the
  publication state, so the same inputs give the same revision and any change
  gives a new one; an existing revision folder is never rewritten.
* `contentHash`: SHA-256 of the canonical manifest JSON (sorted keys, no
  whitespace) without the `contentHash` key.
* `approved` requires a named approver, `watchedComplete: true`, captions and
  a transcript; without an approver the package is `pending`.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from capturd.engine.take_store import TakeStore

SCHEMA = "rhobear.demo-package.v1"
_REVISION = re.compile(r"[0-9a-f]{40}")
_SIGNED = re.compile(r"x-amz-|token=|signature=|sig=|expires=", re.IGNORECASE)
_FILES = {  # role -> file name in the demo folder
    "mp4": "walkthrough.mp4",
    "poster": "poster.png",
    "captions": "walkthrough.vtt",
    "transcript": "walkthrough.transcript.txt",
    "chapters": "walkthrough.chapters.json",
    "interactive": "export.html",
}
_REQUIRED = ("mp4", "poster", "chapters")
#: The interactive export runs scripts but never reaches the embedding page.
SANDBOX = "allow-scripts"


class PackageError(ValueError):
    """The package cannot be built; the reason names what is missing."""


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _probe(mp4: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise PackageError("ffprobe is not on PATH; media facts must be measured")
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,codec_name", "-show_entries", "format=duration",
         "-of", "json", str(mp4)], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise PackageError(f"ffprobe could not read the MP4: {out.stderr.strip()[:200]}")
    info = json.loads(out.stdout)
    stream = (info.get("streams") or [{}])[0]
    if stream.get("codec_name") != "h264":
        raise PackageError(f"the MP4 video is {stream.get('codec_name')!r}, not H.264")
    return {"width": int(stream["width"]), "height": int(stream["height"]),
            "durationSeconds": round(float(info["format"]["duration"]), 3)}


def _check_base(base_url: str) -> str:
    parts = urlparse(base_url)
    if parts.scheme != "https" or not parts.hostname:
        raise PackageError("base_url must be an https URL")
    if parts.query or parts.fragment or _SIGNED.search(base_url):
        raise PackageError("base_url must carry no query, fragment or signing parameter")
    return base_url.rstrip("/")


def build_package(forge: Any, demo_id: str, *, scenario_id: str, engine_or_product: str,
                  source_app: str, source_revision: str, base_url: str,
                  approved_by: str | None = None, watched_complete: bool = False,
                  approved_at: str | None = None) -> tuple[dict[str, Any], Path]:
    if not _REVISION.fullmatch(source_revision or ""):
        raise PackageError("source_revision must be 40 lowercase hex")
    base = _check_base(base_url)
    for name, value in (("scenario_id", scenario_id), ("engine_or_product", engine_or_product),
                        ("source_app", source_app)):
        if not isinstance(value, str) or not value.strip():
            raise PackageError(f"{name} is required")

    store = TakeStore(forge)
    blocks = store.export_blocks(demo_id)
    if blocks is None:
        raise PackageError("no review is open; approve every shot with take.list/take.approve first")
    if blocks:
        raise PackageError("not every shot is approved: " + "; ".join(
            f"shot {b['shotIndex']} take {b['takeIndex']}: {b['needed']}" for b in blocks))

    folder = forge.demo_dir(demo_id)
    present = {role: folder / name for role, name in _FILES.items() if (folder / name).is_file()}
    missing = [_FILES[role] for role in _REQUIRED if role not in present]
    if missing:
        raise PackageError(f"export the MP4 first; missing {missing}")

    approved = approved_by is not None
    if approved:
        if not isinstance(approved_by, str) or not approved_by.strip():
            raise PackageError("an approval names its person")
        if watched_complete is not True:
            raise PackageError("approval requires the whole result watched (watchedComplete)")
        if "captions" not in present or "transcript" not in present:
            raise PackageError("approval requires captions and a transcript (narrate the demo)")
        approved_at = approved_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    media = _probe(present["mp4"])
    chapters = json.loads(present["chapters"].read_text(encoding="utf-8"))
    review = store.review(demo_id)
    takes = [{"shotIndex": t["shotIndex"], "takeIndex": t["takeIndex"], "state": t["state"],
              "sourceRevision": t["sourceRevision"], "approvedBy": t["approvedBy"],
              "rejectedReason": t["rejectedReason"]} for t in review["takes"]]
    hashes = {role: _sha256(path) for role, path in sorted(present.items())}
    state = "approved" if approved else "pending"
    inputs = {"schema": SCHEMA, "engineOrProduct": engine_or_product, "scenarioId": scenario_id,
              "sourceApp": source_app, "sourceRevision": source_revision, "base": base,
              "state": state, "approval": [approved_by, approved_at], "hashes": hashes,
              "takes": takes}
    revision = hashlib.sha256(
        json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]
    url = lambda role: f"{base}/{revision}/{_FILES[role]}"  # noqa: E731

    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "engineOrProduct": engine_or_product,
        "scenarioId": scenario_id,
        "packageRevision": revision,
        "publicationState": state,
        "sourceApp": source_app,
        "sourceRevision": source_revision,
        "renditions": [{"url": url("mp4"), "mime": "video/mp4", **media, "chapters": chapters}],
        "posterUrl": url("poster"),
        "captionsUrl": url("captions") if "captions" in present else None,
        "transcriptUrl": url("transcript") if "transcript" in present else None,
        "evidence": {"inputs": {"takes": takes}, "runIds": [demo_id],
                     "artifactHashes": list(hashes.values())},
        "approval": ({"approvedBy": approved_by, "approvedAt": approved_at, "watchedComplete": True}
                     if approved else None),
        "immutableUrls": True,
    }
    if "interactive" in present:
        manifest["interactive"] = {"url": url("interactive"), "sandbox": SANDBOX}
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["contentHash"] = "sha256:" + hashlib.sha256(canonical).hexdigest()

    target = folder / "package" / revision
    if target.is_dir():  # immutable: the same revision is the same bytes
        return json.loads((target / "manifest.json").read_text(encoding="utf-8")), target
    staging = folder / "package" / f".{revision}.tmp"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for role, path in present.items():
        shutil.copy2(path, staging / _FILES[role])
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True),
                                           encoding="utf-8")
    staging.rename(target)
    return manifest, target
