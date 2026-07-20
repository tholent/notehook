"""FastAPI application factory."""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from notehook_protocol.models.common import fail
from notehook_server.auth.service import AuthService
from notehook_server.config import Settings, get_settings
from notehook_server.db import create_db_engine
from notehook_server.debug.capture_middleware import RequestCaptureMiddleware
from notehook_server.errors import AppError, app_error_handler
from notehook_server.files.blob_store import BlobStore
from notehook_server.files.download_service import DownloadService
from notehook_server.files.upload_service import UploadService
from notehook_server.routers import auth as auth_router
from notehook_server.routers import files_device, oss, stubs

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_dirs_and_secret()

    app = FastAPI(title="notehook server", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.engine = create_db_engine(settings)
    app.state.auth_service = AuthService(settings)
    app.state.blob_store = BlobStore(settings.blob_dir, settings.trash_dir)
    app.state.upload_service = UploadService(settings, app.state.blob_store)
    app.state.download_service = DownloadService(settings)

    # The OSS endpoints authenticate by signed URL, not token; in a single-user
    # server the owning user is unambiguous, so resolve it once at startup.
    with Session(app.state.engine) as session:
        user = app.state.auth_service.get_or_create_user(session)
        app.state.single_user_id = user.id

    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]

    app.include_router(auth_router.router)
    app.include_router(stubs.router)
    app.include_router(files_device.router)
    app.include_router(oss.router)

    if settings.debug_capture:
        app.add_middleware(RequestCaptureMiddleware, captures_dir=settings.captures_dir)

    # Catch-all: real firmware will call endpoints we haven't implemented.
    # Log them (visible in captures) and answer with the failure envelope
    # rather than a bare 404, so the device gets a parseable response.
    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        include_in_schema=False,
    )
    async def catch_all(request: Request, full_path: str) -> JSONResponse:
        logger.warning("unimplemented endpoint called: %s /%s", request.method, full_path)
        return JSONResponse(
            status_code=200,
            content=fail("9999", "not implemented").model_dump(),
        )

    return app


def run() -> None:
    """Console-script entry point."""
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host="0.0.0.0", port=8080)
