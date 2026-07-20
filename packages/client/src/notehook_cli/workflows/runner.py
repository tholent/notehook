"""Runner core: intake/fan-out, coalescing, execution/concurrency/timeout/
retry, crash recovery, housekeeping — spec §6 "Runner lifecycle".

Ties together everything from earlier phases: `events.py` (storage +
atomicity), `installs.py` (`discover()`), `harness.py` (`prepare_job`),
`executor.py` (`execute`). Nothing here talks to disk directly except through
those modules.

Out of scope (Phase 4/5, not built here): the `notehook workflows serve` CLI
command, the poll loop that repeatedly calls `intake_step`/`run_pending`,
`watchfiles` hot reload, and the `install`/`configure`/`run`/`backfill`/`logs`
CLI verbs. `Runner` exposes small, non-blocking, independently-callable step
methods precisely so Phase 5 can wrap them in whatever loop/scheduler it
wants, and so tests never need threads or bare sleeps.

**Type-filter design note** (spec §6, "optionally narrows the decorator's
`on`"): the decorator's actual `on` set lives inside the workflow's Python
source and is only knowable by importing it — which happens in the *spawned
subprocess*, never in the runner's own process (importing untrusted workflow
code in-process would break the "unsandboxed but isolated per job" model).
So: if `install.config.on` is set, it is the pre-spawn filter — the
authoritative signal the runner can actually see. If `install.config.on` is
`None`, the runner cannot know the decorator's set without executing code, so
it queues on a glob/target match alone for all three event types and relies
on the SDK's own per-handler `on` dispatch inside the subprocess to no-op
harmlessly when no handler matches that type. A wasted spawn is an acceptable
cost; silently never running a configured install is not.

**Own-instance guard**: `Runner` itself takes no lock — the caller wraps its
whole lifetime in `lock.file_lock(config.runner_lock_file)`, matching how
`engine.py`'s pass lock is held by its caller rather than by `SyncEngine`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import installs as installs_mod
from .events import ClaimedRun, EventLog, EventRow, PendingRun
from .executor import Invoke, RunOutcome, RunStatus, default_invoke, execute
from .harness import prepare_job
from .installs import Install
from .manifest import RetrySpec

__all__ = ["RunResult", "Runner"]


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(frozen=True)
class RunResult:
    """One completed (or short-circuited) run, as returned by `run_pending`.

    `status` is the *final stored* status, not necessarily the executor's raw
    classification: a `RunOutcome.RETRY` whose attempt count is exhausted is
    stored (and reported here) as `FAILED`, matching what `EventLog.all_runs`
    would show for the same row.
    """

    run_id: int
    install: str
    rel_path: str
    status: RunStatus
    exit_code: int | None


class Runner:
    """Runner core for one `events.db` / install set (spec §6).

    Construct once per `notehook workflows serve` process (or once per test).
    All disk state lives in `event_log` and the `workflows_dir`/
    `workflow_config_dir` trees; `Runner` itself is stateless between calls
    other than the constructor args, so its step methods are safe to call
    repeatedly from a poll loop (Phase 5) or directly from tests.
    """

    def __init__(
        self,
        event_log: EventLog,
        workflows_dir: Path,
        workflow_config_dir: Path,
        sync_root: Path,
        own_equipment_no: str,
        max_parallel: int = 2,
        invoke: Invoke = default_invoke,
        clock: Callable[[], int] = _now_ms,
    ) -> None:
        self._event_log = event_log
        self._workflows_dir = workflows_dir
        self._workflow_config_dir = workflow_config_dir
        self._sync_root = sync_root
        self._own_equipment_no = own_equipment_no
        self._max_parallel = max_parallel
        self._invoke = invoke
        self._clock = clock

    # --- intake / fan-out ---

    def intake_step(self) -> int:
        """One fan-out pass: settled events past the cursor, matched against
        every enabled, valid install, queued as `run` rows, cursor advanced —
        all atomically via `EventLog.intake`. Returns the number of runs
        queued (0 is valid: the cursor still advances past events that
        matched nothing, spec §6 amendment)."""
        settled = self._event_log.unconsumed_settled()
        if not settled:
            return 0

        discovered = installs_mod.discover(self._workflows_dir, self._workflow_config_dir)
        pending: list[PendingRun] = []
        for event in settled:
            for alias, install in discovered.items():
                if not isinstance(install, Install) or not install.config.enabled:
                    continue
                if self._should_queue(event, install):
                    pending.append(
                        PendingRun(
                            install=alias,
                            workflow_name=install.manifest.name,
                            workflow_version=install.manifest.version,
                            event_id=event.id,
                            rel_path=event.rel_path,
                        )
                    )

        new_cursor = settled[-1].id
        self._event_log.intake(pending, new_cursor, now_ms=self._clock())
        return len(pending)

    def _should_queue(self, event: EventRow, install: Install) -> bool:
        """Spec §6 fan-out decision for one (event, install) pair."""
        if install.config.skip_own_changes and event.origin_equipment == self._own_equipment_no:
            return False

        if event.target_install:
            if event.target_install != install.alias:
                return False
            if event.source == "manual":
                # Spec §6: "For manual events the glob and on filters are
                # bypassed entirely" — a manual trigger means "run this
                # workflow on this file", full stop.
                return True
            # backfill (or any other future targeted source): the install is
            # already pinned by target_install, so the glob is moot (the
            # `backfill` command itself only ever emits events for paths that
            # already matched the install's globs at generation time) — but
            # the type/`on` filter still applies normally (spec §6: backfill
            # "simulates first sight", so a workflow subscribed only to
            # `updated` still ignores its `created` events).
            return self._passes_type_filter(event, install)

        # Fan-out: glob match required, plus the type filter.
        if not install.matches_path(event.rel_path):
            return False
        return self._passes_type_filter(event, install)

    @staticmethod
    def _passes_type_filter(event: EventRow, install: Install) -> bool:
        """See the module docstring's "Type-filter design note"."""
        if install.config.on is None:
            return True
        return event.type in install.config.on

    # --- execution ---

    def run_pending(self) -> list[RunResult]:
        """Claim and execute everything currently eligible, respecting
        `max_parallel` (via `ThreadPoolExecutor`) and per-`(install,
        rel_path)` serialization (guaranteed by `EventLog.claim_next`).
        Blocks until the batch drains — i.e. until every worker's own
        `claim_next` call finds nothing left to claim, including work that
        only became claimable because another worker's run just finished
        (each worker loops back to claim again after finishing a job, so a
        run queued behind a `running` one is picked up in the same call
        without needing a second `run_pending` invocation)."""
        discovered = installs_mod.discover(self._workflows_dir, self._workflow_config_dir)
        results: list[RunResult] = []
        append_result = results.append  # list.append is atomic under the GIL

        def worker() -> None:
            while True:
                claimed = self._event_log.claim_next(self._clock())
                if claimed is None:
                    return
                append_result(self._execute_claimed(claimed, discovered))

        worker_count = max(1, self._max_parallel)
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [pool.submit(worker) for _ in range(worker_count)]
            for future in futures:
                future.result()
        return results

    def _execute_claimed(
        self, claimed: ClaimedRun, discovered: dict[str, Install | installs_mod.BrokenInstall]
    ) -> RunResult:
        install = discovered.get(claimed.install)
        if not isinstance(install, Install):
            # Spec doesn't cover this directly, but crashing the loop over an
            # alias that vanished (removed) or turned broken between queueing
            # and claiming would be worse than a clearly-labeled failure.
            now_ms = self._clock()
            self._event_log.finalize_failed(
                claimed.id,
                None,
                "",
                f"install '{claimed.install}' is no longer available (removed or broken) "
                f"at claim time; run failed without executing",
                now_ms,
            )
            return RunResult(claimed.id, claimed.install, claimed.rel_path, RunStatus.FAILED, None)

        payload_event = self._build_event_payload(claimed)
        prepared = prepare_job(
            install, payload_event, install.resolved_config, install.config.secrets
        )
        outcome = execute(prepared, install.manifest.timeout, invoke=self._invoke)
        final_status = self._finalize_outcome(claimed, install.manifest.retry, outcome)
        return RunResult(
            claimed.id, claimed.install, claimed.rel_path, final_status, outcome.exit_code
        )

    def _build_event_payload(self, claimed: ClaimedRun) -> dict[str, Any]:
        """Build the `event` half of the harness payload, matching
        `notehook_workflow.Event.from_payload` field-for-field (`path` is the
        workflow's absolute path, resolved here from `sync_root / rel_path`;
        `timestamp` is epoch ms, matching `event.created_at`)."""
        event = claimed.event
        return {
            "id": event.id,
            "type": event.type,
            "path": str(self._sync_root / event.rel_path),
            "rel_path": event.rel_path,
            "content_hash": event.content_hash,
            "size": event.size,
            "timestamp": event.created_at,
            "source": event.source,
            "origin_equipment": event.origin_equipment,
            "sync_pass": event.sync_pass,
            "attempt": claimed.attempt,
        }

    def _finalize_outcome(
        self, claimed: ClaimedRun, retry_spec: RetrySpec, outcome: RunOutcome
    ) -> RunStatus:
        now_ms = self._clock()
        if outcome.status is RunStatus.SUCCESS:
            self._event_log.finalize_success(
                claimed.id, outcome.exit_code, outcome.stdout, outcome.stderr, now_ms
            )
            return RunStatus.SUCCESS
        if outcome.status is RunStatus.FAILED:
            self._event_log.finalize_failed(
                claimed.id, outcome.exit_code, outcome.stdout, outcome.stderr, now_ms
            )
            return RunStatus.FAILED
        return self._reschedule_or_exhaust(
            claimed, retry_spec, outcome.exit_code, outcome.stdout, outcome.stderr, now_ms
        )

    def _reschedule_or_exhaust(
        self,
        claimed: ClaimedRun,
        retry_spec: RetrySpec,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        now_ms: int,
    ) -> RunStatus:
        """Spec §6 retry math: `min(backoff_base * 4^(attempt-1), backoff_cap)`
        where `attempt` is the attempt that just ran (`claimed.attempt`).
        Exhausted (`claimed.attempt >= max_attempts`) finalizes `failed`
        instead of scheduling an attempt that would never be allowed to run.
        Shared between normal retry-outcome finalization and crash recovery
        (`recover_crashed`, which synthesizes the same retryable outcome for
        a `running` row left over from a dead process). Returns the status
        actually stored (`RETRY` or `FAILED` on exhaustion)."""
        if claimed.attempt >= retry_spec.max_attempts:
            self._event_log.finalize_failed(claimed.id, exit_code, stdout, stderr, now_ms)
            return RunStatus.FAILED
        next_attempt = claimed.attempt + 1
        delay_seconds = min(
            retry_spec.backoff_base * 4 ** (claimed.attempt - 1), retry_spec.backoff_cap
        )
        next_attempt_at_ms = now_ms + delay_seconds * 1000
        self._event_log.finalize_retry(
            claimed.id, next_attempt, next_attempt_at_ms, exit_code, stdout, stderr, now_ms
        )
        return RunStatus.RETRY

    # --- crash recovery ---

    def recover_crashed(self) -> int:
        """At startup: every `running` row belongs to a dead process (this
        one just started, spec §6 "Crash recovery") — reschedule each under
        the normal retry policy, as if it had failed with a retryable
        outcome. Returns the number of rows recovered."""
        discovered = installs_mod.discover(self._workflows_dir, self._workflow_config_dir)
        running = self._event_log.running_runs()
        for claimed in running:
            install = discovered.get(claimed.install)
            retry_spec = install.manifest.retry if isinstance(install, Install) else RetrySpec()
            self._reschedule_or_exhaust(
                claimed,
                retry_spec,
                None,
                "",
                "crash recovery: no process was found running this job at runner startup",
                self._clock(),
            )
        return len(running)

    # --- housekeeping ---

    def sweep(self, retention_days: int) -> None:
        """Delete old terminal `run` rows, then `event` rows with nothing
        left referencing them (spec §6 "Housekeeping"). A plain callable —
        Phase 5's `serve` loop schedules it (daily by default); this class
        doesn't schedule anything itself."""
        self._event_log.sweep(retention_days, now_ms=self._clock())
