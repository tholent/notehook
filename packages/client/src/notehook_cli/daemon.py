"""Daemon mode: filesystem watch + periodic remote poll + server change-feed
long-poll, one sync pass at a time."""

import logging
import threading
from collections.abc import Callable
from typing import Any

import httpx
from watchfiles import watch

from notehook_cli.api_client import ApiError, EndpointUnsupported, SupernoteApiClient
from notehook_cli.engine import SyncEngine, SyncResult

logger = logging.getLogger(__name__)

_DEBOUNCE_SECONDS = 2.0

# workflow-spec.md §7: the server bounds wait_seconds at 30; 25 leaves margin
# and matches the plan's chosen value.
_FEED_WAIT_SECONDS = 25
_FEED_BACKOFF_INITIAL_SECONDS = 1.0
_FEED_BACKOFF_CAP_SECONDS = 30.0


def _has_foreign_change(rows: list[dict[str, Any]], own_equipment_no: str) -> bool:
    """True when any change row was made by equipment other than this
    client itself. This client's own upload echoes must never be a wake
    trigger (spec §7 echo suppression) — pulled out as a pure function so the
    filtering rule is unit-testable without spinning up threads or servers.
    """
    return any(row.get("equipment_no") != own_equipment_no for row in rows)


class SyncDaemon:
    def __init__(
        self,
        engine: SyncEngine,
        poll_interval_seconds: int,
        on_result: Callable[[SyncResult], None] | None = None,
        feed_api: SupernoteApiClient | None = None,
    ) -> None:
        self._engine = engine
        self._poll_interval = poll_interval_seconds
        self._on_result = on_result
        # Dedicated API client for the change-feed long-poll (spec §7 /
        # Phase 6): a separate connection from the engine's own so a 25s
        # long-poll never head-of-line-blocks transfers. None disables the
        # feed entirely (servers/tests that don't wire one up keep today's
        # watch+poll behavior unchanged).
        self._feed_api = feed_api
        self._wake = threading.Event()
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _watch_loop(self) -> None:
        root = self._engine.root
        root.mkdir(parents=True, exist_ok=True)
        try:
            for _changes in watch(root, stop_event=self._stop, debounce=1600):
                self._wake.set()
        except Exception:
            logger.exception("filesystem watcher stopped")

    def _feed_loop(self) -> None:
        """Third trigger thread: long-polls /api/notehook/changes and wakes
        the sync engine when a device (not this client) makes a change.
        Failure handling per spec §7 ("feed absence degrades to today's
        behavior, never breaks sync"): an unsupported endpoint disables the
        feed permanently (the poll timer remains as fallback); transport
        errors and other ApiErrors back off and keep retrying.
        """
        assert self._feed_api is not None
        own_equipment_no = self._feed_api.equipment_no
        backoff = _FEED_BACKOFF_INITIAL_SECONDS
        cursor = 0
        while not self._stop.is_set():
            try:
                if cursor == 0:
                    # since=0 is *always* bootstrap semantics server-side
                    # (spec §7: "returns the current cursor without
                    # history"), regardless of wait_seconds — it never waits
                    # and never returns rows. That's indistinguishable from
                    # "nothing has ever changed on this server yet", so
                    # while the cursor is still 0 we re-bootstrap at a light
                    # pace instead of long-polling (which the server would
                    # just answer instantly, busy-looping us) until the
                    # first change ever lands and the cursor moves.
                    cursor, _rows = self._feed_api.changes(since=0, wait_seconds=0)
                    backoff = _FEED_BACKOFF_INITIAL_SECONDS
                    if cursor == 0:
                        self._stop.wait(1.0)
                    continue
                cursor, rows = self._feed_api.changes(
                    since=cursor, wait_seconds=_FEED_WAIT_SECONDS
                )
                backoff = _FEED_BACKOFF_INITIAL_SECONDS
                if _has_foreign_change(rows, own_equipment_no) and not self._stop.is_set():
                    self._wake.set()
            except EndpointUnsupported:
                logger.info(
                    "server has no change-feed endpoint; disabling long-poll trigger "
                    "(falling back to the poll timer)"
                )
                return
            except (ApiError, httpx.HTTPError) as exc:
                logger.debug("change-feed request failed, backing off %.0fs: %s", backoff, exc)
                self._stop.wait(backoff)
                backoff = min(_FEED_BACKOFF_CAP_SECONDS, backoff * 2)

    def run(self) -> None:
        """Run until stop() is called (or the process is signalled)."""
        watcher = threading.Thread(target=self._watch_loop, daemon=True)
        watcher.start()
        if self._feed_api is not None:
            feed = threading.Thread(target=self._feed_loop, daemon=True)
            feed.start()
        while not self._stop.is_set():
            try:
                result = self._engine.run_once()
                if self._on_result is not None:
                    self._on_result(result)
            except Exception:
                logger.exception("sync pass failed; will retry on next trigger")
            # Wait for a local change or the poll timer, whichever first.
            triggered = self._wake.wait(timeout=self._poll_interval)
            if triggered:
                self._wake.clear()
                # Quiet period so bursts of writes coalesce into one pass.
                self._stop.wait(_DEBOUNCE_SECONDS)
