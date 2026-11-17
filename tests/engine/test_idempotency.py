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


def test_a_failed_attempt_is_unknown_until_reconciled(tmp_path):
    ledger = OperationLedger(tmp_path / "ops.json")
    with pytest.raises(RuntimeError):
        ledger.run("op-f", {"a": 1}, lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(Exception, match="(?i)(unknown|reconcil)"):
        ledger.run("op-f", {"a": 1}, lambda: {"job": "retry"})


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


def _concurrent_process(path, counter, start, results):
    import time
    start.wait()
    def work():
        with open(counter, 'a') as stream:
            stream.write('run\n')
        time.sleep(0.25)
        return {'job': 'once'}
    try:
        results.put(OperationLedger(path).run('shared', {'tool': 'export', 'demo': 'd'}, work))
    except Exception as exc:
        results.put({'error': str(exc)})


def _crash_after_side_effect(path, counter):
    import os
    def work():
        with open(counter, 'a') as stream:
            stream.write('run\n')
            stream.flush()
            os.fsync(stream.fileno())
        os._exit(23)
    OperationLedger(path).run('crash', {'tool': 'export', 'demo': 'd'}, work)


def test_same_operation_runs_once_across_processes(tmp_path):
    import multiprocessing
    ctx = multiprocessing.get_context('fork')
    start, results = ctx.Event(), ctx.Queue()
    path, counter = tmp_path / 'ops.json', tmp_path / 'calls'
    processes = [ctx.Process(target=_concurrent_process, args=(path, counter, start, results)) for _ in range(3)]
    for process in processes:
        process.start()
    start.set()
    responses = [results.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert counter.read_text().splitlines() == ['run']
    assert responses == [{'job': 'once'}] * 3


def test_crash_after_side_effect_is_not_replayed(tmp_path):
    import multiprocessing
    ctx = multiprocessing.get_context('fork')
    path, counter = tmp_path / 'ops.json', tmp_path / 'calls'
    process = ctx.Process(target=_crash_after_side_effect, args=(path, counter))
    process.start()
    process.join(10)
    assert process.exitcode == 23
    with pytest.raises(Exception, match='(?i)(unknown|reconcil)'):
        OperationLedger(path).run('crash', {'tool': 'export', 'demo': 'd'}, lambda: counter.write_text('ran again'))
    assert counter.read_text() == 'run\n'


def _crash_while_committing_result(path, counter):
    import os
    ledger = OperationLedger(path)
    save = ledger._save
    calls = 0
    def crash_second_save(entries):
        nonlocal calls
        calls += 1
        if calls == 2:
            os._exit(24)
        save(entries)
    ledger._save = crash_second_save
    def work():
        counter.write_text('completed')
        return {'job': 'recoverable'}
    ledger.run('commit-crash', {'tool': 'export', 'demo': 'd'}, work)


def test_recovers_durable_result_after_commit_crash(tmp_path):
    import multiprocessing
    ctx = multiprocessing.get_context('fork')
    path, counter = tmp_path / 'ops.json', tmp_path / 'completed'
    process = ctx.Process(target=_crash_while_committing_result, args=(path, counter))
    process.start()
    process.join(10)
    assert process.exitcode == 24
    assert counter.read_text() == 'completed'
    result = OperationLedger(path).run('commit-crash', {'tool': 'export', 'demo': 'd'}, lambda: pytest.fail('work reran'))
    assert result == {'job': 'recoverable'}
