from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .commands import CommandRunner


class ClientError(RuntimeError):
    pass


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
    ) -> dict:
        return self._run(
            [
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
        )

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
