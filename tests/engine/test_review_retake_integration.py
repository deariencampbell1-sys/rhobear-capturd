"""Regression: content edits preserve recaptured source identity and take history."""
import json
from capturd.engine.take_store import TakeStore
from test_take_store import _forge, PNG_A, PNG_B


def test_edit_reopens_review_without_losing_recaptured_pixels(tmp_path):
    forge = _forge(tmp_path, pngs=(PNG_A,))
    store = TakeStore(forge)
    original = store.open_review('demo1')['takes'][0]
    captured = store.recapture('demo1', 0, 0, pixels=PNG_B, expected_revision=original['sourceRevision'])
    store.approve('demo1', 0, 1, by='Neo', expected_revision=captured['sourceRevision'])
    spec = forge.load_spec('demo1')
    spec['steps'][0]['annotation'] = 'Edited line'
    forge.demo_json_path('demo1').write_text(json.dumps(spec))
    reopened = store.open_review('demo1')
    assert len(reopened['takes']) == 2
    assert reopened['takes'][1]['state'] == 'ready'
    assert reopened['history'][-1]['from'] == 'approved'
    retake = store.retake('demo1', 0, 1, direction={'caption': 'New line'}, expected_revision=captured['sourceRevision'])
    store.approve('demo1', 0, 2, by='Neo', expected_revision=retake['sourceRevision'])
    assert (forge.demo_dir('demo1') / store.selected_sources('demo1')[0]).read_bytes() == PNG_B
