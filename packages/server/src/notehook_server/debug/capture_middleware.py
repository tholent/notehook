"""Request/response capture for real-device protocol debugging.

Appends one JSON line per request to data/captures/YYYY-MM-DD.jsonl with
credentials redacted, so captures are safe to share when asking for help.
Bodies are truncated and binary payloads elided — this is for protocol-shape
debugging, not payload archiving.
"""

import datetime
import json
import re
import time
from pathlib import Path
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

_MAX_BODY = 4096
_REDACT_KEYS = {"password", "token", "signature", "authorization", "x-access-token"}
_REDACT_QUERY = re.compile(r"(signature|token|authorization)=[^&]+", re.IGNORECASE)

# D7: notehook's own extension endpoints are never captured. They're not
# real-device protocol surface (nothing to debug against firmware for), and
# the changes long-poll would otherwise write a capture line on every wait
# (up to every 30s, forever) for no diagnostic value.
_SKIP_PREFIXES = ("/api/notehook/",)


def _redact_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: "***" if k.lower() in _REDACT_KEYS else _redact_obj(v) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return _REDACT_QUERY.sub(r"\1=***", obj)
    return obj


def _describe_body(body: bytes, content_type: str) -> Any:
    if not body:
        return None
    if "application/json" in content_type:
        try:
            return _redact_obj(json.loads(body[:_MAX_BODY]))
        except (ValueError, UnicodeDecodeError):
            pass
    if "multipart" in content_type or "octet-stream" in content_type:
        return f"<binary {len(body)} bytes>"
    try:
        return body[:_MAX_BODY].decode()
    except UnicodeDecodeError:
        return f"<binary {len(body)} bytes>"


class RequestCaptureMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, captures_dir: Path) -> None:
        super().__init__(app)
        self._dir = captures_dir

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path.startswith(_SKIP_PREFIXES):
            return await call_next(request)

        started = time.time()
        req_body = b""
        content_type = request.headers.get("content-type", "")
        # Skip buffering file-transfer bodies; everything else is small JSON.
        if "multipart" not in content_type:
            req_body = await request.body()

        response = await call_next(request)

        resp_body = b""
        resp_type = response.headers.get("content-type", "")
        if "application/json" in resp_type:
            chunks = [chunk async for chunk in response.body_iterator]  # type: ignore[attr-defined]
            resp_body = b"".join(chunks)
            response = Response(
                content=resp_body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        record = {
            "ts": started,
            "method": request.method,
            "path": request.url.path,
            "query": _REDACT_QUERY.sub(r"\1=***", request.url.query),
            "headers": _redact_obj(dict(request.headers)),
            "request_body": _describe_body(req_body, content_type),
            "status": response.status_code,
            "response_body": _describe_body(resp_body, resp_type),
            "duration_ms": round((time.time() - started) * 1000, 1),
        }
        self._dir.mkdir(parents=True, exist_ok=True)
        out = self._dir / f"{datetime.date.today().isoformat()}.jsonl"
        with out.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        return response
