"""`notehook workflows run/backfill/logs/serve` -- spec workflow-spec.md
§6/§8, docs/workflow-implementation-plan.md Phase 5.

Follows `test_workflows_cli.py`'s pattern (typer `CliRunner`, `--config-dir`
at a tmp path) for the CLI verbs, and `test_runner.py`'s pattern (stub
`invoke`, no real `uv`/subprocess workflow execution) for anything that
drives a `Runner`. `serve`'s own inner loop (`_serve_loop`) is exercised
directly -- with a real `threading.Event`-based stop/wake pair the test
controls -- the same way `test_daemon.py` drives `SyncDaemon.run()`/`.stop()`,
since `serve()` itself only ever exits via a real `KeyboardInterrupt`, which
isn't safely injectable from another thread in a test.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from notehook_cli import cli
from notehook_cli.config import ClientConfig, WorkflowsConfig
from notehook_cli.lock import file_lock
from notehook_cli.workflows import cli as workflows_cli
from notehook_cli.workflows.events import EventLog, PendingRun
from notehook_cli.workflows.executor import Invoke
from notehook_cli.workflows.runner import Runner as RealRunner

runner = CliRunner()

_HANDLER_BODY = (
    "from notehook_workflow import workflow\n\n\n@workflow()\ndef run(event, config):\n    pass\n"
)


def _pep723_block(table_text: str) -> str:
    lines = ['# /// script', '# requires-python = ">=3.12"', "# dependencies = []", "#"]
    for line in table_text.splitlines():
        lines.append(f"# {line}" if line else "#")
    lines.append("# ///")
    return "\n".join(lines) + "\n"


def _invoke(config_dir: Path, args: list[str], **kwargs: Any) -> Any:
    # `logs`' table has 11 columns -- rich truncates aggressively at the
    # default 80-column width CliRunner's captured stdout reports, which
    # would hide the very content these tests assert on. A wide COLUMNS env
    # var (rich reads it via shutil.get_terminal_size) avoids that.
    env = {"COLUMNS": "220", **kwargs.pop("env", {})}
    return runner.invoke(
        cli.app, ["workflows", *args, "--config-dir", str(config_dir)], env=env, **kwargs
    )


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _make_config(tmp_path: Path, **overrides: Any) -> ClientConfig:
    config_dir = tmp_path / "cfg"
    sync_root = tmp_path / "sync"
    sync_root.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {
        "config_dir": config_dir,
        "sync_root": sync_root,
        "equipment_no": "CLI-test0001",
    }
    kwargs.update(overrides)
    config = ClientConfig(**kwargs)
    config.save()
    return config


def _write_synced_file(config: ClientConfig, rel_path: str, content: bytes = b"hello") -> Path:
    path = config.sync_root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _install(
    config: ClientConfig,
    alias: str = "demo",
    *,
    workflow_name: str = "demo",
    paths: str = '["**"]',
    on: str | None = None,
    enabled: bool = True,
) -> None:
    """Write one healthy install directly (no `uv`, no real `install`
    command -- matches test_runner.py's fixture style)."""
    manifest_text = _pep723_block(f'[tool.notehook]\nname = "{workflow_name}"\n')
    config.workflows_dir.mkdir(parents=True, exist_ok=True)
    (config.workflows_dir / f"{alias}.py").write_text(manifest_text + "\n" + _HANDLER_BODY)

    config.workflow_config_dir.mkdir(parents=True, exist_ok=True)
    lines = [f'workflow = "{workflow_name}"', f"paths = {paths}"]
    if on is not None:
        lines.append(f"on = {on}")
    if not enabled:
        lines.append("enabled = false")
    (config.workflow_config_dir / f"{alias}.toml").write_text("\n".join(lines) + "\n")


def _stub_invoke(exit_code: int = 0) -> Invoke:
    def invoke(argv: list[str], env: dict[str, str], cwd: Path) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [sys.executable, "-c", f"import sys; sys.exit({exit_code})"],
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    return invoke


def _patch_runner_invoke(monkeypatch: pytest.MonkeyPatch, invoke: Invoke) -> None:
    """`run --wait` and `serve` both construct their `Runner` via the
    module-level `Runner` name in `workflows/cli.py`; patching that name lets
    tests inject a stub `invoke` (D6) without touching real `uv`/subprocess
    workflow execution."""

    def factory(
        event_log: EventLog,
        workflows_dir: Path,
        workflow_config_dir: Path,
        sync_root: Path,
        own_equipment_no: str,
        max_parallel: int = 2,
    ) -> RealRunner:
        return RealRunner(
            event_log,
            workflows_dir,
            workflow_config_dir,
            sync_root,
            own_equipment_no,
            max_parallel=max_parallel,
            invoke=invoke,
        )

    monkeypatch.setattr(workflows_cli, "Runner", factory)


# =====================================================================
# run
# =====================================================================


def test_run_without_wait_only_appends_event(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    _install(config, "demo", paths='["Note/**"]')
    file_path = _write_synced_file(config, "Note/a.pdf")

    result = _invoke(config.config_dir, ["run", "demo", "--path", str(file_path)])
    assert result.exit_code == 0, result.output
    assert "Queued" in result.output

    event_log = EventLog(config.events_db_file)
    events = event_log.all_events()
    assert len(events) == 1
    assert events[0].rel_path == "Note/a.pdf"
    assert events[0].type == "created"
    assert events[0].source == "manual"
    assert events[0].target_install == "demo"
    assert events[0].settled is True

    # Fire-and-forget: nothing executes without --wait.
    assert event_log.all_runs() == []


def test_run_wait_executes_synchronously_and_reports_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _make_config(tmp_path)
    _install(config, "demo", paths='["Note/**"]')
    file_path = _write_synced_file(config, "Note/a.pdf")
    _patch_runner_invoke(monkeypatch, _stub_invoke(0))

    result = _invoke(config.config_dir, ["run", "demo", "--path", str(file_path), "--wait"])
    assert result.exit_code == 0, result.output
    assert "success" in result.output

    event_log = EventLog(config.events_db_file)
    runs = event_log.all_runs()
    assert len(runs) == 1
    assert runs[0].status == "success"
    assert runs[0].exit_code == 0


def test_run_wait_executes_synchronously_and_reports_failure_via_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _make_config(tmp_path)
    _install(config, "demo", paths='["Note/**"]')
    file_path = _write_synced_file(config, "Note/a.pdf")
    _patch_runner_invoke(monkeypatch, _stub_invoke(3))

    result = _invoke(config.config_dir, ["run", "demo", "--path", str(file_path), "--wait"])
    assert result.exit_code == 1, result.output
    assert "failed" in result.output

    event_log = EventLog(config.events_db_file)
    runs = event_log.all_runs()
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].exit_code == 3


def test_run_wait_fails_clearly_on_lock_contention_instead_of_racing(tmp_path: Path) -> None:
    """The highest-risk part of Phase 5: `EventLog.claim_next` is only safe
    within one process (events.py module docstring), so `run --wait` must
    acquire `runner_lock_file` itself before driving a local `Runner` pass.
    Held here directly (same pattern as test_lock.py / test_runner.py's own
    lock tests) to prove the *failure* path -- not just that the happy path
    works -- since a silent race here would corrupt run claiming."""
    config = _make_config(tmp_path)
    _install(config, "demo", paths='["Note/**"]')
    file_path = _write_synced_file(config, "Note/a.pdf")

    with file_lock(config.runner_lock_file):
        result = _invoke(config.config_dir, ["run", "demo", "--path", str(file_path), "--wait"])

    assert result.exit_code != 0, result.output
    assert "already running" in result.output.lower() or "lock" in result.output.lower()

    # The manual event was still queued (append happens before the lock is
    # ever touched) but nothing executed -- the failure happened before
    # intake/claim, not mid-way through it.
    event_log = EventLog(config.events_db_file)
    assert len(event_log.all_events()) == 1
    assert event_log.all_runs() == []


# =====================================================================
# backfill
# =====================================================================


def test_backfill_queues_matching_paths_glob_narrowed_and_never_executes(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    _install(config, "demo", paths='["Note/**"]')
    _write_synced_file(config, "Note/a.pdf")
    _write_synced_file(config, "Note/b.pdf")
    _write_synced_file(config, "Other/c.pdf")  # outside the install's own paths -- never queued

    result = _invoke(config.config_dir, ["backfill", "demo"])
    assert result.exit_code == 0, result.output
    assert "2" in result.output

    event_log = EventLog(config.events_db_file)
    events = event_log.all_events()
    assert {e.rel_path for e in events} == {"Note/a.pdf", "Note/b.pdf"}
    assert all(e.source == "backfill" for e in events)
    assert all(e.type == "created" for e in events)
    assert all(e.target_install == "demo" for e in events)
    # Append-only: backfill never drives execution itself.
    assert event_log.all_runs() == []

    narrowed = _invoke(config.config_dir, ["backfill", "demo", "--glob", "**/a.pdf"])
    assert narrowed.exit_code == 0, narrowed.output
    assert "1" in narrowed.output

    backfill_paths = [e.rel_path for e in event_log.all_events() if e.source == "backfill"]
    assert backfill_paths == ["Note/a.pdf", "Note/b.pdf", "Note/a.pdf"]
    assert event_log.all_runs() == []


def test_backfill_unknown_alias_fails_clearly(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    result = _invoke(config.config_dir, ["backfill", "ghost"])
    assert result.exit_code != 0
    assert "ghost" in result.output


# =====================================================================
# logs
# =====================================================================


def test_logs_filters_by_alias_failed_and_limit(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    event_log = EventLog(config.events_db_file)

    ev_a = event_log.append_settled(
        "created", "a.txt", "h", 1, "manual", "", "p1", target_install="alpha"
    )
    event_log.intake([PendingRun("alpha", "alpha-wf", None, ev_a, "a.txt")], ev_a)
    claimed_a = event_log.claim_next(now_ms=1)
    assert claimed_a is not None
    event_log.finalize_success(claimed_a.id, 0, "out", "", finished_at_ms=2)

    ev_b = event_log.append_settled(
        "created", "b.txt", "h", 1, "manual", "", "p2", target_install="beta"
    )
    event_log.intake([PendingRun("beta", "beta-wf", None, ev_b, "b.txt")], ev_b)
    claimed_b = event_log.claim_next(now_ms=3)
    assert claimed_b is not None
    event_log.finalize_failed(claimed_b.id, 1, "", "boom", finished_at_ms=4)

    all_result = _invoke(config.config_dir, ["logs"])
    assert all_result.exit_code == 0, all_result.output
    assert "alpha" in all_result.output
    assert "beta" in all_result.output
    assert "success" in all_result.output
    assert "failed" in all_result.output

    alpha_only = _invoke(config.config_dir, ["logs", "--alias", "alpha"])
    assert alpha_only.exit_code == 0, alpha_only.output
    assert "alpha" in alpha_only.output
    assert "beta" not in alpha_only.output

    failed_only = _invoke(config.config_dir, ["logs", "--failed"])
    assert failed_only.exit_code == 0, failed_only.output
    assert "beta" in failed_only.output
    assert "alpha" not in failed_only.output

    limited = _invoke(config.config_dir, ["logs", "-n", "1"])
    assert limited.exit_code == 0, limited.output
    assert "beta" in limited.output  # most recent
    assert "alpha" not in limited.output


def test_logs_empty_is_a_clean_no_op(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    result = _invoke(config.config_dir, ["logs"])
    assert result.exit_code == 0, result.output


# =====================================================================
# workflows config round-trip
# =====================================================================


def test_workflows_config_defaults_when_section_absent(tmp_path: Path) -> None:
    """A config.toml written before Phase 5 (no `[workflows]` section at
    all) must still load, with defaults filled in and every existing
    top-level key unaffected."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        'server_url = "http://localhost:8080"\n'
        'account = "me@example.com"\n'
        f'sync_root = "{tmp_path / "sync"}"\n'
        "poll_interval_seconds = 60\n"
        'conflict_policy = "keep-both"\n'
        'equipment_no = "CLI-abc123"\n'
    )

    config = ClientConfig.load(config_dir)
    assert config.workflows == WorkflowsConfig()
    assert config.account == "me@example.com"
    assert config.equipment_no == "CLI-abc123"
    assert config.poll_interval_seconds == 60


def test_workflows_config_round_trips_with_existing_keys_unaffected(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config = ClientConfig(
        config_dir=config_dir,
        server_url="http://example.com:9999",
        account="me@example.com",
        sync_root=tmp_path / "sync",
        poll_interval_seconds=42,
        conflict_policy="newest-wins",
        equipment_no="CLI-fixed",
        workflows=WorkflowsConfig(poll_interval_seconds=5, max_parallel=4, retention_days=30),
    )
    config.save()

    reloaded = ClientConfig.load(config_dir)
    assert reloaded.server_url == "http://example.com:9999"
    assert reloaded.account == "me@example.com"
    assert reloaded.sync_root == tmp_path / "sync"
    assert reloaded.poll_interval_seconds == 42
    assert reloaded.conflict_policy == "newest-wins"
    assert reloaded.equipment_no == "CLI-fixed"
    assert reloaded.workflows == WorkflowsConfig(
        poll_interval_seconds=5, max_parallel=4, retention_days=30
    )


def test_workflows_config_defaults_when_malformed_section(tmp_path: Path) -> None:
    """A non-table `[workflows]` value (or wrong TOML shape) degrades to
    defaults rather than raising -- lenient parsing of local config mirrors
    the repo-wide "lenient in, strict out" convention."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('workflows = "not-a-table"\n')

    config = ClientConfig.load(config_dir)
    assert config.workflows == WorkflowsConfig()


# =====================================================================
# serve
# =====================================================================


def test_serve_loop_reaches_terminal_status_and_blocks_concurrent_run_wait(
    tmp_path: Path,
) -> None:
    """Drives `_serve_loop` (serve's actual inner engine, extracted
    specifically for testability -- see its docstring) in a background
    thread with a real `runner_lock_file` held around it, exactly as
    `serve()`'s own body does. Proves: (1) a queued event reaches a terminal
    run status within a bounded wait, (2) while the loop's thread holds the
    lock, a concurrent `run --wait` fails clearly instead of racing it, and
    (3) the thread stops cleanly via `stop`/`wake`, mirroring
    `SyncDaemon.run()`/`.stop()` in test_daemon.py.
    """
    config = _make_config(tmp_path, workflows=WorkflowsConfig(poll_interval_seconds=1))
    _install(config, "demo", paths='["Note/**"]')
    file_path = _write_synced_file(config, "Note/a.pdf")

    event_log = EventLog(config.events_db_file)
    event_log.append_settled("created", "Note/a.pdf", "h", 1, "sync-upload", "", "pass-1")

    stop = threading.Event()
    wake = threading.Event()

    def serve_body() -> None:
        with file_lock(config.runner_lock_file):
            serve_runner = RealRunner(
                event_log,
                config.workflows_dir,
                config.workflow_config_dir,
                config.sync_root,
                config.equipment_no,
                max_parallel=1,
                invoke=_stub_invoke(0),
            )
            workflows_cli._serve_loop(serve_runner, config.workflows, stop, wake)

    thread = threading.Thread(target=serve_body, daemon=True)
    thread.start()
    try:
        assert _wait_for(
            lambda: any(r.status in {"success", "failed"} for r in event_log.all_runs())
        ), event_log.all_runs()
        runs = event_log.all_runs()
        assert len(runs) == 1
        assert runs[0].status == "success"

        # Lock contention: the loop's thread is still holding
        # runner_lock_file (the `with file_lock(...)` block hasn't exited
        # yet), so a concurrent `run --wait` must fail clearly.
        result = _invoke(
            config.config_dir, ["run", "demo", "--path", str(file_path), "--wait"]
        )
        assert result.exit_code != 0, result.output
        assert "already running" in result.output.lower() or "lock" in result.output.lower()
    finally:
        stop.set()
        wake.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_serve_command_fails_clearly_when_lock_already_held(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    with file_lock(config.runner_lock_file):
        result = _invoke(config.config_dir, ["serve"])

    assert result.exit_code != 0, result.output
    assert "already running" in result.output.lower() or "lock" in result.output.lower()


def test_serve_command_recovers_crashed_runs_then_stops_when_loop_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercises `serve()`'s actual top-level wiring (config load, lock,
    `Runner` construction, `recover_crashed()`, watcher thread, console
    output) with `_serve_loop` itself patched to return immediately -- the
    inner loop's own behavior is covered separately above; this test is
    about `serve()`'s startup/shutdown sequencing around it."""
    config = _make_config(tmp_path)
    _install(config, "demo", paths='["Note/**"]')
    _write_synced_file(config, "Note/a.pdf")

    event_log = EventLog(config.events_db_file)
    ev_id = event_log.append_settled("created", "Note/a.pdf", "h", 1, "sync-upload", "", "p1")
    event_log.intake([PendingRun("demo", "demo", None, ev_id, "Note/a.pdf")], ev_id)
    claimed = event_log.claim_next(now_ms=1)
    assert claimed is not None  # now 'running' -- simulates a crashed previous process

    def immediate_stop(
        loop_runner: RealRunner,
        workflows_cfg: WorkflowsConfig,
        stop: threading.Event,
        wake: threading.Event,
    ) -> None:
        stop.set()

    monkeypatch.setattr(workflows_cli, "_serve_loop", immediate_stop)

    result = _invoke(config.config_dir, ["serve"])
    assert result.exit_code == 0, result.output
    assert "Serving workflows" in result.output
    assert "Recovered 1 crashed run" in result.output
