"""Workflow manifest parsing (`[tool.notehook]`) — spec §3.

Two sources, resolved per spec:

- **Single-file workflow**: the PEP 723 `# /// script` block at the top of
  the `.py` file. Extraction uses the regex from PEP 723's reference
  implementation.
- **Package workflow**: `pyproject.toml`'s `[tool.notehook]` table. If a
  package's entry file *also* carries an inline PEP 723 block (e.g. it used
  to be a single-file workflow), `pyproject.toml` wins.

A file/package with no `[tool.notehook]` table at all is valid (spec §3:
"a minimal one-file workflow needs no `[tool.notehook]` at all") — every
field falls back to its documented default. Unknown keys anywhere in the
table warn (`ManifestWarning`) rather than error; malformed TOML or a field
with the wrong type raises `ManifestError`.
"""

from __future__ import annotations

import re
import tomllib
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "InputSpec",
    "Manifest",
    "ManifestError",
    "ManifestWarning",
    "RetrySpec",
    "SecretSpec",
    "extract_pep723_block_text",
    "parse_manifest",
    "parse_package",
    "parse_pep723_metadata",
    "parse_single_file",
]

# PEP 723 reference-implementation regex (https://peps.python.org/pep-0723/).
_PEP723_REGEX = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)

_DEFAULT_ENTRY = "workflow.py"
_DEFAULT_TIMEOUT = 300
_DEFAULT_RETRY_MAX_ATTEMPTS = 20
_DEFAULT_RETRY_BACKOFF_BASE = 60
_DEFAULT_RETRY_BACKOFF_CAP = 3600

_KNOWN_TOP_KEYS = {
    "name",
    "version",
    "description",
    "entry",
    "timeout",
    "retry",
    "suggested_paths",
    "inputs",
    "secrets",
}
_KNOWN_RETRY_KEYS = {"max_attempts", "backoff_base", "backoff_cap"}
_KNOWN_INPUT_KEYS = {"required", "default", "description"}
_KNOWN_SECRET_KEYS = {"required", "description"}


class ManifestError(Exception):
    """Malformed TOML or a `[tool.notehook]` field with the wrong shape/type."""


class ManifestWarning(UserWarning):
    """An unknown key was present in a `[tool.notehook]` table and ignored."""


@dataclass(frozen=True)
class RetrySpec:
    max_attempts: int = _DEFAULT_RETRY_MAX_ATTEMPTS
    backoff_base: int = _DEFAULT_RETRY_BACKOFF_BASE
    backoff_cap: int = _DEFAULT_RETRY_BACKOFF_CAP


@dataclass(frozen=True)
class InputSpec:
    required: bool = False
    default: Any = None
    description: str = ""


@dataclass(frozen=True)
class SecretSpec:
    required: bool = False
    description: str = ""


@dataclass(frozen=True)
class Manifest:
    name: str
    version: str | None = None
    description: str | None = None
    entry: str = _DEFAULT_ENTRY
    timeout: int = _DEFAULT_TIMEOUT
    retry: RetrySpec = field(default_factory=RetrySpec)
    suggested_paths: list[str] = field(default_factory=list)
    inputs: dict[str, InputSpec] = field(default_factory=dict)
    secrets: dict[str, SecretSpec] = field(default_factory=dict)


def parse_manifest(path: Path) -> Manifest:
    """Parse a manifest from a single-file workflow (`path` is a `.py` file)
    or a package workflow (`path` is a directory)."""
    if path.is_dir():
        return parse_package(path)
    return parse_single_file(path)


def parse_single_file(path: Path) -> Manifest:
    """Parse a single-file workflow's PEP 723 `[tool.notehook]` block.

    `name` defaults to the file stem when absent (spec §3).
    """
    table = _extract_tool_notehook_from_script(path)
    return _build_manifest(table or {}, name_default=path.stem)


