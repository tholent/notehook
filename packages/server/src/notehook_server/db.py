"""Database engine and session dependency."""

from collections.abc import Iterator

from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine

from notehook_server.config import Settings


def create_db_engine(settings: Settings) -> Engine:
    url = settings.effective_database_url()
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    SQLModel.metadata.create_all(engine)
    return engine


def session_factory(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session
