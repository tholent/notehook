"""`notehook workflows ...`: install/configure CLI verbs -- spec §5, §8.

Phase 4 builds the management half of the CLI surface table (spec §8):
`install`, `configure`, `enable`, `disable`, `remove`, `update`, `list`. The
operational half (`run`, `backfill`, `logs`, `serve`) is Phase 5 and lands in
this same file, appended below `list` in the same order as the spec table --
keep that ordering when extending this module.

Module layout (decision D3, docs/workflow-implementation-plan.md): this file
exposes `workflows_app = typer.Typer()`, registered in
[cli.py](../cli.py) via `app.add_typer(workflows_app, name="workflows")`.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from watchfiles import watch

from notehook_cli.config import ClientConfig, WorkflowsConfig
from notehook_cli.lock import LockError, file_lock
from notehook_cli.scan import file_md5, scan_local
from notehook_cli.state_db import StateDB
from notehook_cli.workflows.events import EventLog, EventRow, RunRow
from notehook_cli.workflows.installs import (
    BrokenInstall,
    Install,
    InstallError,
    compile_path_glob,
    discover,
    parse_install_config,
    resolve_workflow_path,
)
from notehook_cli.workflows.manifest import (
    Manifest,
    ManifestError,
    parse_manifest,
    parse_pep723_metadata,
)
from notehook_cli.workflows.runner import Runner

logger = logging.getLogger(__name__)

workflows_app = typer.Typer(help="Manage installed workflows (spec §5/§8).")
console = Console()

ConfigDirOpt = Annotated[
    Path | None, typer.Option("--config-dir", help="Override the config directory")
]

# Repeatable `k=v` options share this shape everywhere: default `None` (never
# a mutable `[]` default) so an absent flag is distinguishable from an
# explicitly empty one, normalized to `[]` at the top of each command body.
_InputOpt = Annotated[
    list[str] | None, typer.Option("--input", help="Workflow input as key=value; may repeat")
]
_SecretOpt = Annotated[
    list[str] | None, typer.Option("--secret", help="Workflow secret as key=value; may repeat")
]
_PathsOpt = Annotated[
    list[str] | None,
    typer.Option("--paths", help="Trigger path glob (rel to sync root); may repeat"),
]
_YesOpt = Annotated[
    bool, typer.Option("--yes", help="Non-interactive: fail instead of prompting")
]
_GlobOpt = Annotated[
    list[str] | None,
    typer.Option("--glob", help="Narrow further to files also matching this glob; may repeat"),
]


def _load(config_dir: Path | None) -> ClientConfig:
    return ClientConfig.load(config_dir)


def _fail(message: str, *, code: int = 1) -> typer.Exit:
    console.print(f"[red]{message}[/red]")
    return typer.Exit(code)


_FORBIDDEN_ALIAS = re.compile(r"[/\\\x00-\x1f]")


def _validate_alias(alias: str) -> str:
    """Reject anything that could escape `workflows_dir`/`workflow_config_dir`
    when joined into a path (mirrors the server's `tree_service.validate_name`
    invariant -- client-supplied values never touch filesystem paths
    directly). Must run on every alias before it's used to build a path,
    including `install`'s manifest-derived default: for a git-sourced
    install, `manifest.name` is content from the cloned repo's own
    `pyproject.toml`/PEP 723 block, i.e. attacker-controlled."""
    if not alias or alias in {".", ".."} or _FORBIDDEN_ALIAS.search(alias):
        raise _fail(f"invalid alias: {alias!r}")
    return alias


# --- git URL vs local path detection ---


class _GitError(Exception):
    """`git clone`/`git pull` exited nonzero."""


def _looks_like_git_source(src: str) -> bool:
    """A source is treated as a git URL if it has a scheme (`https://`,
    `file://`, `ssh://`, ...), is a scp-style `git@host:path`, or ends in
    `.git` -- otherwise it's a local filesystem path (spec §5)."""
    return "://" in src or src.startswith("git@") or src.endswith(".git")