def parse_package(dir_path: Path) -> Manifest:
    """Parse a package workflow's manifest.

    `pyproject.toml`'s `[tool.notehook]` wins if present. Otherwise, falls
    back to an inline PEP 723 block in the default entry file
    (`workflow.py`) — the "used to be single-file" case. `name` defaults to
    the directory name when absent (spec §3).
    """
    table = None
    pyproject_path = dir_path / "pyproject.toml"
    if pyproject_path.is_file():
        table = _read_pyproject_tool_notehook(pyproject_path)

    if table is None:
        default_entry_path = dir_path / _DEFAULT_ENTRY
        if default_entry_path.is_file():
            table = _extract_tool_notehook_from_script(default_entry_path)

    return _build_manifest(table or {}, name_default=dir_path.name)


def extract_pep723_block_text(path: Path) -> str | None:
    """Return the raw `# /// script` ... `# ///` block (verbatim, comment
    markers included), or `None` if `path` carries none.

    Used by the harness (`workflows/harness.py`) to copy a single-file
    workflow's PEP 723 metadata into the generated harness script, so
    `uv run` resolves the identical `requires-python`/`dependencies` for
    both. Shares `_PEP723_REGEX` with the manifest-parsing path above rather
    than re-deriving it.
    """
    text = path.read_text()
    matches = [m for m in _PEP723_REGEX.finditer(text) if m.group("type") == "script"]
    if len(matches) > 1:
        raise ManifestError(f"{path}: multiple PEP 723 'script' blocks found")
    if not matches:
        return None
    return matches[0].group(0)


def parse_pep723_metadata(path: Path) -> dict[str, Any] | None:
    """Return the full parsed PEP 723 `# /// script` block (`requires-python`,
    `dependencies`, and any `[tool.*]` tables) as a plain dict, or `None` if
    `path` carries no such block.

    Used by the CLI's install-time disclosure (spec §3: "dependency list")
    to display `requires-python`/`dependencies` without re-deriving the PEP
    723 extraction regex — display purposes only, not validation.
    """
    return _extract_pep723_block(path.read_text())


# --- extraction ---


def _extract_pep723_block(script_text: str, block_type: str = "script") -> dict[str, Any] | None:
    """Extract and parse a PEP 723 metadata block of `block_type`.

    Mirrors the PEP's reference implementation: find `# /// <type>` ...
    `# ///` blocks, strip the `# ` (or `#`) comment prefix from each content
    line, and parse the result as TOML.
    """
    matches = [
        m for m in _PEP723_REGEX.finditer(script_text) if m.group("type") == block_type
    ]
    if len(matches) > 1:
        raise ManifestError(f"multiple PEP 723 '{block_type}' blocks found")
    if not matches:
        return None

    content_lines = matches[0].group("content").splitlines(keepends=True)
    content = "".join(
        line[2:] if line.startswith("# ") else line[1:] for line in content_lines
    )
    try:
        return tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"malformed PEP 723 block: {exc}") from exc


def _extract_tool_notehook_from_script(path: Path) -> dict[str, Any] | None:
    text = path.read_text()
    block = _extract_pep723_block(text)
    if block is None:
        return None
    return _tool_notehook_table(block, source=str(path))


def _read_pyproject_tool_notehook(path: Path) -> dict[str, Any] | None:
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"malformed {path}: {exc}") from exc
    return _tool_notehook_table(data, source=str(path))


def _tool_notehook_table(data: dict[str, Any], *, source: str) -> dict[str, Any] | None:
    tool = data.get("tool", {})
    if not isinstance(tool, dict):
        raise ManifestError(f"{source}: [tool] must be a table")
    notehook = tool.get("notehook")
    if notehook is None:
        return None
    if not isinstance(notehook, dict):
        raise ManifestError(f"{source}: [tool.notehook] must be a table")
    return notehook


# --- table -> Manifest ---


def _build_manifest(table: dict[str, Any], *, name_default: str) -> Manifest:
    _warn_unknown_keys(table, _KNOWN_TOP_KEYS, "[tool.notehook]")

    return Manifest(
        name=_expect_str(table, "name", "[tool.notehook]", default=name_default),
        version=_expect_opt_str(table, "version", "[tool.notehook]"),
        description=_expect_opt_str(table, "description", "[tool.notehook]"),
        entry=_expect_str(table, "entry", "[tool.notehook]", default=_DEFAULT_ENTRY),
        timeout=_expect_int(table, "timeout", "[tool.notehook]", default=_DEFAULT_TIMEOUT),
        retry=_build_retry(table.get("retry", {})),
        suggested_paths=_expect_str_list(table, "suggested_paths", "[tool.notehook]"),
        inputs=_build_inputs(table.get("inputs", {})),
        secrets=_build_secrets(table.get("secrets", {})),
    )


