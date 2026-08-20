# `dispatcher-once` retirement runbook

`dispatcher-once` no longer runs work. ActionQ 0.1.26 removed the execution
plane that version 0.1.2 delegated to. The 0.2.0 command is a fail-fast
tombstone for replacing stale installed launchers safely.

## Operator migration

1. Stop and disable every systemd, cron, tmux, or manual loop that invokes
   `dispatcher-once` or the removed `actionq-daemon`. This is an external,
   operator-owned action; this repository does not mutate hosts.
2. Upgrade installed `actionq-dispatcher` tools to 0.2.0. Confirm invoking the
   command returns the retirement message and a nonzero status without creating
   a child process.
3. Remove `actionq-dispatcher` from host package/update lists, then uninstall
   the tool. Removing the package before callers are stopped can leave a stale
   or confusing command path.
4. Route future execution through the selected product-native runtime and the
   Vuoro federation boundary. There is no replacement queue-worker CLI.

## Known consumers requiring separate changes

- `/projects/dev/AGENTS.md` still names the installed launcher in legacy-pod
  recovery guidance;
- `gitops-nixos/modules/system/actionq-dispatch.nix` installs the package and
  defines `actionq-dispatch.service`, while
  `gitops-nixos/scripts/update-agentops-tools.sh` refreshes the tool;
- `agentops/project.toml`, `agentops/docs/ecosystem.md`, and related runbooks
  still register or describe the compatibility launcher;
- `q-spec/dispatcher-spec.md` still specifies daemon, cron, and manual
  `dispatcher-once` modes;
- appservice preflight documentation detects compatibility invocations.

Those repositories own their migration changes. This PR does not deploy,
disable services, edit host configuration, or alter cluster state.

Historical implementation evidence remains in Git and in the
`actionq-dispatcher-v0.1.2` tag. Do not delete or rewrite that history.

## Repository surface at retirement

The sole published console script is `dispatcher-once`. The Python package has
only `actionq_dispatcher.__version__` and the tombstone CLI module; it contains
no queue client, configuration loader, worker, daemon, or process adapter. The
latest pre-retirement release is GitHub release/tag
`actionq-dispatcher-v0.1.2`. Tests are repository-only falsification gates and
are not wheel package data.
