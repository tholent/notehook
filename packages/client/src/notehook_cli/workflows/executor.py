"""Job execution: spawn, timeout, capture, exit-code protocol — spec §6
"Execution" and §2 "Outcomes".

Decision D6 (docs/workflow-implementation-plan.md): `execute()` takes an
injectable `invoke` callable so unit tests can run plain `python` scripts
directly, with no `uv` involved — exactly one integration test (marked
`@pytest.mark.uv`) exercises the real harness through real `uv run`.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from .harness import PreparedJob

__all__ = [
    "Invoke",
    "RunOutcome",
    "RunStatus",
    "default_invoke",
    "execute",
]

# Timeout → SIGTERM → this much grace → SIGKILL (spec §6). A module constant
# so tests can monkeypatch it small instead of waiting out a real 10s grace.
GRACE_SECONDS = 10.0

# stdout/stderr are each capped at 256 KiB (spec §1 `run` table, §6).
TRUNCATE_BYTES = 256 * 1024

_TRUNCATION_NOTICE = "\n... [truncated: output exceeded 256 KiB]"

# Exit-code protocol (spec §2 "Outcomes"): 0 success, 75 (EX_TEMPFAIL) retry,
# any other exit (or a timeout) is failed/retry per the table below.
_EXIT_SUCCESS = 0
_EXIT_RETRY = 75

Invoke = Callable[[list[str], dict[str, str], Path], "subprocess.Popen[bytes]"]


class RunStatus(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    RETRY = "retry"


@dataclass(frozen=True)
class RunOutcome:
    status: RunStatus
    exit_code: int | None
    stdout: str
    stderr: str
    started_at: datetime
    finished_at: datetime


def default_invoke(argv: list[str], env: dict[str, str], cwd: Path) -> subprocess.Popen[bytes]:
    """Production invoker: spawn `argv` with captured stdout/stderr pipes."""
    return subprocess.Popen(  # noqa: S603 - argv is built by harness.prepare_job, not user input
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def execute(
    prepared_job: PreparedJob,
    timeout_seconds: float,
    *,
    invoke: Invoke = default_invoke,
    keep_job_dir: bool = False,
) -> RunOutcome:
    """Run one prepared job to completion and classify the outcome.

    Waits up to `timeout_seconds`; on expiry sends SIGTERM, waits
    `GRACE_SECONDS` more, then SIGKILL — either way the run is classified
    `RETRY` (a hung push should retry, not hard-fail, spec §6). Captures
    stdout/stderr, each truncated at `TRUNCATE_BYTES`. Cleans up the job dir
    unless `keep_job_dir=True` (debugging).
    """
    started_at = datetime.now(UTC)
    process = invoke(prepared_job.argv, prepared_job.env, prepared_job.cwd)
    try:
        stdout_bytes, stderr_bytes, exit_code, timed_out = _wait(process, timeout_seconds)
    finally:
        if not keep_job_dir:
            shutil.rmtree(prepared_job.job_dir, ignore_errors=True)
    finished_at = datetime.now(UTC)

    return RunOutcome(
        status=_classify(exit_code, timed_out=timed_out),
        exit_code=exit_code,
        stdout=_truncate(stdout_bytes),
        stderr=_truncate(stderr_bytes),
        started_at=started_at,
        finished_at=finished_at,
    )


def _wait(
    process: subprocess.Popen[bytes], timeout_seconds: float
) -> tuple[bytes, bytes, int | None, bool]:
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return stdout, stderr, process.returncode, False
    except subprocess.TimeoutExpired:
        pass

    process.terminate()  # SIGTERM
    try:
        stdout, stderr = process.communicate(timeout=GRACE_SECONDS)
        return stdout, stderr, process.returncode, True
    except subprocess.TimeoutExpired:
        pass

    process.kill()  # SIGKILL
    stdout, stderr = process.communicate()
    return stdout, stderr, process.returncode, True


def _classify(exit_code: int | None, *, timed_out: bool) -> RunStatus:
    if timed_out:
        return RunStatus.RETRY
    if exit_code == _EXIT_SUCCESS:
        return RunStatus.SUCCESS
    if exit_code == _EXIT_RETRY:
        return RunStatus.RETRY
    return RunStatus.FAILED


def _truncate(data: bytes) -> str:
    if len(data) <= TRUNCATE_BYTES:
        return data.decode("utf-8", errors="replace")
    truncated = data[:TRUNCATE_BYTES].decode("utf-8", errors="ignore")
    return truncated + _TRUNCATION_NOTICE
