"""Immutable, project-local storage for artifact members of any file size."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import tempfile
from typing import BinaryIO, Iterator


CHUNK_SIZE = 1024 * 1024


class ObjectStoreError(ValueError):
    pass


@dataclass(frozen=True)
class StoredObject:
    digest: str
    size: int


def file_sha256(path: str | Path) -> str:
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


@contextmanager
def _staged_copy(source: BinaryIO, directory: Path) -> Iterator[tuple[Path, StoredObject]]:
    fd, name = tempfile.mkstemp(prefix='.object-', suffix='.tmp', dir=directory)
    path = Path(name)
    try:
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, 'wb') as target:
            while chunk := source.read(CHUNK_SIZE):
                target.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            target.flush()
            os.fsync(target.fileno())
        yield path, StoredObject(digest.hexdigest(), size)
    finally:
        path.unlink(missing_ok=True)


def copy_verified(source: BinaryIO, target: Path, digest: str, size: int | None, scratch: Path) -> StoredObject:
    with _staged_copy(source, scratch) as (staged, obj):
        if obj.digest != digest or (size is not None and obj.size != size):
            raise ObjectStoreError('ARTIFACT_OBJECT_CORRUPT: copy does not match its digest and size')
        if os.path.lexists(target):
            raise FileExistsError(target)
        os.rename(staged, target)
    return obj


class ContentAddressedStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).absolute()

    def path_for(self, digest: str) -> Path:
        if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
            raise ObjectStoreError('ARTIFACT_DIGEST_INVALID')
        return self.root / digest[:2] / digest

    def _check_path(self, target: Path) -> None:
        if target.resolve() != target or (os.path.lexists(target) and not target.is_file()):
            raise ObjectStoreError('ARTIFACT_OBJECT_PATH: expected a regular file inside the store')

    def _check_existing(self, path: Path, obj: StoredObject) -> None:
        self._check_path(path)
        if path.stat().st_size != obj.size or file_sha256(path) != obj.digest:
            raise ObjectStoreError('ARTIFACT_OBJECT_CORRUPT: existing object does not match its digest')

    def put(self, source: str | Path, *, expected_digest: str | None = None) -> StoredObject:
        """Hash and seal a file without loading it into memory or overwriting different bytes."""
        if self.root.resolve() != self.root:
            raise ObjectStoreError('ARTIFACT_OBJECT_PATH: store must be a project-local directory')
        self.root.mkdir(parents=True, exist_ok=True)
        with Path(source).open('rb') as stream, _staged_copy(stream, self.root) as (staged, obj):
            if expected_digest is not None and obj.digest != expected_digest:
                raise ObjectStoreError('ARTIFACT_CANDIDATE_CHANGED')
            target = self.path_for(obj.digest)
            self._check_path(target)
            target.parent.mkdir(exist_ok=True)
            if os.path.lexists(target):
                self._check_existing(target, obj)
            else:
                try:
                    # Concurrent writers for this name have identical verified content.
                    os.replace(staged, target)
                except OSError:
                    if not os.path.lexists(target):
                        raise
                    self._check_existing(target, obj)
        return obj

    def copy_to(self, digest: str, destination: str | Path, *, expected_size: int | None = None,
                scratch: Path | None = None) -> StoredObject:
        """Verify a copy before publishing it into a caller-owned staging directory."""
        source = self.path_for(digest)
        self._check_path(source)
        if not source.is_file():
            raise ObjectStoreError('ARTIFACT_OBJECT_MISSING')
        target = Path(destination)
        with source.open('rb') as stream:
            return copy_verified(stream, target, digest, expected_size, scratch or target.parent)
