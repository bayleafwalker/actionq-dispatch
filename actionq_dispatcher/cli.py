from __future__ import annotations

import os
import shutil

import click

from . import __version__


@click.command()
@click.option("--config", "config_path", default=None, help="Dispatcher config path")
@click.option(
    "--actionq-daemon",
    "daemon_bin",
    default=None,
    help="Canonical actionq-daemon executable (defaults to ACTIONQ_DAEMON_BIN or PATH)",
)
@click.version_option(__version__, prog_name="dispatcher-once")
def cli(config_path: str | None, daemon_bin: str | None) -> None:
    """Run one cycle through ActionQ's canonical receipt-fenced daemon."""
    executable = daemon_bin or os.environ.get("ACTIONQ_DAEMON_BIN") or "actionq-daemon"
    resolved = shutil.which(executable)
    if resolved is None:
        raise click.ClickException(
            f"canonical ActionQ daemon not found: {executable!r}; "
            "install actionq and ensure actionq-daemon is on PATH"
        )

    command = [resolved]
    if config_path is not None:
        command.extend(["--config", config_path])
    command.append("--once")

    try:
        os.execv(resolved, command)
    except OSError as exc:
        raise click.ClickException(
            f"failed to execute canonical ActionQ daemon: {exc}"
        ) from exc
