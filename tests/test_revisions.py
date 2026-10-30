"""W2-05b — immutable engine-local revisions (``capturd.walk.revisions``).

Every behaviour in the ownership list has a test here, edge cases first-class:

* content-hashed, self-verifying revision ids over canonical spec bytes;
* atomic write / read / list — old bytes never change behind an id;
* immutable approved / published pointers that move without rewriting bytes;
* copy-old-revision-to-new-draft rollback that preserves history;
* concurrency: two writers saving the same draft get two distinct revisions
  and neither can reuse or corrupt an id;
* edge cases: an approved slot forks a new revision instead of mutating; a
  rollback to a missing id raises an error naming the id and writes nothing;
* a source scan proving no hosted URL, CRM publish route, AWS SDK or
  analytics import was introduced by this extraction.
"""

from __future__ import annotations

import ast
import json
import re
import threading
from pathlib import Path

import pytest

from capturd.walk.revisions import (
    RevisionCorrupt,
    RevisionError,
    RevisionNotFound,
    RevisionRecord,
    RevisionStore,
    canonical_bytes,
    content_hash,
    hash_spec,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "neutral-v2" / "revisions"
PRODUCT = Path(__file__).resolve().parents[1] / "capturd" / "walk" / "revisions.py"

REV_ID_RE = re.compile(r"^rv_\d{6}_[0-9a-f]{64}$")


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def spec_v1() -> dict:
    return load_fixture("spec-v1.json")


@pytest.fixture
def spec_v2() -> dict:
    return load_fixture("spec-v2.json")


@pytest.fixture
def store(tmp_path: Path) -> RevisionStore:
    return RevisionStore(tmp_path / "demo")


def frozen_bytes(store: RevisionStore, revision_id: str) -> bytes:
    return (store.revisions_dir / f"{revision_id}.json").read_bytes()


# ---------------------------------------------------------------------------
# Content hashing and ids
# ---------------------------------------------------------------------------


def test_hash_spec_is_canonical_and_key_order_independent(spec_v1: dict) -> None:
    shuffled = dict(reversed(list(spec_v1.items())))
    assert hash_spec(shuffled) == hash_spec(spec_v1)
    assert content_hash(canonical_bytes(spec_v1)) == hash_spec(spec_v1)
    assert hash_spec({**spec_v1, "name": "other"}) != hash_spec(spec_v1)


def test_revision_id_embeds_the_content_hash_and_a_sequence(
    store: RevisionStore, spec_v1: dict
) -> None:
    record = store.save_revision(spec_v1)
    assert isinstance(record, RevisionRecord)
    assert record.seq == 1
    assert record.sha256 == hash_spec(spec_v1)
    assert record.id == f"rv_{record.seq:06d}_{record.sha256}"
    assert REV_ID_RE.match(record.id)
    assert record.size == len(canonical_bytes(spec_v1))


# ---------------------------------------------------------------------------
# Atomic write / read / list
# ---------------------------------------------------------------------------


def test_save_read_roundtrip(store: RevisionStore, spec_v1: dict) -> None:
    record = store.save_revision(spec_v1)
    assert store.read(record.id) == spec_v1
    assert store.read_bytes(record.id) == canonical_bytes(spec_v1)


def test_read_names_a_missing_revision_id(store: RevisionStore, spec_v1: dict) -> None:
    store.save_revision(spec_v1)
    missing = "rv_000099_" + "0" * 64
    with pytest.raises(RevisionNotFound) as excinfo:
        store.read(missing)
    assert missing in str(excinfo.value)


def test_read_refuses_traversal_ids_and_writes_nothing(
    store: RevisionStore, spec_v1: dict
) -> None:
    record = store.save_revision(spec_v1)
    for bad in ("../escape", "", "rv_1_short", "rv_000001_" + "z" * 64, "demo.json"):
        with pytest.raises(RevisionNotFound) as excinfo:
            store.read(bad)
        assert bad in str(excinfo.value)
    assert [r.id for r in store.list_revisions()] == [record.id]


def test_tampered_bytes_are_refused_before_anything_uses_them(
    store: RevisionStore, spec_v1: dict
) -> None:
    record = store.save_revision(spec_v1)
    assert frozen_bytes(store, record.id) == canonical_bytes(spec_v1)
    (store.revisions_dir / f"{record.id}.json").write_bytes(
        canonical_bytes({"tampered": True})
    )
    with pytest.raises(RevisionCorrupt) as excinfo:
        store.read(record.id)
    assert record.id in str(excinfo.value)
    with pytest.raises(RevisionCorrupt):
        store.rollback_to(record.id)
    assert store.load_draft() is None  # the refused rollback wrote nothing


def test_list_is_newest_first_and_ignores_stray_files(
    store: RevisionStore, spec_v1: dict, spec_v2: dict
) -> None:
    first = store.save_revision(spec_v1)
    second = store.save_revision(spec_v2)
    third = store.save_revision({**spec_v1, "name": "third"})
    (store.revisions_dir / "notes.txt").write_text("not a revision", encoding="utf-8")
    (store.revisions_dir / "rv_000042_tooshort.json").write_bytes(b"{}")
    listed = store.list_revisions()
    assert [r.id for r in listed] == [third.id, second.id, first.id]
    assert [r.seq for r in listed] == [3, 2, 1]
    assert all(r.sha256 for r in listed)


def test_each_save_mints_a_fresh_revision_even_for_identical_bytes(
    store: RevisionStore, spec_v1: dict
) -> None:
    first = store.save_revision(spec_v1)
    second = store.save_revision(spec_v1)
    assert first.id != second.id
    assert first.sha256 == second.sha256
    assert store.read(first.id) == store.read(second.id) == spec_v1
    assert len(store.list_revisions()) == 2


def test_no_temporary_files_survive_the_atomic_writes(
    store: RevisionStore, spec_v1: dict
) -> None:
    record = store.save_revision(spec_v1)
    store.save_draft(spec_v1)
    store.approve(record.id)
    assert [p.name for p in store.root.rglob("*.tmp")] == []


# ---------------------------------------------------------------------------
# Concurrency — two writers, same draft, two distinct revisions (edge case 2)
# ---------------------------------------------------------------------------


def _run_writers(root: Path, count: int) -> list[RevisionRecord]:
    """``count`` writers, each its own store instance on a shared root, all
    freezing the same draft at the same moment."""
    draft_setter = RevisionStore(root)
    draft_setter.save_draft(load_fixture("spec-v1.json"))
    stores = [RevisionStore(root) for _ in range(count)]
    results: list[RevisionRecord] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(count)

    def writer(one_store: RevisionStore) -> None:
        try:
            barrier.wait(timeout=10)
            results.append(one_store.save_revision())
        except BaseException as exc:  # surfaced below, never swallowed
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(s,)) for s in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    return results


