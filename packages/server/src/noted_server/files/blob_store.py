"""On-disk blob storage, sharded by inner_name prefix.

inner_name format (per spec): {uuid4}-{tail}.{ext} where tail derives from the
client equipmentNo. Strictly validated — inner_names are the only client-echoed
value that ever touches a filesystem path.
"""

import hashlib
import re
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

from noted_server.errors import InvalidPath, UploadError

_INNER_NAME = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"-[0-9A-Za-z]{1,8}(\.[0-9A-Za-z._-]{1,32})?$"
)


def make_inner_name(equipment_no: str | None, file_name: str | None) -> str:
    tail = re.sub(r"[^0-9A-Za-z]", "", (equipment_no or "000"))[-3:] or "000"
    ext = ""
    if file_name and "." in file_name:
        candidate = file_name.rsplit(".", 1)[1]
        if re.fullmatch(r"[0-9A-Za-z._-]{1,32}", candidate):
            ext = f".{candidate}"
    return f"{uuid.uuid4()}-{tail}{ext}"


def validate_inner_name(inner_name: str) -> str:
    if not _INNER_NAME.match(inner_name):
        raise InvalidPath("invalid storage name")
    return inner_name


class BlobStore:
    def __init__(self, blob_dir: Path, trash_dir: Path) -> None:
        self._blob_dir = blob_dir
        self._trash_dir = trash_dir

    def path_for(self, inner_name: str) -> Path:
        validate_inner_name(inner_name)
        return self._blob_dir / inner_name[:2] / inner_name

    def write_stream(
        self, inner_name: str, chunks: Iterator[bytes], max_bytes: int
    ) -> tuple[int, str]:
        """Stream chunks to the blob, enforcing max size. Returns (size, md5)."""
        dest = self.path_for(inner_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.md5()
        size = 0
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with tmp.open("wb") as fh:
                for chunk in chunks:
                    size += len(chunk)
                    if size > max_bytes:
                        raise UploadError("upload exceeds maximum allowed size")
                    digest.update(chunk)
                    fh.write(chunk)
            tmp.replace(dest)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return size, digest.hexdigest()

    def exists(self, inner_name: str) -> bool:
        return self.path_for(inner_name).exists()

    def open_path(self, inner_name: str) -> Path:
        p = self.path_for(inner_name)
        if not p.exists():
            raise UploadError("blob not found on disk")
        return p

    def trash(self, inner_name: str) -> None:
        """Move a blob into the trash dir (safety net; purge is a separate op)."""
        p = self.path_for(inner_name)
        if p.exists():
            self._trash_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(self._trash_dir / inner_name))
