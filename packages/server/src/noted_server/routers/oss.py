"""OSS-local endpoints: the actual byte transfer targets for upload/download URLs.

These are authenticated by the signed query parameters issued at apply/download
time (the device echoes them back verbatim), not by x-access-token.
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Query, Request, UploadFile
from fastapi.responses import FileResponse

from noted_protocol.models.file import FileChunkVO, UploadFileVO
from noted_server.auth.deps import DbDep
from noted_server.files.blob_store import BlobStore, validate_inner_name
from noted_server.files.download_service import DownloadService
from noted_server.files.upload_service import UploadService

router = APIRouter()

_CHUNK_SIZE = 1024 * 1024


def _upload_service(request: Request) -> UploadService:
    service: UploadService = request.app.state.upload_service
    return service


def _single_user_id(request: Request) -> int:
    user_id: int = request.app.state.single_user_id
    return user_id


@router.post("/api/oss/upload")
def oss_upload(
    request: Request,
    db: DbDep,
    file: UploadFile,
    signature: Annotated[str, Query()],
    path: Annotated[str, Query()],
    timestamp: Annotated[int | None, Query()] = None,
    nonce: Annotated[str | None, Query()] = None,
) -> UploadFileVO:
    service = _upload_service(request)

    # Starlette spools the multipart body to a temp file; stream from it rather
    # than loading the whole upload into memory.
    def chunks() -> Iterator[bytes]:
        while True:
            data = file.file.read(_CHUNK_SIZE)
            if not data:
                return
            yield data

    upload = service.receive_full(db, _single_user_id(request), signature, path, chunks())
    return UploadFileVO(
        success=True, errorCode="0000", innerName=upload.inner_name, md5=upload.computed_md5
    )


@router.post("/api/oss/upload/part")
def oss_upload_part(
    request: Request,
    db: DbDep,
    file: UploadFile,
    signature: Annotated[str, Query()],
    path: Annotated[str, Query()],
    uploadId: Annotated[str, Query()],
    partNumber: Annotated[int, Query()],
    totalChunks: Annotated[int, Query()],
    timestamp: Annotated[int | None, Query()] = None,
    nonce: Annotated[str | None, Query()] = None,
) -> FileChunkVO:
    service = _upload_service(request)
    data = file.file.read()
    upload, completed = service.receive_part(
        db,
        _single_user_id(request),
        signature,
        path,
        uploadId,
        partNumber,
        totalChunks,
        data,
    )
    return FileChunkVO(
        success=True,
        errorCode="0000",
        uploadId=uploadId,
        partNumber=partNumber,
        totalChunks=totalChunks,
        chunkMd5=upload.computed_md5 if completed else None,
        status="completed" if completed else "uploading",
    )


@router.get("/api/oss/download")
def oss_download(
    request: Request,
    path: Annotated[str, Query()],
    signature: Annotated[str, Query()],
    timestamp: Annotated[int, Query()],
    nonce: Annotated[str, Query()],
    pathId: Annotated[int, Query()],
) -> FileResponse:
    validate_inner_name(path)
    download: DownloadService = request.app.state.download_service
    download.verify(path, pathId, timestamp, nonce, signature)
    blobs: BlobStore = request.app.state.blob_store
    return FileResponse(
        blobs.open_path(path),
        media_type="application/octet-stream",
        filename=path,
    )
