# Actionq Dispatcher Agent Guidance

> Shared environment guidance lives in `/projects/dev/AGENTS.md`.

**Status: retired tombstone.** ActionQ 0.1.26 removed the execution plane that
the historical `dispatcher-once` command launched. Version 0.2.0 retains only
a deterministic fail-fast command so upgrades replace stale executable shims
with an actionable retirement message. Do not add dispatch behavior here.

## Ownership

`actionq-dispatcher` owns no runtime behavior. The tombstone must not resolve,
spawn, or replace a process; inspect configuration; access ActionQ or
Sprintctl; or imply that a queue worker remains available. Product-native
runtimes integrate through the Vuoro federation boundary.

The queue and coordinator contracts were maintained separately as q-spec.
The [AgentOps 2026-08-20 closure report](https://github.com/bayleafwalker/agentops/blob/main/docs/assessments/corrected-native-runtime-federation-program-closure-2026-08-20.md)
records that no public q-spec repository exists and that the reviewed bundle
is host-persistent; that source is not available in this checkout. Its purpose
and publication disposition remain unresolved. Do not make this tombstone
depend on q-spec or infer its closure from its absence. Configuration is policy
in TOML, not an invitation to add workflow semantics to the queue.

## Working Rules

- Keep `dispatcher-once` a deterministic nonzero retirement tombstone.
- Do not add queue clients, claim tokens, Sprintctl mutations, worktree
  preparation, policy translation, harness logic, or settlement to this
  package.
- Retain the old option names only so existing callers get the retirement
  message instead of an option-parser error.
- Preserve historical evidence in Git; do not recreate deleted behavior.

## Daemon And Mutation Safety

- This package must not publish, resolve, or start `actionq-daemon`.
- Do not install or schedule `dispatcher-once` as a daemon substitute.
- Disabling external services and removing installed tools are operator-owned
  rollout actions outside this repository.

## Validation

```bash
uv run --extra dev pytest tests/ -q
```
