"""Install config + install/workflow pairing -- spec workflow-spec.md §5."""

from pathlib import Path

import pytest

from notehook_cli.workflows.installs import (
    BrokenInstall,
    Install,
    InstallWarning,
    compile_path_glob,
    discover,
)

# --- path glob matcher ---


@pytest.mark.parametrize(
    ("pattern", "rel_path", "expected"),
    [
        ("Note/ToReader/**", "Note/ToReader/a/b.pdf", True),
        ("Note/ToReader/**", "Note/ToReader/note.pdf", True),
        ("Note/ToReader/**", "Note/ToReader", True),
        ("Note/ToReader/**", "Note/Other/x.pdf", False),
        ("Note/ToReader/**", "Note/ToReaderExtra/x.pdf", False),
        ("**/*.pdf", "note.pdf", True),
        ("**/*.pdf", "a/b/note.pdf", True),
        ("**/*.pdf", "a/b/note.txt", False),
        ("a/**/b.txt", "a/b.txt", True),
        ("a/**/b.txt", "a/x/b.txt", True),
        ("a/**/b.txt", "a/x/y/b.txt", True),
        ("a/**/b.txt", "ab.txt", False),
        ("*.pdf", "note.pdf", True),
        ("*.pdf", "a/note.pdf", False),  # single '*' never crosses '/'
        ("Note/*.pdf", "Note/note.pdf", True),
        ("Note/*.pdf", "Note/sub/note.pdf", False),
        ("no?e.txt", "note.txt", True),
        ("no?e.txt", "noaae.txt", False),
        ("file[0-9].txt", "file3.txt", True),
        ("file[0-9].txt", "filea.txt", False),
        ("file[!0-9].txt", "filea.txt", True),
        ("file[!0-9].txt", "file3.txt", False),
        ("a/**/**/b.txt", "a/x/y/b.txt", True),  # consecutive ** collapse
        ("a[.txt", "a[.txt", True),  # unterminated bracket -> literal '['
    ],
)
def test_glob_matcher(pattern: str, rel_path: str, expected: bool) -> None:
    assert (compile_path_glob(pattern).fullmatch(rel_path) is not None) is expected


# --- fixtures / helpers ---


def _pep723_block(table_text: str) -> str:
    lines = ['# /// script', '# requires-python = ">=3.11"', "# dependencies = []", "#"]
    for line in table_text.splitlines():
        lines.append(f"# {line}" if line else "#")
    lines.append("# ///")
    return "\n".join(lines) + "\n"


_HANDLER_BODY = (
    "from notehook_workflow import workflow\n\n\n@workflow()\ndef run(event, config):\n    pass\n"
)


def _write_single_file(workflows_dir: Path, alias: str, manifest_toml: str) -> Path:
    workflows_dir.mkdir(parents=True, exist_ok=True)
    path = workflows_dir / f"{alias}.py"
    path.write_text(_pep723_block(manifest_toml) + "\n" + _HANDLER_BODY)
    return path


def _write_config(workflow_config_dir: Path, alias: str, config_toml: str) -> Path:
    workflow_config_dir.mkdir(parents=True, exist_ok=True)
    path = workflow_config_dir / f"{alias}.toml"
    path.write_text(config_toml)
    return path


_BASE_MANIFEST = """[tool.notehook]
name = "demo"
"""

_BASE_CONFIG = """workflow = "demo"
paths = ["Note/ToReader/**"]
"""


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "workflows", tmp_path / "workflow-config"


# --- healthy install ---


def test_healthy_single_file_install(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)
    _write_single_file(workflows_dir, "demo", _BASE_MANIFEST)
    _write_config(config_dir, "demo", _BASE_CONFIG)

    result = discover(workflows_dir, config_dir)
    assert set(result) == {"demo"}
    install = result["demo"]
    assert isinstance(install, Install)
    assert install.alias == "demo"
    assert install.manifest.name == "demo"
    assert install.config.enabled is True
    assert install.package_dir is None
    assert install.entry_file == workflows_dir / "demo.py"
    assert install.matches_path("Note/ToReader/a.pdf")
    assert not install.matches_path("Note/Other/a.pdf")