def _build_retry(value: Any) -> RetrySpec:
    if not isinstance(value, dict):
        raise ManifestError(
            f"[tool.notehook.retry] must be a table, got {type(value).__name__}"
        )
    _warn_unknown_keys(value, _KNOWN_RETRY_KEYS, "[tool.notehook.retry]")
    return RetrySpec(
        max_attempts=_expect_int(
            value, "max_attempts", "[tool.notehook.retry]", default=_DEFAULT_RETRY_MAX_ATTEMPTS
        ),
        backoff_base=_expect_int(
            value, "backoff_base", "[tool.notehook.retry]", default=_DEFAULT_RETRY_BACKOFF_BASE
        ),
        backoff_cap=_expect_int(
            value, "backoff_cap", "[tool.notehook.retry]", default=_DEFAULT_RETRY_BACKOFF_CAP
        ),
    )


def _build_inputs(value: Any) -> dict[str, InputSpec]:
    if not isinstance(value, dict):
        raise ManifestError(
            f"[tool.notehook.inputs] must be a table, got {type(value).__name__}"
        )
    result: dict[str, InputSpec] = {}
    for name, spec in value.items():
        context = f"[tool.notehook.inputs.{name}]"
        if not isinstance(spec, dict):
            raise ManifestError(f"{context} must be a table, got {type(spec).__name__}")
        _warn_unknown_keys(spec, _KNOWN_INPUT_KEYS, context)
        result[name] = InputSpec(
            required=_expect_bool(spec, "required", context, default=False),
            default=spec.get("default"),
            description=_expect_str(spec, "description", context, default=""),
        )
    return result


def _build_secrets(value: Any) -> dict[str, SecretSpec]:
    if not isinstance(value, dict):
        raise ManifestError(
            f"[tool.notehook.secrets] must be a table, got {type(value).__name__}"
        )
    result: dict[str, SecretSpec] = {}
    for name, spec in value.items():
        context = f"[tool.notehook.secrets.{name}]"
        if not isinstance(spec, dict):
            raise ManifestError(f"{context} must be a table, got {type(spec).__name__}")
        _warn_unknown_keys(spec, _KNOWN_SECRET_KEYS, context)
        result[name] = SecretSpec(
            required=_expect_bool(spec, "required", context, default=False),
            description=_expect_str(spec, "description", context, default=""),
        )
    return result


# --- typed field helpers ---


def _warn_unknown_keys(table: dict[str, Any], known: set[str], context: str) -> None:
    unknown = sorted(set(table) - known)
    for key in unknown:
        warnings.warn(f"{context}: unknown key '{key}' ignored", ManifestWarning, stacklevel=3)


def _expect_str(table: dict[str, Any], key: str, context: str, *, default: str) -> str:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, str):
        raise ManifestError(f"{context}.{key} must be a string, got {type(value).__name__}")
    return value


def _expect_opt_str(table: dict[str, Any], key: str, context: str) -> str | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, str):
        raise ManifestError(f"{context}.{key} must be a string, got {type(value).__name__}")
    return value


def _expect_int(table: dict[str, Any], key: str, context: str, *, default: int) -> int:
    if key not in table:
        return default
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(f"{context}.{key} must be an integer, got {type(value).__name__}")
    return value


def _expect_bool(table: dict[str, Any], key: str, context: str, *, default: bool) -> bool:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, bool):
        raise ManifestError(f"{context}.{key} must be a bool, got {type(value).__name__}")
    return value


def _expect_str_list(table: dict[str, Any], key: str, context: str) -> list[str]:
    if key not in table:
        return []
    value = table[key]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ManifestError(f"{context}.{key} must be a list of strings")
    return list(value)
