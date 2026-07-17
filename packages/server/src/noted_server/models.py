"""SQLModel tables: user, equipment, tokens, file tree, upload/sync sessions."""

import time

from sqlalchemy import Column, String, UniqueConstraint
from sqlmodel import Field, SQLModel

ROOT_ID = 0  # virtual root directory: no DB row, parent_id == 0 means "in root"


def now_ms() -> int:
    return int(time.time() * 1000)


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    account: str = Field(unique=True, index=True)
    user_name: str = "Supernote User"
    created_at: int = Field(default_factory=now_ms)


class Equipment(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    equipment_no: str = Field(unique=True, index=True)
    user_id: int = Field(foreign_key="user.id")
    equipment_type: int = 3  # 1=Web, 2=App, 3=Terminal/Device, 4=Platform
    first_seen_at: int = Field(default_factory=now_ms)
    last_seen_at: int = Field(default_factory=now_ms)


class AccessToken(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    token: str = Field(unique=True, index=True)
    user_id: int = Field(foreign_key="user.id")
    equipment_id: int = Field(foreign_key="equipment.id")
    issued_at: int = Field(default_factory=now_ms)
    expires_at: int
    revoked: bool = False


class FileNode(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("parent_id", "name", name="uq_sibling_name"),)

    id: int | None = Field(default=None, primary_key=True)
    parent_id: int = Field(default=ROOT_ID, index=True)
    # NOCASE collation: sibling names are unique case-insensitively and path
    # resolution matches regardless of case (the spec hints the real cloud
    # normalizes folder-name casing).
    name: str = Field(sa_column=Column(String(collation="NOCASE"), nullable=False))
    is_folder: bool = False
    size: int = 0
    content_hash: str | None = None  # md5, files only
    inner_name: str | None = None  # blob storage key, files only
    last_update_time: int = Field(default_factory=now_ms)
    version: int = 1
    owner_user_id: int = Field(foreign_key="user.id", index=True)
    last_modified_by: str | None = None  # equipment_no, audit only


class UploadSession(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    signature: str = Field(unique=True, index=True)  # opaque token echoed by client
    inner_name: str = Field(unique=True, index=True)
    equipment_id: int = Field(foreign_key="equipment.id")
    expected_path: str = ""
    expected_file_name: str = ""
    expected_size: int | None = None
    upload_id: str | None = None  # multipart assembly key
    total_chunks: int | None = None
    bytes_received: int = 0
    computed_md5: str | None = None
    status: str = "pending"  # pending | completed | expired
    created_at: int = Field(default_factory=now_ms)
    expires_at: int


class SyncSession(SQLModel, table=True):
    """Audit log of device sync sessions. Informational only — never a lock."""

    id: int | None = Field(default=None, primary_key=True)
    equipment_no: str = Field(index=True)
    started_at: int = Field(default_factory=now_ms)
    ended_at: int | None = None
    flag: str | None = None
    status: str = "active"  # active | completed
