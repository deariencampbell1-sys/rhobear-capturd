"""GAPS-NOW M3 (JOURNEY A5/E13/S7, brief 06 item 3): one operation, one result.

`operation_id` on a submit: the same id with the same payload returns the
first result without doing the work again (also across a restart and under
concurrency); the same id with a changed payload is `idempotency_mismatch`.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from capturd.engine.idempotency import IdempotencyMismatch, OperationLedger


def test_same_id_same_payload_runs_once(tmp_path):
    ledger = OperationLedger(tmp_path / "ops.json")
    calls = []
    first = ledger.run("op-1", {"a": 1}, lambda: calls.append(1) or {"job": "j1"})
    again = ledger.run("op-1", {"a": 1}, lambda: calls.append(1) or {"job": "j2"})
    assert first == again == {"job": "j1"} and calls == [1]


def test_changed_payload_is_a_mismatch(tmp_path):
    ledger = OperationLedger(tmp_path / "ops.json")
    ledger.run("op-1", {"a": 1}, lambda: {"job": "j1"})
    with pytest.raises(IdempotencyMismatch) as exc:
        ledger.run("op-1", {"a": 2}, lambda: {"job": "j2"})
    assert exc.value.payload()["code"] == "idempotency_mismatch"


def test_the_ledger_survives_a_restart(tmp_path):
    OperationLedger(tmp_path / "ops.json").run("op-1", {"a": 1}, lambda: {"job": "j1"})
    reborn = OperationLedger(tmp_path / "ops.json")
    assert reborn.run("op-1", {"a": 1}, lambda: {"job": "other"}) == {"job": "j1"}


def test_concurrent_same_key_does_the_work_once(tmp_path):
    ledger = OperationLedger(tmp_path / "ops.json")
    calls, gate = [], threading.Barrier(8)
    results = []

    def work():
        calls.append(1)
        return {"job": f"j{len(calls)}"}

    def submit():
        gate.wait()
        results.append(ledger.run("op-c", {"a": 1}, work))

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == [1] and all(r == {"job": "j1"} for r in results)


def test_a_failed_attempt_is_not_remembered(tmp_path):
    ledger = OperationLedger(tmp_path / "ops.json")
    with pytest.raises(RuntimeError):
        ledger.run("op-f", {"a": 1}, lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ledger.run("op-f", {"a": 1}, lambda: {"job": "retry"}) == {"job": "retry"}


def test_demo_export_with_an_operation_id_is_idempotent(tmp_path, monkeypatch):
    from capturd.mcp.server import _build_server
    from capturd.walk.coordinator import DemoForge

    forge = DemoForge(demos_dir=tmp_path / "demos")
    exports = []
    monkeypatch.setattr(forge, "export_demo",
                        lambda demo_id, fmt="html": exports.append(fmt) or (tmp_path / "x.html"))
    server = _build_server(forge)

    def call(args):
        result = asyncio.run(server.call_tool("demo.export", args))
        return result.structured_content.get("result", result.structured_content)

    a = call({"demo_id": "d1", "format": "html", "operation_id": "op-e"})
    b = call({"demo_id": "d1", "format": "html", "operation_id": "op-e"})
    assert a == b and exports == ["html"]
    clash = call({"demo_id": "d1", "format": "mp4", "operation_id": "op-e"})
    assert clash["ok"] is False and clash["code"] == "idempotency_mismatch"
