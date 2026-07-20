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
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from notehook_cli.config import ClientConfig
from notehook_cli.workflows.events import EventLog, RunRow
from notehook_cli.workflows.installs import (
    BrokenInstall,
    Install,
    InstallError,
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


# Phase 5 (docs/workflow-implementation-plan.md): `serve`, `run`, `backfill`,
# `logs` are appended below this line, in that CLI-surface-table order.
