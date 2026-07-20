"""Install config + install/workflow pairing — spec §5.

Layout on disk (paths derive from `ClientConfig`, decision D2 — never
hardcode `~/.config/notehook/…`):

```
workflows/<alias>/        # or workflows/<alias>.py for a single-file install
                           # git clone (or copied local dir) — code only
workflow-config/<alias>.toml   # local config, 0600, never committed
```

`discover()` pairs each alias's `workflow-config/<alias>.toml` with the
manifest parsed from its `workflows/<alias>` entry (via `manifest.py` —
this module never re-parses manifests itself) and validates the pairing
per spec §5/§2: name mismatch, missing required inputs/secrets, and
unknown-workflow-code/unknown-config orphans all fail *before* any
subprocess would spawn, surfacing as `BrokenInstall` rather than raising —
one bad install must never take the runner down (spec §6 "Hot reload").
"""

from __future__ import annotations

import re
import tomllib
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .manifest import Manifest, ManifestError, parse_manifest

__all__ = [
    "BrokenInstall",
    "Install",
    "InstallConfig",
    "InstallError",
    "InstallWarning",
    "compile_path_glob",
    "discover",
    "parse_install_config",
    "resolve_workflow_path",
]

_VALID_ON_TYPES = frozenset({"created", "updated", "deleted"})


class InstallError(Exception):
    """Malformed `workflow-config/<alias>.toml` (wrong TOML, wrong field
    shape, or a missing required key like `workflow`/`paths`)."""


class InstallWarning(UserWarning):
    """An `[inputs]`/`[secrets]` key in an install config has no matching
    declaration in the workflow's manifest and was ignored for validation
    purposes (kept in the resolved config passed to the workflow)."""


@dataclass(frozen=True)
class InstallConfig:
    """One `workflow-config/<alias>.toml`, parsed but not yet cross-checked
    against the workflow's manifest (spec §5)."""

    workflow: str
    source: str = ""
    enabled: bool = True
    paths: list[str] = field(default_factory=list)
    on: list[str] | None = None
    skip_own_changes: bool = False
    inputs: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Install:
    """A validated, ready-to-run install: config + manifest + resolved
    entry point. `package_dir` is `None` for a single-file install."""

    alias: str
    config: InstallConfig
    manifest: Manifest
    entry_file: Path
    package_dir: Path | None
    resolved_config: dict[str, Any]
    path_patterns: tuple[re.Pattern[str], ...]

    def matches_path(self, rel_path: str) -> bool:
        """True if `rel_path` (posix, relative to the sync root) matches any
        of this install's `paths` globs (spec §5, the authoritative trigger
        binding — the runner, not this module, additionally applies the
        effective `on` filter)."""
        return any(pattern.fullmatch(rel_path) is not None for pattern in self.path_patterns)


@dataclass(frozen=True)
class BrokenInstall:
    """An alias whose config/workflow-code pairing failed validation, or
    whose config or code is missing entirely (orphan). Carried through
    `discover()` rather than raised — consumed by `list`/logs (Phase 4) and
    skipped at intake (Phase 3e)."""

    alias: str
    error: str


def discover(workflows_dir: Path, workflow_config_dir: Path) -> dict[str, Install | BrokenInstall]:
    """Resolve every alias found under `workflows_dir` and/or
    `workflow_config_dir` into an `Install` or a `BrokenInstall`.

    Never raises for a single bad install — malformed TOML, a malformed
    manifest, a name mismatch, missing required inputs/secrets, or an
    orphaned config/workflow-dir all become `BrokenInstall` entries so one
    broken install can't stop discovery of the rest (spec §6 hot reload).
    """
    aliases = _collect_aliases(workflows_dir, workflow_config_dir)
    return {alias: _load_install(alias, workflows_dir, workflow_config_dir) for alias in aliases}


