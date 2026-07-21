"""File-Device (NAS sync) endpoints: sync session, tree ops, upload/download apply."""

import time

from fastapi import APIRouter, Request

from notehook_protocol.models.file import (
    AllocationVO,
    CapacityLocalDTO,
    CapacityLocalVO,
    CreateFolderLocalDTO,
    CreateFolderLocalVO,
    DeleteFolderLocalDTO,
    DeleteFolderLocalVO,
    FileCopyLocalDTO,
    FileCopyLocalVO,
    FileDownloadLocalDTO,
    FileDownloadLocalVO,
    FileMoveLocalDTO,
    FileMoveLocalVO,
    FileQueryByPathLocalDTO,
    FileQueryByPathLocalVO,
    FileQueryLocalDTO,
    FileQueryLocalVO,
    FileUploadApplyLocalDTO,
    FileUploadApplyLocalVO,
    FileUploadFinishLocalDTO,
    FileUploadFinishLocalVO,
    ListFolderLocalDTO,
    ListFolderLocalVO,
    ListFolderV2DTO,
    SynchronousEndLocalDTO,
    SynchronousEndLocalVO,
    SynchronousStartLocalDTO,
    SynchronousStartLocalVO,
)
from notehook_server.auth.deps import CurrentDep, DbDep, SettingsDep
from notehook_server.errors import NotFound
from notehook_server.files import sync_service, tree_service
from notehook_server.files.blob_store import BlobStore
from notehook_server.files.download_service import DownloadService
from notehook_server.files.upload_service import UploadService
from notehook_server.models import ROOT_ID

router = APIRouter()


def _guard_sync(db: DbDep, settings: SettingsDep, equipment_no: str) -> None:
    """Reject a mutation with E0079 while another device is mid-sync."""
    sync_service.guard_not_syncing(db, equipment_no, settings.sync_session_ttl_seconds * 1000)


def _upload_service(request: Request) -> UploadService:
    service: UploadService = request.app.state.upload_service
    return service


def _download_service(request: Request) -> DownloadService:
    service: DownloadService = request.app.state.download_service
    return service


def _blob_store(request: Request) -> BlobStore:
    store: BlobStore = request.app.state.blob_store
    return store


@router.post("/api/file/2/files/synchronous/start")
def sync_start(
    dto: SynchronousStartLocalDTO, db: DbDep, current: CurrentDep, settings: SettingsDep
) -> SynchronousStartLocalVO:
    equipment_no = dto.equipmentNo or current.equipment.equipment_no
    # Single-device lock: rejects with E0078 if another device is mid-sync.
    sync_service.begin_sync(db, equipment_no, settings.sync_session_ttl_seconds * 1000)
    # Account-scoped: True only if the server already holds data. Reporting
    # False when data exists could make the device treat it as mass deletion.
    syn_type = tree_service.has_any_files(db, current.user.id or 0)
    return SynchronousStartLocalVO(
        success=True, errorCode="0000", equipmentNo=equipment_no, synType=syn_type
    )


@router.post("/api/file/2/files/synchronous/end")
def sync_end(
    dto: SynchronousEndLocalDTO, db: DbDep, current: CurrentDep
) -> SynchronousEndLocalVO:
    equipment_no = dto.equipmentNo or current.equipment.equipment_no
    sync_service.end_sync(db, equipment_no, dto.flag)
    return SynchronousEndLocalVO(success=True, errorCode="0000", equipmentNo=equipment_no)


@router.post("/api/file/2/files/create_folder_v2")
def create_folder_v2(
    dto: CreateFolderLocalDTO, db: DbDep, current: CurrentDep, settings: SettingsDep
) -> CreateFolderLocalVO:
    _guard_sync(db, settings, dto.equipmentNo or current.equipment.equipment_no)
    node = tree_service.create_folder(
        db,
        current.user.id or 0,
        dto.path or "",
        dto.autorename,
        dto.equipmentNo or current.equipment.equipment_no,
    )
    return CreateFolderLocalVO(
        success=True,
        errorCode="0000",
        equipmentNo=dto.equipmentNo or current.equipment.equipment_no,
        metadata=tree_service.to_metadata(db, node),
    )