def test_two_concurrent_writers_get_two_distinct_uncorrupted_revisions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "demo"
    records = _run_writers(root, 2)
    assert len(records) == 2
    ids = {record.id for record in records}
    assert len(ids) == 2
    assert sorted(record.seq for record in records) == [1, 2]
    spec = load_fixture("spec-v1.json")
    for record in records:
        assert REV_ID_RE.match(record.id)
        data = (root / "revisions" / f"{record.id}.json").read_bytes()
        assert content_hash(data) == record.sha256 == hash_spec(spec)
        assert RevisionStore(root).read(record.id) == spec
    assert len(RevisionStore(root).list_revisions()) == 2


def test_a_storm_of_concurrent_writes_never_reuses_an_id(tmp_path: Path) -> None:
    root = tmp_path / "demo"
    records = _run_writers(root, 8)
    ids = [record.id for record in records]
    assert len(set(ids)) == 8
    assert sorted(record.seq for record in records) == list(range(1, 9))


# ---------------------------------------------------------------------------
# Approved / published pointers — immutability edge cases (edge case 1)
# ---------------------------------------------------------------------------


def test_approved_revision_is_immutable_a_new_revision_is_created_instead(
    store: RevisionStore, spec_v1: dict, spec_v2: dict
) -> None:
    approved = store.save_revision(spec_v1)
    store.approve(approved.id)
    original = frozen_bytes(store, approved.id)

    forked = store.save_revision(spec_v2, revision_id=approved.id)
    assert forked.id != approved.id
    assert store.read(forked.id) == spec_v2
    assert store.read(approved.id) == spec_v1
    assert frozen_bytes(store, approved.id) == original
    assert store.approved_revision_id() == approved.id

    # Even byte-identical content must not reuse an approved slot.
    identical = store.save_revision(spec_v1, revision_id=approved.id)
    assert identical.id != approved.id
    assert store.read(identical.id) == spec_v1
    assert store.read(approved.id) == spec_v1
    assert frozen_bytes(store, approved.id) == original
    assert store.approved_revision_id() == approved.id