def test_enabled_flag_default_true_and_explicit_false(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)
    _write_single_file(workflows_dir, "a", _BASE_MANIFEST)
    _write_config(config_dir, "a", _BASE_CONFIG)
    _write_single_file(workflows_dir, "b", _BASE_MANIFEST.replace("demo", "other"))
    _write_config(
        config_dir, "b", _BASE_CONFIG.replace("demo", "other") + "enabled = false\n"
    )

    result = discover(workflows_dir, config_dir)
    a = result["a"]
    b = result["b"]
    assert isinstance(a, Install)
    assert isinstance(b, Install)
    assert a.config.enabled is True
    assert b.config.enabled is False


# --- package install ---


def test_healthy_package_install_custom_entry(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)
    pkg_dir = workflows_dir / "pkg-demo"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "pyproject.toml").write_text(
        """[project]
name = "pkg-demo"
version = "0.1.0"
requires-python = ">=3.12"

[tool.notehook]
name = "pkg-demo"
entry = "main.py"
"""
    )
    (pkg_dir / "main.py").write_text(_HANDLER_BODY)
    _write_config(config_dir, "pkg-demo", 'workflow = "pkg-demo"\npaths = ["**"]\n')

    result = discover(workflows_dir, config_dir)
    install = result["pkg-demo"]
    assert isinstance(install, Install)
    assert install.package_dir == pkg_dir
    assert install.entry_file == pkg_dir / "main.py"


# --- broken: validation matrix ---


def test_name_mismatch_is_broken(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)
    _write_single_file(workflows_dir, "demo", _BASE_MANIFEST)
    _write_config(config_dir, "demo", 'workflow = "not-demo"\npaths = ["**"]\n')

    result = discover(workflows_dir, config_dir)
    broken = result["demo"]
    assert isinstance(broken, BrokenInstall)
    assert "does not match" in broken.error


def test_missing_required_input_is_broken(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)
    manifest = """[tool.notehook]
name = "demo"

[tool.notehook.inputs]
device_ip = { required = true }
"""
    _write_single_file(workflows_dir, "demo", manifest)
    _write_config(config_dir, "demo", _BASE_CONFIG)

    result = discover(workflows_dir, config_dir)
    broken = result["demo"]
    assert isinstance(broken, BrokenInstall)
    assert "device_ip" in broken.error


def test_missing_required_secret_is_broken(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)
    manifest = """[tool.notehook]
name = "demo"

[tool.notehook.secrets]
x4_api_key = { required = true }
"""
    _write_single_file(workflows_dir, "demo", manifest)
    _write_config(config_dir, "demo", _BASE_CONFIG)

    result = discover(workflows_dir, config_dir)
    broken = result["demo"]
    assert isinstance(broken, BrokenInstall)
    assert "x4_api_key" in broken.error


def test_required_secret_satisfied_is_healthy(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)
    manifest = """[tool.notehook]
name = "demo"

[tool.notehook.secrets]
x4_api_key = { required = true }
"""
    _write_single_file(workflows_dir, "demo", manifest)
    _write_config(config_dir, "demo", _BASE_CONFIG + '\n[secrets]\nx4_api_key = "shh"\n')

    result = discover(workflows_dir, config_dir)
    install = result["demo"]
    assert isinstance(install, Install)
    assert install.config.secrets == {"x4_api_key": "shh"}


def test_defaults_filled_into_resolved_config(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)
    manifest = """[tool.notehook]
name = "demo"

[tool.notehook.inputs]
target_folder = { default = "Books" }
device_ip = { required = true }
"""
    _write_single_file(workflows_dir, "demo", manifest)
    _write_config(
        config_dir,
        "demo",
        _BASE_CONFIG + '\n[inputs]\ndevice_ip = "192.168.1.50"\n',
    )

    result = discover(workflows_dir, config_dir)
    install = result["demo"]
    assert isinstance(install, Install)
    assert install.resolved_config == {"target_folder": "Books", "device_ip": "192.168.1.50"}


def test_configured_input_overrides_default(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)
    manifest = """[tool.notehook]
name = "demo"

[tool.notehook.inputs]
target_folder = { default = "Books" }
"""
    _write_single_file(workflows_dir, "demo", manifest)
    _write_config(
        config_dir,
        "demo",
        _BASE_CONFIG + '\n[inputs]\ntarget_folder = "Notes"\n',
    )

    result = discover(workflows_dir, config_dir)
    install = result["demo"]
    assert isinstance(install, Install)
    assert install.resolved_config == {"target_folder": "Notes"}


