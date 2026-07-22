from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .acl import ACL, claude_tool_args
from .commands import CommandRunner, git_safe_env
from .config import ActionConfig, GlobalConfig


class WorkerError(RuntimeError):
    pass


@dataclass
class WorkerResult:
    stdout: str
    stderr: str


class ClaudeWorker:
    def __init__(self, global_config: GlobalConfig, runner: CommandRunner):
        self.global_config = global_config
        self.runner = runner

    def invoke(
        self,
        *,
        action_config: ActionConfig,
        worktree: Path,
        prompt: str,
        acl: ACL,
    ) -> WorkerResult:
        allowed, denied = claude_tool_args(acl)
        cmd = [
            self.global_config.claude_bin,
            "-p",
            "--model",
            action_config.model,
            "--output-format",
            "json",
            "--no-session-persistence",
            "--add-dir",
            str(worktree),
        ]
        if action_config.reasoning:
            cmd.extend(["--effort", action_config.reasoning])
        if allowed:
            cmd.extend(["--allowedTools", ",".join(allowed)])
        if denied:
            cmd.extend(["--disallowedTools", ",".join(denied)])
        result = self.runner.run(
            cmd,
            cwd=worktree,
            env=git_safe_env(worktree, self.global_config.worker_env),
            timeout=action_config.timeout_minutes * 60,
            input_text=prompt,
        )
        if result.returncode != 0:
            raise WorkerError(result.stderr or result.stdout or "worker failed")
        return WorkerResult(result.stdout, result.stderr)


class FakeCommitWorker:
    """Smoke worker that exercises worktree, commit, ACL, and validation paths."""

    def __init__(self, runner: CommandRunner):
        self.runner = runner

    def invoke(
        self,
        *,
        action_config: ActionConfig,
        worktree: Path,
        prompt: str,
        acl: ACL,
    ) -> WorkerResult:
        rel_path = action_config.fake_commit_path
        target = worktree / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "# actionq dispatch smoke\n\n"
            "This file was created by the actionq dispatcher fake worker.\n"
        )
        env = git_safe_env(worktree)
        add = self.runner.run(["git", "add", rel_path], cwd=worktree, env=env)
        if add.returncode != 0:
            raise WorkerError(add.stderr or add.stdout or "fake worker git add failed")
        commit = self.runner.run(
            [
                "git",
                "-c",
                "user.name=actionq smoke",
                "-c",
                "user.email=actionq-smoke@example.invalid",
                "commit",
                "-m",
                "actionq dispatch smoke",
            ],
            cwd=worktree,
            env=env,
        )
        if commit.returncode != 0:
            raise WorkerError(
                commit.stderr or commit.stdout or "fake worker git commit failed"
            )
        return WorkerResult(commit.stdout, commit.stderr)


class ConfiguredWorker:
    """Legacy one-shot worker.

    ``Dispatcher.run_once`` resolves routing before invoking this class and
    rejects non-Claude local routes. Multi-harness execution belongs to the
    long-running daemon's adapter registry; this worker must never silently
    reinterpret a Codex/OpenCode route as Claude.
    """

    def __init__(self, global_config: GlobalConfig, runner: CommandRunner):
        self.claude = ClaudeWorker(global_config, runner)
        self.fake = FakeCommitWorker(runner)

    def invoke(
        self,
        *,
        action_config: ActionConfig,
        worktree: Path,
        prompt: str,
        acl: ACL,
    ) -> WorkerResult:
        if action_config.runner in {"fake", "fake-commit"}:
            return self.fake.invoke(
                action_config=action_config,
                worktree=worktree,
                prompt=prompt,
                acl=acl,
            )
        if action_config.runner != "local":
            raise WorkerError(f"Unsupported runner for v1: {action_config.runner}")
        return self.claude.invoke(
            action_config=action_config,
            worktree=worktree,
            prompt=prompt,
            acl=acl,
        )
