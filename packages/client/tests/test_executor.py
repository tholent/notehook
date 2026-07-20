"""Job execution -- spec workflow-spec.md §6 "Execution", §2 "Outcomes".

Decision D6: exercised entirely with plain `python` scripts via
`default_invoke` -- no `uv` involved (see test_harness.py for the harness
generation and the single `@pytest.mark.uv` integration test for the real
`uv run` path).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from notehook_cli.workflows import executor
from notehook_cli.workflows.executor import (
    RunStatus,
    default_invoke,
    execute,
)
from notehook_cli.workflows.harness import PreparedJob


def _make_job(tmp_path: Path, script: str, *, name: str = "job") -> PreparedJob:
    job_dir = tmp_path / name
    job_dir.mkdir()
    script_path = job_dir / "run.py"
    script_path.write_text(script)
    return PreparedJob(
        argv=[sys.executable, str(script_path)],
        env=dict(os.environ),
        cwd=job_dir,
        job_dir=job_dir,
        payload_file=job_dir / "payload.json",
    )


# --- exit-code protocol ---


def test_exit_0_is_success(tmp_path: Path) -> None:
    job = _make_job(tmp_path, "raise SystemExit(0)\n")
    outcome = execute(job, timeout_seconds=5)
    assert outcome.status == RunStatus.SUCCESS
    assert outcome.exit_code == 0


def test_exit_1_is_failed(tmp_path: Path) -> None:
    job = _make_job(tmp_path, "raise SystemExit(1)\n")
    outcome = execute(job, timeout_seconds=5)
    assert outcome.status == RunStatus.FAILED
    assert outcome.exit_code == 1


def test_exit_75_is_retry(tmp_path: Path) -> None:
    job = _make_job(tmp_path, "raise SystemExit(75)\n")
    outcome = execute(job, timeout_seconds=5)
    assert outcome.status == RunStatus.RETRY
    assert outcome.exit_code == 75


def test_other_nonzero_exit_is_failed(tmp_path: Path) -> None:
    job = _make_job(tmp_path, "raise SystemExit(3)\n")
    outcome = execute(job, timeout_seconds=5)
    assert outcome.status == RunStatus.FAILED
    assert outcome.exit_code == 3


def test_uncaught_exception_is_failed(tmp_path: Path) -> None:
    job = _make_job(tmp_path, "raise RuntimeError('boom')\n")
    outcome = execute(job, timeout_seconds=5)
    assert outcome.status == RunStatus.FAILED
    assert outcome.exit_code == 1
    assert "boom" in outcome.stderr


# --- capture ---


def test_stdout_and_stderr_captured(tmp_path: Path) -> None:
    script = "import sys\nprint('hello-out')\nprint('hello-err', file=sys.stderr)\n"
    job = _make_job(tmp_path, script)
    outcome = execute(job, timeout_seconds=5)
    assert "hello-out" in outcome.stdout
    assert "hello-err" in outcome.stderr


def test_output_truncated_at_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "TRUNCATE_BYTES", 100)
    script = "print('x' * 10_000)\n"
    job = _make_job(tmp_path, script)
    outcome = execute(job, timeout_seconds=5)
    assert len(outcome.stdout.encode()) < 10_000
    assert "truncated" in outcome.stdout


def test_started_and_finished_timestamps_ordered(tmp_path: Path) -> None:
    job = _make_job(tmp_path, "raise SystemExit(0)\n")
    outcome = execute(job, timeout_seconds=5)
    assert outcome.started_at <= outcome.finished_at


# --- timeout / signal handling ---


def test_timeout_sends_sigterm_and_retries_when_honored(tmp_path: Path) -> None:
    script = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "time.sleep(10)\n"
    )
    job = _make_job(tmp_path, script)
    outcome = execute(job, timeout_seconds=0.3)
    assert outcome.status == RunStatus.RETRY


def test_timeout_sigkill_when_sigterm_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor, "GRACE_SECONDS", 0.3)
    script = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n"
    )
    job = _make_job(tmp_path, script)
    outcome = execute(job, timeout_seconds=0.3)
    assert outcome.status == RunStatus.RETRY


# --- job dir cleanup ---


def test_job_dir_removed_after_execute(tmp_path: Path) -> None:
    job = _make_job(tmp_path, "raise SystemExit(0)\n")
    assert job.job_dir.is_dir()
    execute(job, timeout_seconds=5)
    assert not job.job_dir.exists()


def test_job_dir_kept_with_debug_flag(tmp_path: Path) -> None:
    job = _make_job(tmp_path, "raise SystemExit(0)\n")
    execute(job, timeout_seconds=5, keep_job_dir=True)
    assert job.job_dir.is_dir()


def test_job_dir_removed_even_on_timeout(tmp_path: Path) -> None:
    script = "import time\ntime.sleep(10)\n"
    job = _make_job(tmp_path, script)
    execute(job, timeout_seconds=0.2)
    assert not job.job_dir.exists()


# --- D6 invoke seam ---


def test_invoke_receives_argv_env_cwd(tmp_path: Path) -> None:
    job = _make_job(tmp_path, "raise SystemExit(0)\n")
    calls: list[tuple[list[str], dict[str, str], Path]] = []

    def recording_invoke(
        argv: list[str], env: dict[str, str], cwd: Path
    ) -> "subprocess.Popen[bytes]":
        calls.append((argv, env, cwd))
        return default_invoke(argv, env, cwd)

    outcome = execute(job, timeout_seconds=5, invoke=recording_invoke)
    assert outcome.status == RunStatus.SUCCESS
    assert len(calls) == 1
    assert calls[0][0] == job.argv
    assert calls[0][2] == job.cwd
