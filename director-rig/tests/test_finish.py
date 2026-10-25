"""Regression tests for finish.py's ffmpeg-filter escaping and path validation
(2026-07-22, per rhobear-reviews HIGH findings on PR #35; 2026-10-25 drive-letter
carve-out for the Windows absolute-path failures). No ffmpeg binary required --
these test pure string/path logic, not subprocess calls.
Run directly: `python director-rig/tests/test_finish.py`

Documented rule for `_safe_input_path` (mirrors finish.py's comment):

  1. A leading scheme-shaped prefix ("concat:", "http://", "data:", ...) is a
     ffmpeg protocol, not a path -> rejected with a non-zero exit. The ONLY
     exception is the Windows drive-letter form: a single ASCII letter, then
     ':', then '\\', '/' or end-of-string (C:\\Users\\me\\shot.png,
     C:/Users/me/shot.png, C:) -- treated as a path.
  2. Drive-relative "C:shot.png" (no separator after the colon) stays
     rejected: ambiguous to ffmpeg's parser; pass absolute paths.
  3. UNC ("\\\\server\\share\\f.png") and device paths ("\\\\?\\C:\\...",
     "\\\\.\\pipe\\...") start with a backslash, never match the scheme
     pattern, and fall through to is_file()+resolve(), which are the real
     safety net: the resolved ABSOLUTE path is always returned, never the raw
     argument.
"""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from finish import _ff_text, _looks_like_protocol, _safe_input_path  # noqa: E402

WINDOWS = sys.platform.startswith("win")


def test_ff_text_escapes_single_quote():
    # A single quote would otherwise close drawtext's text='...' early.
    assert "'" not in _ff_text("it's a demo").replace(r"'\''", "")


def test_ff_text_escapes_colon_and_percent():
    out = _ff_text("Save 50%: today only")
    assert "\\:" in out
    assert "\\%" in out


def test_ff_text_escapes_backslash():
    # NOT vacuous: verified against two broken variants of _ff_text (strip
    # the backslash entirely; no-op the backslash replace) and both fail
    # this assertion (count drops to 0), confirming it actually detects a
    # regression rather than always passing. Exact-value check, not just a
    # count, so there's no ambiguity about what "escaped" means here.
    assert _ff_text(r"C:\path") == "C\\:\\\\path"
    assert _ff_text(r"C:\path").count("\\\\") == 1


def test_ff_text_escapes_windows_drive_colon():
    # The drive colon must still be escaped for ffmpeg filter strings even
    # though the path guard now accepts drive-letter paths.
    out = _ff_text(r"C:\Users\me\shot.png")
    assert "\\:" in out


# ---------------------------------------------------------------------------
# Classification rule -- pure string logic, identical on POSIX and Windows.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    # ffmpeg protocols that must stay rejected.
    "concat:a.mp4|b.mp4",
    "subfile:chunk.mp4",
    "http://evil/x.png",
    "https://evil/x.png",
    "data:image/png;base64,AAAA",
    "pipe:0",
    "file:///etc/passwd",
    "crypto:key",
    "zip:archive.zip",
    "tcp://host:1234",
    "udp://host:1234",
    "unknown-scheme:anything",
    # drive-RELATIVE form (no separator after the colon): stays rejected by
    # the documented rule -- ambiguous to ffmpeg, use absolute paths.
    "c:x.png",
    "C:x.png",
    # single letter schemes that are NOT followed by a separator stay protocols.
    "x:http://a",
])
def test_classification_rejects_protocols(raw):
    assert _looks_like_protocol(raw) is True


@pytest.mark.parametrize("raw", [
    # Windows drive-letter paths: absolute with backslashes / forward slashes,
    # and a bare drive (colon then end-of-string).
    r"C:\Users\me\shot.png",
    "C:/Users/me/shot.png",
    r"c:\users\me\shot.png",
    "C:",
    # colon appearing mid-name (underscore prevents a scheme match) stays legal.
    "logo_v2:final.png",
    # UNC and device paths: backslash-first, never scheme-shaped.
    r"\\server\share\shot.png",
    r"\\?\C:\Users\me\shot.png",
    r"\\.\pipe\capturd-finish",
    # ordinary POSIX paths.
    "/tmp/shot.png",
    "./shot.png",
    "shot.png",
])
def test_classification_accepts_paths(raw):
    assert _looks_like_protocol(raw) is False


# ---------------------------------------------------------------------------
# End-to-end _safe_input_path behaviour (SystemExit == non-zero exit).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "concat:a.mp4|b.mp4",
    "subfile:chunk.mp4",
    "http://evil/x.png",
    "data:image/png;base64,AAAA",
    "pipe:0",
    "file:///etc/passwd",
    "crypto:key",
    "zip:archive.zip",
    "unknown-scheme:anything",
    "tcp://host:1234",
    # drive-relative: same non-zero exit, on every platform, before any
    # filesystem access happens.
    "c:x.png",
])
def test_safe_input_path_rejects_protocol_prefix(bad):
    try:
        _safe_input_path(bad, "watermark")
    except SystemExit as exc:
        assert "protocol" in str(exc.code)
        return
    raise AssertionError(f"expected SystemExit for {bad!r}")


