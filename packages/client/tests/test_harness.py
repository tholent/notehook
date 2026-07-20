"""Job harness -- spec workflow-spec.md §2 "Invocation mechanics", §6 "Execution"."""

import json
import os
import shutil
from pathlib import Path

import pytest

from notehook_cli.workflows.harness import PreparedJob, prepare_job
from notehook_cli.workflows.installs import Install, discover
from notehook_cli.workflows.manifest import extract_pep723_block_text

_MANIFEST_TABLE = """[tool.notehook]
name = "demo"
"""

_SINGLE_FILE_SOURCE = (
    '# /// script\n'
    '# requires-python = ">=3.11"\n'
    '# dependencies = ["requests"]\n'
    "#\n"
    "# [tool.notehook]\n"
    '# name = "demo"\n'
    "# ///\n"
    "from notehook_workflow import workflow\n\n\n"
    "@workflow()\n"
    "def run(event, config):\n"
    "    pass\n"
)

_CONFIG_TOML = 'workflow = "demo"\npaths = ["**"]\n'


def _single_file_install(tmp_path: Path) -> Install:
    workflows_dir = tmp_path / "workflows"
    config_dir = tmp_path / "workflow-config"
    workflows_dir.mkdir()
    config_dir.mkdir()
    (workflows_dir / "demo.py").write_text(_SINGLE_FILE_SOURCE)
    (config_dir / "demo.toml").write_text(_CONFIG_TOML)
    result = discover(workflows_dir, config_dir)
    install = result["demo"]
    assert isinstance(install, Install)
    return install


def _package_install(tmp_path: Path) -> Install:
    workflows_dir = tmp_path / "workflows"
    config_dir = tmp_path / "workflow-config"
    pkg_dir = workflows_dir / "pkg-demo"
    pkg_dir.mkdir(parents=True)
    config_dir.mkdir()
    (pkg_dir / "pyproject.toml").write_text(
        """[project]
name = "pkg-demo"
version = "0.1.0"
requires-python = ">=3.12"

[tool.notehook]
name = "pkg-demo"
"""
    )
    (pkg_dir / "workflow.py").write_text(
        "from notehook_workflow import workflow\n\n\n@workflow()\ndef run(event, config):\n"
        "    pass\n"
    )
    (config_dir / "pkg-demo.toml").write_text('workflow = "pkg-demo"\npaths = ["**"]\n')
    result = discover(workflows_dir, config_dir)
    install = result["pkg-demo"]
    assert isinstance(install, Install)
    return install


_EVENT = {
    "id": 1,
    "type": "created",
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
_CONFIG = {"device_ip": "192.168.1.50"}


# --- single-file form ---


def test_single_file_harness_carries_pep723_block_verbatim(tmp_path: Path) -> None:
    install = _single_file_install(tmp_path)
    job = prepare_job(install, _EVENT, _CONFIG, {})
    try:
        original_block = extract_pep723_block_text(install.entry_file)
        assert original_block is not None
        harness_source = job.cwd.joinpath("harness.py").read_text()
        assert original_block in harness_source
        assert "notehook_workflow._main()" in harness_source
    finally:
        _cleanup(job)


def test_single_file_argv_uses_no_project(tmp_path: Path) -> None:
    install = _single_file_install(tmp_path)
    job = prepare_job(install, _EVENT, _CONFIG, {})
    try:
        assert job.argv == ["uv", "run", "--no-project", str(job.cwd / "harness.py")]
    finally:
        _cleanup(job)


# --- package form ---


def test_package_harness_has_no_pep723_block(tmp_path: Path) -> None:
    install = _package_install(tmp_path)
    job = prepare_job(install, _EVENT, {}, {})
    try:
        harness_source = job.cwd.joinpath("harness.py").read_text()
        assert "# ///" not in harness_source
        assert "notehook_workflow._main()" in harness_source
    finally:
        _cleanup(job)


def test_package_argv_uses_project_flag(tmp_path: Path) -> None:
    install = _package_install(tmp_path)
    job = prepare_job(install, _EVENT, {}, {})
    try:
        assert job.argv == [
            "uv",
            "run",
            "--project",
            str(install.package_dir),
            "python",
            str(job.cwd / "harness.py"),
        ]
    finally:
        _cleanup(job)


# --- payload file ---


def test_payload_file_round_trips_event_and_config(tmp_path: Path) -> None:
    install = _single_file_install(tmp_path)
    job = prepare_job(install, _EVENT, _CONFIG, {})
    try:
        payload = json.loads(job.payload_file.read_text())
        assert payload == {"event": _EVENT, "config": _CONFIG}
    finally:
        _cleanup(job)


# --- env assembly ---


def test_env_includes_payload_and_workflow_file(tmp_path: Path) -> None:
    install = _single_file_install(tmp_path)
    job = prepare_job(install, _EVENT, _CONFIG, {})
    try:
        assert job.env["NOTEHOOK_PAYLOAD_FILE"] == str(job.payload_file)
        assert job.env["NOTEHOOK_WORKFLOW_FILE"] == str(install.entry_file)
    finally:
        _cleanup(job)


def test_env_carries_configured_secrets_uppercased(tmp_path: Path) -> None:
    install = _single_file_install(tmp_path)
    job = prepare_job(install, _EVENT, _CONFIG, {"x4_api_key": "sekret"})
    try:
        assert job.env["NOTEHOOK_SECRET_X4_API_KEY"] == "sekret"
    finally:
        _cleanup(job)


def test_env_scrubs_preexisting_notehook_secret_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTEHOOK_SECRET_LEAKED", "should-not-appear")
    install = _single_file_install(tmp_path)
    job = prepare_job(install, _EVENT, _CONFIG, {"x4_api_key": "sekret"})
    try:
        assert "NOTEHOOK_SECRET_LEAKED" not in job.env
        assert job.env["NOTEHOOK_SECRET_X4_API_KEY"] == "sekret"
    finally:
        _cleanup(job)


def test_env_prepends_sdk_dir_to_existing_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/some/existing/path")
    install = _single_file_install(tmp_path)
    job = prepare_job(install, _EVENT, _CONFIG, {})
    try:
        entries = job.env["PYTHONPATH"].split(os.pathsep)
        assert entries[0].endswith("_sdk")
        assert "/some/existing/path" in entries
    finally:
        _cleanup(job)


def test_env_sets_pythonpath_when_none_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PYTHONPATH", raising=False)
    install = _single_file_install(tmp_path)
    job = prepare_job(install, _EVENT, _CONFIG, {})
    try:
        assert job.env["PYTHONPATH"].endswith("_sdk")
    finally:
        _cleanup(job)


def test_env_strips_virtual_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/some/other/venv")
    install = _single_file_install(tmp_path)
    job = prepare_job(install, _EVENT, _CONFIG, {})
    try:
        assert "VIRTUAL_ENV" not in job.env
    finally:
        _cleanup(job)


# --- job dir ---


def test_job_dir_contains_expected_files(tmp_path: Path) -> None:
    install = _single_file_install(tmp_path)
    job = prepare_job(install, _EVENT, _CONFIG, {})
    try:
        assert job.job_dir == job.cwd
        assert (job.job_dir / "harness.py").is_file()
        assert (job.job_dir / "payload.json").is_file()
    finally:
        _cleanup(job)


def _cleanup(job: PreparedJob) -> None:
    shutil.rmtree(job.job_dir, ignore_errors=True)
