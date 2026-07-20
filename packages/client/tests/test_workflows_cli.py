"""`notehook workflows ...` install/configure CLI verbs -- spec workflow-spec.md §5, §8.

Follows `test_cli.py`'s pattern: typer `CliRunner`, `--config-dir` at a tmp
path. All git-path tests use local `file://` repos created in-test (no
network).
"""

import os
import subprocess
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from notehook_cli import cli
from notehook_cli.config import ClientConfig
from notehook_cli.workflows.events import EventLog, PendingRun
from notehook_cli.workflows.installs import Install, discover

runner = CliRunner()

_HANDLER_BODY = (
    "from notehook_workflow import workflow\n\n\n@workflow()\ndef run(event, config):\n    pass\n"
)
_REQUIRED_DEVICE_IP = "device_ip = { required = true }\n"


def _write_package_fixture(
    dir_path: Path,
    *,
    name: str = "demo-pkg",
    inputs_table: str = "",
    secrets_table: str = "",
    deps: list[str] | None = None,
    suggested_paths: list[str] | None = None,
) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    deps = deps if deps is not None else []
    deps_toml = ", ".join(f'"{d}"' for d in deps)
    pyproject = f"""[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [{deps_toml}]

[tool.notehook]
name = "{name}"
"""
    if suggested_paths is not None:
        paths_toml = ", ".join(f'"{p}"' for p in suggested_paths)
        pyproject += f"suggested_paths = [{paths_toml}]\n"
    if inputs_table:
        pyproject += f"\n[tool.notehook.inputs]\n{inputs_table}\n"
    if secrets_table:
        pyproject += f"\n[tool.notehook.secrets]\n{secrets_table}\n"
    (dir_path / "pyproject.toml").write_text(pyproject)
    (dir_path / "workflow.py").write_text(_HANDLER_BODY)


def _write_single_file_fixture(path: Path, *, name: str = "solo-wf") -> None:
    text = (
        "# /// script\n"
        '# requires-python = ">=3.12"\n'
        '# dependencies = ["requests"]\n'
        "#\n"
        "# [tool.notehook]\n"
        f'# name = "{name}"\n'
        "# ///\n"
    ) + _HANDLER_BODY
    path.write_text(text)


def _invoke(config_dir: Path, args: list[str], **kwargs: Any) -> Any:
    return runner.invoke(cli.app, ["workflows", *args, "--config-dir", str(config_dir)], **kwargs)


def _install(config_dir: Path, src: Path | str, alias: str, *extra: str) -> Any:
    """The common case: install `src` under `--as alias` with `--paths ** --yes`
    (plus any `extra` flags), used by tests whose focus is elsewhere."""
    args = ["install", str(src), "--as", alias, "--paths", "**", "--yes", *extra]
    return _invoke(config_dir, args)


def _run_git(args: list[str], cwd: Path) -> None:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)


# --- install: package form, end-to-end disclosure + config perms ---


