# actionq-dispatcher

Deterministic one-action-at-a-time coordinator for actionq.

`dispatcher-once` claims at most one action, prepares a scoped worktree, invokes a worker, validates the resulting artifact, and records the outcome through `actionctl`.
