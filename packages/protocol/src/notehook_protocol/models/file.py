"""File-Device DTO/VO models (docs/openapi/components/schemas/files-core.yaml
and files-upload-share-oss.yaml).

Deliberately lenient on numeric types: the spec types ``size`` as string on the
upload DTOs but integer elsewhere, and real firmware behavior is unverified, so
sizes accept either and normalize to int.
"""

from typing import Literal

from pydantic import field_validator

from notehook_protocol.models.common import BaseVO, ProtocolModel


class _LenientSize(ProtocolModel):
    @field_validator("size", mode="before", check_fields=False)
    @classmethod
    def _coerce_size(cls, v: object) -> object:
        if isinstance(v, str) and v.strip():
            return int(v)
        if isinstance(v, str):
            return None
        return v


# --- Sync session ---


class SynchronousStartLocalDTO(ProtocolModel):
    equipmentNo: str | None = None


class SynchronousStartLocalVO(BaseVO):
    equipmentNo: str | None = None
    # True: differential sync (server has data). False: init mode (server empty).
    # Must reflect real server state or the device may misread it as mass deletion.
    synType: bool = False


class SynchronousEndLocalDTO(ProtocolModel):
    equipmentNo: str | None = None
    flag: str | None = None


class SynchronousEndLocalVO(BaseVO):
    equipmentNo: str | None = None


# --- Entries / metadata ---


class MetadataVO(ProtocolModel):
    tag: Literal["file", "folder"] | None = None
    id: str | None = None
    name: str | None = None
    path_display: str | None = None


class EntriesVO(ProtocolModel):
    tag: Literal["file", "folder"] | None = None
    id: str | None = None
    name: str | None = None
    path_display: str | None = None
    content_hash: str | None = None
    is_downloadable: bool | None = None
    size: int | None = None
    lastUpdateTime: int | None = None
    parent_path: str | None = None
    # notehook extension, not in the spec: equipment_no that last modified the
    # node. Consumed by the notehook CLI for workflow event attribution.
    last_modified_by: str | None = None


# --- Folder operations ---


class CreateFolderLocalDTO(ProtocolModel):
    equipmentNo: str | None = None
    path: str | None = None
    autorename: bool = False


class CreateFolderLocalVO(BaseVO):
    equipmentNo: str | None = None
    metadata: MetadataVO | None = None


class ListFolderV2DTO(ProtocolModel):
    equipmentNo: str | None = None
    path: str | None = None
    recursive: bool = False


class ListFolderLocalDTO(ProtocolModel):
    equipmentNo: str | None = None
    id: int = 0
    recursive: bool = False


class ListFolderLocalVO(BaseVO):
    equipmentNo: str | None = None
    entries: list[EntriesVO] = []


class DeleteFolderLocalDTO(ProtocolModel):
    equipmentNo: str | None = None
    id: int


class DeleteFolderLocalVO(BaseVO):
    equipmentNo: str | None = None
    metadata: MetadataVO | None = None


# --- Query ---


class FileQueryLocalDTO(ProtocolModel):
    equipmentNo: str | None = None
    id: str | None = None


class FileQueryLocalVO(BaseVO):
    equipmentNo: str | None = None
    entriesVO: EntriesVO | None = None


class FileQueryByPathLocalDTO(ProtocolModel):
    equipmentNo: str | None = None
    path: str | None = None


class FileQueryByPathLocalVO(BaseVO):
    equipmentNo: str | None = None
    entriesVO: EntriesVO | None = None


# --- Move / copy ---


class FileMoveLocalDTO(ProtocolModel):
    equipmentNo: str | None = None
    autorename: bool = False
    id: int
    to_path: str | None = None


class FileMoveLocalVO(BaseVO):
    equipmentNo: str | None = None
    entriesVO: EntriesVO | None = None


class FileCopyLocalDTO(ProtocolModel):
    equipmentNo: str | None = None
    id: int
    autorename: bool = False
    to_path: str


class FileCopyLocalVO(BaseVO):
    equipmentNo: str | None = None
    entriesVO: EntriesVO | None = None


# --- Upload / download ---


class FileUploadApplyLocalDTO(_LenientSize):
    equipmentNo: str | None = None
    path: str | None = None
    fileName: str | None = None
    size: int | None = None


class FileUploadApplyLocalVO(BaseVO):
    equipmentNo: str | None = None
    bucketName: str | None = None
    innerName: str | None = None
    xAmzDate: str | None = None
    authorization: str | None = None
    fullUploadUrl: str | None = None
    partUploadUrl: str | None = None


class FileUploadFinishLocalDTO(_LenientSize):
    fileName: str
    content_hash: str
    innerName: str
    equipmentNo: str | None = None
    path: str | None = None
    size: int | None = None


class FileUploadFinishLocalVO(BaseVO):
    equipmentNo: str | None = None
    path_display: str | None = None
    id: str | None = None
    size: int | None = None
    name: str | None = None
    content_hash: str | None = None


class FileDownloadLocalDTO(ProtocolModel):
    equipmentNo: str | None = None
    id: int


class FileDownloadLocalVO(BaseVO):
    equipmentNo: str | None = None
    id: str | None = None
    url: str | None = None
    name: str | None = None
    path_display: str | None = None
    content_hash: str | None = None
    size: int | None = None
    is_downloadable: bool | None = None


# --- Capacity ---


class CapacityLocalDTO(ProtocolModel):
    equipmentNo: str | None = None


class AllocationVO(ProtocolModel):
    tag: str | None = None
    allocated: int | None = None


class CapacityLocalVO(BaseVO):
    used: int | None = None
    allocationVO: AllocationVO | None = None
    equipmentNo: str | None = None


# --- OSS (system.yaml) ---


class UploadFileVO(BaseVO):
    innerName: str | None = None
    md5: str | None = None


class FileChunkVO(BaseVO):
    uploadId: str | None = None
    partNumber: int | None = None
    totalChunks: int | None = None
    chunkMd5: str | None = None
    status: str | None = None
