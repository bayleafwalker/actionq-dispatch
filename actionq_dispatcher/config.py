from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = "~/.config/actionq-dispatcher/config.toml"


class ConfigError(ValueError):
    pass


@dataclass
class GlobalConfig:
    poll_interval_seconds: int
    default_timeout_minutes: int
    event_jsonl_path: Path | None
    budget_daily_usd: float
    budget_window_hours: int
    worktree_root: Path
    pause_file: Path
    actionctl_bin: str
    claude_bin: str
    worker_env: dict[str, str] | None = None


@dataclass
class ProjectConfig:
    name: str
    path: Path
    base_ref: str = "HEAD"
    env: dict[str, str] | None = None


@dataclass
class ActionConfig:
    name: str
    model: str
    runner: str
    prompt_template: Path
    working_dir: str
    tool_acl: Path
    timeout_minutes: int
    max_cost_usd: float
    test_command: str
    pre_gates: list[str]
    post_gates: list[str]
    fake_commit_path: str = "docs/actionq-dispatch-smoke.md"


@dataclass
class DispatcherConfig:
    path: Path
    global_config: GlobalConfig
    projects: dict[str, ProjectConfig]
    actions: dict[str, ActionConfig]


def _expand_path(raw: str, *, base: Path | None = None) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not path.is_absolute() and base is not None:
        return (base / path).resolve()
    return path


def _required(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"Missing {where}.{key}")
    return mapping[key]


def load_config(path: str | Path | None = None) -> DispatcherConfig:
    config_path = Path(
        os.path.expanduser(
            str(path or os.environ.get("DISPATCHER_CONFIG", DEFAULT_CONFIG))
        )
    )
    if not config_path.exists():
        raise ConfigError(f"Dispatcher config not found: {config_path}")

    raw = tomllib.loads(config_path.read_text())
    base = config_path.parent
    g = raw.get("global", {})
    worker_env = g.get("worker_env")
    if worker_env is not None and not isinstance(worker_env, dict):
        raise ConfigError("global.worker_env must be a table/object")
    global_config = GlobalConfig(
        poll_interval_seconds=int(g.get("poll_interval_seconds", 30)),
        default_timeout_minutes=int(g.get("default_timeout_minutes", 30)),
        event_jsonl_path=(
            _expand_path(g["event_jsonl_path"], base=base)
            if g.get("event_jsonl_path")
            else None
        ),
        budget_daily_usd=float(g.get("budget_daily_usd", 20.0)),
        budget_window_hours=int(g.get("budget_window_hours", 24)),
        worktree_root=_expand_path(
            g.get("worktree_root", "~/.local/state/actionq-dispatcher/worktrees"),
            base=base,
        ),
        pause_file=_expand_path(
            g.get("pause_file", "~/.local/state/actionq-dispatcher/PAUSED"),
            base=base,
        ),
        actionctl_bin=g.get("actionctl_bin", "actionctl"),
        claude_bin=g.get("claude_bin", "claude"),
        worker_env={str(key): str(value) for key, value in (worker_env or {}).items()}
        or None,
    )

    projects = {}
    for name, item in raw.get("projects", {}).items():
        env = item.get("env")
        if env is not None and not isinstance(env, dict):
            raise ConfigError(f"projects.{name}.env must be a table/object")
        projects[name] = ProjectConfig(
            name=name,
            path=_expand_path(_required(item, "path", f"projects.{name}"), base=base),
            base_ref=item.get("base_ref", "HEAD"),
            env={str(key): str(value) for key, value in (env or {}).items()} or None,
        )

    actions = {}
    for name, item in raw.get("actions", {}).items():
        actions[name] = ActionConfig(
            name=name,
            model=_required(item, "model", f"actions.{name}"),
            runner=item.get("runner", "local"),
            prompt_template=_expand_path(
                _required(item, "prompt_template", f"actions.{name}"),
                base=base,
            ),
            working_dir=item.get(
                "working_dir", "{worktree_root}/{project}/{action.id}"
            ),
            tool_acl=_expand_path(_required(item, "tool_acl", f"actions.{name}"), base=base),
            timeout_minutes=int(
                item.get("timeout_minutes", global_config.default_timeout_minutes)
            ),
            max_cost_usd=float(item.get("max_cost_usd", 0.0)),
            test_command=item.get("test_command", "pytest"),
            pre_gates=list(item.get("pre_gates", [])),
            post_gates=list(item.get("post_gates", [])),
            fake_commit_path=item.get(
                "fake_commit_path", "docs/actionq-dispatch-smoke.md"
            ),
        )

    if not actions:
        raise ConfigError("At least one action config is required")
    return DispatcherConfig(config_path, global_config, projects, actions)
