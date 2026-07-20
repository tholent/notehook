"""Workflow author SDK -- spec workflow-spec.md §2 (frozen contract).

In-process tests (registry, `on`-filtering, definition order, `Event`
parsing, `secret()`) put the `_sdk` directory on `sys.path` and import
`notehook_workflow` as a top-level module, exactly like a workflow venv
would (decision D4, docs/workflow-implementation-plan.md) -- never through
`notehook_cli.workflows._sdk`.

Subprocess tests exercise `_main()`'s exit-code protocol end-to-end (a real
process boundary matters here: stdout/stderr capture and `sys.exit` codes)
and the D4 purity guarantee (stdlib-only import).
"""

import importlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import notehook_cli

SDK_DIR = Path(notehook_cli.__file__).parent / "workflows" / "_sdk"


@pytest.fixture
def sdk() -> Iterator[ModuleType]:
    """Import `notehook_workflow` as production does: `_sdk` on `sys.path`,
    imported by its own name. Registry cleared before and after so tests
    don't leak handlers into each other."""
    if str(SDK_DIR) not in sys.path:
        sys.path.insert(0, str(SDK_DIR))
    module = importlib.import_module("notehook_workflow")
    module._REGISTRY.clear()
    yield module
    module._REGISTRY.clear()


def _payload_event(type_: str = "created", **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": 1,
        "type": type_,
        "path": "/sync/root/note.pdf",
        "rel_path": "note.pdf",
        "content_hash": "abc123",
        "size": 42,
        "timestamp": 1_700_000_000_000,
        "source": "sync-upload",
        "origin_equipment": "CLI-abc",
        "sync_pass": "uuid-1",
        "attempt": 1,
    }
    data.update(overrides)
    return data


# --- registry, on-filtering, definition order ---


def test_workflow_decorator_default_on(sdk: ModuleType) -> None:
    @sdk.workflow()
    def handler(event: Any, config: Any) -> None:
        pass

    assert len(sdk._REGISTRY) == 1
    assert sdk._REGISTRY[0].on == ("created", "updated")
    assert sdk._REGISTRY[0].handler is handler


def test_workflow_decorator_custom_on(sdk: ModuleType) -> None:
    @sdk.workflow(on=["deleted"])
    def handler(event: Any, config: Any) -> None:
        pass

    assert sdk._REGISTRY[0].on == ("deleted",)


def test_workflow_decorator_rejects_invalid_on(sdk: ModuleType) -> None:
    with pytest.raises(ValueError, match="bogus"):

        @sdk.workflow(on=["created", "bogus"])
        def handler(event: Any, config: Any) -> None:
            pass  # pragma: no cover - decorator raises before this matters


def test_workflow_returns_function_unchanged(sdk: ModuleType) -> None:
    @sdk.workflow()
    def handler(event: Any, config: Any) -> str:
        return "called"

    assert handler(None, {}) == "called"


def test_registry_definition_order_and_on_filtering(sdk: ModuleType) -> None:
    calls: list[tuple[str, str]] = []

    @sdk.workflow(on=["created"])
    def first(event: Any, config: Any) -> None:
        calls.append(("first", event.type))

    @sdk.workflow(on=["created", "updated"])
    def second(event: Any, config: Any) -> None:
        calls.append(("second", event.type))

    @sdk.workflow(on=["deleted"])
    def third(event: Any, config: Any) -> None:
        calls.append(("third", event.type))

    created_event = sdk.Event.from_payload(_payload_event(type_="created"))
    sdk._dispatch(created_event, {})
    assert calls == [("first", "created"), ("second", "created")]

    calls.clear()
    updated_event = sdk.Event.from_payload(_payload_event(type_="updated"))
    sdk._dispatch(updated_event, {})
    assert calls == [("second", "updated")]

    calls.clear()
    deleted_event = sdk.Event.from_payload(_payload_event(type_="deleted"))
    sdk._dispatch(deleted_event, {})
    assert calls == [("third", "deleted")]


# --- Event ---


def test_event_from_payload_all_fields(sdk: ModuleType) -> None:
    event = sdk.Event.from_payload(_payload_event())
    assert event.id == 1
    assert event.type == "created"
    assert event.path == Path("/sync/root/note.pdf")
    assert event.rel_path == "note.pdf"
    assert event.content_hash == "abc123"
    assert event.size == 42
    assert event.timestamp == datetime.fromtimestamp(1_700_000_000, tz=UTC)
    assert event.source == "sync-upload"
    assert event.origin_equipment == "CLI-abc"
    assert event.sync_pass == "uuid-1"
    assert event.attempt == 1


def test_event_from_payload_ignores_unknown_keys(sdk: ModuleType) -> None:
    data = _payload_event(some_future_field="should be ignored", another=123)
    event = sdk.Event.from_payload(data)
    assert event.id == 1
    assert not hasattr(event, "some_future_field")