@router.post("/api/file/2/files/list_folder")
def list_folder(dto: ListFolderV2DTO, db: DbDep, current: CurrentDep) -> ListFolderLocalVO:
    user_id = current.user.id or 0
    if dto.path in (None, "", "/"):
        parent_id = ROOT_ID
    else:
        node = tree_service.resolve_path(db, user_id, dto.path)
        if node is None:
            parent_id = ROOT_ID
        elif not node.is_folder:
            raise NotFound(f"not a folder: {dto.path}")
        else:
            parent_id = node.id or 0
    entries = tree_service.list_entries(db, user_id, parent_id, dto.recursive)
    return ListFolderLocalVO(
        success=True,
        errorCode="0000",
        equipmentNo=dto.equipmentNo or current.equipment.equipment_no,
        entries=entries,
    )


@router.post("/api/file/3/files/list_folder_v3")
def list_folder_v3(
    dto: ListFolderLocalDTO, db: DbDep, current: CurrentDep
) -> ListFolderLocalVO:
    user_id = current.user.id or 0
    if dto.id != ROOT_ID:
        node = tree_service.get_node(db, user_id, dto.id)
        if not node.is_folder:
            raise NotFound(f"not a folder: {dto.id}")
    entries = tree_service.list_entries(db, user_id, dto.id, dto.recursive)
    return ListFolderLocalVO(
        success=True,
        errorCode="0000",
        equipmentNo=dto.equipmentNo or current.equipment.equipment_no,
        entries=entries,
    )


@router.post("/api/file/3/files/delete_folder_v3")
def delete_folder_v3(
    dto: DeleteFolderLocalDTO,
    db: DbDep,
    current: CurrentDep,
    request: Request,
    settings: SettingsDep,
) -> DeleteFolderLocalVO:
    _guard_sync(db, settings, dto.equipmentNo or current.equipment.equipment_no)
    node, orphaned = tree_service.delete_node(
        db, current.user.id or 0, dto.id, dto.equipmentNo or current.equipment.equipment_no
    )
    metadata = tree_service.to_metadata(db, node)
    blobs = _blob_store(request)
    for inner_name in orphaned:
        if not tree_service.blob_referenced(db, inner_name):
            blobs.trash(inner_name)
    return DeleteFolderLocalVO(
        success=True,
        errorCode="0000",
        equipmentNo=dto.equipmentNo or current.equipment.equipment_no,
        metadata=metadata,
    )


@router.post("/api/file/3/files/query_v3")
def query_v3(dto: FileQueryLocalDTO, db: DbDep, current: CurrentDep) -> FileQueryLocalVO:
    if not dto.id:
        raise NotFound("missing id")
    node = tree_service.get_node(db, current.user.id or 0, int(dto.id))
    return FileQueryLocalVO(
        success=True,
        errorCode="0000",
        equipmentNo=dto.equipmentNo or current.equipment.equipment_no,
        entriesVO=tree_service.to_entry(db, node),
    )


@router.post("/api/file/3/files/query/by/path_v3")
def query_by_path_v3(
    dto: FileQueryByPathLocalDTO, db: DbDep, current: CurrentDep
) -> FileQueryByPathLocalVO:
    node = tree_service.resolve_path(db, current.user.id or 0, dto.path or "")
    if node is None:
        raise NotFound("root has no entry")
    return FileQueryByPathLocalVO(
        success=True,
        errorCode="0000",
        equipmentNo=dto.equipmentNo or current.equipment.equipment_no,
        entriesVO=tree_service.to_entry(db, node),
    )


@router.post("/api/file/3/files/move_v3")
def move_v3(
    dto: FileMoveLocalDTO, db: DbDep, current: CurrentDep, settings: SettingsDep
) -> FileMoveLocalVO:
    _guard_sync(db, settings, dto.equipmentNo or current.equipment.equipment_no)
    node = tree_service.move_node(
        db,
        current.user.id or 0,
        dto.id,
        dto.to_path or "",
        dto.autorename,
        dto.equipmentNo or current.equipment.equipment_no,
    )
    return FileMoveLocalVO(
        success=True,
        errorCode="0000",
        equipmentNo=dto.equipmentNo or current.equipment.equipment_no,
        entriesVO=tree_service.to_entry(db, node),
    )


