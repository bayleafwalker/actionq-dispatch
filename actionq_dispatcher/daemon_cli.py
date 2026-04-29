from __future__ import annotations

import logging
import sys

import click

from . import __version__
from .commands import CommandRunner
from .config import ConfigError, load_config
from .daemon import ActionqDaemon


@click.command()
@click.option("--config", "config_path", default=None, help="Config file path")
@click.option("--log-level", default="INFO", show_default=True, help="Logging level")
@click.version_option(__version__, prog_name="actionq-daemon")
def cli(config_path: str | None, log_level: str) -> None:
    """Long-running actionq daemon: polls the queue and supervises agent sessions."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    daemon = ActionqDaemon(config, runner=CommandRunner())
    daemon.run()
