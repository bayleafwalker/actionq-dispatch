from __future__ import annotations

import json
import shlex
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from .acl import ACL, load_acl, validate_changed_paths
from .clients import ActionctlClient, ClientError, SprintctlClient
from .commands import CommandRunner, git_safe_env
from .config import ActionConfig, DispatcherConfig, ProjectConfig
from .worker import ConfiguredWorker, WorkerError


class CoordinatorError(RuntimeError):
    pass


class _ActionSettled(Exception):
    """Raised by ScopeIterateHandler.prepare() when the action was settled early."""

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome


class ActionctlLike(Protocol):
    def claim(self, *, worker: str, timeout_minutes: int) -> dict | None: ...
    def complete(self, action_id: int, result_ref: str, actor: str) -> dict: ...
    def fail(self, action_id: int, reason: str, actor: str) -> dict: ...
    def reject(self, action_id: int, reason: str, validator: str, actor: str) -> dict: ...
    def emit(
        self,
        event_type: str,
        *,
        payload: dict,
        actor: str,
        action_id: int | None = None,
    ) -> dict: ...
    def events(
        self,
        *,
        event_type: str | None = None,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict]: ...


class SprintctlLike(Protocol):
    def item_show(self, item_id: int) -> dict: ...
    def claim_start(
        self,
        *,
        item_id: int,
        actor: str,
        ttl_seconds: int,
        branch: str,
        worktree: Path,
        runtime_session_id: str | None = None,
    ) -> dict: ...
    def done_from_claim(self, *, item_id: int, claim_id: int, claim_token: str, actor: str) -> dict: ...
    def block_with_claim(self, *, item_id: int, claim_id: int, claim_token: str, actor: str) -> dict: ...
    def release_claim(self, *, claim_id: int, claim_token: str, actor: str) -> None: ...
    def coordination_failure(self, *, sprint_id: int, item_id: int, actor: str, summary: str, detail: str) -> dict: ...


class WorkerLike(Protocol):
    def invoke(self, *, action_config: ActionConfig, worktree: Path, prompt: str, acl) -> object: ...


@dataclass
class PreparedSession:
    """Context produced by ScopeIterateHandler.prepare(); consumed by settle()."""

    action_id: int
    project: ProjectConfig
    sprintctl: SprintctlLike
    item_id: int
    item_bundle: dict
    claim_id: int
    claim_token: str
    worktree: Path
    branch: str
    base_sha: str
    acl: ACL
    prompt: str


@dataclass
class DispatchResult:
    exit_code: int
    result: str
    action_id: int | None = None


def default_clients(config: DispatcherConfig, runner: CommandRunner):
    actionctl = ActionctlClient(config.global_config.actionctl_bin, runner)

    def sprintctl_factory(project: ProjectConfig) -> SprintctlClient:
        return SprintctlClient(
            project.sprintctl_path or project.path,
            runner,
            project_env=project.env,
        )

    worker = ConfiguredWorker(config.global_config, runner)
    return actionctl, sprintctl_factory, worker


