# `dispatcher-once` compatibility runbook

Use `dispatcher-once` only for a bounded manual/debug cycle required by a
legacy caller. It delegates to the installed ActionQ daemon with `--once`.

Before any queue-backed run:

1. install the intended immutable ActionQ artifact;
2. validate `actionctl check-compatibility`;
3. use an ActionQ-owned config with an explicit project, sprint, harness,
   scope-iterate policy, path ACL, and test command;
4. keep the ActionQ pause file present until the disposable fake-worker and
   provider-backed gates have produced reviewable artifacts.

Run one cycle:

```bash
dispatcher-once --config ~/.config/actionq/config.toml
```

For continuous execution, follow the ActionQ daemon runbook and start
`actionq-daemon` directly in a named `tmux` session. This compatibility package
does not own daemon scheduling, queue mutation, worktrees, or cleanup.

Legacy configuration under `~/.config/actionq-dispatcher/config.toml` may still
be found by ActionQ when no explicit path is supplied, but it must already use
the current ActionQ schema. This package deliberately performs no lossy config
translation.
