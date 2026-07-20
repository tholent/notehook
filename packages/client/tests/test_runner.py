"""Runner core -- spec workflow-spec.md §6 "Runner lifecycle".

Everything here is driven through stub `invoke` callables (decision D6): no
real `uv`, no bare sleeps. Retry/crash-recovery timing uses an injectable
clock rather than real wall time. `EventLog`'s new consumer-side methods
(cursor, `intake`, `claim_next`, `running_runs`, `finalize_*`, `sweep`) are
exercised both directly and through `Runner`'s step methods.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import notehook_cli
from notehook_cli.lock import LockError, file_lock
from notehook_cli.workflows import events as events_module
from notehook_cli.workflows.events import EventLog, PendingRun
from notehook_cli.workflows.executor import Invoke, RunStatus
from notehook_cli.workflows.runner import Runner

SDK_DIR = Path(notehook_cli.__file__).parent / "workflows" / "_sdk"

DAY_MS = 86_400_000


@pytest.fixture
def sdk() -> Iterator[ModuleType]:
    """Import the real `notehook_workflow` SDK exactly as a workflow venv
    would (see test_workflow_sdk.py) -- used by the payload-shape contract
    test to round-trip a runner-built payload through the real `Event`."""
    if str(SDK_DIR) not in sys.path:
        sys.path.insert(0, str(SDK_DIR))
    module = importlib.import_module("notehook_workflow")
    module._REGISTRY.clear()
    yield module
    module._REGISTRY.clear()


# --- fixtures / helpers ---


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "workflows", tmp_path / "workflow-config"


def _pep723_block(table_text: str) -> str:
    lines = ['# /// script', '# requires-python = ">=3.11"', "# dependencies = []", "#"]
    for line in table_text.splitlines():
        lines.append(f"# {line}" if line else "#")
    lines.append("# ///")
    return "\n".join(lines) + "\n"


_HANDLER_BODY = (
    "from notehook_workflow import workflow\n\n\n@workflow()\ndef run(event, config):\n    pass\n"
)

_BASE_MANIFEST = _pep723_block('[tool.notehook]\nname = "demo"\n')


def _write_workflow(workflows_dir: Path, alias: str, manifest_text: str = _BASE_MANIFEST) -> None:
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / f"{alias}.py").write_text(manifest_text + "\n" + _HANDLER_BODY)


def _write_config(config_dir: Path, alias: str, config_toml: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / f"{alias}.toml").write_text(config_toml)


def _install(
    tmp_path: Path,
    alias: str = "demo",
    *,
    paths: str = '["**"]',
    on: str | None = None,
    skip_own_changes: bool = False,
    enabled: bool = True,
    workflow_name: str | None = None,
    manifest_text: str = _BASE_MANIFEST,
) -> tuple[Path, Path]:
    """Write one healthy install (config + workflow code) and return
    (workflows_dir, config_dir)."""
    workflows_dir, config_dir = _dirs(tmp_path)
    _write_workflow(workflows_dir, alias, manifest_text)
    lines = [f'workflow = "{workflow_name or "demo"}"', f"paths = {paths}"]
    if on is not None:
        lines.append(f"on = {on}")
    if skip_own_changes:
        lines.append("skip_own_changes = true")
    if not enabled:
        lines.append("enabled = false")
    _write_config(config_dir, alias, "\n".join(lines) + "\n")
    return workflows_dir, config_dir


def _stub_invoke(exit_code: int = 0, capture: list[dict[str, Any]] | None = None) -> Invoke:
    """D6 seam: no `uv`, no real workflow execution. Optionally captures the
    real payload file `harness.prepare_job` wrote (read synchronously, in the
    calling worker thread, before the trivial subprocess spawns) so tests can
    assert on the exact JSON a real job would receive."""

    def invoke(
        argv: list[str], env: dict[str, str], cwd: Path
    ) -> subprocess.Popen[bytes]:
        if capture is not None:
            capture.append(json.loads(Path(env["NOTEHOOK_PAYLOAD_FILE"]).read_text()))
        return subprocess.Popen(
            [sys.executable, "-c", f"import sys; sys.exit({exit_code})"],
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    return invoke


class FakeClock:
    """Injectable clock (epoch ms) for deterministic retry/crash-recovery math."""

    def __init__(self, start_ms: int = 0) -> None:
        self._t = start_ms

    def __call__(self) -> int:
        return self._t

    def advance(self, delta_ms: int) -> None:
        self._t += delta_ms


def _runner(
    tmp_path: Path,
    workflows_dir: Path,
    config_dir: Path,
    *,
    own_equipment_no: str = "CLI-me",
    max_parallel: int = 2,
    invoke: Invoke | None = None,
    clock: FakeClock | None = None,
) -> tuple[Runner, EventLog]:
    log = EventLog(tmp_path / "events.db")
    runner = Runner(
        log,
        workflows_dir,
        config_dir,
        tmp_path / "sync",
        own_equipment_no,
        max_parallel=max_parallel,
        invoke=invoke or _stub_invoke(),
        clock=clock or FakeClock(),
    )
    return runner, log


# =====================================================================
# Fan-out / intake
# =====================================================================


def test_glob_match_required_for_fan_out(tmp_path: Path) -> None:
    workflows_dir, config_dir = _install(tmp_path, paths='["Note/**"]')
    runner, log = _runner(tmp_path, workflows_dir, config_dir)
    log.append_settled("created", "Other/x.txt", "h", 1, "sync-upload", "", "p1")

    queued = runner.intake_step()
    assert queued == 0
    assert log.all_runs() == []


def test_glob_match_queues_run(tmp_path: Path) -> None:
    workflows_dir, config_dir = _install(tmp_path, paths='["Note/**"]')
    runner, log = _runner(tmp_path, workflows_dir, config_dir)
    log.append_settled("created", "Note/a.pdf", "h", 1, "sync-upload", "", "p1")

    queued = runner.intake_step()
    assert queued == 1
    run = log.all_runs()[0]
    assert run.install == "demo"
    assert run.rel_path == "Note/a.pdf"
    assert run.status == "queued"
    assert run.attempt == 1
    assert run.workflow_name == "demo"


def test_on_set_narrows_to_matching_types(tmp_path: Path) -> None:
    workflows_dir, config_dir = _install(tmp_path, on='["deleted"]')
    runner, log = _runner(tmp_path, workflows_dir, config_dir)
    log.append_settled("created", "a.txt", "h", 1, "sync-upload", "", "p1")
    log.append_settled("deleted", "b.txt", "", 0, "sync-upload", "", "p1")

    queued = runner.intake_step()
    assert queued == 1
    assert [r.rel_path for r in log.all_runs()] == ["b.txt"]


def test_on_unset_queues_regardless_of_type(tmp_path: Path) -> None:
    """Design decision (see runner.py module docstring): the decorator's
    real `on` set only exists inside the workflow's source, importable only
    in the spawned subprocess -- never in the runner's own process. With no
    `on` narrowing in the install config, the runner cannot know that set
    without executing untrusted code, so it queues on a glob/target match
    alone for every event type and trusts the SDK's per-handler dispatch to
    no-op harmlessly inside the subprocess if nothing matches. A wasted spawn
    beats a configured install that silently never runs."""
    workflows_dir, config_dir = _install(tmp_path, on=None)
    runner, log = _runner(tmp_path, workflows_dir, config_dir)
    log.append_settled("created", "a.txt", "h", 1, "sync-upload", "", "p1")
    log.append_settled("updated", "b.txt", "h", 1, "sync-upload", "", "p1")
    log.append_settled("deleted", "c.txt", "", 0, "sync-upload", "", "p1")

    queued = runner.intake_step()
    assert queued == 3


def test_target_install_manual_bypasses_glob_and_on(tmp_path: Path) -> None:
    workflows_dir, config_dir = _install(tmp_path, paths='["Note/**"]', on='["deleted"]')
    runner, log = _runner(tmp_path, workflows_dir, config_dir)
    # Neither the glob ("Outside/") nor the type ("created", not "deleted")
    # would pass for fan-out -- but target_install + source="manual" bypasses
    # both entirely (spec §6).
    log.append_settled(
        "created", "Outside/x.txt", "h", 1, "manual", "", "p1", target_install="demo"
    )

    queued = runner.intake_step()
    assert queued == 1
    run = log.all_runs()[0]
    assert run.install == "demo"
    assert run.rel_path == "Outside/x.txt"


def test_target_install_backfill_respects_type_filter(tmp_path: Path) -> None:
    workflows_dir, config_dir = _install(tmp_path, paths='["Note/**"]', on='["updated"]')
    runner, log = _runner(tmp_path, workflows_dir, config_dir)
    # backfill events carry type "created" (spec §6: "simulates first sight")
    # -- an install narrowed to on=["updated"] must still ignore it, even
    # though it's targeted directly at this alias.
    log.append_settled(
        "created", "Note/a.pdf", "h", 1, "backfill", "", "p1", target_install="demo"
    )

    queued = runner.intake_step()
    assert queued == 0
    assert log.all_runs() == []


def test_target_install_only_considers_that_alias(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)
    _write_workflow(workflows_dir, "a")
    _write_config(config_dir, "a", 'workflow = "demo"\npaths = ["**"]\n')
    _write_workflow(workflows_dir, "b", _pep723_block('[tool.notehook]\nname = "other"\n'))
    _write_config(config_dir, "b", 'workflow = "other"\npaths = ["**"]\n')
    runner, log = _runner(tmp_path, workflows_dir, config_dir)

    log.append_settled(
        "created", "x.txt", "h", 1, "manual", "", "p1", target_install="a"
    )
    queued = runner.intake_step()
    assert queued == 1
    assert log.all_runs()[0].install == "a"


def test_disabled_install_never_queues(tmp_path: Path) -> None:
    workflows_dir, config_dir = _install(tmp_path, enabled=False)
    runner, log = _runner(tmp_path, workflows_dir, config_dir)
    log.append_settled("created", "a.txt", "h", 1, "sync-upload", "", "p1")

    assert runner.intake_step() == 0
    assert log.all_runs() == []


def test_broken_install_never_queues(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)
    _write_workflow(workflows_dir, "demo")
    _write_config(config_dir, "demo", 'workflow = "wrong-name"\npaths = ["**"]\n')  # name mismatch
    runner, log = _runner(tmp_path, workflows_dir, config_dir)
    log.append_settled("created", "a.txt", "h", 1, "sync-upload", "", "p1")

    assert runner.intake_step() == 0
    assert log.all_runs() == []


def test_skip_own_changes_drops_matching_origin(tmp_path: Path) -> None:
    workflows_dir, config_dir = _install(tmp_path, skip_own_changes=True)
    runner, log = _runner(tmp_path, workflows_dir, config_dir, own_equipment_no="CLI-me")
    log.append_settled("created", "own.txt", "h", 1, "sync-upload", "CLI-me", "p1")
    log.append_settled("created", "other.txt", "h", 1, "sync-download", "SN-device", "p1")

    queued = runner.intake_step()
    assert queued == 1
    assert [r.rel_path for r in log.all_runs()] == ["other.txt"]


def test_cursor_advances_even_with_zero_matches(tmp_path: Path) -> None:
    workflows_dir, config_dir = _install(tmp_path, paths='["Note/**"]')
    runner, log = _runner(tmp_path, workflows_dir, config_dir)
    ev_id = log.append_settled("created", "Nowhere/x.txt", "h", 1, "sync-upload", "", "p1")

    assert log.read_cursor() == 0
    queued = runner.intake_step()
    assert queued == 0
    assert log.read_cursor() == ev_id


def test_intake_step_is_noop_when_nothing_settled(tmp_path: Path) -> None:
    workflows_dir, config_dir = _install(tmp_path)
    runner, log = _runner(tmp_path, workflows_dir, config_dir)
    assert runner.intake_step() == 0
    assert log.read_cursor() == 0


def test_unconsumed_settled_stops_at_first_unsettled_row(tmp_path: Path) -> None:
    """A concurrent writer (e.g. a daemon pass mid-flight) can leave a lower
    id unsettled while a higher id from a different writer (e.g. backfill)
    is already settled -- the cursor must not jump past the gap (see
    `EventLog.unconsumed_settled`'s docstring)."""
    log = EventLog(tmp_path / "events.db")
    unsettled_id = log.append(
        "created", "slow.txt", "h", 1, "sync-upload", "", "pass-slow", settled=False
    )
    log.append_settled("created", "fast.txt", "h", 1, "backfill", "", "pass-fast")

    assert log.unconsumed_settled() == []

    log.settle_pass("pass-slow")
    settled = log.unconsumed_settled()
    assert [e.id for e in settled] == [unsettled_id, unsettled_id + 1]


# =====================================================================
# Coalescing
# =====================================================================


def test_coalescing_supersedes_older_pending_run(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.db")
    ev1 = log.append_settled("created", "f.txt", "h1", 1, "sync-upload", "", "p1")
    log.intake([PendingRun("x4", "wf", None, ev1, "f.txt")], ev1)
    ev2 = log.append_settled("updated", "f.txt", "h2", 2, "sync-upload", "", "p2")
    log.intake([PendingRun("x4", "wf", None, ev2, "f.txt")], ev2)

    runs = {r.event_id: r for r in log.all_runs()}
    assert runs[ev1].status == "superseded"
    assert runs[ev1].finished_at is not None
    assert runs[ev2].status == "queued"


def test_coalescing_never_touches_a_running_run(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.db")
    ev1 = log.append_settled("created", "f.txt", "h1", 1, "sync-upload", "", "p1")
    log.intake([PendingRun("x4", "wf", None, ev1, "f.txt")], ev1)
    claimed = log.claim_next(1_000)
    assert claimed is not None

    ev2 = log.append_settled("updated", "f.txt", "h2", 2, "sync-upload", "", "p2")
    log.intake([PendingRun("x4", "wf", None, ev2, "f.txt")], ev2)

    runs = {r.event_id: r for r in log.all_runs()}
    assert runs[ev1].status == "running"  # never superseded mid-flight
    assert runs[ev2].status == "queued"  # queues behind it


def test_coalescing_scoped_to_install_and_rel_path(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.db")
    e1 = log.append_settled("created", "a.txt", "h", 1, "sync-upload", "", "p1")
    e2 = log.append_settled("created", "b.txt", "h", 1, "sync-upload", "", "p1")
    e3 = log.append_settled("created", "a.txt", "h", 1, "sync-upload", "", "p1")
    log.intake(
        [
            PendingRun("x4", "wf", None, e1, "a.txt"),
            PendingRun("x4", "wf", None, e2, "b.txt"),
            PendingRun("other-install", "wf", None, e3, "a.txt"),
        ],
        e3,
    )
    runs = log.all_runs()
    assert len(runs) == 3
    assert {r.status for r in runs} == {"queued"}


# =====================================================================
# Claiming / concurrency
# =====================================================================


def test_claim_next_never_double_claims_same_pair_under_concurrency(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.db")
    ev1 = log.append_settled("created", "shared.txt", "h1", 1, "sync-upload", "", "p1")
    log.intake([PendingRun("x4", "wf", None, ev1, "shared.txt")], ev1)
    claimed_a = log.claim_next(1_000)
    assert claimed_a is not None  # A is now 'running'

    ev2 = log.append_settled("updated", "shared.txt", "h2", 2, "sync-upload", "", "p2")
    log.intake([PendingRun("x4", "wf", None, ev2, "shared.txt")], ev2)
    run_b = [r for r in log.all_runs() if r.event_id == ev2][0]
    assert run_b.status == "queued"  # queued behind running A

    # Hammer claim_next concurrently while A is (still, deliberately) running:
    # B must never be handed out.
    results: list[Any] = []
    results_lock = threading.Lock()

    def hammer() -> None:
        for _ in range(20):
            r = log.claim_next(1_000)
            with results_lock:
                results.append(r)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r is None for r in results)

    # Now finish A and hammer again: exactly one thread should win B.
    log.finalize_success(claimed_a.id, 0, "", "", 2_000)
    results2: list[Any] = []

    def hammer_once() -> None:
        r = log.claim_next(3_000)
        with results_lock:
            results2.append(r)

    threads2 = [threading.Thread(target=hammer_once) for _ in range(8)]
    for t in threads2:
        t.start()
    for t in threads2:
        t.join()
    winners = [r for r in results2 if r is not None]
    assert len(winners) == 1
    assert winners[0].id == run_b.id


def test_max_parallel_respected_and_achieved(tmp_path: Path) -> None:
    """4 independent (different rel_path) runs, max_parallel=2: a
    `threading.Barrier(2)` inside the stub invoke deterministically proves
    exactly 2 workers execute concurrently (a 3rd concurrent arrival would
    over-satisfy the barrier's `parties`, and fewer than 2 would time out) --
    no timing-based flakiness."""
    workflows_dir, config_dir = _install(tmp_path)
    barrier = threading.Barrier(2, timeout=5)

    def invoke(argv: list[str], env: dict[str, str], cwd: Path) -> subprocess.Popen[bytes]:
        barrier.wait()
        return subprocess.Popen(
            [sys.executable, "-c", "pass"],
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    runner, log = _runner(tmp_path, workflows_dir, config_dir, max_parallel=2, invoke=invoke)
    for i in range(4):
        log.append_settled("created", f"f{i}.txt", "h", 1, "sync-upload", "", "p1")
    runner.intake_step()

    results = runner.run_pending()
    assert len(results) == 4
    assert all(r.status == RunStatus.SUCCESS for r in results)


# =====================================================================
# Exit-code -> outcome
# =====================================================================


def test_success_exit_code_finalizes_success_no_further_run_rows(tmp_path: Path) -> None:
    workflows_dir, config_dir = _install(tmp_path)
    runner, log = _runner(tmp_path, workflows_dir, config_dir, invoke=_stub_invoke(0))
    log.append_settled("created", "f.txt", "h", 1, "sync-upload", "", "p1")
    runner.intake_step()

    results = runner.run_pending()
    assert len(results) == 1
    assert results[0].status == RunStatus.SUCCESS
    assert results[0].exit_code == 0

    runs = log.all_runs()
    assert len(runs) == 1
    assert runs[0].status == "success"


def test_other_nonzero_exit_finalizes_failed(tmp_path: Path) -> None:
    workflows_dir, config_dir = _install(tmp_path)
    runner, log = _runner(tmp_path, workflows_dir, config_dir, invoke=_stub_invoke(3))
    log.append_settled("created", "f.txt", "h", 1, "sync-upload", "", "p1")
    runner.intake_step()

    results = runner.run_pending()
    assert results[0].status == RunStatus.FAILED
    assert results[0].exit_code == 3
    assert log.all_runs()[0].status == "failed"


def test_retry_exit_code_computes_next_attempt_at_with_injected_clock(tmp_path: Path) -> None:
    workflows_dir, config_dir = _install(tmp_path)  # default retry: base=60, cap=3600, max=20
    clock = FakeClock(1_000_000)
    runner, log = _runner(
        tmp_path, workflows_dir, config_dir, invoke=_stub_invoke(75), clock=clock
    )
    log.append_settled("created", "f.txt", "h", 1, "sync-upload", "", "p1")
    runner.intake_step()

    results = runner.run_pending()
    assert results[0].status == RunStatus.RETRY
    assert results[0].exit_code == 75

    run = log.all_runs()[0]
    assert run.status == "retry"
    assert run.attempt == 2
    # attempt=1 just ran: delay = min(60 * 4**0, 3600) = 60s = 60_000ms
    assert run.next_attempt_at == 1_000_000 + 60_000


def test_exhausted_attempts_finalizes_failed_not_retry(tmp_path: Path) -> None:
    manifest = _pep723_block(
        '[tool.notehook]\nname = "demo"\n\n'
        "[tool.notehook.retry]\nmax_attempts = 2\nbackoff_base = 1\nbackoff_cap = 10\n"
    )
    workflows_dir, config_dir = _install(tmp_path, manifest_text=manifest)
    clock = FakeClock(0)
    runner, log = _runner(
        tmp_path, workflows_dir, config_dir, invoke=_stub_invoke(75), clock=clock
    )
    log.append_settled("created", "f.txt", "h", 1, "sync-upload", "", "p1")
    runner.intake_step()

    first = runner.run_pending()
    assert first[0].status == RunStatus.RETRY
    run = log.all_runs()[0]
    assert run.status == "retry"
    assert run.attempt == 2
    assert run.next_attempt_at is not None

    clock.advance(run.next_attempt_at - clock() + 1)  # past next_attempt_at
    second = runner.run_pending()
    assert second[0].status == RunStatus.FAILED  # attempt 2 >= max_attempts 2 -> exhausted
    assert log.all_runs()[0].status == "failed"


def test_install_vanished_at_claim_time_finalizes_failed(tmp_path: Path) -> None:
    workflows_dir, config_dir = _install(tmp_path)
    runner, log = _runner(tmp_path, workflows_dir, config_dir)
    log.append_settled("created", "f.txt", "h", 1, "sync-upload", "", "p1")
    runner.intake_step()

    (workflows_dir / "demo.py").unlink()
    (config_dir / "demo.toml").unlink()

    results = runner.run_pending()
    assert results[0].status == RunStatus.FAILED
    run = log.all_runs()[0]
    assert run.status == "failed"
    assert "no longer available" in (run.stderr or "")


# =====================================================================
# Crash recovery
# =====================================================================


def test_recover_crashed_reschedules_running_row_as_retry(tmp_path: Path) -> None:
    workflows_dir, config_dir = _install(tmp_path)  # default retry: base=60
    clock = FakeClock(500_000)
    runner, log = _runner(tmp_path, workflows_dir, config_dir, clock=clock)
    log.append_settled("created", "f.txt", "h", 1, "sync-upload", "", "p1")
    runner.intake_step()

    claimed = log.claim_next(clock())  # simulate a previous process claiming, then dying
    assert claimed is not None
    assert log.all_runs()[0].status == "running"

    count = runner.recover_crashed()
    assert count == 1
    run = log.all_runs()[0]
    assert run.status == "retry"
    assert run.attempt == 2
    assert run.next_attempt_at == 500_000 + 60_000


def test_recover_crashed_no_running_rows_is_noop(tmp_path: Path) -> None:
    workflows_dir, config_dir = _install(tmp_path)
    runner, _log = _runner(tmp_path, workflows_dir, config_dir)
    assert runner.recover_crashed() == 0


# =====================================================================
# sweep() delegation + default clock
# =====================================================================


def test_runner_sweep_delegates_to_event_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflows_dir, config_dir = _install(tmp_path)
    clock = FakeClock(0)
    runner, log = _runner(tmp_path, workflows_dir, config_dir, clock=clock)
    monkeypatch.setattr(events_module, "_now_ms", lambda: 0)
    log.append_settled("created", "ancient.txt", "h", 1, "sync-upload", "", "p1")
    # Move the fake "now" far enough forward that everything above is old,
    # then let Runner.sweep() (not EventLog.sweep() directly) do the work.
    clock.advance(200 * DAY_MS)
    runner.sweep(retention_days=10)
    assert log.all_events() == []


def test_default_clock_returns_a_plausible_epoch_ms() -> None:
    from notehook_cli.workflows.runner import _now_ms

    before = _now_ms()
    assert before > 1_700_000_000_000  # sometime after Nov 2023, sanity bound


# =====================================================================
# Housekeeping sweep
# =====================================================================


def test_sweep_run_before_event_and_respects_referencing_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = EventLog(tmp_path / "events.db")

    monkeypatch.setattr(events_module, "_now_ms", lambda: 0)
    log.append_settled("created", "old-orphan.txt", "h", 1, "sync-upload", "", "p1")
    old_event_with_old_run = log.append_settled(
        "created", "old-done.txt", "h", 1, "sync-upload", "", "p2"
    )
    old_event_with_live_run = log.append_settled(
        "created", "old-still-queued.txt", "h", 1, "sync-upload", "", "p3"
    )

    recent_ts = 55 * DAY_MS
    monkeypatch.setattr(events_module, "_now_ms", lambda: recent_ts)
    log.append_settled("created", "recent.txt", "h", 1, "sync-upload", "", "p4")
    monkeypatch.undo()

    # Old event whose run completed long ago (terminal + old finished_at) --
    # both the run and (once unreferenced) the event should be swept.
    log.intake(
        [PendingRun("x4", "wf", None, old_event_with_old_run, "old-done.txt")],
        old_event_with_old_run,
        now_ms=0,
    )
    claimed = log.claim_next(0)
    assert claimed is not None
    log.finalize_success(claimed.id, 0, "", "", finished_at_ms=0)

    # Old event whose run is still live (never finished) -- must survive.
    log.intake(
        [PendingRun("x4", "wf", None, old_event_with_live_run, "old-still-queued.txt")],
        old_event_with_live_run,
        now_ms=0,
    )

    log.sweep(retention_days=10, now_ms=60 * DAY_MS)  # cutoff = 50 * DAY_MS

    remaining_events = {e.rel_path for e in log.all_events()}
    remaining_runs = {r.rel_path for r in log.all_runs()}

    assert "old-orphan.txt" not in remaining_events  # old, never had a run -> swept
    assert "old-done.txt" not in remaining_runs  # old terminal run -> swept
    assert "old-done.txt" not in remaining_events  # ...then the now-unreferenced event
    assert "old-still-queued.txt" in remaining_runs  # never terminal -> never swept
    assert "old-still-queued.txt" in remaining_events  # a run still references it
    assert "recent.txt" in remaining_events  # too new to sweep


# =====================================================================
# Payload shape (contract test against the real SDK)
# =====================================================================


def test_event_payload_round_trips_through_real_sdk_event(
    tmp_path: Path, sdk: ModuleType
) -> None:
    workflows_dir, config_dir = _install(tmp_path)
    captured: list[dict[str, Any]] = []
    runner, log = _runner(
        tmp_path, workflows_dir, config_dir, invoke=_stub_invoke(0, capture=captured)
    )
    ev_id = log.append_settled(
        "created", "Note/a.pdf", "abc123", 42, "sync-upload", "CLI-orig", "pass-1"
    )
    runner.intake_step()

    results = runner.run_pending()
    assert results[0].status == RunStatus.SUCCESS
    assert len(captured) == 1

    event = sdk.Event.from_payload(captured[0]["event"])
    assert event.id == ev_id
    assert event.type == "created"
    assert event.path == tmp_path / "sync" / "Note/a.pdf"
    assert event.rel_path == "Note/a.pdf"
    assert event.content_hash == "abc123"
    assert event.size == 42
    assert event.source == "sync-upload"
    assert event.origin_equipment == "CLI-orig"
    assert event.sync_pass == "pass-1"
    assert event.attempt == 1


# =====================================================================
# Own-instance lock
# =====================================================================


def test_runner_lock_second_holder_fails_clearly(tmp_path: Path) -> None:
    """`Runner` takes no lock itself (see module docstring); the caller wraps
    its lifetime in `file_lock(config.runner_lock_file)`, exactly like the
    engine's own pass lock."""
    lock_file = tmp_path / "runner.lock"
    with file_lock(lock_file):
        with pytest.raises(LockError):
            with file_lock(lock_file, timeout_seconds=0.3, retry_interval=0.05):
                pass  # pragma: no cover - never reached
