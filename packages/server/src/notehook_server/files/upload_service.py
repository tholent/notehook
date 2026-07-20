"""Upload session lifecycle: apply -> receive bytes (full or chunked) -> finish."""

import secrets
import time
from collections.abc import Iterator
from pathlib import Path

from sqlmodel import Session, select

from notehook_server.config import Settings
from notehook_server.errors import QuotaExceeded, SignatureInvalid, UploadError
from notehook_server.files import tree_service
from notehook_server.files.blob_store import BlobStore, make_inner_name
from notehook_server.models import FileNode, UploadSession, now_ms


class UploadService:
    def __init__(self, settings: Settings, blob_store: BlobStore) -> None:
        self._settings = settings
        self._blobs = blob_store

    def apply(
        self,
        session: Session,
        equipment_id: int,
        equipment_no: str | None,
        path: str | None,
        file_name: str | None,
        size: int | None,
    ) -> UploadSession:
        if size is not None and size > self._settings.max_upload_bytes:
            raise UploadError("file exceeds maximum allowed size")
        inner_name = make_inner_name(equipment_no, file_name)
        upload = UploadSession(
            signature=secrets.token_urlsafe(24),
            inner_name=inner_name,
            equipment_id=equipment_id,
            expected_path=path or "",
            expected_file_name=file_name or "",
            expected_size=size,
            expires_at=now_ms() + self._settings.upload_session_ttl_seconds * 1000,
        )
        session.add(upload)
        session.commit()
        session.refresh(upload)
        return upload

    def upload_urls(self, upload: UploadSession) -> tuple[str, str]:
        base = self._settings.base_url.rstrip("/")
        ts = int(time.time() * 1000)
        nonce = secrets.token_hex(8)
        query = (
            f"signature={upload.signature}&timestamp={ts}"
            f"&nonce={nonce}&path={upload.inner_name}"
        )
        return (
            f"{base}/api/oss/upload?{query}",
            f"{base}/api/oss/upload/part?{query}",
        )

    def _active_session(self, session: Session, signature: str, path: str) -> UploadSession:
        upload = session.exec(
            select(UploadSession).where(UploadSession.signature == signature)
        ).one_or_none()
        if (
            upload is None
            or upload.inner_name != path
            or upload.status != "pending"
            or upload.expires_at < now_ms()
        ):
            raise SignatureInvalid()
        return upload

    def _check_quota(self, session: Session, user_id: int, incoming: int) -> None:
        used = tree_service.used_bytes(session, user_id)
        if used + incoming > self._settings.total_capacity_bytes:
            raise QuotaExceeded()

    def receive_full(
        self,
        session: Session,
        user_id: int,
        signature: str,
        path: str,
        chunks: Iterator[bytes],
    ) -> UploadSession:
        upload = self._active_session(session, signature, path)
        self._check_quota(session, user_id, upload.expected_size or 0)
        size, md5 = self._blobs.write_stream(
            upload.inner_name, chunks, self._settings.max_upload_bytes
        )
        self._check_quota(session, user_id, 0)  # re-check with actual bytes on disk
        upload.bytes_received = size
        upload.computed_md5 = md5
        upload.status = "completed"
        session.add(upload)
        session.commit()
        session.refresh(upload)
        return upload

    def _chunk_path(self, upload_id: str, part_number: int) -> Path:
        safe = "".join(c for c in upload_id if c.isalnum() or c in "-_")[:64]
        if not safe:
            raise UploadError("invalid uploadId")
        return self._settings.chunks_dir / f"{safe}.{part_number:05d}"

    def receive_part(
        self,
        session: Session,
        user_id: int,
        signature: str,
        path: str,
        upload_id: str,
        part_number: int,
        total_chunks: int,
        data: bytes,
    ) -> tuple[UploadSession, bool]:
        """Store one chunk; assemble when all parts are present.

        Returns (session, completed).
        """
        upload = self._active_session(session, signature, path)
        if part_number < 1 or part_number > total_chunks:
            raise UploadError("invalid partNumber")
        if upload.upload_id is None:
            upload.upload_id = upload_id
            upload.total_chunks = total_chunks
            session.add(upload)
            session.commit()
        elif upload.upload_id != upload_id or upload.total_chunks != total_chunks:
            raise UploadError("uploadId/totalChunks mismatch")

        chunk_file = self._chunk_path(upload_id, part_number)
        chunk_file.parent.mkdir(parents=True, exist_ok=True)
        running_total = sum(
            self._chunk_path(upload_id, n).stat().st_size
            for n in range(1, total_chunks + 1)
            if self._chunk_path(upload_id, n).exists()
        )
        if running_total + len(data) > self._settings.max_upload_bytes:
            raise UploadError("upload exceeds maximum allowed size")
        chunk_file.write_bytes(data)

        parts = [self._chunk_path(upload_id, n) for n in range(1, total_chunks + 1)]
        if not all(p.exists() for p in parts):
            return upload, False

        self._check_quota(session, user_id, sum(p.stat().st_size for p in parts))

        def assembled() -> Iterator[bytes]:
            for p in parts:
                yield p.read_bytes()

        size, md5 = self._blobs.write_stream(
            upload.inner_name, assembled(), self._settings.max_upload_bytes
        )
        for p in parts:
            p.unlink(missing_ok=True)
        upload.bytes_received = size
        upload.computed_md5 = md5
        upload.status = "completed"
        session.add(upload)
        session.commit()
        session.refresh(upload)
        return upload, True

    def finish(
        self,
        session: Session,
        user_id: int,
        equipment_no: str | None,
        inner_name: str,
        file_name: str,
        content_hash: str,
        path: str | None,
    ) -> FileNode:
        upload = session.exec(
            select(UploadSession).where(UploadSession.inner_name == inner_name)
        ).one_or_none()
        if upload is None or upload.status != "completed":
            raise UploadError("no completed upload for this innerName")
        # Never trust the client-claimed hash: compare against server-computed md5.
        if upload.computed_md5 != content_hash.lower():
            raise UploadError("content_hash does not match uploaded bytes")

        folder_path = path or upload.expected_path
        parent_id = tree_service.ensure_folder(session, user_id, folder_path, equipment_no)
        name = tree_service.validate_name(file_name)
        existing = tree_service.find_child(session, user_id, parent_id, name)
        if existing is not None and not existing.is_folder:
            # Same file re-uploaded (modified): replace content in place.
            old_inner = existing.inner_name
            existing.inner_name = upload.inner_name
            existing.size = upload.bytes_received
            existing.content_hash = upload.computed_md5
            existing.last_update_time = now_ms()
            existing.version += 1
            existing.last_modified_by = equipment_no
            session.add(existing)
            session.commit()
            session.refresh(existing)
            if old_inner and not tree_service.blob_referenced(session, old_inner):
                self._blobs.trash(old_inner)
            node = existing
        elif existing is not None:
            raise UploadError(f"a folder named {name!r} already exists at that path")
        else:
            node = FileNode(
                parent_id=parent_id,
                name=name,
                is_folder=False,
                size=upload.bytes_received,
                content_hash=upload.computed_md5,
                inner_name=upload.inner_name,
                owner_user_id=user_id,
                last_modified_by=equipment_no,
            )
            session.add(node)
            session.commit()
            session.refresh(node)
        session.delete(upload)
        session.commit()
        return node

    def cleanup_expired(self, session: Session) -> int:
        """Remove expired pending sessions and their orphaned chunk files."""
        expired = session.exec(
            select(UploadSession).where(
                UploadSession.status == "pending",
                UploadSession.expires_at < now_ms(),
            )
        ).all()
        for upload in expired:
            if upload.upload_id and upload.total_chunks:
                for n in range(1, upload.total_chunks + 1):
                    self._chunk_path(upload.upload_id, n).unlink(missing_ok=True)
            session.delete(upload)
        session.commit()
        return len(expired)
