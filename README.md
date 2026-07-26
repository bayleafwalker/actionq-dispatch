# actionq-dispatcher

This package is a compatibility launcher for ActionQ.

ActionQ owns queue claims, receipt renewal and fencing, Sprintctl claim
coordination, scope-iterate worktrees and policy, harness execution, artifact
verification, session recovery, and final settlement. This repository does not
implement a second coordinator or daemon.

`dispatcher-once` remains available for callers that used the historical
one-shot command. It resolves `actionq-daemon` from `PATH` (or
`ACTIONQ_DAEMON_BIN`) and executes exactly:

```text
actionq-daemon [--config PATH] --once
```

The child exit code is returned unchanged. Queue claim receipts and Sprintctl
claim tokens never pass through the compatibility process.

## Install

Install ActionQ first, then this compatibility command:

```bash
uv tool install /projects/dev/actionq/
uv tool install /projects/dev/actionq-dispatcher/
```

For continuous operation, run `actionq-daemon` directly using ActionQ's
configuration and runbook. Do not schedule `dispatcher-once` in a cron or
systemd loop.

## Usage

```bash
dispatcher-once --config ~/.config/actionq/config.toml
```

An alternate executable may be selected explicitly for controlled validation:

```bash
dispatcher-once \
  --actionq-daemon /path/to/actionq-daemon \
  --config /path/to/config.toml
```

The compatibility launcher does not translate legacy dispatcher TOML. The
configuration must satisfy the installed ActionQ daemon, including an explicit
`scope_iterate` policy for governed scope-iterate actions.

## Development

```bash
uv run --extra dev pytest tests/ -q
```
