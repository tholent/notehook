"""Local directory scanning with mtime+size fast-path hashing."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from notehook_cli.state_db import SyncedFile

_IGNORED_SUFFIX = ".notehook-tmp"


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class LocalFile:
    rel_path: str
    abs_path: Path
    size: int
    mtime_ns: int
    is_folder: bool
    _cached_hash: str | None = None

    def content_hash(self, known: SyncedFile | None = None) -> str:
        """md5 of the file, reusing the recorded hash when mtime+size match."""
        if self._cached_hash is None:
            if (
                known is not None
                and known.local_mtime_ns == self.mtime_ns
                and known.local_size == self.size
            ):
                self._cached_hash = known.local_hash
            else:
                self._cached_hash = file_md5(self.abs_path)
        return self._cached_hash


def scan_local(root: Path) -> dict[str, LocalFile]:
    """Walk the sync root, returning {relative posix path: LocalFile}."""
    result: dict[str, LocalFile] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        name = path.name
        if name.startswith(".") or name.endswith(_IGNORED_SUFFIX):
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        rel = path.relative_to(root).as_posix()
        stat = path.stat()
        result[rel] = LocalFile(
            rel_path=rel,
            abs_path=path,
            size=stat.st_size if path.is_file() else 0,
            mtime_ns=stat.st_mtime_ns,
            is_folder=path.is_dir(),
        )
    return result
