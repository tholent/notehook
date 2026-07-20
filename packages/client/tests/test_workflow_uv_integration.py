"""One real end-to-end test through `uv run` -- spec workflow-spec.md §2
"Invocation mechanics", decision D6 (docs/workflow-implementation-plan.md).

Everything else (harness generation, exit-code/timeout classification) is
covered without `uv` in test_harness.py/test_executor.py; this is the single
place that proves the real subprocess boundary works: a single-file
workflow with a stdlib-only PEP 723 block runs under `uv run --no-project`,
imports the SDK off `PYTHONPATH`, and receives the payload.

Kept hermetic/offline: no third-party dependencies, so `uv` never needs
network access to resolve anything beyond the interpreter it already has.
"""

import json
import shutil
from pathlib import Path

import pytest

from notehook_cli.workflows.executor import RunStatus, default_invoke, execute
from notehook_cli.workflows.harness import prepare_job
from notehook_cli.workflows.installs import Install, discover

pytestmark = [
    pytest.mark.uv,
    pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH"),
]

_WORKFLOW_SOURCE = """# /// script
# requires-python = ">=3.11"
# dependencies = []
#
# [tool.notehook]
# name = "marker-writer"
# ///
import json
from pathlib import Path

from notehook_workflow import workflow


@workflow()
def run(event, config):
    marker = Path(config["marker_path"])
    marker.write_text(
        json.dumps(
            {
                "rel_path": event.rel_path,
                "content_hash": event.content_hash,
                "type": event.type,
                "attempt": event.attempt,
            }
        )
    )
"""

_CONFIG_TOML = 'workflow = "marker-writer"\npaths = ["**"]\n'


def test_single_file_workflow_runs_end_to_end_through_real_uv(tmp_path: Path) -> None:
    workflows_dir = tmp_path / "workflows"
    config_dir = tmp_path / "workflow-config"
    workflows_dir.mkdir()
    config_dir.mkdir()
    (workflows_dir / "marker-writer.py").write_text(_WORKFLOW_SOURCE)
    (config_dir / "marker-writer.toml").write_text(_CONFIG_TOML)

    result = discover(workflows_dir, config_dir)
    install = result["marker-writer"]
    assert isinstance(install, Install)

    marker_path = tmp_path / "marker.json"
    event = {
        "id": 42,
        "type": "created",
        "path": str(tmp_path / "note.pdf"),
        "rel_path": "Note/ToReader/note.pdf",
        "content_hash": "deadbeef",
        "size": 7,
        "timestamp": 1_700_000_000_000,
        "source": "sync-upload",
        "origin_equipment": "CLI-test",
        "sync_pass": "uuid-int-1",
        "attempt": 1,
    }
    config = {"marker_path": str(marker_path)}

    job = prepare_job(install, event, config, secrets={})
    outcome = execute(job, timeout_seconds=60, invoke=default_invoke)

    assert outcome.status == RunStatus.SUCCESS, outcome.stderr
    assert marker_path.is_file()
    marker_data = json.loads(marker_path.read_text())
    assert marker_data == {
        "rel_path": "Note/ToReader/note.pdf",
        "content_hash": "deadbeef",
        "type": "created",
        "attempt": 1,
    }
