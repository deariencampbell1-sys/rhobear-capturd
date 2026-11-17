"""Atomic local outputs without following symlinks in their paths."""
from __future__ import annotations

import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def _directory(path: Path):
    # Walk every component from the filesystem root. Holding the resulting
    # directory fd prevents a rename from redirecting subsequent operations.
    path = Path(os.path.abspath(path))
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def _check_target(fd: int, name: str) -> None:
    try:
        mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise OSError(f"output target must be a regular file: {name}")


def atomic_write(path: Path, data: bytes) -> None:
    path = Path(path)
    with _directory(path.parent) as directory:
        _check_target(directory, path.name)
        temp = f".{path.name}.{uuid.uuid4().hex}.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _check_target(directory, path.name)
            os.replace(temp, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temp, dir_fd=directory)
            except FileNotFoundError:
                pass


def safe_unlink(path: Path) -> None:
    path = Path(path)
    with _directory(path.parent) as directory:
        _check_target(directory, path.name)
        try:
            os.unlink(path.name, dir_fd=directory)
        except FileNotFoundError:
            pass