def test_unknown_input_key_warns_but_keeps_going(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)
    _write_single_file(workflows_dir, "demo", _BASE_MANIFEST)
    _write_config(
        config_dir,
        "demo",
        _BASE_CONFIG + '\n[inputs]\nnot_declared = "whatever"\n',
    )

    with pytest.warns(InstallWarning, match="not_declared"):
        result = discover(workflows_dir, config_dir)
    install = result["demo"]
    assert isinstance(install, Install)
    # still installed (validation didn't fail), unknown key carried through
    assert install.resolved_config == {"not_declared": "whatever"}


def test_invalid_on_value_is_broken(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)
    _write_single_file(workflows_dir, "demo", _BASE_MANIFEST)
    _write_config(config_dir, "demo", _BASE_CONFIG + 'on = ["created", "sideways"]\n')

    result = discover(workflows_dir, config_dir)
    broken = result["demo"]
    assert isinstance(broken, BrokenInstall)
    assert "sideways" in broken.error


def test_on_narrowing_is_stored_verbatim(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)
    _write_single_file(workflows_dir, "demo", _BASE_MANIFEST)
    _write_config(config_dir, "demo", _BASE_CONFIG + 'on = ["deleted"]\n')

    result = discover(workflows_dir, config_dir)
    install = result["demo"]
    assert isinstance(install, Install)
    assert install.config.on == ["deleted"]


def test_malformed_toml_is_broken(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)
    _write_single_file(workflows_dir, "demo", _BASE_MANIFEST)
    _write_config(config_dir, "demo", "this is not [ valid toml")

    result = discover(workflows_dir, config_dir)
    broken = result["demo"]
    assert isinstance(broken, BrokenInstall)


def test_missing_paths_key_is_broken(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)
    _write_single_file(workflows_dir, "demo", _BASE_MANIFEST)
    _write_config(config_dir, "demo", 'workflow = "demo"\n')

    result = discover(workflows_dir, config_dir)
    broken = result["demo"]
    assert isinstance(broken, BrokenInstall)
    assert "paths" in broken.error


# --- orphans ---


def test_orphan_config_without_workflow_dir(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)
    _write_config(config_dir, "ghost", _BASE_CONFIG)

    result = discover(workflows_dir, config_dir)
    broken = result["ghost"]
    assert isinstance(broken, BrokenInstall)
    assert "no workflow code" in broken.error


def test_orphan_workflow_dir_without_config(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)
    _write_single_file(workflows_dir, "ghost", _BASE_MANIFEST)

    result = discover(workflows_dir, config_dir)
    broken = result["ghost"]
    assert isinstance(broken, BrokenInstall)
    assert "no install config" in broken.error


# --- mixed matrix / discover never raises ---


def test_discover_mixed_healthy_and_broken_no_raise(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)

    # healthy
    _write_single_file(workflows_dir, "healthy", _BASE_MANIFEST.replace("demo", "healthy"))
    _write_config(config_dir, "healthy", _BASE_CONFIG.replace("demo", "healthy"))

    # broken: name mismatch
    _write_single_file(workflows_dir, "broken", _BASE_MANIFEST.replace("demo", "broken"))
    _write_config(config_dir, "broken", 'workflow = "wrong-name"\npaths = ["**"]\n')

    # orphan config
    _write_config(config_dir, "orphan-config", _BASE_CONFIG.replace("demo", "orphan-config"))

    # orphan workflow dir
    _write_single_file(
        workflows_dir, "orphan-code", _BASE_MANIFEST.replace("demo", "orphan-code")
    )

    result = discover(workflows_dir, config_dir)
    assert set(result) == {"healthy", "broken", "orphan-config", "orphan-code"}
    assert isinstance(result["healthy"], Install)
    assert isinstance(result["broken"], BrokenInstall)
    assert isinstance(result["orphan-config"], BrokenInstall)
    assert isinstance(result["orphan-code"], BrokenInstall)


def test_discover_on_empty_dirs_returns_empty(tmp_path: Path) -> None:
    workflows_dir, config_dir = _dirs(tmp_path)
    assert discover(workflows_dir, config_dir) == {}
