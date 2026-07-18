# Actionq Dispatcher Agent Guidance

> Shared environment guidance lives in `/projects/dev/AGENTS.md`.

## Ownership

`actionq-dispatcher` is the deterministic coordinator between actionq and
bounded worker execution. One `dispatcher-once` invocation may claim and
process at most one action. It owns worktree preparation, configured gates,
tool ACL enforcement, harness invocation, cost/budget checks, and action
outcome recording.

The queue contract lives in `../q-spec/actionq-spec.md`; the coordinator
contract lives in `../q-spec/dispatcher-spec.md`. Configuration is policy in
TOML, not an invitation to add workflow semantics to the queue.

## Working Rules

- Start with the smallest relevant TOML example under `examples/` and keep
  project paths, ACLs, prompt templates, gate commands, and model routing
  explicit.
- Preserve the one-action-per-cycle and pause-file behavior. Normal dispatcher
  outcomes are recorded through `actionctl`; only coordinator-internal failure
  should produce a nonzero process exit.
- Treat pre-gates, post-gates, path ACLs, timeouts, and budget limits as
  enforcement boundaries. Do not weaken them to make a smoke path pass.
- Use the fake worker for queue/worktree/gate smoke checks before a
  provider-backed run. Failed and rejected worktrees are evidence; inspect the
  action and branch before explicitly removing either.
- Resolved model and reasoning values must reach the harness command. Only pass
  provider-specific reasoning controls whose syntax has been verified.
- Preserve injected provider credentials when changing users or `PATH` in the
  legacy code-shell. A sudden authentication failure after `runuser` commonly
  means the environment was dropped.

## Daemon And Mutation Safety

- `actionq-daemon` and other long-running dispatcher sessions must run inside a
  named `tmux` session. The daemon refuses to start outside one.
- Do not start the daemon until fake-worker and provider-backed disposable
  cycles produce reviewable artifacts without merges, pushes, deploys, or
  production writes.
- Do not use broad cleanup under `worktree_root`, force-push, merge a worker
  branch, or mutate cluster state as part of dispatcher validation.
- Claim tokens remain with the orchestrator or daemon. Do not place them in
  prompts, artifacts, or subagent context.

## Validation

```bash
uv run --extra dev pytest tests/test_config.py tests/test_routing.py tests/test_harness.py tests/test_daemon.py -q
uv run --extra dev pytest tests/ -q
```

Use `docs/runbook.md` for disposable smoke dispatches. Keep integration effects
scoped to the dedicated smoke schema, temporary sprint item, and fake worker
until explicit operational authorization exists.