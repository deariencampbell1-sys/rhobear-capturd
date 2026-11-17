"""Review integrity across content edits and concurrent writers."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import pytest
from test_take_store import _forge, _store, PNG_A
from capturd.engine import take_store
from capturd.walk.coordinator import DemoExportBlocked, DemoForgeError


def test_edit_reopens_approval_with_auditable_history(tmp_path):
    forge = _forge(tmp_path, pngs=(PNG_A,))
    store = _store(forge)
    old = store.open_review('demo1')
    take = old['takes'][0]
    store.approve('demo1', 0, 0, by='Neo', expected_revision=take['sourceRevision'])
    data = forge.load_spec('demo1')
    data['steps'][0]['annotation'] = 'Changed line'
    forge.save_spec('demo1', data)
    with pytest.raises(DemoExportBlocked):
        forge.export_demo('demo1', fmt='html')
    reopened = store.open_review('demo1')
    assert reopened['takes'][0]['state'] == 'ready'
    assert reopened['takes'][0]['sourceRevision'] == take['sourceRevision']
    assert reopened['contentRevision'] != old['contentRevision']
    assert reopened['history'][-1]['oldContentRevision'] == old['contentRevision']
    assert reopened['history'][-1]['newContentRevision'] == reopened['contentRevision']
    assert reopened['history'][-1]['from'] == 'approved'


def test_two_stores_cannot_approve_same_stale_state(tmp_path, monkeypatch):
    forge = _forge(tmp_path, pngs=(PNG_A,))
    stores = [_store(forge), _store(forge)]
    take = stores[0].open_review('demo1')['takes'][0]
    original = take_store.TakeStore._read
    barrier = threading.Barrier(2)
    seen = threading.local()
    def simultaneous_read(self, demo_id):
        value = original(self, demo_id)
        if not getattr(seen, 'waited', False):
            seen.waited = True
            barrier.wait(timeout=5)
        return value
    monkeypatch.setattr(take_store.TakeStore, '_read', simultaneous_read)
    def attempt(store):
        try:
            store.approve('demo1', 0, 0, by='Neo', expected_revision=take['sourceRevision'])
            return 'approved'
        except take_store.TakeStoreError:
            return 'refused'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, stores)) == ['approved', 'refused']


def test_symlinked_demo_cannot_write_outside_root(tmp_path):
    forge = _forge(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'demo.json').write_text((forge.demo_dir('demo1') / 'demo.json').read_text())
    (forge.demos_dir / 'linked').symlink_to(outside, target_is_directory=True)
    with pytest.raises(DemoForgeError):
        _store(forge).open_review('linked')
    assert not (outside / 'takes.json').exists()
