"""Local sync state: the three-way merge base (last-known local + remote state)."""

from pathlib import Path

from sqlmodel import Field, Session, SQLModel, create_engine, select


class SyncedFile(SQLModel, table=True):
    """Last synced state for one file, keyed by path relative to the sync root."""

    rel_path: str = Field(primary_key=True)
    server_id: int
    server_hash: str
    local_hash: str
    local_mtime_ns: int
    local_size: int
    is_folder: bool = False


class StateDB:
    def __init__(self, db_file: Path) -> None:
        db_file.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(
            f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
        )
        SQLModel.metadata.create_all(self._engine)

    def all(self) -> dict[str, SyncedFile]:
        with Session(self._engine) as session:
            return {row.rel_path: row for row in session.exec(select(SyncedFile)).all()}

    def upsert(self, record: SyncedFile) -> None:
        with Session(self._engine) as session:
            session.merge(record)
            session.commit()

    def remove(self, rel_path: str) -> None:
        with Session(self._engine) as session:
            row = session.get(SyncedFile, rel_path)
            if row is not None:
                session.delete(row)
                session.commit()