class Dispatcher:
    def __init__(
        self,
        config: DispatcherConfig,
        *,
        runner: CommandRunner | None = None,
        actionctl: ActionctlLike | None = None,
        sprintctl_factory: Callable[[ProjectConfig], SprintctlLike] | None = None,
        worker: WorkerLike | None = None,
    ):
        self.config = config
        self.runner = runner or CommandRunner()
        if actionctl is None or sprintctl_factory is None or worker is None:
            real_actionctl, real_sprintctl_factory, real_worker = default_clients(
                config, self.runner
            )
            actionctl = actionctl or real_actionctl
            sprintctl_factory = sprintctl_factory or real_sprintctl_factory
            worker = worker or real_worker
        self.actionctl = actionctl
        self.sprintctl_factory = sprintctl_factory
        self.worker = worker
        self.invocation_id = str(uuid.uuid4())
        self.actor = f"dispatcher:{socket.gethostname()}:{self.invocation_id}"

    def run_once(self) -> DispatchResult:
        started = time.monotonic()
        if self.config.global_config.pause_file.exists():
            self.actionctl.emit(
                "coordinator_paused",
                actor=self.actor,
                payload={
                    "invocation_id": self.invocation_id,
                    "host": socket.gethostname(),
                    "pause_file": str(self.config.global_config.pause_file),
                },
            )
            return DispatchResult(0, "paused")

        action = self.actionctl.claim(
            worker=self.actor,
            timeout_minutes=self.config.global_config.default_timeout_minutes,
        )
        if action is None:
            self._emit_cycle(started, claimed=False, result="no_action")
            return DispatchResult(0, "no_action")

        action_id = int(action["id"])
        action_type = action["action_type"]
        if action_type not in self.config.actions:
            reason = f"No dispatcher config for action type {action_type}"
            self.actionctl.reject(action_id, reason, "action-type-config", self.actor)
            self._emit_cycle(started, claimed=True, action_id=action_id, result="rejected")
            return DispatchResult(0, "rejected", action_id)

        if action_type != "scope-iterate":
            reason = f"Action type {action_type} is configured but not implemented in v1"
            self.actionctl.reject(action_id, reason, "action-type-implemented", self.actor)
            self._emit_cycle(started, claimed=True, action_id=action_id, result="rejected")
            return DispatchResult(0, "rejected", action_id)

        action_config = self.config.actions[action_type]
        handler = ScopeIterateHandler(
            self.config,
            self.runner,
            self.sprintctl_factory,
            self.worker,
            self.actionctl,
            self.actor,
        )
        try:
            result = handler.handle(action, action_config)
        except CoordinatorError:
            raise
        except Exception as exc:
            self.actionctl.fail(action_id, f"coordinator failure: {exc}", self.actor)
            self._emit_cycle(
                started,
                claimed=True,
                action_id=action_id,
                result="failed",
                cost_usd=handler.cost_usd,
            )
            return DispatchResult(0, "failed", action_id)

        self._emit_cycle(
            started,
            claimed=True,
            action_id=action_id,
            result=result,
            cost_usd=handler.cost_usd,
        )
        return DispatchResult(0, result, action_id)

    def _emit_cycle(
        self,
        started: float,
        *,
        claimed: bool,
        result: str,
        action_id: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        payload = {
            "invocation_id": self.invocation_id,
            "host": socket.gethostname(),
            "claimed": claimed,
            "action_id": action_id,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "result": result,
        }
        if cost_usd is not None:
            payload["cost_usd"] = cost_usd
        self.actionctl.emit(
            "coordinator_cycle",
            actor=self.actor,
            action_id=action_id,
            payload=payload,
        )


class ScopeIterateHandler:
    def __init__(
        self,
        config: DispatcherConfig,
        runner: CommandRunner,
        sprintctl_factory: Callable[[ProjectConfig], SprintctlLike],
        worker: WorkerLike | None,
        actionctl: ActionctlLike,
        actor: str,
    ):
        self.config = config
        self.runner = runner
        self.sprintctl_factory = sprintctl_factory
        self.worker = worker
        self.actionctl = actionctl
        self.actor = actor
        self.cost_usd = 0.0

    def prepare(
        self,
        action: dict,
        action_config: ActionConfig,
        *,
        runtime_session_id: str | None = None,
    ) -> PreparedSession:
        """Run pre-gates, worktree setup, sprint claim, ACL, and prompt.

        Raises _ActionSettled if the action was rejected/failed before workerstart.
        Raises CoordinatorError on fatal coordinator failures.
        """
        action_id = int(action["id"])

        if "budget" in action_config.pre_gates:
            allowed, reason = self._budget_allows(action_config)
            if not allowed:
                self.actionctl.reject(action_id, reason, "budget", self.actor)
                raise _ActionSettled("rejected")

        project_name = action.get("project")
        if not project_name or project_name not in self.config.projects:
            self.actionctl.reject(
                action_id,
                f"Unknown project: {project_name}",
                "project-exists",
                self.actor,
            )
            raise _ActionSettled("rejected")

        project = self.config.projects[project_name]
        target_ref = action.get("target_ref")
        try:
            item_id = int(target_ref)
        except (TypeError, ValueError):
            self.actionctl.reject(
                action_id,
                "target_ref must be a sprintctl item id",
                "sprint-item-exists",
                self.actor,
            )
            raise _ActionSettled("rejected")

        sprintctl = self.sprintctl_factory(project)
        try:
            item_bundle = sprintctl.item_show(item_id)
        except ClientError as exc:
            self.actionctl.reject(action_id, str(exc), "sprint-item-exists", self.actor)
            raise _ActionSettled("rejected")

        ready, reason = self._sprint_item_ready(item_bundle)
        if not ready:
            self.actionctl.reject(action_id, reason, "sprint-item-ready", self.actor)
            raise _ActionSettled("rejected")

        branch = f"agent/scope-iterate/{action_id}"
        if self._branch_exists(project.path, branch):
            self.actionctl.reject(
                action_id,
                f"Branch already exists: {branch}",
                "branch-absent",
                self.actor,
            )
            raise _ActionSettled("rejected")

        base_sha = self._git(project.path, ["rev-parse", project.base_ref]).strip()
        worktree = self._worktree_path(project_name, action_id)
        if worktree.exists() and any(worktree.iterdir()):
            self.actionctl.reject(
                action_id,
                f"Worktree already exists: {worktree}",
                "worktree-empty",
                self.actor,
            )
            raise _ActionSettled("rejected")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        self._git(project.path, ["worktree", "add", "-b", branch, str(worktree), base_sha])
        # The worktrees-smoke path lives under ~/.local/state which has 770 perms on this
        # cluster filesystem. Without this, git sees every checked-out file as mode-changed
        # (100644 committed → 100755 on disk), which causes worktree-clean post-gate failures.
        self._git(worktree, ["config", "core.fileMode", "false"])

        ttl_seconds = (action_config.timeout_minutes + 10) * 60
        claim = sprintctl.claim_start(
            item_id=item_id,
            actor=f"dispatcher:actionq:{action_id}",
            ttl_seconds=ttl_seconds,
            branch=branch,
            worktree=worktree,
            runtime_session_id=runtime_session_id,
        )
        claim_id = int(claim.get("claim_id") or claim.get("claim", {}).get("claim_id"))
        claim_token = claim.get("claim_token") or claim.get("claim", {}).get("claim_token")
        if not claim_token:
            raise CoordinatorError("sprintctl claim start did not return claim_token")

        variables = {
            "working_dir": str(worktree),
            "project": project_name,
            "branch_name": branch,
        }
        acl = load_acl(action_config.tool_acl, variables)
        prompt = self._render_prompt(
            action=action,
            item_bundle=item_bundle,
            action_config=action_config,
            worktree=worktree,
            branch=branch,
            acl=acl,
        )

        return PreparedSession(
            action_id=action_id,
            project=project,
            sprintctl=sprintctl,
            item_id=item_id,
            item_bundle=item_bundle,
            claim_id=claim_id,
            claim_token=claim_token,
            worktree=worktree,
            branch=branch,
            base_sha=base_sha,
            acl=acl,
            prompt=prompt,
        )

    def settle(
        self,
        prepared: PreparedSession,
        action_config: ActionConfig,
        *,
        exit_code: int,
    ) -> str:
        """Post-validate and settle a completed action. Returns outcome string."""
        if exit_code != 0:
            return self._fail_after_claim(
                prepared.action_id,
                prepared.item_bundle,
                prepared.sprintctl,
                prepared.item_id,
                prepared.claim_id,
                prepared.claim_token,
                f"harness exited with code {exit_code}",
            )

        passed, validator, reason = self._post_validate(
            worktree=prepared.worktree,
            base_sha=prepared.base_sha,
            branch=prepared.branch,
            acl=prepared.acl,
            action_config=action_config,
        )
        if not passed:
            return self._reject_after_claim(
                prepared.action_id,
                validator,
                reason,
                prepared.item_bundle,
                prepared.sprintctl,
                prepared.item_id,
                prepared.claim_id,
                prepared.claim_token,
            )

        prepared.sprintctl.done_from_claim(
            item_id=prepared.item_id,
            claim_id=prepared.claim_id,
            claim_token=prepared.claim_token,
            actor=f"dispatcher:actionq:{prepared.action_id}",
        )
        head_sha = self._git(prepared.worktree, ["rev-parse", "HEAD"]).strip()
        result_ref = f"branch={prepared.branch} worktree={prepared.worktree} commit={head_sha}"
        self.actionctl.complete(prepared.action_id, result_ref, self.actor)
        return "completed"

    def handle(self, action: dict, action_config: ActionConfig) -> str:
        """Synchronous one-shot dispatch. Used by dispatcher-once."""
        try:
            prepared = self.prepare(action, action_config)
        except _ActionSettled as e:
            return e.outcome

        try:
            self.cost_usd = action_config.max_cost_usd
            self.worker.invoke(
                action_config=action_config,
                worktree=prepared.worktree,
                prompt=prepared.prompt,
                acl=prepared.acl,
            )
        except WorkerError as exc:
            return self._fail_after_claim(
                prepared.action_id,
                prepared.item_bundle,
                prepared.sprintctl,
                prepared.item_id,
                prepared.claim_id,
                prepared.claim_token,
                f"worker failed: {exc}",
            )

        return self.settle(prepared, action_config, exit_code=0)

    def _worktree_path(self, project_name: str, action_id: int) -> Path:
        return self.config.global_config.worktree_root / project_name / str(action_id)

    def _render_prompt(
        self,
        *,
        action: dict,
        item_bundle: dict,
        action_config: ActionConfig,
        worktree: Path,
        branch: str,
        acl,
    ) -> str:
        template = action_config.prompt_template.read_text()
        return template.format(
            action_json=json.dumps(action, indent=2, default=str),
            sprint_item_json=json.dumps(item_bundle, indent=2, default=str),
            working_dir=str(worktree),
            branch_name=branch,
            allowed_scope=json.dumps(
                {
                    "path_allowlist": acl.path_allowlist,
                    "path_denylist": acl.path_denylist,
                    "bash_allowlist": acl.bash_allowlist,
                    "bash_denylist": acl.bash_denylist,
                    "network": acl.network,
                },
                indent=2,
            ),
            test_command=action_config.test_command,
        )

    def _budget_allows(self, action_config: ActionConfig) -> tuple[bool, str]:
        since = datetime.now(timezone.utc) - timedelta(
            hours=self.config.global_config.budget_window_hours
        )
        events = self.actionctl.events(
            event_type="coordinator_cycle",
            since=since,
            limit=1000,
        )
        spent = 0.0
        for event in events:
            payload = event.get("payload") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}
            try:
                spent += float(payload.get("cost_usd") or 0.0)
            except (TypeError, ValueError):
                continue
        projected = spent + action_config.max_cost_usd
        budget = self.config.global_config.budget_daily_usd
        if projected > budget:
            return (
                False,
                f"Budget exceeded: spent={spent:.2f}, action_max={action_config.max_cost_usd:.2f}, budget={budget:.2f}",
            )
        return True, "ok"

    def _sprint_item_ready(self, item_bundle: dict) -> tuple[bool, str]:
        item = item_bundle.get("item", item_bundle)
        status = item.get("status")
        active_claims = item_bundle.get("active_claims") or []
        # Allow re-dispatch of active items with no live claims (previous dispatch failed/released).
        if status not in ("pending", "active"):
            return False, f"Item status must be pending for scope-iterate, got {status}"
        if active_claims:
            return False, f"Item already has active claims: {len(active_claims)}"
        return True, "ok"

    def _post_validate(
        self,
        *,
        worktree: Path,
        base_sha: str,
        branch: str,
        acl,
        action_config: ActionConfig,
    ) -> tuple[bool, str, str]:
        current_branch = self._git(worktree, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
        if current_branch != branch:
            return False, "branch-exists", f"Expected branch {branch}, got {current_branch}"

        ahead = int(self._git(worktree, ["rev-list", "--count", f"{base_sha}..HEAD"]).strip())
        if ahead <= 0:
            return False, "branch-ahead-of-base", "Branch has no commits ahead of base"

        changed = [
            line
            for line in self._git(worktree, ["diff", "--name-only", f"{base_sha}..HEAD"]).splitlines()
            if line.strip()
        ]
        if not changed:
            return False, "diff-nonempty", "No committed diff"

        dirty = self._git(worktree, ["status", "--porcelain"]).strip()
        if dirty:
            return False, "worktree-clean", f"Worktree has uncommitted changes: {dirty}"

        ok, reason = validate_changed_paths(changed, worktree=worktree, acl=acl)
        if not ok:
            return False, "diff-scope-respected", reason or "ACL rejected diff"

        test_result = self.runner.run(shlex.split(action_config.test_command), cwd=worktree)
        if test_result.returncode != 0:
            return False, "tests-pass", test_result.stderr or test_result.stdout or "tests failed"
        return True, "ok", "ok"

    def _reject(self, action_id: int, validator: str, reason: str) -> str:
        self.actionctl.reject(action_id, reason, validator, self.actor)
        return "rejected"

    def _fail_after_claim(
        self,
        action_id: int,
        item_bundle: dict,
        sprintctl: SprintctlLike,
        item_id: int,
        claim_id: int,
        claim_token: str,
        reason: str,
    ) -> str:
        self._record_failure(item_bundle, sprintctl, item_id, claim_id, claim_token, reason)
        self.actionctl.fail(action_id, reason, self.actor)
        return "failed"

    def _reject_after_claim(
        self,
        action_id: int,
        validator: str,
        reason: str,
        item_bundle: dict,
        sprintctl: SprintctlLike,
        item_id: int,
        claim_id: int,
        claim_token: str,
    ) -> str:
        self._record_failure(item_bundle, sprintctl, item_id, claim_id, claim_token, reason)
        self.actionctl.reject(action_id, reason, validator, self.actor)
        return "rejected"

    def _record_failure(
        self,
        item_bundle: dict,
        sprintctl: SprintctlLike,
        item_id: int,
        claim_id: int,
        claim_token: str,
        detail: str,
    ) -> None:
        item = item_bundle.get("item", item_bundle)
        sprint_id = int(item["sprint_id"])
        actor = f"dispatcher:actionq"
        try:
            sprintctl.coordination_failure(
                sprint_id=sprint_id,
                item_id=item_id,
                actor=actor,
                summary=f"actionq scope-iterate failed for item #{item_id}",
                detail=detail,
            )
        except Exception:
            pass
        block_error: Exception | None = None
        try:
            sprintctl.block_with_claim(
                item_id=item_id,
                claim_id=claim_id,
                claim_token=claim_token,
                actor=actor,
            )
        except Exception as exc:
            block_error = exc
        try:
            sprintctl.release_claim(claim_id=claim_id, claim_token=claim_token, actor=actor)
        except Exception as exc:
            try:
                sprintctl.coordination_failure(
                    sprint_id=sprint_id,
                    item_id=item_id,
                    actor=actor,
                    summary=f"actionq scope-iterate claim release failed for item #{item_id}",
                    detail=str(exc),
                )
            except Exception:
                pass
        if block_error is not None:
            raise block_error

    def _branch_exists(self, repo: Path, branch: str) -> bool:
        result = self.runner.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=repo,
        )
        return result.returncode == 0

    def _git(self, cwd: Path, args: list[str]) -> str:
        result = self.runner.run(["git", *args], cwd=cwd, env=git_safe_env(cwd))
        if result.returncode != 0:
            raise CoordinatorError(result.stderr or result.stdout or f"git {' '.join(args)} failed")
        return result.stdout
