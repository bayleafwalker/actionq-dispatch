# actionq-dispatcher

Deterministic one-action-at-a-time coordinator for actionq.

`dispatcher-once` claims at most one action, prepares a scoped worktree, invokes a worker, validates the resulting artifact, and records the outcome through `actionctl`.

The current v1 implementation is intentionally narrow:

- it runs a single dispatcher cycle per invocation
- it supports the `scope-iterate` action type
- it creates an isolated git worktree for the target project
- it can invoke either a local Claude worker or a fake commit worker
- it enforces configured pre-gates, post-gates, and path ACL validation before completion

This repository is the coordinator layer. It expects an existing actionq installation and the project-specific tools needed by the configured action type.

## What it does

For each invocation, the dispatcher:

1. checks for a pause file and exits cleanly if dispatching is paused
2. claims at most one pending action through `actionctl`
3. loads the matching action config and target project config
4. creates a dedicated worktree and branch for that action
5. claims the corresponding sprintctl item
6. renders the worker prompt and invokes the configured worker
7. validates the resulting diff, branch state, worktree cleanliness, ACL scope, and configured test command
8. records the result back through `actionctl` and `sprintctl`

The CLI prints a small JSON result payload such as:

```json
{"result": "completed", "action_id": 123}
```

## Requirements

- Python 3.11+
- git with worktree support
- `actionctl` available on `PATH` or configured explicitly
- `sprintctl` access for `scope-iterate` actions
- `claude` available on `PATH` when using `runner = "local"`

## Install

Install from the repository root with `uv` or `pip`:

```bash
uv tool install .
```

or

```bash
python -m pip install .
```

This exposes the `dispatcher-once` command.

## Quick start

1. Copy one of the example configs and adjust project paths, tool paths, and environment variables for your environment.
2. Ensure `actionctl` can reach the target action queue and that the referenced project repository exists locally.
3. Run one cycle:

```bash
dispatcher-once --config /path/to/config.toml
```

You can also point the dispatcher at a config via `DISPATCHER_CONFIG`. If neither the flag nor environment variable is set, the default path is `~/.config/actionq-dispatcher/config.toml`.

## Configuration

Dispatcher configuration is TOML with three top-level areas:

- `[global]` for runtime behavior, budget limits, tool paths, event logging, and worktree locations
- `[projects.<name>]` for local repository paths, base refs, and project-specific environment overrides
- `[actions.<type>]` for worker model/runner settings, prompts, ACLs, gates, and validation commands

Example:

```toml
[global]
worktree_root = "~/.local/state/actionq-dispatcher/worktrees"
pause_file = "~/.local/state/actionq-dispatcher/PAUSED"
actionctl_bin = "actionctl"
claude_bin = "claude"

[projects.sprintctl]
path = "/projects/dev/sprintctl"
base_ref = "HEAD"

[actions.scope-iterate]
model = "claude-haiku-4-5-20251001"
reasoning = "medium"
runner = "local"
prompt_template = "/projects/dev/actionq-dispatcher/prompts/scope-iterate.md"
tool_acl = "/projects/dev/actionq-dispatcher/acls/scope-iterate.json"
test_command = "pytest"
```

See:

- `examples/config.toml` for a normal local configuration
- `examples/config.smoke.toml` for the fake-worker smoke setup
- `docs/runbook.md` for end-to-end smoke and operational notes

## Runners

The dispatcher currently supports two runner modes:

- `local`: invokes the Claude CLI in the action worktree with the configured model and ACL-derived tool allow/deny lists
- `fake` or `fake-commit`: writes a deterministic smoke file and creates a git commit without calling Claude

The fake worker is useful for validating the queue, worktree, branch, ACL, and post-gate flow before allowing real model-backed runs.

Actions, projects, and harnesses may set an optional `reasoning` value. It uses
the same precedence as model routing. Claude-backed dispatches pass it as
`--effort`; unsupported harnesses safely ignore it until their syntax is
verified.

## Operations

This project is intentionally centered on one-shot execution through `dispatcher-once`. Scheduling is expected to come from external tooling such as cron or systemd. Example unit files live under `ops/systemd/`.

If dispatching needs to stop without changing service wiring, create the configured pause file. A paused invocation emits a `coordinator_paused` event and exits without claiming work.

Failed or rejected worktrees are left in place deliberately for inspection. Use the runbook before cleaning them up so you do not remove a still-useful artifact branch.

## Development

Run the test suite from the repository root:

```bash
python -m pytest -q
```

## Repository layout

- `actionq_dispatcher/`: coordinator, config, worker, client, and ACL code
- `examples/`: example dispatcher configs, including smoke configuration
- `acls/`: tool/path ACL definitions passed to the worker
- `prompts/`: action prompt templates
- `docs/`: operational notes and runbooks
- `ops/`: service and scheduling examples