# --- secret() ---


def test_secret_set(sdk: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTEHOOK_SECRET_X4_API_KEY", "s3kr3t")
    assert sdk.secret("x4_api_key") == "s3kr3t"


def test_secret_unset(sdk: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTEHOOK_SECRET_X4_API_KEY", raising=False)
    assert sdk.secret("x4_api_key") is None


def test_secret_uppercases_name(sdk: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTEHOOK_SECRET_DEVICE_IP", "192.168.1.1")
    assert sdk.secret("device_ip") == "192.168.1.1"


# --- _main() exit-code protocol, in a real subprocess ---


def _run_main(
    tmp_path: Path, workflow_src: str, event_type: str = "created"
) -> subprocess.CompletedProcess[str]:
    workflow_file = tmp_path / "wf.py"
    workflow_file.write_text(workflow_src)
    payload_file = tmp_path / "payload.json"
    payload = {"event": _payload_event(type_=event_type), "config": {"k": "v"}}
    payload_file.write_text(json.dumps(payload))

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SDK_DIR)
    env["NOTEHOOK_PAYLOAD_FILE"] = str(payload_file)
    env["NOTEHOOK_WORKFLOW_FILE"] = str(workflow_file)
    return subprocess.run(
        [sys.executable, "-c", "import notehook_workflow; notehook_workflow._main()"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_main_success_exit_zero(tmp_path: Path) -> None:
    result = _run_main(
        tmp_path,
        "from notehook_workflow import workflow\n\n"
        "@workflow()\n"
        "def run(event, config):\n"
        "    print(f'ran {event.id} {config[\"k\"]}')\n",
    )
    assert result.returncode == 0, result.stderr
    assert "ran 1 v" in result.stdout


def test_main_failure_exit_one(tmp_path: Path) -> None:
    result = _run_main(
        tmp_path,
        "from notehook_workflow import workflow\n\n"
        "@workflow()\n"
        "def run(event, config):\n"
        "    raise ValueError('boom')\n",
    )
    assert result.returncode == 1
    assert "ValueError: boom" in result.stderr


def test_main_retry_later_exit_75(tmp_path: Path) -> None:
    result = _run_main(
        tmp_path,
        "from notehook_workflow import workflow, RetryLater\n\n"
        "@workflow()\n"
        "def run(event, config):\n"
        "    raise RetryLater('device offline')\n",
    )
    assert result.returncode == 75
    assert "device offline" in result.stderr


def test_main_multiple_handlers_definition_order(tmp_path: Path) -> None:
    result = _run_main(
        tmp_path,
        "from notehook_workflow import workflow\n\n"
        "@workflow(on=['created'])\n"
        "def first(event, config):\n"
        "    print('first')\n\n"
        "@workflow(on=['created', 'updated'])\n"
        "def second(event, config):\n"
        "    print('second')\n",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["first", "second"]


def test_main_handler_not_matching_type_is_skipped(tmp_path: Path) -> None:
    result = _run_main(
        tmp_path,
        "from notehook_workflow import workflow\n\n"
        "@workflow(on=['deleted'])\n"
        "def run(event, config):\n"
        "    print('should not run')\n",
        event_type="created",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_main_missing_env_vars_exit_one(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SDK_DIR)
    env.pop("NOTEHOOK_PAYLOAD_FILE", None)
    env.pop("NOTEHOOK_WORKFLOW_FILE", None)
    result = subprocess.run(
        [sys.executable, "-c", "import notehook_workflow; notehook_workflow._main()"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert "NOTEHOOK_PAYLOAD_FILE" in result.stderr


# --- D4: pure-stdlib import purity ---


def test_sdk_import_purity_in_subprocess(tmp_path: Path) -> None:
    """The SDK must import cleanly with only the `_sdk` directory + stdlib
    on `sys.path` -- no `notehook_cli`, no third-party package (decision
    D4). Run with `-S` (skip site-packages) so nothing beyond stdlib +
    `PYTHONPATH` is even reachable."""
    script = tmp_path / "check_purity.py"
    script.write_text(
        "import sys\n"
        "import notehook_workflow\n"
        "bad = sorted(\n"
        "    m for m in sys.modules\n"
        "    if m == 'notehook_cli' or m.startswith('notehook_cli.')\n"
        "    or m in {'httpx', 'pydantic', 'typer', 'sqlmodel', 'fastapi', 'watchfiles'}\n"
        ")\n"
        "print(','.join(bad))\n"
    )
    env = {"PYTHONPATH": str(SDK_DIR)}
    if "PATH" in os.environ:
        env["PATH"] = os.environ["PATH"]
    result = subprocess.run(
        [sys.executable, "-S", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
