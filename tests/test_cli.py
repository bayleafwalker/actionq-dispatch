from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from actionq_dispatcher.cli import cli


def _fake_daemon(tmp_path: Path) -> Path:
    executable = tmp_path / "actionq-daemon"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


class Executed(Exception):
    pass


def test_delegates_exactly_one_cycle_with_process_replacement(tmp_path, monkeypatch):
    daemon = _fake_daemon(tmp_path)
    observed = []

    def fake_execv(path, argv):
        observed.append((path, argv))
        raise Executed

    monkeypatch.setattr("actionq_dispatcher.cli.os.execv", fake_execv)
    result = CliRunner().invoke(
        cli,
        ["--actionq-daemon", str(daemon), "--config", "/tmp/actionq.toml"],
    )

    assert isinstance(result.exception, Executed)
    assert observed == [
        (
            str(daemon),
            [str(daemon), "--config", "/tmp/actionq.toml", "--once"],
        )
    ]


def test_uses_actionq_daemon_bin_environment_without_config(tmp_path, monkeypatch):
    daemon = _fake_daemon(tmp_path)
    observed = []

    def fake_execv(path, argv):
        observed.append((path, argv))
        raise Executed

    monkeypatch.setattr("actionq_dispatcher.cli.os.execv", fake_execv)

    result = CliRunner().invoke(
        cli,
        [],
        env={"ACTIONQ_DAEMON_BIN": str(daemon)},
    )

    assert isinstance(result.exception, Executed)
    assert observed == [(str(daemon), [str(daemon), "--once"])]


def test_missing_canonical_daemon_fails_before_any_queue_access():
    result = CliRunner().invoke(
        cli,
        ["--actionq-daemon", "/definitely/missing/actionq-daemon"],
    )

    assert result.exit_code == 1
    assert "canonical ActionQ daemon not found" in result.output


def test_exec_failure_is_reported_as_compatibility_error(tmp_path, monkeypatch):
    daemon = _fake_daemon(tmp_path)

    def fake_execv(path, argv):
        raise OSError("exec denied")

    monkeypatch.setattr("actionq_dispatcher.cli.os.execv", fake_execv)
    result = CliRunner().invoke(cli, ["--actionq-daemon", str(daemon)])

    assert result.exit_code == 1
    assert "failed to execute canonical ActionQ daemon: exec denied" in result.output


def test_version_reports_compatibility_package_version():
    result = CliRunner().invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert "0.1.1" in result.output
