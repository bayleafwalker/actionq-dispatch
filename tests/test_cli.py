from __future__ import annotations

import ast
from pathlib import Path
import tomllib

import pytest
from click.testing import CliRunner

from actionq_dispatcher import __version__
from actionq_dispatcher.cli import RETIREMENT_MESSAGE, cli


ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    "arguments",
    (
        [],
        ["--config", "/tmp/actionq.toml"],
        ["--actionq-daemon", "/definitely/missing/actionq-daemon"],
        [
            "--actionq-daemon",
            "/definitely/missing/actionq-daemon",
            "--config",
            "/tmp/actionq.toml",
        ],
    ),
)
def test_every_legacy_invocation_fails_with_the_same_retirement_message(arguments):
    result = CliRunner().invoke(cli, arguments)

    assert result.exit_code == 1
    assert RETIREMENT_MESSAGE in result.output


def test_explicit_legacy_executable_is_never_started(tmp_path):
    marker = tmp_path / "executed"
    executable = tmp_path / "actionq-daemon"
    executable.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    executable.chmod(0o755)

    result = CliRunner().invoke(cli, ["--actionq-daemon", str(executable)])

    assert result.exit_code == 1
    assert RETIREMENT_MESSAGE in result.output
    assert not marker.exists()


def test_legacy_environment_executable_is_never_resolved_or_started(tmp_path):
    marker = tmp_path / "executed-from-environment"
    executable = tmp_path / "actionq-daemon"
    executable.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    executable.chmod(0o755)

    result = CliRunner().invoke(cli, [], env={"ACTIONQ_DAEMON_BIN": str(executable)})

    assert result.exit_code == 1
    assert RETIREMENT_MESSAGE in result.output
    assert not marker.exists()


def test_tombstone_source_has_no_process_or_executable_resolution_boundary():
    source_path = ROOT / "actionq_dispatcher/cli.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imports.isdisjoint({"os", "shutil", "subprocess"})


def test_release_metadata_publishes_only_the_retirement_tombstone():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert metadata["version"] == __version__ == "0.2.0"
    assert metadata["scripts"] == {"dispatcher-once": "actionq_dispatcher.cli:cli"}
    assert "Development Status :: 7 - Inactive" in metadata["classifiers"]


def test_help_identifies_ignored_legacy_options_and_version_is_available():
    help_result = CliRunner().invoke(cli, ["--help"])
    version_result = CliRunner().invoke(cli, ["--version"])

    assert help_result.exit_code == 0
    assert help_result.output.count("Ignored legacy option") == 2
    assert version_result.exit_code == 0
    assert "0.2.0" in version_result.output
