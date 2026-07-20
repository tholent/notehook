"""Job harness: builds the generated harness script + `uv run` invocation
for one workflow job (spec §2 "Invocation mechanics", §6 "Execution").

Single-file workflow: the harness carries a verbatim copy of the workflow's
PEP 723 block (so `uv run` resolves the identical `requires-python`/
`dependencies`), followed by `import notehook_workflow;
notehook_workflow._main()`. `uv run --no-project` is used because the job
dir lives under the system tmp root — outside any uv workspace — but
pinning it explicitly stops `uv` from walking up the directory tree looking
for a `pyproject.toml` to adopt as "the project" (verified empirically: a
bare `uv run <script-with-PEP723-block>.py` already treats the file as a
self-contained script and behaves identically, but `--no-project` documents
the intent and is a no-op safety net if that ever changes).

Package workflow: the harness is a plain script with no PEP 723 block;
`uv run --project <install-dir>` runs it against the package's own
`pyproject.toml`/committed `uv.lock` (spec §2).

Both forms get `notehook_workflow` on `PYTHONPATH` (decision D4) and the
payload file via `NOTEHOOK_PAYLOAD_FILE`. The real workflow module is loaded
by the SDK's `_main()` at dispatch time via `NOTEHOOK_WORKFLOW_FILE` — the
harness itself never imports it directly.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .installs import Install
from .manifest import extract_pep723_block_text

__all__ = ["PreparedJob", "prepare_job"]

_SDK_DIR = Path(__file__).parent / "_sdk"

_HARNESS_BODY = "import notehook_workflow\n\nnotehook_workflow._main()\n"


@dataclass(frozen=True)
class PreparedJob:
    """Everything needed to spawn and clean up after one workflow run."""

    argv: list[str]
    env: dict[str, str]
    cwd: Path
    job_dir: Path
    payload_file: Path


def prepare_job(
    install: Install,
    event: dict[str, Any],
    config: dict[str, Any],
    secrets: dict[str, str],
) -> PreparedJob:
    """Build argv/env/cwd for one run of `install` against `event`.

    Creates a fresh job temp dir (also the subprocess's cwd, spec §6),
    writes the `{"event": ..., "config": ...}` payload file there, and
    generates the harness script appropriate to the install's single-file
    vs. package form. `event`/`config` are plain dicts — this module doesn't
    interpret them, just serializes them verbatim for the SDK to parse on
    the other side of the process boundary.
    """
    job_dir = Path(tempfile.mkdtemp(prefix="notehook-workflow-"))
    payload_file = job_dir / "payload.json"
    payload_file.write_text(json.dumps({"event": event, "config": config}))

    env = _build_env(install.entry_file, payload_file, secrets)
    harness_path = job_dir / "harness.py"

    if install.package_dir is not None:
        harness_path.write_text(_HARNESS_BODY)
        argv = [
            "uv",
            "run",
            "--project",
            str(install.package_dir),
            "python",
            str(harness_path),
        ]
    else:
        pep723_block = extract_pep723_block_text(install.entry_file)
        harness_source = f"{pep723_block}\n{_HARNESS_BODY}" if pep723_block else _HARNESS_BODY
        harness_path.write_text(harness_source)
        argv = ["uv", "run", "--no-project", str(harness_path)]

    return PreparedJob(
        argv=argv, env=env, cwd=job_dir, job_dir=job_dir, payload_file=payload_file
    )


def _build_env(entry_file: Path, payload_file: Path, secrets: dict[str, str]) -> dict[str, str]:
    """Inherited environment minus any pre-existing `NOTEHOOK_SECRET_*` (a
    workflow must only ever see the secrets *this* install configured, never
    whatever happens to be in the parent process's environment), plus the
    SDK on `PYTHONPATH` (prepended, so an existing `PYTHONPATH` still works),
    the payload/workflow-file pointers, and one `NOTEHOOK_SECRET_<NAME>` env
    var per configured secret (spec §2: env delivery, not config/payload, so
    logging/dumping config can't leak them)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("NOTEHOOK_SECRET_")}
    # Each job gets its own `uv`-managed environment (script venv or the
    # install's project venv); a `VIRTUAL_ENV` inherited from the runner's
    # own process (e.g. `notehook workflows serve` launched from a dev venv)
    # would only make `uv` print a mismatch warning onto captured stderr.
    env.pop("VIRTUAL_ENV", None)
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{_SDK_DIR}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(_SDK_DIR)
    )
    env["NOTEHOOK_PAYLOAD_FILE"] = str(payload_file)
    env["NOTEHOOK_WORKFLOW_FILE"] = str(entry_file)
    for name, value in secrets.items():
        env[f"NOTEHOOK_SECRET_{name.upper()}"] = value
    return env
