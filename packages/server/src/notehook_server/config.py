"""Server settings, loaded from environment variables with the NOTEHOOK_ prefix."""

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NOTEHOOK_")

    # Single-account credentials. password_md5 = MD5 hex of the plaintext password;
    # generate with scripts/hash_password.py. The plaintext is never stored server-side.
    account: str = "user@example.com"
    password_md5: str = ""
    user_name: str = "Supernote User"

    # External base URL clients can reach us at — embedded in upload/download URLs
    # handed to the device, so it must be resolvable/reachable from the device.
    base_url: str = "http://localhost:8080"

    data_dir: Path = Path("data")
    database_url: str = ""  # defaults to sqlite file under data_dir

    token_ttl_seconds: int = 30 * 24 * 3600
    random_code_ttl_seconds: int = 120
    upload_session_ttl_seconds: int = 3600
    download_url_ttl_seconds: int = 900
    login_attempts_per_minute: int = 5
    # After this long an unfinished sync session is treated as abandoned so the
    # single-device sync lock (E0078/E0079) self-heals if a device crashes.
    sync_session_ttl_seconds: int = 600

    max_upload_bytes: int = 2 * 1024**3
    total_capacity_bytes: int = 32 * 1024**3

    debug_capture: bool = False

    # HMAC key for signed download URLs; auto-generated and persisted if unset.
    secret_key: str = ""

    @property
    def blob_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def trash_dir(self) -> Path:
        return self.data_dir / "trash"

    @property
    def chunks_dir(self) -> Path:
        return self.data_dir / "chunks"

    @property
    def captures_dir(self) -> Path:
        return self.data_dir / "captures"

    def effective_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_dir / 'notehook.db'}"

    def ensure_dirs_and_secret(self) -> None:
        for d in (self.data_dir, self.blob_dir, self.trash_dir, self.chunks_dir):
            d.mkdir(parents=True, exist_ok=True)
        if self.debug_capture:
            self.captures_dir.mkdir(parents=True, exist_ok=True)
        if not self.secret_key:
            key_file = self.data_dir / "secret_key"
            if not key_file.exists():
                key_file.write_text(secrets.token_hex(32))
                key_file.chmod(0o600)
            self.secret_key = key_file.read_text().strip()


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs_and_secret()
    return settings