@router.post("/api/file/3/files/copy_v3")
def copy_v3(
    dto: FileCopyLocalDTO, db: DbDep, current: CurrentDep, settings: SettingsDep
) -> FileCopyLocalVO:
    _guard_sync(db, settings, dto.equipmentNo or current.equipment.equipment_no)
    node = tree_service.copy_node(
        db,
        current.user.id or 0,
        dto.id,
        dto.to_path,
        dto.autorename,
        dto.equipmentNo or current.equipment.equipment_no,
    )
    return FileCopyLocalVO(
        success=True,
        errorCode="0000",
        equipmentNo=dto.equipmentNo or current.equipment.equipment_no,
        entriesVO=tree_service.to_entry(db, node),
    )


@router.post("/api/file/3/files/upload/apply")
def upload_apply_v3(
    dto: FileUploadApplyLocalDTO,
    db: DbDep,
    current: CurrentDep,
    request: Request,
    settings: SettingsDep,
) -> FileUploadApplyLocalVO:
    _guard_sync(db, settings, dto.equipmentNo or current.equipment.equipment_no)
    service = _upload_service(request)
    upload = service.apply(
        db,
        equipment_id=current.equipment.id or 0,
        equipment_no=dto.equipmentNo or current.equipment.equipment_no,
        path=dto.path,
        file_name=dto.fileName,
        size=dto.size,
    )
    full_url, part_url = service.upload_urls(upload)
    return FileUploadApplyLocalVO(
        success=True,
        errorCode="0000",
        equipmentNo=dto.equipmentNo or current.equipment.equipment_no,
        bucketName="supernote",
        innerName=upload.inner_name,
        xAmzDate=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        authorization=upload.signature,
        fullUploadUrl=full_url,
        partUploadUrl=part_url,
    )


@router.post("/api/file/2/files/upload/finish")
def upload_finish_v2(
    dto: FileUploadFinishLocalDTO,
    db: DbDep,
    current: CurrentDep,
    request: Request,
    settings: SettingsDep,
) -> FileUploadFinishLocalVO:
    _guard_sync(db, settings, dto.equipmentNo or current.equipment.equipment_no)
    service = _upload_service(request)
    node = service.finish(
        db,
        user_id=current.user.id or 0,
        equipment_no=dto.equipmentNo or current.equipment.equipment_no,
        inner_name=dto.innerName,
        file_name=dto.fileName,
        content_hash=dto.content_hash,
        path=dto.path,
    )
    return FileUploadFinishLocalVO(
        success=True,
        errorCode="0000",
        equipmentNo=dto.equipmentNo or current.equipment.equipment_no,
        path_display=tree_service.node_path(db, node),
        id=str(node.id),
        size=node.size,
        name=node.name,
        content_hash=node.content_hash,
    )


@router.post("/api/file/3/files/download_v3")
def download_v3(
    dto: FileDownloadLocalDTO, db: DbDep, current: CurrentDep, request: Request
) -> FileDownloadLocalVO:
    node = tree_service.get_node(db, current.user.id or 0, dto.id)
    if node.is_folder or not node.inner_name:
        raise NotFound("not a downloadable file")
    url = _download_service(request).signed_url(node.inner_name, node.id or 0)
    return FileDownloadLocalVO(
        success=True,
        errorCode="0000",
        equipmentNo=dto.equipmentNo or current.equipment.equipment_no,
        id=str(node.id),
        url=url,
        name=node.name,
        path_display=tree_service.node_path(db, node),
        content_hash=node.content_hash,
        size=node.size,
        is_downloadable=True,
    )


@router.post("/api/file/2/users/get_space_usage")
def get_space_usage(
    dto: CapacityLocalDTO, db: DbDep, current: CurrentDep, settings: SettingsDep
) -> CapacityLocalVO:
    used = tree_service.used_bytes(db, current.user.id or 0)
    return CapacityLocalVO(
        success=True,
        errorCode="0000",
        used=used,
        allocationVO=AllocationVO(tag="personal", allocated=settings.total_capacity_bytes),
        equipmentNo=dto.equipmentNo or current.equipment.equipment_no,
    )
