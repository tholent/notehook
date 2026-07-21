"""Client configuration and credential storage.

Config lives in ~/.config/notehook/config.toml. Only the access token is
persisted (0600 file); the password is used transiently at login and discarded.
"""

import os
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def default_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "notehook"


@dataclass
class WorkflowsConfig:
    """`[workflows]` section of config.toml (docs/workflow-spec.md §6
    "Housekeeping" + docs/workflow-implementation-plan.md Phase 5): runner
    settings for `notehook workflows serve`."""

    poll_interval_seconds: int = 2
    max_parallel: int = 2
    retention_days: int = 90


@dataclass
class ClientConfig:
    server_url: str = "http://localhost:8080"
    account: str = ""
    sync_root: Path = field(default_factory=lambda: Path.home() / "Supernote")
    poll_interval_seconds: int = 60
    conflict_policy: str = "keep-both"  # keep-both | newest-wins | local-wins | remote-wins
    equipment_no: str = ""
    config_dir: Path = field(default_factory=default_config_dir)
    workflows: WorkflowsConfig = field(default_factory=WorkflowsConfig)

    def __post_init__(self) -> None:
        if not self.equipment_no:
            self.equipment_no = f"CLI-{secrets.token_hex(6)}"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def token_file(self) -> Path:
        return self.config_dir / "token"

    @property
    def state_db_file(self) -> Path:
        return self.config_dir / "state.db"

    @property
    def events_db_file(self) -> Path:
        return self.config_dir / "events.db"

    @property
    def engine_lock_file(self) -> Path:
        return self.config_dir / "events.db.lock"

    @property
    def workflows_dir(self) -> Path:
        return self.config_dir / "workflows"

    @property
    def workflow_config_dir(self) -> Path:
        return self.config_dir / "workflow-config"

    @property
    def runner_lock_file(self) -> Path:
        """One `notehook workflows serve` runner per config dir at a time
        (spec §6 crash-recovery assumes a single runner) — same `flock`
        pattern as `engine_lock_file`, held via `lock.file_lock` around the
        `Runner`'s lifetime by the caller (the `serve` daemon, Phase 5)."""
        return self.config_dir / "runner.lock"

    @classmethod
    def load(cls, config_dir: Path | None = None) -> "ClientConfig":
        config_dir = config_dir or default_config_dir()
        cfg_file = config_dir / "config.toml"
        if not cfg_file.exists():
            return cls(config_dir=config_dir)
        data = tomllib.loads(cfg_file.read_text())
        workflows_data = data.get("workflows", {})
        if not isinstance(workflows_data, dict):
            workflows_data = {}
        return cls(
            server_url=data.get("server_url", "http://localhost:8080"),
            account=data.get("account", ""),
            sync_root=Path(data.get("sync_root", str(Path.home() / "Supernote"))),
            poll_interval_seconds=int(data.get("poll_interval_seconds", 60)),
            conflict_policy=data.get("conflict_policy", "keep-both"),
            equipment_no=data.get("equipment_no", ""),
            config_dir=config_dir,
            workflows=WorkflowsConfig(
                poll_interval_seconds=int(workflows_data.get("poll_interval_seconds", 2)),
                max_parallel=int(workflows_data.get("max_parallel", 2)),
                retention_days=int(workflows_data.get("retention_days", 90)),
            ),
        )

    def save(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            f'server_url = "{self.server_url}"',
            f'account = "{self.account}"',
            f'sync_root = "{self.sync_root}"',
            f"poll_interval_seconds = {self.poll_interval_seconds}",
            f'conflict_policy = "{self.conflict_policy}"',
            f'equipment_no = "{self.equipment_no}"',
            "",
            "[workflows]",
            f"poll_interval_seconds = {self.workflows.poll_interval_seconds}",
            f"max_parallel = {self.workflows.max_parallel}",
            f"retention_days = {self.workflows.retention_days}",
        ]
        self.config_file.write_text("\n".join(lines) + "\n")

    def save_token(self, token: str) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(token)
        self.token_file.chmod(0o600)

    def load_token(self) -> str | None:
        if self.token_file.exists():
            return self.token_file.read_text().strip() or None
        return None
