from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ACLError(ValueError):
    pass


@dataclass
class ACL:
    version: int
    allowed_tools: list[str]
    path_allowlist: list[str]
    path_denylist: list[str]
    bash_allowlist: list[str]
    bash_denylist: list[str]
    network: str


def load_acl(path: Path, variables: dict[str, str]) -> ACL:
    raw = json.loads(path.read_text())

    def render_list(name: str) -> list[str]:
        return [str(item).format(**variables) for item in raw.get(name, [])]

    return ACL(
        version=int(raw.get("version", 1)),
        allowed_tools=list(raw.get("allowed_tools", [])),
        path_allowlist=render_list("path_allowlist"),
        path_denylist=render_list("path_denylist"),
        bash_allowlist=render_list("bash_allowlist"),
        bash_denylist=render_list("bash_denylist"),
        network=raw.get("network", "none"),
    )


def claude_tool_args(acl: ACL) -> tuple[list[str], list[str]]:
    allowed: list[str] = []
    for tool in acl.allowed_tools:
        normalized = tool.lower()
        if normalized == "read":
            allowed.append("Read")
        elif normalized == "edit":
            allowed.append("Edit")
        elif normalized == "grep":
            allowed.append("Grep")
        elif normalized == "bash":
            for prefix in acl.bash_allowlist:
                allowed.append(f"Bash({prefix}:*)")
        else:
            allowed.append(tool)

    denied = [f"Bash({prefix}:*)" for prefix in acl.bash_denylist]
    return allowed, denied


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_changed_paths(
    changed_paths: list[str],
    *,
    worktree: Path,
    acl: ACL,
) -> tuple[bool, str | None]:
    root = worktree.resolve()
    for rel in changed_paths:
        absolute = (root / rel).resolve(strict=False)
        if not _is_under(absolute, root):
            return False, f"changed path escapes worktree: {rel}"
        value = str(absolute)
        if any(fnmatch.fnmatch(value, pattern) for pattern in acl.path_denylist):
            return False, f"changed path denied by ACL: {rel}"
        if not any(fnmatch.fnmatch(value, pattern) for pattern in acl.path_allowlist):
            return False, f"changed path outside ACL allowlist: {rel}"
    return True, None