def test_safe_input_path_allows_midname_colon():
    # A colon that isn't a leading protocol scheme is a legal Linux
    # filename character (e.g. an asset export named "logo_v2:final.png")
    # and must NOT be rejected -- narrowed from an earlier blanket
    # any-colon rejection per rhobear-reviews feedback on PR #35.
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "logo_v2:final.png"
        p.write_bytes(b"\x89PNG")
        resolved = _safe_input_path(str(p), "watermark")
        assert Path(resolved).is_file()


def test_safe_input_path_rejects_missing_file():
    try:
        _safe_input_path("/definitely/not/a/real/file.png", "watermark")
    except SystemExit:
        pass
    else:
        raise AssertionError("expected SystemExit for a nonexistent file")


def test_safe_input_path_accepts_real_file():
    # On Windows f.name is an absolute C:\... temp path -- exactly the shape
    # that regressed (rejected as a protocol prefix) before the drive-letter
    # carve-out; on POSIX it is an absolute /... path. Both must resolve.
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        resolved = _safe_input_path(f.name, "watermark")
        resolved_path = Path(resolved)
        assert resolved_path.is_file()
        # The resolved ABSOLUTE path is returned, never the raw argument.
        assert resolved_path.is_absolute()
        assert resolved_path == Path(f.name).resolve()


@pytest.mark.skipif(not WINDOWS, reason="Windows drive-letter path semantics")
@pytest.mark.parametrize("make", [
    pytest.param(lambda p: str(p), id="C:\\...\\file.png"),
    pytest.param(lambda p: str(p).replace("\\", "/"), id="C:/.../file.png"),
])
def test_safe_input_path_accepts_windows_absolute_drive_paths(make):
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        raw = make(Path(f.name))
        assert raw[:2].endswith(":")  # sanity: we really are passing a drive path
        resolved = _safe_input_path(raw, "watermark")
        assert Path(resolved).is_file()
        assert Path(resolved).is_absolute()


@pytest.mark.skipif(WINDOWS, reason="POSIX absolute-path semantics")
def test_safe_input_path_accepts_posix_absolute_path():
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        resolved = _safe_input_path(f.name, "watermark")
        assert resolved.startswith("/")
        assert Path(resolved).is_file()


def test_safe_input_path_rejects_missing_windows_drive_path():
    # Nonexistent drive-letter path: on Windows the carve-out lets it reach
    # the is_file() check (-> "not a real file" exit); on POSIX it is a
    # relative path that doesn't exist either way. Non-zero exit on both.
    with pytest.raises(SystemExit):
        _safe_input_path(r"C:\definitely\not\a\real\shot.png", "infile")


def test_unc_and_device_paths_fall_through_to_file_checks():
    # Documented rule: UNC/device paths are never protocol-rejected; they fall
    # through to is_file()+resolve(). These names don't exist, so the exit is
    # the "not a real file" one -- proving they reached the file checks.
    for raw in (r"\\.\pipe\capturd-finish-does-not-exist", r"\\?\C:\definitely\not\real.png"):
        with pytest.raises(SystemExit) as excinfo:
            _safe_input_path(raw, "watermark")
        assert "not a real file" in str(excinfo.value.code)


if __name__ == "__main__":
    import pytest as _pytest

    def _cases(t):
        """Expand single-argument pytest.mark.parametrize marks and honour
        skipif, so the no-pytest-engine direct runner stays meaningful."""
        marks = [m for m in getattr(t, "pytestmark", []) if isinstance(m, _pytest.Mark)]
        if any(m.name == "skipif" and any(m.args) for m in marks):
            return [(f"{t.__name__} (platform)", None, True)]
        params = [m for m in marks if m.name == "parametrize"]
        if not params:
            return [(t.__name__, t, False)]
        out = []
        for v in params[0].args[1]:
            if isinstance(v, _pytest.Mark):  # pytest.param(...) wrapper
                val, label = v.args[0], v.id or repr(v.args[0])
            else:
                val, label = v, repr(v)
            out.append((f"{t.__name__}[{label}]", _make(t, val), False))
        return out

    def _make(t, val):
        return lambda: t(val)

    passed = skipped = 0
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        for label, call, skip in _cases(t):
            if skip:
                print(f"SKIP {label}")
                skipped += 1
                continue
            call()
            print(f"PASS {label}")
            passed += 1
    print(f"{passed} passed, {skipped} skipped")
