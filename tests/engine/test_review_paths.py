"""Screenshot and lock paths must never escape the demo directory."""
import base64
import hashlib

import pytest
from capturd.walk.coordinator import DemoForgeError
from test_take_store import _forge, _store, PNG_A


def _point_to(forge, path):
    data = forge.load_spec('demo1')
    data['steps'][0]['screenshotBase64'] = None
    data['steps'][0]['screenshotPath'] = str(path)
    forge.save_spec('demo1', data)


def test_absolute_screenshot_is_refused_even_with_local_basename(tmp_path):
    forge = _forge(tmp_path, pngs=(PNG_A,))
    outside = tmp_path / 'outside.png'
    outside.write_bytes(b'secret')
    (forge.demo_dir('demo1') / outside.name).write_bytes(PNG_A)
    _point_to(forge, outside)
    with pytest.raises(DemoForgeError):
        _store(forge).open_review('demo1')


def test_demo_local_screenshot_symlink_is_refused(tmp_path):
    forge = _forge(tmp_path, pngs=(PNG_A,))
    outside = tmp_path / 'outside.png'
    outside.write_bytes(b'secret')
    (forge.demo_dir('demo1') / 'step_000.png').symlink_to(outside)
    _point_to(forge, 'step_000.png')
    with pytest.raises(DemoForgeError):
        _store(forge).open_review('demo1')


def test_screenshot_parent_symlink_is_refused(tmp_path):
    forge = _forge(tmp_path, pngs=(PNG_A,))
    outside = tmp_path / 'external'
    outside.mkdir()
    (outside / 'step.png').write_bytes(b'secret')
    (forge.demo_dir('demo1') / 'link').symlink_to(outside, target_is_directory=True)
    _point_to(forge, 'link/step.png')
    with pytest.raises(DemoForgeError):
        _store(forge).open_review('demo1')


@pytest.mark.parametrize('name', ['step_000.png', 'source_' + hashlib.sha1(PNG_A).hexdigest() + '.png'])
def test_valid_demo_screenshot_paths_work(tmp_path, name):
    forge = _forge(tmp_path, pngs=(PNG_A,))
    (forge.demo_dir('demo1') / name).write_bytes(PNG_A)
    _point_to(forge, f'demos/demo1/{name}')
    take = _store(forge).open_review('demo1')['takes'][0]
    assert take['sourceRevision'] == hashlib.sha1(PNG_A).hexdigest()
    assert take['state'] == 'ready'


def test_symlinked_lock_does_not_touch_external_sentinel(tmp_path):
    forge = _forge(tmp_path, pngs=(PNG_A,))
    sentinel = tmp_path / 'sentinel'
    sentinel.write_bytes(b'unchanged')
    (forge.demo_dir('demo1') / '.takes.lock').symlink_to(sentinel)
    with pytest.raises((DemoForgeError, OSError, ValueError)):
        _store(forge).open_review('demo1')
    assert sentinel.read_bytes() == b'unchanged'


def test_nonregular_lock_is_refused(tmp_path):
    forge = _forge(tmp_path, pngs=(PNG_A,))
    (forge.demo_dir('demo1') / '.takes.lock').mkdir()
    with pytest.raises((DemoForgeError, OSError, ValueError)):
        _store(forge).open_review('demo1')
