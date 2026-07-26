# Actionq Dispatcher Agent Guidance

> Shared environment guidance lives in `/projects/dev/AGENTS.md`.

## Ownership

`actionq-dispatcher` is a compatibility launcher for callers of the historical
`dispatcher-once` command. The command delegates one bounded cycle to
ActionQ's canonical daemon. `../actionq` owns queue claims and receipts,
worktree preparation, configured gates, tool ACL enforcement, harness
invocation, cost/budget checks, Sprintctl claim coordination, and settlement.

The queue contract lives in `../q-spec/actionq-spec.md`; the coordinator
contract lives in `../q-spec/dispatcher-spec.md`. Configuration is policy in
TOML, not an invitation to add workflow semantics to the queue.

## Working Rules

- Keep `dispatcher-once` a transparent `actionq-daemon --once` launcher.
- Do not add queue clients, claim tokens, Sprintctl mutations, worktree
  preparation, policy translation, harness logic, or settlement to this
  package.
- Preserve the child process exit code and argument boundaries.
- Do not infer or rewrite legacy configuration. ActionQ validates its own
  configuration and safety policy.

## Daemon And Mutation Safety

- This package does not publish or start `actionq-daemon`.
- Long-running operation, disposable gates, cleanup, claim-token handling, and
  mutation safety are governed by `../actionq/AGENTS.md`.
- Do not schedule `dispatcher-once` as a daemon substitute.

## Validation

```bash
uv run --extra dev pytest tests/ -q
```