def compile_path_glob(pattern: str) -> re.Pattern[str]:
    """Compile one `paths` glob into a regex matched (via `fullmatch`)
    against a posix `rel_path`.

    `*`/`?` match within a single path segment only (never across `/`);
    `**` matches zero or more whole path segments, absorbing the slash on
    whichever side(s) it borders — e.g. `Note/ToReader/**` matches
    `Note/ToReader/a/b.pdf` (and `Note/ToReader` itself) but not
    `Note/Other/x.pdf`. `pathlib.PurePosixPath.full_match`'s `**` semantics
    land in Python 3.13; this repo targets 3.12+, so this is a small
    hand-rolled equivalent instead.
    """
    raw_segments = pattern.split("/")
    segments: list[str] = []
    for seg in raw_segments:
        if seg == "**" and segments and segments[-1] == "**":
            continue  # collapse consecutive ** (e.g. "a/**/**/b")
        segments.append(seg)

    parts: list[str] = []
    n = len(segments)
    for idx, seg in enumerate(segments):
        is_first = idx == 0
        is_last = idx == n - 1
        if seg == "**":
            if is_first and is_last:
                parts.append(".*")
            elif is_first:
                parts.append("(?:.*/)?")
            elif is_last:
                parts.append("(?:/.*)?")
            else:
                parts.append("/(?:.*/)?")
        else:
            prev_seg = segments[idx - 1] if idx > 0 else None
            if idx > 0 and prev_seg != "**":
                parts.append("/")
            parts.append(_translate_segment(seg))
    return re.compile("^" + "".join(parts) + "$")


def _translate_segment(segment: str) -> str:
    """fnmatch-style translation of one path segment (`*`, `?`, `[seq]`),
    restricted to never match `/` — unlike `fnmatch.translate`, which treats
    `*` as matching any character."""
    out: list[str] = []
    i, n = 0, len(segment)
    while i < n:
        c = segment[i]
        i += 1
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = i
            if j < n and segment[j] == "!":
                j += 1
            if j < n and segment[j] == "]":
                j += 1
            while j < n and segment[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape("["))
            else:
                stuff = segment[i:j]
                i = j + 1
                if stuff.startswith("!"):
                    stuff = "^" + stuff[1:]
                elif stuff.startswith("^"):
                    stuff = "\\" + stuff
                out.append(f"[{stuff}]")
        else:
            out.append(re.escape(c))
    return "".join(out)


# --- alias resolution ---


def _collect_aliases(workflows_dir: Path, workflow_config_dir: Path) -> set[str]:
    aliases: set[str] = set()
    if workflows_dir.is_dir():
        for child in workflows_dir.iterdir():
            if child.is_dir():
                aliases.add(child.name)
            elif child.is_file() and child.suffix == ".py":
                aliases.add(child.stem)
    if workflow_config_dir.is_dir():
        for child in workflow_config_dir.glob("*.toml"):
            aliases.add(child.stem)
    return aliases


def _resolve_workflow_path(workflows_dir: Path, alias: str) -> Path | None:
    """A package install is a directory `workflows/<alias>/`; a single-file
    install is `workflows/<alias>.py`. Directory wins if somehow both exist
    (shouldn't happen from `install`, but keeps resolution deterministic)."""
    dir_path = workflows_dir / alias
    if dir_path.is_dir():
        return dir_path
    file_path = workflows_dir / f"{alias}.py"
    if file_path.is_file():
        return file_path
    return None


def resolve_workflow_path(workflows_dir: Path, alias: str) -> Path | None:
    """Public wrapper around `_resolve_workflow_path` (Phase 4 CLI verbs need
    it directly: `configure`/`enable`/`disable`/`remove`/`update` operate on
    one alias's code location without running the rest of `discover()`'s
    validation)."""
    return _resolve_workflow_path(workflows_dir, alias)


# --- load + validate one install ---


def _load_install(
    alias: str, workflows_dir: Path, workflow_config_dir: Path
) -> Install | BrokenInstall:
    config_path = workflow_config_dir / f"{alias}.toml"
    workflow_path = _resolve_workflow_path(workflows_dir, alias)

    if not config_path.is_file():
        return BrokenInstall(alias, f"no install config found at {config_path}")
    if workflow_path is None:
        return BrokenInstall(
            alias, f"no workflow code found for alias '{alias}' under {workflows_dir}"
        )

    try:
        install_config = _parse_install_config(config_path)
    except InstallError as exc:
        return BrokenInstall(alias, str(exc))

    try:
        manifest = parse_manifest(workflow_path)
    except ManifestError as exc:
        return BrokenInstall(alias, str(exc))

    if manifest.name != install_config.workflow:
        return BrokenInstall(
            alias,
            f"manifest name '{manifest.name}' does not match install config "
            f"workflow '{install_config.workflow}'",
        )

    package_dir = workflow_path if workflow_path.is_dir() else None
    entry_file = (workflow_path / manifest.entry) if package_dir is not None else workflow_path
    if not entry_file.is_file():
        return BrokenInstall(alias, f"entry file not found: {entry_file}")

    if install_config.on is not None:
        invalid_on = sorted(set(install_config.on) - _VALID_ON_TYPES)
        if invalid_on:
            return BrokenInstall(
                alias,
                f"invalid 'on' value(s) {invalid_on}; "
                f"must be a subset of {sorted(_VALID_ON_TYPES)}",
            )

    try:
        path_patterns = tuple(compile_path_glob(p) for p in install_config.paths)
    except re.error as exc:
        return BrokenInstall(alias, f"invalid path pattern: {exc}")

    resolved_config, error = _resolve_inputs(alias, manifest, install_config)
    if error is not None:
        return BrokenInstall(alias, error)

    secret_error = _validate_secrets(alias, manifest, install_config)
    if secret_error is not None:
        return BrokenInstall(alias, secret_error)

    return Install(
        alias=alias,
        config=install_config,
        manifest=manifest,
        entry_file=entry_file,
        package_dir=package_dir,
        resolved_config=resolved_config,
        path_patterns=path_patterns,
    )


