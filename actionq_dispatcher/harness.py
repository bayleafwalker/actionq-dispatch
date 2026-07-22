from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .acl import claude_tool_args
from .commands import git_safe_env
from .config import ActionConfig
from .core import PreparedSession


class ProcessLike(Protocol):
    @property
    def pid(self) -> int: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[[list[str], Path, dict[str, str], str | None], ProcessLike]


class HarnessAdapter(Protocol):
    def start(
        self,
        action_config: ActionConfig,
        prepared: PreparedSession,
        process_factory: ProcessFactory,
    ) -> ProcessLike: ...


@dataclass
class ClaudeAdapter:
    """Launches Claude Code CLI in non-interactive (-p) mode."""

    bin: str
    worker_env: dict[str, str] | None

    def start(
        self,
        action_config: ActionConfig,
        prepared: PreparedSession,
        process_factory: ProcessFactory,
    ) -> ProcessLike:
        allowed, denied = claude_tool_args(prepared.acl)
        cmd = [
            self.bin,
            "-p",
            "--model",
            action_config.model,
            "--output-format",
            "json",
            "--no-session-persistence",
            "--add-dir",
            str(prepared.worktree),
        ]
        if action_config.reasoning:
            cmd.extend(["--effort", action_config.reasoning])
        if allowed:
            cmd.extend(["--allowedTools", ",".join(allowed)])
        if denied:
            cmd.extend(["--disallowedTools", ",".join(denied)])
        env = git_safe_env(prepared.worktree, self.worker_env)
        return process_factory(cmd, prepared.worktree, env, prepared.prompt)


@dataclass
class CodexAdapter:
    """Launches current Codex CLI non-interactively with prompt on stdin."""

    bin: str
    worker_env: dict[str, str] | None

    def start(
        self,
        action_config: ActionConfig,
        prepared: PreparedSession,
        process_factory: ProcessFactory,
    ) -> ProcessLike:
        cmd = [
            self.bin,
            "exec",
            "--skip-git-repo-check",
            "--json",
            "--sandbox",
            "workspace-write",
            "-C",
            str(prepared.worktree),
            "--model",
            action_config.model,
            "-",
        ]
        env = git_safe_env(prepared.worktree, self.worker_env)
        return process_factory(cmd, prepared.worktree, env, prepared.prompt)


@dataclass
class OpenCodeAdapter:
    """Launches OpenCode CLI in non-interactive (run) mode, prompt via stdin."""

    bin: str
    worker_env: dict[str, str] | None

    def start(
        self,
        action_config: ActionConfig,
        prepared: PreparedSession,
        process_factory: ProcessFactory,
    ) -> ProcessLike:
        cmd = [
            self.bin,
            "run",
            "--model",
            action_config.model,
        ]
        env = git_safe_env(prepared.worktree, self.worker_env)
        return process_factory(cmd, prepared.worktree, env, prepared.prompt)
