from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .commands import CommandRunner


class ClientError(RuntimeError):
    pass


@dataclass
class AuditResult:
    ok: bool
    event_id: str | None
    error: str | None


def _json_stdout(result, command: list[str]):
    if result.returncode != 0:
        raise ClientError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n{result.stderr}"
        )
    return json.loads(result.stdout or "{}")


class ActionctlClient:
    def __init__(self, bin_name: str, runner: CommandRunner):
        self.bin_name = bin_name
        self.runner = runner

    def claim(self, *, worker: str, timeout_minutes: int) -> dict | None:
        cmd = [
            self.bin_name,
            "claim",
            "--worker",
            worker,
            "--timeout",
            str(timeout_minutes),
        ]
        result = self.runner.run(cmd)
        if result.returncode == 2:
            return None
        return _json_stdout(result, cmd)

    def complete(self, action_id: int, result_ref: str, actor: str) -> dict:
        cmd = [
            self.bin_name,
            "complete",
            str(action_id),
            "--result",
            result_ref,
            "--actor",
            actor,
        ]
        return _json_stdout(self.runner.run(cmd), cmd)

    def fail(self, action_id: int, reason: str, actor: str) -> dict:
        cmd = [
            self.bin_name,
            "fail",
            str(action_id),
            "--reason",
            reason,
            "--actor",
            actor,
        ]
        return _json_stdout(self.runner.run(cmd), cmd)

    def reject(self, action_id: int, reason: str, validator: str, actor: str) -> dict:
        cmd = [
            self.bin_name,
            "reject",
            str(action_id),
            "--reason",
            reason,
            "--validator",
            validator,
            "--actor",
            actor,
        ]
        return _json_stdout(self.runner.run(cmd), cmd)

    def emit(
        self,
        event_type: str,
        *,
        payload: dict,
        actor: str,
        action_id: int | None = None,
    ) -> dict:
        cmd = [
            self.bin_name,
            "emit",
            "--type",
            event_type,
            "--actor",
            actor,
            "--payload",
            json.dumps(payload),
        ]
        if action_id is not None:
            cmd.extend(["--action", str(action_id)])
        return _json_stdout(self.runner.run(cmd), cmd)

    def events(
        self,
        *,
        event_type: str | None = None,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        cmd = [self.bin_name, "events", "--limit", str(limit)]
        if event_type:
            cmd.extend(["--type", event_type])
        if since:
            timestamp = since.astimezone(timezone.utc).isoformat()
            cmd.extend(["--since", timestamp])
        result = self.runner.run(cmd)
        rows = _json_stdout(result, cmd)
        if not isinstance(rows, list):
            raise ClientError("actionctl events returned non-list JSON")
        return rows

    def sessions(
        self,
        *,
        project: str | None = None,
        active_only: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        cmd = [self.bin_name, "sessions", "--limit", str(limit)]
        if project:
            cmd.extend(["--project", project])
        if active_only:
            cmd.append("--active")
        rows = _json_stdout(self.runner.run(cmd), cmd)
        if not isinstance(rows, list):
            raise ClientError("actionctl sessions returned non-list JSON")
        return rows


class SprintctlClient:
    def __init__(
        self,
        project_path: Path,
        runner: CommandRunner,
        *,
        project_env: dict[str, str] | None = None,
    ):
        self.project_path = project_path
        self.runner = runner
        self.project_env = project_env or {}

    def _run(self, args: list[str]) -> dict:
        cmd = ["direnv", "exec", str(self.project_path), "sprintctl", *args]
        return _json_stdout(
            self.runner.run(cmd, cwd=self.project_path, env=self.project_env),
            cmd,
        )

    def takeup_take(
        self,
        *,
        sprint_id: int,
        actor: str,
        instance_id: str,
        runtime_session_id: str,
        context: str | None = None,
    ) -> dict:
        args = [
            "takeup",
            "take",
            "--sprint-id",
            str(sprint_id),
            "--actor",
            actor,
            "--actor-kind",
            "agent",
            "--instance-id",
            instance_id,
            "--runtime-session-id",
            runtime_session_id,
            "--json",
        ]
        if context:
            args.extend(["--context", context])
        return self._run(args)

    def takeup_release(
        self,
        *,
        sprint_id: int,
        actor: str,
        instance_id: str,
        runtime_session_id: str,
        reason: str | None = None,
    ) -> dict:
        args = [
            "takeup",
            "release",
            "--sprint-id",
            str(sprint_id),
            "--actor",
            actor,
            "--actor-kind",
            "agent",
            "--instance-id",
            instance_id,
            "--runtime-session-id",
            runtime_session_id,
            "--json",
        ]
        if reason:
            args.extend(["--reason", reason])
        return self._run(args)

    def item_show(self, item_id: int) -> dict:
        return self._run(["item", "show", "--id", str(item_id), "--json"])

    def claim_start(
        self,
        *,
        item_id: int,
        actor: str,
        ttl_seconds: int,
        branch: str,
        worktree: Path,
        runtime_session_id: str | None = None,
    ) -> dict:
        args = [
            "claim",
            "start",
            "--item-id",
            str(item_id),
            "--actor",
            actor,
            "--ttl",
            str(ttl_seconds),
            "--branch",
            branch,
            "--worktree",
            str(worktree),
            "--json",
        ]
        if runtime_session_id:
            args.extend(["--runtime-session-id", runtime_session_id])
        return self._run(args)

    def done_from_claim(self, *, item_id: int, claim_id: int, claim_token: str, actor: str) -> dict:
        return self._run(
            [
                "item",
                "done-from-claim",
                "--id",
                str(item_id),
                "--claim-id",
                str(claim_id),
                "--claim-token",
                claim_token,
                "--actor",
                actor,
                "--json",
            ]
        )

    def block_with_claim(self, *, item_id: int, claim_id: int, claim_token: str, actor: str) -> dict:
        return self._run(
            [
                "item",
                "status",
                "--id",
                str(item_id),
                "--status",
                "blocked",
                "--actor",
                actor,
                "--claim-id",
                str(claim_id),
                "--claim-token",
                claim_token,
                "--json",
            ]
        )

    def release_claim(self, *, claim_id: int, claim_token: str, actor: str) -> None:
        cmd = [
            "direnv",
            "exec",
            str(self.project_path),
            "sprintctl",
            "claim",
            "release",
            "--id",
            str(claim_id),
            "--claim-token",
            claim_token,
            "--actor",
            actor,
        ]
        result = self.runner.run(cmd, cwd=self.project_path, env=self.project_env)
        if result.returncode != 0:
            raise ClientError(result.stderr)

    def takeup_sweep(
        self,
        *,
        actionctl_bin: str = "actionctl",
        stale_after: int | None = None,
    ) -> dict:
        args = ["takeup", "sweep", "--actionctl-bin", actionctl_bin, "--json"]
        if stale_after is not None:
            args.extend(["--stale-after", str(stale_after)])
        return self._run(args)

    def coordination_failure(
        self,
        *,
        sprint_id: int,
        item_id: int,
        actor: str,
        summary: str,
        detail: str,
    ) -> dict:
        payload = {
            "summary": summary,
            "detail": detail,
            "tags": ["actionq", "dispatcher", "coordination"],
        }
        cmd = [
            "direnv",
            "exec",
            str(self.project_path),
            "sprintctl",
            "event",
            "add",
            "--sprint-id",
            str(sprint_id),
            "--type",
            "coordination-failure",
            "--actor",
            actor,
            "--item-id",
            str(item_id),
            "--source",
            "system",
            "--payload",
            json.dumps(payload),
        ]
        result = self.runner.run(cmd, cwd=self.project_path, env=self.project_env)
        if result.returncode != 0:
            raise ClientError(result.stderr)
        if result.stdout.strip():
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return {"stdout": result.stdout.strip()}
        return {}


class AuditctlClient:
    """Wraps `direnv exec <project_path> auditctl add --json`.

    Runs in the project directory so the project .envrc provides AUDITCTL_DB
    and AUDITCTL_ARTIFACTS_ROOT. Always returns AuditResult rather than raising
    so the caller can decide how to handle failures.
    """

    _DEFAULT_TIMEOUT = 15

    def __init__(
        self,
        project_path: Path,
        runner: CommandRunner,
        *,
        project_env: dict[str, str] | None = None,
        bin_name: str = "auditctl",
    ) -> None:
        self.project_path = project_path
        self.runner = runner
        self.project_env = project_env or {}
        self.bin_name = bin_name

    def add(
        self,
        *,
        type_: str,
        actor: str,
        summary: str,
        refs: list[str] | None = None,
        metadata: dict | None = None,
        detail: str | None = None,
        source: str = "actionq-daemon",
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> AuditResult:
        cmd = [
            "direnv", "exec", str(self.project_path),
            self.bin_name, "add",
            "--type", type_,
            "--source", source,
            "--actor", actor,
            "--summary", summary,
            "--json",
        ]
        for ref in refs or []:
            cmd.extend(["--ref", ref])
        if metadata:
            cmd.extend(["--metadata", json.dumps(metadata, separators=(",", ":"))])
        if detail:
            cmd.extend(["--detail", detail])

        try:
            result = self.runner.run(
                cmd,
                cwd=self.project_path,
                env=self.project_env,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return AuditResult(ok=False, event_id=None, error=f"auditctl timed out after {timeout}s")

        if result.returncode != 0:
            return AuditResult(
                ok=False,
                event_id=None,
                error=(result.stderr or f"exit {result.returncode}").strip(),
            )

        try:
            payload = json.loads(result.stdout)
            return AuditResult(ok=True, event_id=payload.get("id"), error=None)
        except json.JSONDecodeError as exc:
            return AuditResult(ok=False, event_id=None, error=f"invalid JSON from auditctl: {exc}")
