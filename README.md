# actionq-dispatcher

This package is the retirement tombstone for the historical ActionQ
`dispatcher-once` command.

ActionQ 0.1.26 removed the daemon, harness, worktree, and standalone server
execution plane. Consequently, there is no valid process for this package to
launch. Version 0.2.0 keeps the console-script name only so an upgrade replaces
old launchers with a deterministic, actionable failure.

Every execution attempt exits nonzero without resolving an executable,
starting a process, reading configuration, or accessing a queue:

```console
$ dispatcher-once --config ~/.config/actionq/config.toml
Error: dispatcher-once is retired because ActionQ 0.1.26 removed the actionq-daemon execution plane. ...
```

`--config` and `--actionq-daemon` remain accepted only so legacy callers reach
the same retirement message instead of a misleading option error.

## Install

Do not add this package to a new installation. For an existing installation,
upgrade once to replace stale executable shims, then remove the package after
scheduled callers and services have been retired:

```bash
uv tool install --force /projects/dev/actionq-dispatcher/
uv tool uninstall actionq-dispatcher
```

There is no replacement queue-worker command in this repository or in current
ActionQ. Select a product-native runtime through the Vuoro federation boundary.
Stopping services, deleting cron entries, and changing host package lists are
separate operator-owned rollout steps.

See [the retirement runbook](docs/runbook.md) for the consumer inventory and
operator migration sequence. Historical implementations remain available in
Git history and the `actionq-dispatcher-v0.1.2` tag.

## Development

```bash
uv run --extra dev pytest tests/ -q
```