def test_published_revision_also_forks_rather_than_mutating(
    store: RevisionStore, spec_v1: dict, spec_v2: dict
) -> None:
    published = store.save_revision(spec_v1)
    store.publish(published.id)
    original = frozen_bytes(store, published.id)
    forked = store.save_revision(spec_v2, revision_id=published.id)
    assert forked.id != published.id
    assert frozen_bytes(store, published.id) == original
    assert store.published_revision_id() == published.id


def test_identical_content_at_an_unapproved_slot_is_idempotent(
    store: RevisionStore, spec_v1: dict, spec_v2: dict
) -> None:
    record = store.save_revision(spec_v1)
    again = store.save_revision(spec_v1, revision_id=record.id)
    assert again.id == record.id
    changed = store.save_revision(spec_v2, revision_id=record.id)
    assert changed.id != record.id
    assert store.read(record.id) == spec_v1  # old bytes stay put regardless
    missing = "rv_000009_" + "a" * 64
    with pytest.raises(RevisionNotFound) as excinfo:
        store.save_revision(spec_v1, revision_id=missing)
    assert missing in str(excinfo.value)


def test_pointers_move_without_touching_any_revision_bytes(
    store: RevisionStore, spec_v1: dict, spec_v2: dict
) -> None:
    first = store.save_revision(spec_v1)
    second = store.save_revision(spec_v2)
    first_bytes = frozen_bytes(store, first.id)
    second_bytes = frozen_bytes(store, second.id)
    assert store.approved_revision_id() is None
    assert store.published_revision_id() is None

    store.approve(first.id)
    assert frozen_bytes(store, first.id) == first_bytes
    assert frozen_bytes(store, second.id) == second_bytes

    store.approve(second.id)  # the pointer moves; the old bytes do not
    assert frozen_bytes(store, first.id) == first_bytes
    assert frozen_bytes(store, second.id) == second_bytes

    store.publish(first.id)
    assert store.published_revision_id() == first.id
    assert store.approved_revision_id() == second.id
    assert frozen_bytes(store, first.id) == first_bytes
    assert store.read(first.id) == spec_v1
    assert store.read(second.id) == spec_v2


def test_approve_of_an_unknown_id_names_it_and_writes_nothing(
    store: RevisionStore, spec_v1: dict
) -> None:
    record = store.save_revision(spec_v1)
    store.approve(record.id)
    missing = "rv_000077_" + "b" * 64
    with pytest.raises(RevisionNotFound) as excinfo:
        store.approve(missing)
    assert missing in str(excinfo.value)
    with pytest.raises(RevisionNotFound):
        store.publish("not-a-revision")
    assert store.approved_revision_id() == record.id
    assert store.published_revision_id() is None
    assert [r.id for r in store.list_revisions()] == [record.id]


def test_published_bytes_are_identical_after_every_operation(
    store: RevisionStore, spec_v1: dict, spec_v2: dict
) -> None:
    published = store.save_revision(spec_v1)
    store.publish(published.id)
    original = frozen_bytes(store, published.id)

    def unchanged() -> None:
        assert frozen_bytes(store, published.id) == original
        assert store.read(published.id) == spec_v1
        assert store.published_revision_id() == published.id

    store.save_draft(spec_v2); unchanged()
    fork = store.save_revision(spec_v2); unchanged()
    store.approve(fork.id); unchanged()
    store.rollback_to(fork.id); unchanged()
    store.save_revision({**spec_v1, "name": "third"}); unchanged()
    store.approve(published.id); unchanged()
    assert len(store.list_revisions()) == 3


# ---------------------------------------------------------------------------
# Draft and copy-to-new-draft rollback
# ---------------------------------------------------------------------------


def test_draft_roundtrip(store: RevisionStore, spec_v1: dict) -> None:
    assert store.load_draft() is None
    record = store.save_draft(spec_v1)
    assert record["sha256"] == hash_spec(spec_v1)
    assert record["size"] == len(canonical_bytes(spec_v1))
    assert record["forkedFrom"] is None
    assert store.load_draft() == spec_v1