def _resolve_inputs(
    alias: str, manifest: Manifest, install_config: InstallConfig
) -> tuple[dict[str, Any], str | None]:
    """Build the resolved config dict: configured values override manifest
    defaults; a required input with neither is a broken install (spec §2:
    misconfiguration fails before spawn, not a retryable runtime error).
    Unknown keys (present in config, absent from the manifest) warn but are
    kept in the resolved dict."""
    resolved: dict[str, Any] = {}
    for name, spec in manifest.inputs.items():
        if name in install_config.inputs:
            resolved[name] = install_config.inputs[name]
        elif spec.default is not None:
            resolved[name] = spec.default
        elif spec.required:
            return {}, f"missing required input '{name}'"

    unknown = sorted(set(install_config.inputs) - set(manifest.inputs))
    for name in unknown:
        warnings.warn(
            f"install '{alias}': unknown input '{name}' not declared by the workflow manifest",
            InstallWarning,
            stacklevel=2,
        )
        resolved[name] = install_config.inputs[name]

    return resolved, None


def _validate_secrets(alias: str, manifest: Manifest, install_config: InstallConfig) -> str | None:
    for name, spec in manifest.secrets.items():
        if spec.required and name not in install_config.secrets:
            return f"missing required secret '{name}'"

    unknown = sorted(set(install_config.secrets) - set(manifest.secrets))
    for name in unknown:
        warnings.warn(
            f"install '{alias}': unknown secret '{name}' not declared by the workflow manifest",
            InstallWarning,
            stacklevel=2,
        )
    return None


# --- install config TOML parsing ---


def _parse_install_config(path: Path) -> InstallConfig:
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise InstallError(f"malformed {path}: {exc}") from exc

    workflow = data.get("workflow")
    if not isinstance(workflow, str) or not workflow:
        raise InstallError(f"{path}: 'workflow' is required and must be a non-empty string")

    source = data.get("source", "")
    if not isinstance(source, str):
        raise InstallError(f"{path}: 'source' must be a string")

    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise InstallError(f"{path}: 'enabled' must be a bool")

    paths = data.get("paths")
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise InstallError(f"{path}: 'paths' is required and must be a list of strings")

    on_value = data.get("on")
    on: list[str] | None
    if on_value is None:
        on = None
    elif isinstance(on_value, list) and all(isinstance(v, str) for v in on_value):
        on = list(on_value)
    else:
        raise InstallError(f"{path}: 'on' must be a list of strings")

    skip_own_changes = data.get("skip_own_changes", False)
    if not isinstance(skip_own_changes, bool):
        raise InstallError(f"{path}: 'skip_own_changes' must be a bool")

    inputs = data.get("inputs", {})
    if not isinstance(inputs, dict):
        raise InstallError(f"{path}: [inputs] must be a table")

    secrets = data.get("secrets", {})
    if not isinstance(secrets, dict) or not all(isinstance(v, str) for v in secrets.values()):
        raise InstallError(f"{path}: [secrets] must be a table of strings")

    return InstallConfig(
        workflow=workflow,
        source=source,
        enabled=enabled,
        paths=list(paths),
        on=on,
        skip_own_changes=skip_own_changes,
        inputs=dict(inputs),
        secrets=dict(secrets),
    )


def parse_install_config(path: Path) -> InstallConfig:
    """Public wrapper around `_parse_install_config`, for the same reason as
    `resolve_workflow_path` above: the Phase 4 CLI verbs need to load one
    `<alias>.toml` on its own (e.g. to merge in new values before rewriting)
    without going through the full `discover()` pairing/validation pass."""
    return _parse_install_config(path)
