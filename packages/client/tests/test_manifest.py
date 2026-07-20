"""Workflow manifest parsing (`[tool.notehook]`) -- spec workflow-spec.md §3."""

from pathlib import Path

import pytest

from notehook_cli.workflows.manifest import (
    InputSpec,
    Manifest,
    ManifestError,
    ManifestWarning,
    RetrySpec,
    SecretSpec,
    parse_manifest,
    parse_package,
    parse_single_file,
)

# Verbatim from docs/workflow-spec.md §3.
SPEC_3_EXAMPLE = """# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "pypdf"]
#
# [tool.notehook]
# name = "supernote-to-x4"
# version = "0.2.0"
# description = "Convert synced notes to PDF and push to an Xteink X4"
# timeout = 300                      # seconds; optional, default 300
# suggested_paths = ["Note/ToReader/**"]   # default offered at configure time
#
# [tool.notehook.inputs]
# device_ip = { required = true, description = "X4 IP on the local network" }
# target_folder = { default = "Books" }
#
# [tool.notehook.secrets]
# x4_api_key = { required = false }
# ///
"""

# Verbatim from docs/workflow-spec.md §9.
SPEC_9_EXAMPLE = """# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "websockets"]
#
# [tool.notehook]
# name = "push-to-x4"
# suggested_paths = ["Note/ToReader/**"]
# [tool.notehook.inputs]
# device_ip = { required = true }
# ///
from notehook_workflow import workflow, RetryLater
import requests

@workflow(on=["created", "updated"])
def run(event, config):
    marker = event.path.with_suffix(event.path.suffix + f".{event.content_hash[:8]}.sent")
    if marker.exists():          # idempotency: this exact content already pushed
        return
    try:
        upload_to_x4(event.path, config["device_ip"])   # REST/WS per api.html
    except (requests.ConnectionError, requests.Timeout) as e:
        raise RetryLater(f"X4 unreachable: {e}") from e
    marker.write_text("")
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


# --- spec examples, verbatim ---


def test_spec_3_example_parses(tmp_path: Path) -> None:
    path = _write(tmp_path, "supernote_to_x4.py", SPEC_3_EXAMPLE)
    manifest = parse_single_file(path)
    assert manifest.name == "supernote-to-x4"
    assert manifest.version == "0.2.0"
    assert manifest.description == "Convert synced notes to PDF and push to an Xteink X4"
    assert manifest.timeout == 300
    assert manifest.suggested_paths == ["Note/ToReader/**"]
    assert manifest.inputs == {
        "device_ip": InputSpec(
            required=True, default=None, description="X4 IP on the local network"
        ),
        "target_folder": InputSpec(required=False, default="Books", description=""),
    }
    assert manifest.secrets == {"x4_api_key": SecretSpec(required=False, description="")}
    assert manifest.retry == RetrySpec()
    assert manifest.entry == "workflow.py"


def test_spec_9_example_parses(tmp_path: Path) -> None:
    path = _write(tmp_path, "push_to_x4.py", SPEC_9_EXAMPLE)
    manifest = parse_single_file(path)
    assert manifest.name == "push-to-x4"
    assert manifest.suggested_paths == ["Note/ToReader/**"]
    assert manifest.inputs == {
        "device_ip": InputSpec(required=True, default=None, description=""),
    }
    assert manifest.secrets == {}
    assert manifest.version is None
    assert manifest.description is None
    assert manifest.timeout == 300


# --- defaults / minimal ---


def test_minimal_file_with_no_tool_notehook(tmp_path: Path) -> None:
    path = _write(tmp_path, "my_workflow.py", "print('hello')\n")
    manifest = parse_single_file(path)
    assert manifest == Manifest(name="my_workflow")


def test_name_defaults_to_file_stem(tmp_path: Path) -> None:
    path = _write(tmp_path, "cool_workflow.py", "# nothing here\n")
    manifest = parse_single_file(path)
    assert manifest.name == "cool_workflow"


def test_name_defaults_to_directory_name_for_packages(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "my-cool-package"
    pkg_dir.mkdir()
    manifest = parse_package(pkg_dir)
    assert manifest.name == "my-cool-package"
    assert manifest.entry == "workflow.py"


def test_defaults_match_spec(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.py",
        '# /// script\n# requires-python = ">=3.11"\n# ///\n',
    )
    manifest = parse_single_file(path)
    assert manifest.timeout == 300
    assert manifest.retry == RetrySpec(max_attempts=20, backoff_base=60, backoff_cap=3600)
    assert manifest.suggested_paths == []
    assert manifest.inputs == {}
    assert manifest.secrets == {}
    assert manifest.entry == "workflow.py"
    assert manifest.version is None
    assert manifest.description is None


def test_retry_overrides(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.py",
        "# /// script\n#\n"
        "# [tool.notehook.retry]\n"
        "# max_attempts = 5\n"
        "# backoff_base = 10\n"
        "# backoff_cap = 100\n"
        "# ///\n",
    )
    manifest = parse_single_file(path)
    assert manifest.retry == RetrySpec(max_attempts=5, backoff_base=10, backoff_cap=100)


# --- parse_manifest dispatch (file vs directory) ---


def test_parse_manifest_dispatches_by_path_kind(tmp_path: Path) -> None:
    file_path = _write(tmp_path, "single.py", SPEC_9_EXAMPLE)
    assert parse_manifest(file_path).name == "push-to-x4"

    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "pyproject.toml").write_text(
        '[project]\nname = "pkg"\n\n[tool.notehook]\nname = "pkg-workflow"\n'
    )
    assert parse_manifest(pkg_dir).name == "pkg-workflow"


# --- package: pyproject wins over inline PEP 723 block ---


def test_pyproject_wins_over_inline_pep723(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "pyproject.toml").write_text(
        '[project]\nname = "pkg"\n\n[tool.notehook]\nname = "from-pyproject"\ntimeout = 42\n'
    )
    (pkg_dir / "workflow.py").write_text(
        '# /// script\n# requires-python = ">=3.11"\n#\n'
        '# [tool.notehook]\n# name = "from-inline"\n# timeout = 99\n# ///\n'
        "from notehook_workflow import workflow\n"
    )
    manifest = parse_package(pkg_dir)
    assert manifest.name == "from-pyproject"
    assert manifest.timeout == 42


def test_package_falls_back_to_inline_when_pyproject_has_no_tool_notehook(
    tmp_path: Path,
) -> None:
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "pyproject.toml").write_text('[project]\nname = "pkg"\n')
    (pkg_dir / "workflow.py").write_text(
        '# /// script\n# requires-python = ">=3.11"\n#\n'
        '# [tool.notehook]\n# name = "from-inline"\n# ///\n'
    )
    manifest = parse_package(pkg_dir)
    assert manifest.name == "from-inline"


def test_package_with_no_pyproject_falls_back_to_inline(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "workflow.py").write_text(
        '# /// script\n# requires-python = ">=3.11"\n#\n'
        '# [tool.notehook]\n# name = "from-inline-only"\n# ///\n'
    )
    manifest = parse_package(pkg_dir)
    assert manifest.name == "from-inline-only"


def test_package_custom_entry_from_pyproject(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "pyproject.toml").write_text(
        '[project]\nname = "pkg"\n\n[tool.notehook]\nentry = "main.py"\n'
    )
    manifest = parse_package(pkg_dir)
    assert manifest.entry == "main.py"


def test_package_no_manifest_anywhere_is_valid(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "pyproject.toml").write_text('[project]\nname = "pkg"\n')
    manifest = parse_package(pkg_dir)
    assert manifest == Manifest(name="pkg")


# --- unknown keys warn, never error ---


def test_unknown_top_level_key_warns(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.py",
        '# /// script\n#\n# [tool.notehook]\n# name = "wf"\n# bogus_key = "x"\n# ///\n',
    )
    with pytest.warns(ManifestWarning, match="bogus_key"):
        manifest = parse_single_file(path)
    assert manifest.name == "wf"


def test_unknown_input_key_warns(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.py",
        "# /// script\n#\n"
        "# [tool.notehook.inputs]\n"
        '# foo = { required = true, bogus = 1 }\n'
        "# ///\n",
    )
    with pytest.warns(ManifestWarning, match="bogus"):
        manifest = parse_single_file(path)
    assert manifest.inputs["foo"].required is True


def test_unknown_secret_key_warns(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.py",
        "# /// script\n#\n"
        "# [tool.notehook.secrets]\n"
        '# foo = { required = true, bogus = 1 }\n'
        "# ///\n",
    )
    with pytest.warns(ManifestWarning, match="bogus"):
        manifest = parse_single_file(path)
    assert manifest.secrets["foo"].required is True


def test_unknown_retry_key_warns(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.py",
        "# /// script\n#\n"
        "# [tool.notehook.retry]\n"
        "# max_attempts = 5\n"
        "# bogus = 1\n"
        "# ///\n",
    )
    with pytest.warns(ManifestWarning, match="bogus"):
        manifest = parse_single_file(path)
    assert manifest.retry.max_attempts == 5


# --- error cases: ManifestError ---


def test_malformed_toml_raises_manifest_error(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.py",
        "# /// script\n# this is not = valid [ toml\n# ///\n",
    )
    with pytest.raises(ManifestError):
        parse_single_file(path)


def test_wrong_type_timeout_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.py",
        '# /// script\n#\n# [tool.notehook]\n# timeout = "soon"\n# ///\n',
    )
    with pytest.raises(ManifestError, match="timeout"):
        parse_single_file(path)


def test_wrong_type_timeout_bool_raises(tmp_path: Path) -> None:
    # bool is an int subclass in Python -- must still be rejected.
    path = _write(
        tmp_path,
        "wf.py",
        "# /// script\n#\n# [tool.notehook]\n# timeout = true\n# ///\n",
    )
    with pytest.raises(ManifestError, match="timeout"):
        parse_single_file(path)


def test_wrong_type_suggested_paths_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.py",
        '# /// script\n#\n# [tool.notehook]\n# suggested_paths = "not-a-list"\n# ///\n',
    )
    with pytest.raises(ManifestError, match="suggested_paths"):
        parse_single_file(path)


def test_wrong_type_suggested_paths_element_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.py",
        "# /// script\n#\n# [tool.notehook]\n# suggested_paths = [1, 2]\n# ///\n",
    )
    with pytest.raises(ManifestError, match="suggested_paths"):
        parse_single_file(path)


def test_tool_notehook_not_a_table_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.py",
        '# /// script\n#\n# [tool]\n# notehook = "not-a-table"\n# ///\n',
    )
    with pytest.raises(ManifestError, match="table"):
        parse_single_file(path)


def test_inputs_entry_not_a_table_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.py",
        '# /// script\n#\n# [tool.notehook.inputs]\n# foo = "not-a-table"\n# ///\n',
    )
    with pytest.raises(ManifestError, match="foo"):
        parse_single_file(path)


def test_input_required_wrong_type_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "wf.py",
        "# /// script\n#\n"
        "# [tool.notehook.inputs]\n"
        '# foo = { required = "yes" }\n'
        "# ///\n",
    )
    with pytest.raises(ManifestError, match="required"):
        parse_single_file(path)


def test_multiple_script_blocks_raises(tmp_path: Path) -> None:
    text = (
        "# /// script\n# a = 1\n# ///\n"
        "print('between')\n"
        "# /// script\n# b = 2\n# ///\n"
    )
    path = _write(tmp_path, "wf.py", text)
    with pytest.raises(ManifestError, match="multiple"):
        parse_single_file(path)


def test_pyproject_malformed_raises(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "pyproject.toml").write_text("this [ is not valid")
    with pytest.raises(ManifestError):
        parse_package(pkg_dir)


def test_pyproject_tool_not_a_table_raises(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "pyproject.toml").write_text('tool = "not-a-table"\n')
    with pytest.raises(ManifestError, match="table"):
        parse_package(pkg_dir)
