# actionq dispatcher runbook

## Code-shell setup

The appservice GitOps setup provisions a small CloudNativePG cluster in the
`vscode` namespace for code-shell smoke runs:

- cluster: `actionq-cnpg-main`
- database: `actionq`
- application user: `actionq`
- initial schema: `ACTIONQ_SCHEMA=actionq_smoke`

The `vscode-shell` deployment receives `ACTIONQ_URL` from the CNPG-generated
`actionq-cnpg-main-app` secret. It also sets:

```bash
ACTIONQ_SCHEMA=actionq_smoke
DISPATCHER_CONFIG=/projects/dev/actionq-dispatcher/examples/config.smoke.toml
SPRINTCTL_DB=/projects/dev/sprintctl/.sprintctl/sprintctl.db
KCTL_DB=/projects/dev/sprintctl/.kctl/kctl.db
```

It also receives LLM provider credentials from the `vscode-shell-llm-api-keys`
secret. Keep those variables in the environment when running Claude-backed
dispatches; do not replace the whole environment with only `ACTIONQ_URL` and
`SPRINTCTL_URL`.

After the appservice change reconciles, verify from an appservice shell:

```bash
direnv exec /projects/dev/appservice kubectl -n vscode get cluster actionq-cnpg-main
direnv exec /projects/dev/appservice kubectl -n vscode get secret actionq-cnpg-main-app
direnv exec /projects/dev/appservice kubectl -n vscode rollout status deploy/vscode-shell
```

Then SSH into code-shell and install the local tools into the persistent dev
home:

```bash
uv tool install /projects/dev/actionq/
uv tool install /projects/dev/actionq-dispatcher/
actionctl migrate
```

### Stale shim and user environment checks

If `/home/dev/.local/bin/actionctl`, `/home/dev/.local/bin/dispatcher-once`, or
`/home/dev/.local/bin/sprintctl` fails with `bad interpreter`, treat it as a
stale uv shim first. The persistent pod virtualenv entry points are the preferred
fallback:

```bash
/home/dev/.local/state/pod-venvs/actionq/bin/actionctl
/home/dev/.local/state/pod-venvs/actionq-dispatcher/bin/dispatcher-once
/home/dev/.local/state/pod-venvs/sprintctl/bin/sprintctl
```

When running through `kubectl exec`, verify the effective user and environment:

```bash
direnv exec /projects/dev/appservice kubectl -n vscode exec deploy/vscode-shell -- \
  bash -lc 'id; test -n "$ACTIONQ_URL" && echo ACTIONQ_URL=set; test -n "$ANTHROPIC_API_KEY" && echo ANTHROPIC_API_KEY=set'
```

`kubectl exec` may enter as `root`. The worker harness must run as `dev`, but it
must retain the injected LLM credentials. Prefer preserving the environment:

```bash
direnv exec /projects/dev/appservice kubectl -n vscode exec deploy/vscode-shell -- \
  bash -lc 'runuser -u dev --preserve-environment -- dispatcher-once --config "$DISPATCHER_CONFIG"'
```

If you need to bypass stale shims, override only `PATH` and keep the rest of the
deployment environment:

```bash
direnv exec /projects/dev/appservice kubectl -n vscode exec deploy/vscode-shell -- \
  bash -lc 'export PATH="/home/dev/.local/state/pod-venvs/actionq/bin:/home/dev/.local/state/pod-venvs/actionq-dispatcher/bin:/home/dev/.local/state/pod-venvs/sprintctl/bin:$PATH"; runuser -u dev --preserve-environment -- dispatcher-once --config "$DISPATCHER_CONFIG"'
```

If Claude returns `401 Invalid API key` immediately after a `runuser`, `su`, or
manual `env` wrapper change, check for dropped LLM environment variables before
debugging actionq, sprintctl, or CNPG connectivity.

## First smoke dispatch

1. Install tools in persistent user-local paths:

```bash
uv tool install /projects/dev/actionq/
uv tool install /projects/dev/actionq-dispatcher/
```

2. Configure Postgres against the smoke schema:

```bash
export ACTIONQ_URL='postgresql://...'
export ACTIONQ_SCHEMA=actionq_smoke
actionctl migrate
```

3. Review the smoke dispatcher config at `/projects/dev/actionq-dispatcher/examples/config.smoke.toml`.

The smoke config uses `runner = "fake"`, explicit `SPRINTCTL_DB`, `ACTIONQ_SCHEMA=actionq_smoke`, and `python3 -c pass` as the validation command. It does not call Claude.

4. Create a disposable pending sprintctl item named for dispatch smoke:

```bash
SPRINTCTL_DB=/projects/dev/sprintctl/.sprintctl/sprintctl.db \
  sprintctl item add --sprint-id 1 --track dispatch-smoke \
  --title "actionq dispatcher smoke: docs-only fake worker" --json
```

5. Enqueue a disposable `scope-iterate` action:

```bash
actionctl add --type scope-iterate --project sprintctl --target <work-item-id> --created-by human:manual
```

6. Run exactly one cycle:

```bash
dispatcher-once --config /projects/dev/actionq-dispatcher/examples/config.smoke.toml
```

7. Verify:

```bash
actionctl show <action-id>
actionctl events --type coordinator_cycle --limit 5
actionctl sessions --active --project sprintctl
SPRINTCTL_DB=/projects/dev/sprintctl/.sprintctl/sprintctl.db sprintctl item show --id <work-item-id> --json
git -C /projects/dev/sprintctl worktree list
```

Expected result: the action is completed, the sprintctl item is done, the branch is `agent/scope-iterate/<action-id>`, the worktree exists under `~/.local/state/actionq-dispatcher/worktrees-smoke/sprintctl/<action-id>`, and the coordinator cycle includes `claimed=true`, `result=completed`, and `cost_usd`.

Do not enable the daemon until one fake-worker/manual cycle and one Claude-backed disposable cycle have both produced reviewable branches without merges, pushes, deploys, or production writes.

## Failed or rejected worktrees

The dispatcher intentionally leaves failed and rejected worktrees/branches in place for review. After inspecting the artifact and action history, remove the reviewed worktree explicitly:

```bash
git -C /projects/dev/<project> worktree list
git -C /projects/dev/<project> worktree remove ~/.local/state/actionq-dispatcher/worktrees/<project>/<action-id>
git -C /projects/dev/<project> branch -D agent/scope-iterate/<action-id>
```

Do not run broad cleanup against `worktree_root`; use `actionctl show <action-id>` first so you know whether the artifact is still useful.
