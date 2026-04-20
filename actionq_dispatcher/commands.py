from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
        input_text: str | None = None,
    ) -> CommandResult:
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            timeout=timeout,
            text=True,
            input=input_text,
            capture_output=True,
            check=False,
        )
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def git_safe_env(path: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    """Return env overrides that mark a dispatcher-owned repo/worktree safe for git."""
    result = dict(env or {})
    try:
        count = int(result.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    result[f"GIT_CONFIG_KEY_{count}"] = "safe.directory"
    result[f"GIT_CONFIG_VALUE_{count}"] = str(path)
    result["GIT_CONFIG_COUNT"] = str(count + 1)
    return result
