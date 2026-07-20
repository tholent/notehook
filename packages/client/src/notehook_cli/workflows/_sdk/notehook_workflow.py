"""notehook workflow author SDK — FROZEN CONTRACT (docs/workflow-spec.md §2).

This module is shipped inside notehook-cli but does **not** run there: the
runner's harness (a later PR) puts this file's directory on `PYTHONPATH` and
spawns workflows in their own `uv run` subprocess/venv, where they do
``from notehook_workflow import workflow, secret, RetryLater``. Two hard
rules follow from that (decision D4,
docs/workflow-implementation-plan.md):

1. Pure stdlib only. This module must never import `notehook_cli` or any
   third-party package — it has to import cleanly with nothing but the
   standard library on `sys.path`.
2. Python 3.11+ syntax only (spec §3 examples pin
   ``requires-python = ">=3.11"`` for workflows), even though the rest of
   the notehook-cli host targets 3.12+.

Payload contract (informative — spec §2 "invocation mechanics"): the harness
writes a JSON object ``{"event": {...}, "config": {...}}`` to the file named
by ``NOTEHOOK_PAYLOAD_FILE``, and points ``NOTEHOOK_WORKFLOW_FILE`` at the
workflow module to load. ``event`` is the frozen field table verbatim, with
``timestamp`` as epoch milliseconds; ``config`` is the resolved inputs dict.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

__all__ = [
    "Event",
    "RetryLater",
    "secret",
    "workflow",
]

_VALID_EVENT_TYPES = frozenset({"created", "updated", "deleted"})
_DEFAULT_ON: tuple[str, ...] = ("created", "updated")

# Exit-code protocol (spec §2 "Outcomes"): 0 success, 1 permanent failure,
# 75 (EX_TEMPFAIL) retry-later.
_EXIT_SUCCESS = 0
_EXIT_FAILURE = 1
_EXIT_RETRY = 75

Handler = Callable[["Event", dict[str, Any]], None]


class RetryLater(Exception):
    """Raise from a handler to signal a transient failure.

    Maps to exit code 75; the runner reschedules the run with backoff
    (spec §2 Outcomes, §6 Retry). The canonical case is an unreachable
    external device.
    """


@dataclass(frozen=True)
class _Registration:
    handler: Handler
    on: tuple[str, ...]


# Module-level registry, in definition order (spec §2: "every function whose
# `on` includes the event's type is called, in definition order").
_REGISTRY: list[_Registration] = []


def workflow(on: list[str] | None = None) -> Callable[[Handler], Handler]:
    """Decorator: register a handler to run for events of type in `on`.

    `on` defaults to `["created", "updated"]` (spec §2). Entries are
    validated against `{"created", "updated", "deleted"}` at decoration
    time — a typo in `on` fails fast at import, not at dispatch.
    """
    types: tuple[str, ...] = tuple(on) if on is not None else _DEFAULT_ON
    invalid = sorted(set(types) - _VALID_EVENT_TYPES)
    if invalid:
        raise ValueError(
            f"workflow(on=...) contains invalid event type(s) {invalid}; "
            f"must be a subset of {sorted(_VALID_EVENT_TYPES)}"
        )

    def decorator(func: Handler) -> Handler:
        _REGISTRY.append(_Registration(handler=func, on=types))
        return func

    return decorator


def secret(name: str) -> str | None:
    """Read a secret the runner injected as `NOTEHOOK_SECRET_<NAME_UPPER>`.

    Returns `None` if unset — a missing but `required` secret is caught by
    the runner at configure/validate time, before the subprocess spawns
    (spec §2 "Secrets"), so this accessor itself never raises.
    """
    return os.environ.get(f"NOTEHOOK_SECRET_{name.upper()}")


@dataclass(frozen=True)
class Event:
    """One event-log row as seen by workflow authors (spec §2, frozen fields).

    New fields may be added to the payload later; unknown keys are ignored
    here so old workflows keep working (spec §2: "workflows must tolerate
    unknown fields").
    """

    id: int
    type: str
    path: Path
    rel_path: str
    content_hash: str
    size: int
    timestamp: datetime
    source: str
    origin_equipment: str
    sync_pass: str
    attempt: int

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> Event:
        """Build from the payload's `event` dict. Unknown keys are ignored;
        `timestamp` arrives as epoch milliseconds."""
        return cls(
            id=int(data["id"]),
            type=str(data["type"]),
            path=Path(data["path"]),
            rel_path=str(data["rel_path"]),
            content_hash=str(data["content_hash"]),
            size=int(data["size"]),
            timestamp=datetime.fromtimestamp(int(data["timestamp"]) / 1000, tz=UTC),
            source=str(data["source"]),
            origin_equipment=str(data["origin_equipment"]),
            sync_pass=str(data["sync_pass"]),
            attempt=int(data["attempt"]),
        )


def _load_workflow_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("_notehook_workflow_target", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load workflow module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dispatch(event: Event, config: dict[str, Any]) -> None:
    """Call every registered handler whose `on` includes `event.type`, in
    definition order (spec §2)."""
    for registration in _REGISTRY:
        if event.type in registration.on:
            registration.handler(event, config)


def main() -> int:
    """Read the payload, import the workflow module, dispatch handlers.

    Returns the process exit code rather than calling `sys.exit` directly,
    so it stays testable in-process; `_main()` below is the process-exit
    wrapper the harness calls.
    """
    payload_file = os.environ.get("NOTEHOOK_PAYLOAD_FILE")
    workflow_file = os.environ.get("NOTEHOOK_WORKFLOW_FILE")
    if not payload_file or not workflow_file:
        print(
            "notehook_workflow: NOTEHOOK_PAYLOAD_FILE and NOTEHOOK_WORKFLOW_FILE "
            "must both be set",
            file=sys.stderr,
        )
        return _EXIT_FAILURE

    try:
        payload = json.loads(Path(payload_file).read_text())
        event = Event.from_payload(payload["event"])
        config: dict[str, Any] = payload.get("config", {})
        _load_workflow_module(Path(workflow_file))
        _dispatch(event, config)
    except RetryLater as exc:
        print(str(exc), file=sys.stderr)
        return _EXIT_RETRY
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return _EXIT_FAILURE
    return _EXIT_SUCCESS


def _main() -> None:
    """Process-exit wrapper around `main()`. What the future harness calls:
    ``import notehook_workflow; notehook_workflow._main()``."""
    sys.exit(main())


if __name__ == "__main__":
    _main()
