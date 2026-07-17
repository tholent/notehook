"""Stateless HMAC-signed download URLs, verified by /api/oss/download."""

import hashlib
import hmac
import secrets
import time

from noted_server.auth.service import TTLStore
from noted_server.config import Settings
from noted_server.errors import SignatureInvalid


class DownloadService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Replay guard: each signed URL nonce is single-use within its lifetime.
        self._seen_nonces = TTLStore(settings.download_url_ttl_seconds)

    def _sign(self, inner_name: str, node_id: int, timestamp: int, nonce: str) -> str:
        message = f"{inner_name}|{node_id}|{timestamp}|{nonce}".encode()
        return hmac.new(
            self._settings.secret_key.encode(), message, hashlib.sha256
        ).hexdigest()

    def signed_url(self, inner_name: str, node_id: int) -> str:
        timestamp = int(time.time() * 1000)
        nonce = secrets.token_hex(8)
        signature = self._sign(inner_name, node_id, timestamp, nonce)
        base = self._settings.base_url.rstrip("/")
        return (
            f"{base}/api/oss/download?path={inner_name}&signature={signature}"
            f"&timestamp={timestamp}&nonce={nonce}&pathId={node_id}"
        )

    def verify(
        self, inner_name: str, node_id: int, timestamp: int, nonce: str, signature: str
    ) -> None:
        expected = self._sign(inner_name, node_id, timestamp, nonce)
        if not hmac.compare_digest(expected, signature):
            raise SignatureInvalid()
        age_ms = int(time.time() * 1000) - timestamp
        if age_ms < 0 or age_ms > self._settings.download_url_ttl_seconds * 1000:
            raise SignatureInvalid("download link expired")
        if self._seen_nonces.pop(nonce) is not None:
            raise SignatureInvalid("download link already used")
        self._seen_nonces.put(nonce, "1")