def test_draft_refuses_specs_that_are_not_objects(store: RevisionStore) -> None:
    with pytest.raises(RevisionError):
        store.save_draft([1, 2, 3])
    with pytest.raises(RevisionError):
        store.save_revision({"bad": object()})
    with pytest.raises(RevisionError) as excinfo:
        store.save_revision()  # nothing to freeze
    assert "draft" in str(excinfo.value)


def test_corrupt_draft_is_reported_not_silently_used(
    store: RevisionStore, spec_v1: dict
) -> None:
    store.save_draft(spec_v1)
    (store.root / "draft.json").write_bytes(b"{not json")
    with pytest.raises(RevisionCorrupt):
        store.load_draft()


def test_rollback_creates_a_new_draft_and_preserves_history(
    store: RevisionStore, spec_v1: dict, spec_v2: dict
) -> None:
    old = store.save_revision(spec_v1)
    newer = store.save_revision(spec_v2)
    store.save_draft(spec_v2)
    old_bytes = frozen_bytes(store, old.id)
    newer_bytes = frozen_bytes(store, newer.id)

    result = store.rollback_to(old.id)
    assert result["forkedFrom"] == old.id
    assert store.load_draft() == spec_v1          # the NEW draft holds old content
    assert [r.id for r in store.list_revisions()] == [newer.id, old.id]  # history intact
    assert frozen_bytes(store, old.id) == old_bytes
    assert frozen_bytes(store, newer.id) == newer_bytes

    # Editing the rolled-back draft freezes a third revision; history grows.
    store.save_draft({**spec_v1, "name": "after rollback"})
    third = store.save_revision(store.load_draft())
    assert third.id not in {old.id, newer.id}
    assert [r.id for r in store.list_revisions()] == [third.id, newer.id, old.id]
    assert store.read(old.id) == spec_v1
    assert store.read(newer.id) == spec_v2
    assert store.approved_revision_id() is None


def test_rollback_to_a_missing_revision_names_the_id_and_writes_nothing(
    store: RevisionStore, tmp_path: Path, spec_v1: dict
) -> None:
    record = store.save_revision(spec_v1)
    store.save_draft(spec_v1)
    draft_before = (store.root / "draft.json").read_bytes()

    missing = "rv_000042_" + "c" * 64
    with pytest.raises(RevisionNotFound) as excinfo:
        store.rollback_to(missing)
    assert missing in str(excinfo.value)
    with pytest.raises(RevisionNotFound):
        store.rollback_to("../../draft.json")
    assert (store.root / "draft.json").read_bytes() == draft_before
    assert store.load_draft() == spec_v1
    assert [r.id for r in store.list_revisions()] == [record.id]

    # A store that never had a draft stays completely untouched.
    empty = RevisionStore(tmp_path / "never-written")
    with pytest.raises(RevisionNotFound) as excinfo:
        empty.rollback_to(missing)
    assert missing in str(excinfo.value)
    assert not empty.root.exists()


# ---------------------------------------------------------------------------
# Proof — the extraction introduces no hosted, CRM, AWS or analytics surface
# ---------------------------------------------------------------------------

BANNED_PATTERNS = [
    (r"https?://", "hosted URL / web address"),
    (r"\b(?:boto3|botocore|polly|transcribe|bedrock|aws)\b", "AWS SDK / service"),
    (r"\b(?:frontman|herald|crm)\b", "CRM publish surface"),
    (r"\banalytics\b", "analytics import"),
    (r"\b(?:urllib|requests|socket|httpx|aiohttp|websocket)\b", "network import"),
    (r"^\s*(?:from|import)\s+service\b", "service package import"),
    (r"\b(?:engagement_tools|viewer_session|attribution_token)\b", "hosted engagement layer"),
    (r"\b(?:vertex|gemini)\b", "model service"),
]

STDLIB_ALLOWED = {
    "__future__",
    "dataclasses",
    "hashlib",
    "json",
    "os",
    "pathlib",
    "re",
    "threading",
    "time",
    "typing",
}


def test_revisions_source_scan_introduces_no_forbidden_surface() -> None:
    source = PRODUCT.read_text(encoding="utf-8")
    for pattern, label in BANNED_PATTERNS:
        match = re.search(pattern, source, re.MULTILINE | re.IGNORECASE)
        assert match is None, f"{label} found in revisions.py: {match.group(0)!r}"


def test_revisions_module_imports_are_standard_library_only() -> None:
    tree = ast.parse(PRODUCT.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= STDLIB_ALLOWED, sorted(roots - STDLIB_ALLOWED)