def test_install_package_end_to_end(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    src_dir = tmp_path / "src-pkg"
    _write_package_fixture(
        src_dir,
        name="demo-pkg",
        inputs_table='device_ip = { required = true, description = "X4 IP" }\n',
        secrets_table="api_key = { required = true }\n",
        deps=["requests"],
    )

    result = _invoke(
        config_dir,
        [
            "install",
            str(src_dir),
            "--as",
            "demo",
            "--input",
            "device_ip=192.168.1.50",
            "--secret",
            "api_key=shh",
            "--paths",
            "Note/ToReader/**",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output

    # disclosure block (spec §3): inputs, secrets, deps, unsandboxed notice.
    assert "device_ip" in result.output
    assert "required" in result.output
    assert "api_key" in result.output
    assert "requests" in result.output
    assert "UNSANDBOXED" in result.output

    config = ClientConfig.load(config_dir)
    config_file = config.workflow_config_dir / "demo.toml"
    assert config_file.is_file()
    assert oct(config_file.stat().st_mode)[-3:] == "600"

    installs = discover(config.workflows_dir, config.workflow_config_dir)
    install = installs["demo"]
    assert isinstance(install, Install)
    assert install.manifest.name == "demo-pkg"
    assert install.config.paths == ["Note/ToReader/**"]
    assert install.config.source == str(src_dir)
    assert install.resolved_config == {"device_ip": "192.168.1.50"}
    assert install.config.secrets == {"api_key": "shh"}


# --- install: single-file form lands as a bare .py, not a directory ---


def test_install_single_file_lands_as_bare_py(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    src_file = tmp_path / "solo.py"
    _write_single_file_fixture(src_file, name="solo-wf")

    result = _invoke(config_dir, ["install", str(src_file), "--paths", "**", "--yes"])
    assert result.exit_code == 0, result.output

    config = ClientConfig.load(config_dir)
    workflow_path = config.workflows_dir / "solo-wf.py"
    assert workflow_path.is_file()
    assert not (config.workflows_dir / "solo-wf").exists()

    installs = discover(config.workflows_dir, config.workflow_config_dir)
    install = installs["solo-wf"]
    assert isinstance(install, Install)
    assert install.package_dir is None


# --- install: alias collision ---


def test_install_collision_leaves_first_install_intact(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    src_dir = tmp_path / "src-pkg"
    _write_package_fixture(src_dir, name="demo-pkg")

    first = _install(config_dir, src_dir, "demo")
    assert first.exit_code == 0, first.output

    config = ClientConfig.load(config_dir)
    workflow_dir = config.workflows_dir / "demo"
    original_pyproject = (workflow_dir / "pyproject.toml").read_text()
    original_config = (config.workflow_config_dir / "demo.toml").read_text()

    second = _install(config_dir, src_dir, "demo")
    assert second.exit_code != 0
    assert "already exists" in second.output

    assert (workflow_dir / "pyproject.toml").read_text() == original_pyproject
    assert (config.workflow_config_dir / "demo.toml").read_text() == original_config


# --- install: same source, two aliases, distinct configs ---


def test_double_install_distinct_aliases(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    src_dir = tmp_path / "src-pkg"
    _write_package_fixture(src_dir, name="demo-pkg", inputs_table=_REQUIRED_DEVICE_IP)

    for alias, ip in (("demo-a", "10.0.0.1"), ("demo-b", "10.0.0.2")):
        result = _install(config_dir, src_dir, alias, "--input", f"device_ip={ip}")
        assert result.exit_code == 0, result.output

    config = ClientConfig.load(config_dir)
    installs = discover(config.workflows_dir, config.workflow_config_dir)
    install_a = installs["demo-a"]
    install_b = installs["demo-b"]
    assert isinstance(install_a, Install)
    assert isinstance(install_b, Install)
    assert install_a.resolved_config == {"device_ip": "10.0.0.1"}
    assert install_b.resolved_config == {"device_ip": "10.0.0.2"}


# --- install: broken manifest cleans up the staged copy ---


def test_install_broken_manifest_cleans_up(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    src_dir = tmp_path / "src-broken"
    src_dir.mkdir()
    (src_dir / "pyproject.toml").write_text("this is not [ valid toml")
    (src_dir / "workflow.py").write_text(_HANDLER_BODY)

    result = _install(config_dir, src_dir, "broken")
    assert result.exit_code != 0

    config = ClientConfig.load(config_dir)
    assert not (config.workflows_dir / "broken").exists()
    assert not (config.workflow_config_dir / "broken.toml").exists()


# --- install: --yes with missing required input fails clearly, no hang ---


def test_install_yes_without_required_input_fails_clearly(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    src_dir = tmp_path / "src-pkg"
    _write_package_fixture(src_dir, name="demo-pkg", inputs_table=_REQUIRED_DEVICE_IP)

    result = _install(config_dir, src_dir, "demo")
    assert result.exit_code != 0
    assert "device_ip" in result.output

    config = ClientConfig.load(config_dir)
    assert not (config.workflows_dir / "demo").exists()


# --- install: --yes with no --paths and no suggested_paths fails clearly ---


def test_install_yes_without_paths_or_suggestion_fails(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    src_dir = tmp_path / "src-pkg"
    _write_package_fixture(src_dir, name="demo-pkg")

    result = _invoke(config_dir, ["install", str(src_dir), "--as", "demo", "--yes"])
    assert result.exit_code != 0
    assert "paths" in result.output.lower()


# --- install: --yes with a suggested_paths default fills paths ---


def test_install_yes_uses_suggested_paths_when_paths_omitted(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    src_dir = tmp_path / "src-pkg"
    _write_package_fixture(src_dir, name="demo-pkg", suggested_paths=["Note/ToReader/**"])

    result = _invoke(config_dir, ["install", str(src_dir), "--as", "demo", "--yes"])
    assert result.exit_code == 0, result.output

    config = ClientConfig.load(config_dir)
    installs = discover(config.workflows_dir, config.workflow_config_dir)
    install = installs["demo"]
    assert isinstance(install, Install)
    assert install.config.paths == ["Note/ToReader/**"]


# --- configure ---


def test_configure_changes_input_and_paths(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    src_dir = tmp_path / "src-pkg"
    _write_package_fixture(src_dir, name="demo-pkg", inputs_table=_REQUIRED_DEVICE_IP)
    install_result = _install(config_dir, src_dir, "demo", "--input", "device_ip=1.1.1.1")
    assert install_result.exit_code == 0, install_result.output

    result = _invoke(
        config_dir,
        ["configure", "demo", "--input", "device_ip=2.2.2.2", "--paths", "B/**", "--yes"],
    )
    assert result.exit_code == 0, result.output

    config = ClientConfig.load(config_dir)
    installs = discover(config.workflows_dir, config.workflow_config_dir)
    install = installs["demo"]
    assert isinstance(install, Install)
    assert install.resolved_config == {"device_ip": "2.2.2.2"}
    assert install.config.paths == ["B/**"]


def test_configure_rejects_and_rolls_back_when_now_broken(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    src_dir = tmp_path / "src-pkg"
    _write_package_fixture(src_dir, name="demo-pkg")
    install_result = _install(config_dir, src_dir, "demo")
    assert install_result.exit_code == 0, install_result.output

    config = ClientConfig.load(config_dir)
    pyproject_path = config.workflows_dir / "demo" / "pyproject.toml"
    original_config_text = (config.workflow_config_dir / "demo.toml").read_text()
    # Simulate the workflow's own code being renamed without a reinstall --
    # `configure` doesn't touch the `workflow` field, so this reproduces a
    # manifest-name/config-name mismatch discover() will flag as broken.
    pyproject_path.write_text(pyproject_path.read_text().replace("demo-pkg", "renamed-pkg"))

    result = _invoke(config_dir, ["configure", "demo", "--paths", "Other/**", "--yes"])
    assert result.exit_code != 0
    assert "broken" in result.output

    assert (config.workflow_config_dir / "demo.toml").read_text() == original_config_text


# --- enable / disable ---


def test_enable_disable_roundtrip(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    src_dir = tmp_path / "src-pkg"
    _write_package_fixture(src_dir, name="demo-pkg")
    install_result = _install(config_dir, src_dir, "demo")
    assert install_result.exit_code == 0, install_result.output
    config = ClientConfig.load(config_dir)

    disable_result = _invoke(config_dir, ["disable", "demo"])
    assert disable_result.exit_code == 0, disable_result.output
    installs = discover(config.workflows_dir, config.workflow_config_dir)
    disabled = installs["demo"]
    assert isinstance(disabled, Install)
    assert disabled.config.enabled is False

    enable_result = _invoke(config_dir, ["enable", "demo"])
    assert enable_result.exit_code == 0, enable_result.output
    installs = discover(config.workflows_dir, config.workflow_config_dir)
    enabled = installs["demo"]
    assert isinstance(enabled, Install)
    assert enabled.config.enabled is True


def test_enable_missing_alias_errors_clearly(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    result = _invoke(config_dir, ["enable", "ghost"])
    assert result.exit_code != 0
    assert "ghost" in result.output


# --- remove ---


def test_remove_deletes_code_and_config_with_yes(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    src_dir = tmp_path / "src-pkg"
    _write_package_fixture(src_dir, name="demo-pkg")
    install_result = _install(config_dir, src_dir, "demo")
    assert install_result.exit_code == 0, install_result.output
    config = ClientConfig.load(config_dir)

    result = _invoke(config_dir, ["remove", "demo", "--yes"])
    assert result.exit_code == 0, result.output
    assert not (config.workflows_dir / "demo").exists()
    assert not (config.workflow_config_dir / "demo.toml").exists()


def test_remove_prompts_and_respects_answer(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    src_dir = tmp_path / "src-pkg"
    _write_package_fixture(src_dir, name="demo-pkg")
    install_result = _install(config_dir, src_dir, "demo")
    assert install_result.exit_code == 0, install_result.output
    config = ClientConfig.load(config_dir)

    declined = _invoke(config_dir, ["remove", "demo"], input="n\n")
    assert declined.exit_code != 0
    assert (config.workflows_dir / "demo").exists()
    assert (config.workflow_config_dir / "demo.toml").exists()

    accepted = _invoke(config_dir, ["remove", "demo"], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert not (config.workflows_dir / "demo").exists()
    assert not (config.workflow_config_dir / "demo.toml").exists()


# --- update ---


def test_update_pulls_git_source(tmp_path: Path) -> None:
    remote_dir = tmp_path / "remote-src"
    _write_package_fixture(remote_dir, name="demo-pkg")
    _run_git(["init", "-b", "main"], remote_dir)
    _run_git(["add", "."], remote_dir)
    _run_git(["commit", "-m", "initial"], remote_dir)

    config_dir = tmp_path / "cfg"
    src_url = f"file://{remote_dir}"
    install_result = _install(config_dir, src_url, "demo")
    assert install_result.exit_code == 0, install_result.output

    config = ClientConfig.load(config_dir)
    workflow_file = config.workflows_dir / "demo" / "workflow.py"
    assert "marker-for-update" not in workflow_file.read_text()

    (remote_dir / "workflow.py").write_text(_HANDLER_BODY + "\n# marker-for-update\n")
    _run_git(["add", "."], remote_dir)
    _run_git(["commit", "-m", "second"], remote_dir)

    update_result = _invoke(config_dir, ["update", "demo", "--yes"])
    assert update_result.exit_code == 0, update_result.output
    assert "marker-for-update" in workflow_file.read_text()


def test_update_non_git_source_errors_clearly(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    src_dir = tmp_path / "src-pkg"
    _write_package_fixture(src_dir, name="demo-pkg")
    install_result = _install(config_dir, src_dir, "demo")
    assert install_result.exit_code == 0, install_result.output

    result = _invoke(config_dir, ["update", "demo", "--yes"])
    assert result.exit_code != 0
    assert "not cloned from git" in result.output


# --- list ---


def test_list_shows_healthy_broken_and_last_run_status(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    src_dir = tmp_path / "src-pkg"
    _write_package_fixture(src_dir, name="demo-pkg")
    install_result = _install(config_dir, src_dir, "healthy")
    assert install_result.exit_code == 0, install_result.output

    config = ClientConfig.load(config_dir)
    # Orphan config -> broken install (no matching workflow code).
    config.workflow_config_dir.mkdir(parents=True, exist_ok=True)
    (config.workflow_config_dir / "ghost.toml").write_text('workflow = "ghost"\npaths = ["**"]\n')

    event_log = EventLog(config.events_db_file)
    event_id = event_log.append_settled(
        "created", "Note/a.pdf", "abc123", 10, "manual", "", "pass-1", target_install="healthy"
    )
    event_log.intake(
        [
            PendingRun(
                install="healthy",
                workflow_name="demo-pkg",
                workflow_version=None,
                event_id=event_id,
                rel_path="Note/a.pdf",
            )
        ],
        new_cursor=event_id,
    )
    claimed = event_log.claim_next(now_ms=1)
    assert claimed is not None
    event_log.finalize_success(claimed.id, 0, "", "", finished_at_ms=2)

    result = _invoke(config_dir, ["list"])
    assert result.exit_code == 0, result.output
    assert "healthy" in result.output
    assert "ghost" in result.output
    assert "broken" in result.output
    assert "success" in result.output


def test_list_never_alias_when_no_runs(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    src_dir = tmp_path / "src-pkg"
    _write_package_fixture(src_dir, name="demo-pkg")
    install_result = _install(config_dir, src_dir, "demo")
    assert install_result.exit_code == 0, install_result.output

    result = _invoke(config_dir, ["list"])
    assert result.exit_code == 0, result.output
    assert "never" in result.output