def _git_clone(src: str, dest: Path) -> None:
    result = subprocess.run(  # noqa: S603,S607 - fixed argv; src is a CLI argument, not shell text
        ["git", "clone", "--depth", "1", src, str(dest)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise _GitError(result.stderr.strip() or f"git clone exited {result.returncode}")


def _git_pull(repo_dir: Path) -> None:
    result = subprocess.run(  # noqa: S603,S607 - fixed argv, cwd pinned to the install's own dir
        ["git", "pull", "--ff-only"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise _GitError(result.stderr.strip() or f"git pull exited {result.returncode}")


# --- disclosure block (spec §3 "Install-time disclosure") ---


def _read_dependency_info(staged_path: Path, manifest: Manifest) -> tuple[str | None, list[str]]:
    """Best-effort `requires-python`/`dependencies` lookup for display only
    (never validated) -- `manifest.py` doesn't expose deps (they live in
    standard packaging metadata, not `[tool.notehook]`), so this reads
    `pyproject.toml`'s `[project]` table (package form) or the PEP 723 block
    (single-file form, or a package whose entry file still carries one)."""
    if staged_path.is_dir():
        pyproject_path = staged_path / "pyproject.toml"
        if pyproject_path.is_file():
            try:
                data = tomllib.loads(pyproject_path.read_text())
            except tomllib.TOMLDecodeError:
                data = {}
            project = data.get("project")
            if isinstance(project, dict):
                return _extract_requires_deps(project)
        entry_path = staged_path / manifest.entry
        if entry_path.is_file():
            try:
                meta = parse_pep723_metadata(entry_path)
            except ManifestError:
                meta = None
            if meta is not None:
                return _extract_requires_deps(meta)
        return (None, [])

    try:
        meta = parse_pep723_metadata(staged_path)
    except ManifestError:
        meta = None
    if meta is None:
        return (None, [])
    return _extract_requires_deps(meta)


def _extract_requires_deps(table: dict[str, Any]) -> tuple[str | None, list[str]]:
    requires_python = table.get("requires-python")
    deps = table.get("dependencies")
    return (
        requires_python if isinstance(requires_python, str) else None,
        [str(d) for d in deps] if isinstance(deps, list) else [],
    )


def _print_disclosure(manifest: Manifest, staged_path: Path) -> None:
    version_suffix = f" v{manifest.version}" if manifest.version else ""
    console.print(f"\n[bold]{manifest.name}{version_suffix}[/bold]")
    if manifest.description:
        console.print(manifest.description)

    console.print("\n[bold]Inputs:[/bold]")
    if manifest.inputs:
        for name, spec in manifest.inputs.items():
            req = "required" if spec.required else "optional"
            desc = f" -- {spec.description}" if spec.description else ""
            console.print(f"  {name} ({req}){desc}")
    else:
        console.print("  (none declared)")

    console.print("\n[bold]Secrets:[/bold]")
    if manifest.secrets:
        for name, secret_spec in manifest.secrets.items():
            req = "required" if secret_spec.required else "optional"
            desc = f" -- {secret_spec.description}" if secret_spec.description else ""
            console.print(f"  {name} ({req}){desc}")
    else:
        console.print("  (none declared)")

    requires_python, deps = _read_dependency_info(staged_path, manifest)
    console.print("\n[bold]Dependencies:[/bold]")
    if requires_python:
        console.print(f"  python {requires_python}")
    for dep in deps:
        console.print(f"  {dep}")
    if not requires_python and not deps:
        console.print("  (none declared)")

    console.print(
        "\n[yellow]This workflow runs UNSANDBOXED with network access and full "
        "user-level file access.[/yellow]\n"
    )


# --- inputs/secrets/paths resolution (install, configure, update) ---


def _parse_kv_list(pairs: list[str], *, label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise _fail(f"invalid --{label} value '{pair}'; expected key=value", code=2)
        key, _, value = pair.partition("=")
        result[key] = value
    return result


def _resolve_inputs(
    manifest: Manifest, supplied: dict[str, Any], *, yes: bool
) -> dict[str, Any]:
    """Prompt (or fail under `--yes`) for any required input not already in
    `supplied`. Optional inputs are left absent -- the manifest default
    applies at `discover()`/run time, spec §5."""
    resolved = dict(supplied)
    for name, spec in manifest.inputs.items():
        if not spec.required or name in resolved:
            continue
        if yes:
            raise _fail(f"missing required input '{name}' (use --input {name}=...)", code=2)
        prompt = f"{name}" + (f" ({spec.description})" if spec.description else "")
        resolved[name] = typer.prompt(prompt)
    return resolved


def _resolve_secrets(manifest: Manifest, supplied: dict[str, str], *, yes: bool) -> dict[str, str]:
    resolved = dict(supplied)
    for name, spec in manifest.secrets.items():
        if not spec.required or name in resolved:
            continue
        if yes:
            raise _fail(f"missing required secret '{name}' (use --secret {name}=...)", code=2)
        prompt = f"{name}" + (f" ({spec.description})" if spec.description else "")
        resolved[name] = typer.prompt(prompt, hide_input=True)
    return resolved


def _resolve_paths(manifest: Manifest, cli_paths: list[str] | None, *, yes: bool) -> list[str]:
    """`paths` is required and authoritative (spec §5) -- `suggested_paths`
    is only ever a *default offered*, never silently substituted when the
    user could still be asked."""
    if cli_paths:
        return list(cli_paths)
    if yes:
        if manifest.suggested_paths:
            return list(manifest.suggested_paths)
        raise _fail(
            "--paths is required (no suggested_paths in the manifest to fall back on)", code=2
        )
    suggestion = ", ".join(manifest.suggested_paths) if manifest.suggested_paths else None
    raw = typer.prompt("Trigger paths (comma-separated globs)", default=suggestion)
    values = [p.strip() for p in raw.split(",") if p.strip()]
    if not values:
        raise _fail("paths is required", code=2)
    return values


# --- install config TOML writer ---


def _dump_toml_str(value: str) -> str:
    return json.dumps(value)


def _dump_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return _dump_toml_str(str(value))


def _dump_toml_list(values: list[str]) -> str:
    return "[" + ", ".join(_dump_toml_str(v) for v in values) + "]"


def _write_install_config(
    path: Path,
    *,
    workflow: str,
    source: str,
    enabled: bool,
    paths: list[str],
    on: list[str] | None,
    skip_own_changes: bool,
    inputs: dict[str, Any],
    secrets: dict[str, str],
) -> None:
    """Write one `workflow-config/<alias>.toml` per the install config format
    (spec §5), chmod 0600 (never committed, may hold secrets)."""
    lines = [
        f"workflow = {_dump_toml_str(workflow)}",
        f"source = {_dump_toml_str(source)}",
        f"enabled = {'true' if enabled else 'false'}",
        f"paths = {_dump_toml_list(paths)}",
    ]
    if on is not None:
        lines.append(f"on = {_dump_toml_list(on)}")
    lines.append(f"skip_own_changes = {'true' if skip_own_changes else 'false'}")
    lines.append("")
    lines.append("[inputs]")
    for name, value in inputs.items():
        lines.append(f"{name} = {_dump_toml_value(value)}")
    lines.append("")
    lines.append("[secrets]")
    for name, value in secrets.items():
        lines.append(f"{name} = {_dump_toml_value(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


# --- install ---


@workflows_app.command()
def install(
    src: Annotated[str, typer.Argument(help="Git URL, or a local directory/.py file path")],
    alias: Annotated[
        str | None, typer.Option("--as", help="Install alias (default: the manifest name)")
    ] = None,
    inputs: _InputOpt = None,
    secrets: _SecretOpt = None,
    paths: _PathsOpt = None,
    yes: _YesOpt = False,
    config_dir: ConfigDirOpt = None,
) -> None:
    """Install a workflow from a git URL or local path (spec §5)."""
    config = _load(config_dir)
    workflows_dir = config.workflows_dir
    workflow_config_dir = config.workflow_config_dir

    staging_root = Path(tempfile.mkdtemp(prefix="notehook-install-"))
    try:
        staged_path = _stage_source(src, staging_root)

        try:
            manifest = parse_manifest(staged_path)
        except ManifestError as exc:
            raise _fail(f"invalid manifest: {exc}") from exc

        final_alias = _validate_alias(alias or manifest.name)
        is_package = staged_path.is_dir()
        final_workflow_path = (
            workflows_dir / final_alias if is_package else workflows_dir / f"{final_alias}.py"
        )
        final_config_path = workflow_config_dir / f"{final_alias}.toml"

        alias_taken = (
            resolve_workflow_path(workflows_dir, final_alias) is not None
            or final_config_path.is_file()
        )
        if alias_taken:
            raise _fail(
                f"alias '{final_alias}' already exists; pass --as to choose a different alias"
            )

        _print_disclosure(manifest, staged_path)

        supplied_inputs = _parse_kv_list(inputs or [], label="input")
        supplied_secrets = _parse_kv_list(secrets or [], label="secret")
        resolved_inputs = _resolve_inputs(manifest, supplied_inputs, yes=yes)
        resolved_secrets = _resolve_secrets(manifest, supplied_secrets, yes=yes)
        resolved_paths = _resolve_paths(manifest, paths, yes=yes)

        workflows_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged_path), str(final_workflow_path))

        _write_install_config(
            final_config_path,
            workflow=manifest.name,
            source=src,
            enabled=True,
            paths=resolved_paths,
            on=None,
            skip_own_changes=False,
            inputs=resolved_inputs,
            secrets=resolved_secrets,
        )
        version_suffix = f" v{manifest.version}" if manifest.version else ""
        console.print(f"[green]Installed[/green] '{final_alias}' ({manifest.name}{version_suffix})")
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _stage_source(src: str, staging_root: Path) -> Path:
    """Clone/copy `src` into (or under) `staging_root`, returning the staged
    workflow path. `staging_root` is a fresh temp dir owned by the caller,
    always cleaned up in the caller's `finally` -- this function itself never
    touches `workflows_dir`, so a failure past this point leaves nothing
    behind (spec §5: no half-installed alias on error)."""
    if _looks_like_git_source(src):
        try:
            _git_clone(src, staging_root)
        except _GitError as exc:
            raise _fail(f"git clone failed: {exc}") from exc
        return staging_root

    src_path = Path(src).expanduser()
    if src_path.is_dir():
        dest = staging_root / (src_path.resolve().name or "workflow")
        shutil.copytree(src_path, dest)
        return dest
    if src_path.is_file() and src_path.suffix == ".py":
        dest = staging_root / src_path.name
        shutil.copy2(src_path, dest)
        return dest
    raise _fail(f"source not found or not a directory/.py file: {src_path}")


# --- configure ---


@workflows_app.command()
def configure(
    alias: Annotated[str, typer.Argument(help="Install alias")],
    inputs: _InputOpt = None,
    secrets: _SecretOpt = None,
    paths: _PathsOpt = None,
    yes: _YesOpt = False,
    config_dir: ConfigDirOpt = None,
) -> None:
    """Re-prompt/re-set inputs, secrets, and paths for an existing install."""
    _validate_alias(alias)
    config = _load(config_dir)
    config_path = config.workflow_config_dir / f"{alias}.toml"
    if not config_path.is_file():
        raise _fail(f"no install config found for alias '{alias}'")
    try:
        current = parse_install_config(config_path)
    except InstallError as exc:
        raise _fail(str(exc)) from exc

    workflow_path = resolve_workflow_path(config.workflows_dir, alias)
    if workflow_path is None:
        raise _fail(f"no workflow code found for alias '{alias}'")
    try:
        manifest = parse_manifest(workflow_path)
    except ManifestError as exc:
        raise _fail(f"invalid manifest: {exc}") from exc

    supplied_inputs = _parse_kv_list(inputs or [], label="input")
    supplied_secrets = _parse_kv_list(secrets or [], label="secret")
    merged_inputs = _resolve_inputs(manifest, {**current.inputs, **supplied_inputs}, yes=yes)
    merged_secrets = _resolve_secrets(manifest, {**current.secrets, **supplied_secrets}, yes=yes)
    merged_paths = list(paths) if paths else list(current.paths)

    original_text = config_path.read_text()
    _write_install_config(
        config_path,
        workflow=current.workflow,
        source=current.source,
        enabled=current.enabled,
        paths=merged_paths,
        on=current.on,
        skip_own_changes=current.skip_own_changes,
        inputs=merged_inputs,
        secrets=merged_secrets,
    )

    result = discover(config.workflows_dir, config.workflow_config_dir)
    entry = result.get(alias)
    if isinstance(entry, BrokenInstall):
        config_path.write_text(original_text)
        config_path.chmod(0o600)
        raise _fail(f"new configuration would leave '{alias}' broken: {entry.error} (not saved)")

    console.print(f"[green]Configured[/green] '{alias}'.")


# --- enable / disable ---


def _set_enabled(alias: str, enabled: bool, config_dir: Path | None) -> None:
    _validate_alias(alias)
    config = _load(config_dir)
    config_path = config.workflow_config_dir / f"{alias}.toml"
    if not config_path.is_file():
        raise _fail(f"no install config found for alias '{alias}'")
    try:
        current = parse_install_config(config_path)
    except InstallError as exc:
        raise _fail(str(exc)) from exc

    _write_install_config(
        config_path,
        workflow=current.workflow,
        source=current.source,
        enabled=enabled,
        paths=current.paths,
        on=current.on,
        skip_own_changes=current.skip_own_changes,
        inputs=current.inputs,
        secrets=current.secrets,
    )
    console.print(f"[green]{alias}[/green] {'enabled' if enabled else 'disabled'}.")


@workflows_app.command()
def enable(alias: Annotated[str, typer.Argument()], config_dir: ConfigDirOpt = None) -> None:
    """Enable an install without reinstalling it."""
    _set_enabled(alias, True, config_dir)


@workflows_app.command()
def disable(alias: Annotated[str, typer.Argument()], config_dir: ConfigDirOpt = None) -> None:
    """Disable an install without uninstalling it."""
    _set_enabled(alias, False, config_dir)


# --- remove ---


@workflows_app.command()
def remove(
    alias: Annotated[str, typer.Argument(help="Install alias")],
    keep_runs: Annotated[
        bool,
        typer.Option(
            "--keep-runs", help="Reserved; run history in events.db is always kept in v1"
        ),
    ] = False,
    yes: _YesOpt = False,
    config_dir: ConfigDirOpt = None,
) -> None:
    """Delete an install's code and config (spec §8).

    `keep_runs` is reserved: run history in events.db is never purged by this
    command in v1 regardless of the flag (see the confirmation prompt below).
    """
    _validate_alias(alias)
    config = _load(config_dir)
    workflow_path = resolve_workflow_path(config.workflows_dir, alias)
    config_path = config.workflow_config_dir / f"{alias}.toml"
    if workflow_path is None and not config_path.is_file():
        raise _fail(f"no install found for alias '{alias}'")

    if not yes:
        confirmed = typer.confirm(
            f"Remove install '{alias}'? Run history for it in events.db is retained "
            "either way (never purged by this command).",
            default=False,
        )
        if not confirmed:
            console.print("Aborted.")
            raise typer.Exit(1)

    if workflow_path is not None:
        if workflow_path.is_dir():
            shutil.rmtree(workflow_path)
        else:
            workflow_path.unlink()
    if config_path.is_file():
        config_path.unlink()
    console.print(f"[green]Removed[/green] '{alias}'.")


# --- update ---


@workflows_app.command()
def update(
    alias: Annotated[str, typer.Argument(help="Install alias")],
    inputs: _InputOpt = None,
    secrets: _SecretOpt = None,
    yes: _YesOpt = False,
    config_dir: ConfigDirOpt = None,
) -> None:
    """`git pull` a git-sourced install, revalidate, and re-prompt for any
    newly required inputs/secrets (spec §5)."""
    _validate_alias(alias)
    config = _load(config_dir)
    config_path = config.workflow_config_dir / f"{alias}.toml"
    if not config_path.is_file():
        raise _fail(f"no install config found for alias '{alias}'")
    try:
        current = parse_install_config(config_path)
    except InstallError as exc:
        raise _fail(str(exc)) from exc

    if not _looks_like_git_source(current.source):
        raise _fail(
            "this install was copied from a local path, not cloned from git; "
            "re-run 'notehook workflows install' to update it"
        )

    workflow_dir = config.workflows_dir / alias
    if not workflow_dir.is_dir():
        raise _fail(f"no workflow directory found at {workflow_dir}")

    try:
        _git_pull(workflow_dir)
    except _GitError as exc:
        raise _fail(f"git pull failed: {exc}") from exc

    try:
        manifest = parse_manifest(workflow_dir)
    except ManifestError as exc:
        raise _fail(f"post-update manifest is invalid: {exc}") from exc

    supplied_inputs = _parse_kv_list(inputs or [], label="input")
    supplied_secrets = _parse_kv_list(secrets or [], label="secret")
    merged_inputs = _resolve_inputs(manifest, {**current.inputs, **supplied_inputs}, yes=yes)
    merged_secrets = _resolve_secrets(manifest, {**current.secrets, **supplied_secrets}, yes=yes)

    _write_install_config(
        config_path,
        workflow=manifest.name,
        source=current.source,
        enabled=current.enabled,
        paths=current.paths,
        on=current.on,
        skip_own_changes=current.skip_own_changes,
        inputs=merged_inputs,
        secrets=merged_secrets,
    )
    version_suffix = f" v{manifest.version}" if manifest.version else ""
    console.print(f"[green]Updated[/green] '{alias}' ({manifest.name}{version_suffix})")


# --- list ---


def _last_run_status_by_install(runs: list[RunRow]) -> dict[str, str]:
    """`all_runs()` is ordered by id ascending, so a plain overwrite while
    iterating leaves the latest run per install."""
    latest: dict[str, str] = {}
    for run in runs:
        latest[run.install] = run.status
    return latest


@workflows_app.command(name="list")
def list_installs(config_dir: ConfigDirOpt = None) -> None:
    """List installed workflows: alias, workflow, enabled, paths, status,
    last run (spec §8)."""
    config = _load(config_dir)
    installs = discover(config.workflows_dir, config.workflow_config_dir)
    last_run_status = _last_run_status_by_install(EventLog(config.events_db_file).all_runs())

    table = Table()
    table.add_column("Alias")
    table.add_column("Workflow")
    table.add_column("Enabled")
    table.add_column("Paths")
    table.add_column("Status")
    table.add_column("Last run")

    for entry_alias in sorted(installs):
        entry = installs[entry_alias]
        last_run = last_run_status.get(entry_alias, "never")
        if isinstance(entry, Install):
            version_suffix = f" v{entry.manifest.version}" if entry.manifest.version else ""
            table.add_row(
                entry_alias,
                f"{entry.manifest.name}{version_suffix}",
                "yes" if entry.config.enabled else "no",
                ", ".join(entry.config.paths),
                "[green]healthy[/green]",
                last_run,
            )
        else:
            table.add_row(
                entry_alias, "-", "-", "-", f"[red]broken: {entry.error}[/red]", last_run
            )

    console.print(table)


# --- Phase 5: run / backfill / logs / serve (spec §6, §8) ---
#
# Operational half of the CLI surface table, appended below `list` in spec
# §8's literal order: run, backfill, logs, serve.


def _lookup_install(config: ClientConfig, alias: str) -> Install:
    """Shared by `run`/`backfill`: resolve one alias to a healthy `Install`,
    failing clearly (not with a KeyError/AttributeError) for a missing or
    broken one."""
    installs = discover(config.workflows_dir, config.workflow_config_dir)
    entry = installs.get(alias)
    if entry is None:
        raise _fail(f"no install found for alias '{alias}'")
    if isinstance(entry, BrokenInstall):
        raise _fail(f"install '{alias}' is broken: {entry.error}")
    return entry


# --- run ---


@workflows_app.command(name="run")
def run_workflow(
    alias: Annotated[str, typer.Argument(help="Install alias")],
    path: Annotated[
        Path, typer.Option("--path", help="File to trigger the workflow on")
    ],
    wait: Annotated[
        bool,
        typer.Option("--wait", help="Run synchronously and wait for the result (CI-friendly)"),
    ] = False,
    config_dir: ConfigDirOpt = None,
) -> None:
    """Manually trigger `alias` on one file: appends a `manual` event
    targeted at it, bypassing its glob/`on` filters (spec §8).

    Without `--wait`, this is fire-and-forget: the event is appended and the
    command returns immediately, to be picked up by a live `serve` (or a
    later `run --wait`/poll). With `--wait`, it acquires the runner lock
    itself, drives one intake+execute step, and reports the outcome -- this
    fails clearly (does not race) if a `serve` (or another `run --wait`) is
    already holding the lock, per the single-runner-process invariant
    (runner.py module docstring, "Own-instance guard").
    """
    _validate_alias(alias)
    config = _load(config_dir)
    sync_root = config.sync_root.expanduser().resolve()

    resolved = path.expanduser().resolve()
    try:
        rel_path = resolved.relative_to(sync_root).as_posix()
    except ValueError as exc:
        raise _fail(f"{resolved} is not inside the sync root {sync_root}") from exc

    _lookup_install(config, alias)

    state = StateDB(config.state_db_file).all()
    event_type = "updated" if rel_path in state else "created"

    if resolved.is_file():
        content_hash = file_md5(resolved)
        size = resolved.stat().st_size
    else:
        content_hash = ""
        size = 0

    event_log = EventLog(config.events_db_file)
    event_id = event_log.append_settled(
        event_type,
        rel_path,
        content_hash,
        size,
        "manual",
        config.equipment_no,
        str(uuid.uuid4()),
        target_install=alias,
    )
    console.print(
        f"Queued manual event [bold]{event_id}[/bold] for '{alias}' ({rel_path}, {event_type})."
    )

    if not wait:
        return

    try:
        with file_lock(config.runner_lock_file):
            runner = Runner(
                event_log,
                config.workflows_dir,
                config.workflow_config_dir,
                sync_root,
                config.equipment_no,
                max_parallel=config.workflows.max_parallel,
            )
            runner.intake_step()
            runner.run_pending()
    except LockError as exc:
        raise _fail(
            f"could not run synchronously: {exc} "
            f"(is 'notehook workflows serve' or another 'run --wait' already running?)"
        ) from exc

    matching = [r for r in event_log.all_runs() if r.event_id == event_id]
    if not matching:
        console.print(
            "[yellow]No run was queued for this event "
            "(install may be disabled or skip_own_changes dropped it).[/yellow]"
        )
        return

    any_failed = False
    for run_row in matching:
        color = "green" if run_row.status == "success" else "red"
        console.print(
            f"  run {run_row.id}: [{color}]{run_row.status}[/{color}] "
            f"(exit={run_row.exit_code}, attempt={run_row.attempt})"
        )
        if run_row.status == "failed":
            any_failed = True
    if any_failed:
        raise typer.Exit(1)


# --- backfill ---


@workflows_app.command()
def backfill(
    alias: Annotated[str, typer.Argument(help="Install alias")],
    glob: _GlobOpt = None,
    config_dir: ConfigDirOpt = None,
) -> None:
    """Append `created`/`backfill` events for every existing file matching
    `alias`'s own trigger paths (narrowed, never widened, by `--glob`) --
    spec §8. Only appends events; never executes anything itself (that's
    `serve`'s or `run --wait`'s job).

    All files queued by one `backfill` invocation share a single `sync_pass`
    uuid (a documented choice: this is one logical batch, not N independent
    passes -- matching how a real sync pass groups its events).
    """
    _validate_alias(alias)
    config = _load(config_dir)
    install = _lookup_install(config, alias)

    extra_patterns = [compile_path_glob(g) for g in (glob or [])]
    local_files = scan_local(config.sync_root)
    event_log = EventLog(config.events_db_file)
    sync_pass = str(uuid.uuid4())

    count = 0
    for rel_path in sorted(local_files):
        local_file = local_files[rel_path]
        if local_file.is_folder:
            continue
        if not install.matches_path(rel_path):
            continue
        if extra_patterns and not any(p.fullmatch(rel_path) for p in extra_patterns):
            continue
        event_log.append_settled(
            "created",
            rel_path,
            local_file.content_hash(),
            local_file.size,
            "backfill",
            "",
            sync_pass,
            target_install=alias,
        )
        count += 1

    console.print(f"Queued [bold]{count}[/bold] backfill event(s) for '{alias}'.")


# --- logs ---

_LOG_DEFAULT_LIMIT = 20
# Follow-mode poll cadence -- deliberately short and distinct from the
# runner's own `poll_interval_seconds` (spec: "NOT the daemon's poll
# interval"); this is purely a display refresh rate.
_LOGS_FOLLOW_POLL_SECONDS = 1.0

_STATUS_COLORS = {
    "success": "green",
    "failed": "red",
    "running": "cyan",
    "retry": "yellow",
    "superseded": "dim",
    "queued": "white",
}


def _filter_runs(runs: list[RunRow], *, alias: str | None, failed: bool) -> list[RunRow]:
    result = runs
    if alias is not None:
        result = [r for r in result if r.install == alias]
    if failed:
        result = [r for r in result if r.status == "failed"]
    return result


def _fmt_epoch_ms(value: int | None) -> str:
    if value is None:
        return "-"
    return datetime.fromtimestamp(value / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def _output_indicator(text: str | None) -> str:
    if not text:
        return "-"
    marker = " (truncated)" if "[truncated:" in text else ""
    return f"{len(text)}b{marker}"


def _runs_table(runs: list[RunRow], events_by_id: dict[int, EventRow]) -> Table:
    table = Table()
    table.add_column("Run")
    table.add_column("Install")
    table.add_column("Path")
    table.add_column("Type/Source")
    table.add_column("Status")
    table.add_column("Attempt")
    table.add_column("Exit")
    table.add_column("Started")
    table.add_column("Finished")
    table.add_column("stdout")
    table.add_column("stderr")

    for run_row in runs:
        event = events_by_id.get(run_row.event_id)
        type_source = f"{event.type}/{event.source}" if event is not None else "-"
        color = _STATUS_COLORS.get(run_row.status, "white")
        table.add_row(
            str(run_row.id),
            run_row.install,
            run_row.rel_path,
            type_source,
            f"[{color}]{run_row.status}[/{color}]",
            str(run_row.attempt),
            str(run_row.exit_code) if run_row.exit_code is not None else "-",
            _fmt_epoch_ms(run_row.started_at),
            _fmt_epoch_ms(run_row.finished_at),
            _output_indicator(run_row.stdout),
            _output_indicator(run_row.stderr),
        )
    return table


def _print_runs(event_log: EventLog, runs: list[RunRow]) -> None:
    events_by_id = {e.id: e for e in event_log.all_events()}
    console.print(_runs_table(runs, events_by_id))


def _logs_follow(event_log: EventLog, *, alias: str | None, failed: bool) -> None:
    """Poll `all_runs()` every `_LOGS_FOLLOW_POLL_SECONDS`, printing only
    rows that are new or whose (status, finished_at) changed since the last
    poll, until Ctrl-C (mirrors `daemon`'s KeyboardInterrupt -> clean-stop
    shape)."""
    seen: dict[int, tuple[str, int | None]] = {}
    try:
        while True:
            runs = _filter_runs(event_log.all_runs(), alias=alias, failed=failed)
            changed = [r for r in runs if seen.get(r.id) != (r.status, r.finished_at)]
            if changed:
                _print_runs(event_log, changed)
            for r in runs:
                seen[r.id] = (r.status, r.finished_at)
            time.sleep(_LOGS_FOLLOW_POLL_SECONDS)
    except KeyboardInterrupt:
        console.print("Stopped.")


@workflows_app.command()
def logs(
    alias: Annotated[
        str | None, typer.Option("--alias", help="Filter to one install alias")
    ] = None,
    failed: Annotated[bool, typer.Option("--failed", help="Only show failed runs")] = False,
    follow: Annotated[
        bool, typer.Option("--follow", help="Poll for new/changed runs until Ctrl-C")
    ] = False,
    limit: Annotated[
        int, typer.Option("-n", "--limit", help="Show the most recent N runs")
    ] = _LOG_DEFAULT_LIMIT,
    config_dir: ConfigDirOpt = None,
) -> None:
    """Run log viewer (spec §8)."""
    config = _load(config_dir)
    event_log = EventLog(config.events_db_file)

    if follow:
        _logs_follow(event_log, alias=alias, failed=failed)
        return

    runs = _filter_runs(event_log.all_runs(), alias=alias, failed=failed)
    if limit > 0:
        runs = runs[-limit:]
    _print_runs(event_log, runs)


# --- serve ---

# Housekeeping runs roughly once per day (spec §6 "Housekeeping"), tracked by
# elapsed wall time rather than tick count so it's independent of
# `poll_interval_seconds`.
_HOUSEKEEPING_INTERVAL_SECONDS = 86_400.0


def _serve_watch_loop(
    workflows_dir: Path, workflow_config_dir: Path, stop: threading.Event, wake: threading.Event
) -> None:
    """Mirrors `daemon.py`'s `_watch_loop` almost exactly: its only job is to
    shorten the wait between polls by setting `wake` on any change under
    `workflows_dir`/`workflow_config_dir`. No incremental "reparse only the
    changed install" bookkeeping is needed here -- `Runner.intake_step()`/
    `run_pending()` already call `installs.discover()` fresh on every call,
    so hot reload is automatic (spec §6 "Hot reload")."""
    workflows_dir.mkdir(parents=True, exist_ok=True)
    workflow_config_dir.mkdir(parents=True, exist_ok=True)
    try:
        for _changes in watch(workflows_dir, workflow_config_dir, stop_event=stop, debounce=1600):
            wake.set()
    except Exception:
        logger.exception("workflow install watcher stopped")


def _serve_loop(
    runner: Runner,
    workflows_cfg: WorkflowsConfig,
    stop: threading.Event,
    wake: threading.Event,
) -> None:
    """One `intake_step` + `run_pending` per tick, woken early by `wake`
    (hot-reload signal) or after `poll_interval_seconds`, with a daily
    housekeeping sweep. On `stop`, any in-flight `run_pending()` finishes
    naturally before the loop exits (it's only re-entered after re-checking
    `stop.is_set()`) -- satisfying "finish running jobs, no new claims"
    without any cancellation machinery."""
    last_sweep = time.monotonic()
    while not stop.is_set():
        runner.intake_step()
        runner.run_pending()
        now = time.monotonic()
        if now - last_sweep >= _HOUSEKEEPING_INTERVAL_SECONDS:
            runner.sweep(workflows_cfg.retention_days)
            last_sweep = now
        wake.wait(timeout=workflows_cfg.poll_interval_seconds)
        wake.clear()


@workflows_app.command()
def serve(config_dir: ConfigDirOpt = None) -> None:
    """Run the workflow runner daemon: poll loop (intake + execute) with hot
    reload and daily housekeeping (spec §6). One runner per config dir at a
    time -- held for this process's entire lifetime via the same
    `runner_lock_file` flock the module docstring's "Own-instance guard"
    describes; a second `serve` (or a `run --wait`) fails clearly instead of
    racing this one.
    """
    logging.basicConfig(level=logging.INFO)
    config = _load(config_dir)
    workflows_cfg = config.workflows

    try:
        with file_lock(config.runner_lock_file):
            event_log = EventLog(config.events_db_file)
            runner = Runner(
                event_log,
                config.workflows_dir,
                config.workflow_config_dir,
                config.sync_root,
                config.equipment_no,
                max_parallel=workflows_cfg.max_parallel,
            )
            console.print(
                f"Serving workflows from [bold]{config.workflows_dir}[/bold], "
                f"polling every {workflows_cfg.poll_interval_seconds}s, "
                f"max_parallel={workflows_cfg.max_parallel}. Ctrl-C to stop."
            )
            recovered = runner.recover_crashed()
            if recovered:
                console.print(f"Recovered {recovered} crashed run(s) from a previous instance.")

            stop = threading.Event()
            wake = threading.Event()
            watcher = threading.Thread(
                target=_serve_watch_loop,
                args=(config.workflows_dir, config.workflow_config_dir, stop, wake),
                daemon=True,
            )
            watcher.start()
            try:
                _serve_loop(runner, workflows_cfg, stop, wake)
            except KeyboardInterrupt:
                stop.set()
                wake.set()
                console.print("Stopped.")
            watcher.join(timeout=5)
    except LockError as exc:
        raise _fail(
            f"could not start: {exc} (is another 'notehook workflows serve' already running?)"
        ) from exc
