"""Daemon mode: filesystem watch + periodic remote poll, one sync pass at a time."""

import logging
import threading
from collections.abc import Callable

from watchfiles import watch

from noted_cli.engine import SyncEngine, SyncResult

logger = logging.getLogger(__name__)

_DEBOUNCE_SECONDS = 2.0


class SyncDaemon:
    def __init__(
        self,
        engine: SyncEngine,
        poll_interval_seconds: int,
        on_result: Callable[[SyncResult], None] | None = None,
    ) -> None:
        self._engine = engine
        self._poll_interval = poll_interval_seconds
        self._on_result = on_result
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

    def run(self) -> None:
        """Run until stop() is called (or the process is signalled)."""
        watcher = threading.Thread(target=self._watch_loop, daemon=True)
        watcher.start()
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
