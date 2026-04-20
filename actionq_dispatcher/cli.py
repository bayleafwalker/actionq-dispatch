from __future__ import annotations

import json

import click

from . import __version__
from .commands import CommandRunner
from .config import ConfigError, load_config
from .core import CoordinatorError, Dispatcher


@click.command()
@click.option("--config", "config_path", default=None, help="Dispatcher config path")
@click.version_option(__version__, prog_name="dispatcher-once")
def cli(config_path: str | None) -> None:
    """Run one deterministic dispatcher cycle."""
    try:
        config = load_config(config_path)
        result = Dispatcher(config, runner=CommandRunner()).run_once()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    except CoordinatorError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(json.dumps({"result": result.result, "action_id": result.action_id}))
    raise click.exceptions.Exit(result.exit_code)
