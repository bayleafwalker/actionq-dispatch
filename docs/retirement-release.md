# ActionQ Dispatcher 0.2.0 retirement release plan

This plan prepares a tombstone release; it does not authorize creating a tag,
publishing a GitHub release, changing installed tools, or stopping services.

Basis verified 2026-08-20: ActionQ PR #30 merged as
`9dccf4e084da88f3f0c52bb9d845b64bed89d202`; ActionQ `main` is version 0.1.26
and publishes only `actionctl` and `actionq-completion-outbox` console scripts.

## Release intent

Publish `actionq-dispatcher` 0.2.0 only after independent review of the
retirement PR. The release deliberately preserves the `dispatcher-once`
console-script name while replacing its removed daemon delegation with one
deterministic nonzero retirement message. This lets an upgrade overwrite stale
entry-point shims before operators remove the package.

## Acceptance gates

- source, wheel, and installed-command smokes prove no executable resolution,
  process creation, configuration read, or queue access occurs;
- every historical option combination reaches the same retirement message;
- wheel metadata reports version 0.2.0, inactive status, and exactly one
  `dispatcher-once` tombstone script;
- full tests and the agentops repository artifact validator pass;
- the PR records external consumers without changing them;
- an independent reviewer confirms ActionQ PR #30 is merged and no supported
  ActionQ artifact still publishes `actionq-daemon`.

## Proposed publication sequence

1. Merge the reviewed retirement PR.
2. Build the wheel from the merge commit in a clean checkout and repeat the
   installed-command smoke.
3. Create annotated tag `actionq-dispatcher-v0.2.0`.
4. Publish a GitHub release titled `ActionQ Dispatcher 0.2.0 — retired` with
   the migration note from `docs/runbook.md`.
5. Separately update `gitops-nixos`, `agentops`, q-spec, and appservice-owned
   documentation before any operator changes the installed estate.

Rollback is repository-only: revert the retirement commit before publication.
Do not reinstall 0.1.2 after ActionQ 0.1.26, because that recreates a launcher
whose required execution plane no longer exists.
