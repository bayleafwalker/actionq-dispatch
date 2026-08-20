from __future__ import annotations

import click

from . import __version__


RETIREMENT_MESSAGE = (
    "dispatcher-once is retired because ActionQ 0.1.26 removed the "
    "actionq-daemon execution plane. This package no longer starts a worker or "
    "accesses the queue. Remove scheduled dispatcher-once invocations, uninstall "
    "actionq-dispatcher, and use the selected product-native runtime through the "
    "Vuoro federation boundary."
)


@click.command()
@click.option(
    "--config",
    "config_path",
    default=None,
    help="Ignored legacy option retained so old callers receive the retirement message.",
)
@click.option(
    "--actionq-daemon",
    "daemon_bin",
    default=None,
    help="Ignored legacy option retained so old callers receive the retirement message.",
)
@click.version_option(__version__, prog_name="dispatcher-once")
def cli(config_path: str | None, daemon_bin: str | None) -> None:
    """Explain that the historical one-shot execution path is retired."""
    del config_path, daemon_bin
    raise click.ClickException(RETIREMENT_MESSAGE)
