from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = "~/.config/actionq-dispatcher/config.toml"
DAEMON_CONFIG = "~/.config/actionq/config.toml"


class ConfigError(ValueError):
    pass


@dataclass
class SprintctlTakeupConfig:
    enabled: bool = False
    remote_only: bool = True
    actor_prefix: str = "actionq"
    release_on_sprintctl_error: bool = False


@dataclass
class HarnessConfig:
    name: str
    bin: str
    kind: str
    default_model: str
    default_reasoning: str | None = None
    provider: str | None = None
    transport: str | None = None
    surface: str | None = None


@dataclass
class RoutingConfig:
    """Trusted runtime routing context.

    Caller identity is deliberately configuration/session metadata.  It is
    never populated from an action prompt or worker output.
    """

    policy_path: Path | None = None
    default_harness: str | None = None
    trusted_caller_harness: str | None = None
    caller_provider: str | None = None
    caller_transport: str | None = None
    caller_surface: str | None = None


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
    heartbeat_interval_seconds: int = 60
    graceful_shutdown_seconds: int = 30
    session_state_path: Path | None = None
    audit_enabled: bool = True
    fail_action_on_emit_error: bool = False
    sprintctl_takeup: SprintctlTakeupConfig = field(
        default_factory=SprintctlTakeupConfig
    )


@dataclass
class ProjectConfig:
    name: str
    path: Path
    base_ref: str = "HEAD"
    env: dict[str, str] | None = None
    default_harness: str | None = None
    default_model: str | None = None
    sprintctl_path: Path | None = None
    default_reasoning: str | None = None


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
    default_harness: str | None = None
    reasoning: str | None = None


@dataclass
class DispatcherConfig:
    path: Path
    global_config: GlobalConfig
    projects: dict[str, ProjectConfig]
    actions: dict[str, ActionConfig]
    harnesses: dict[str, HarnessConfig] = field(default_factory=dict)
    routing: RoutingConfig = field(default_factory=RoutingConfig)


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
    explicit = path or os.environ.get("DISPATCHER_CONFIG") or os.environ.get("ACTIONQ_CONFIG")
    if explicit:
        config_path = Path(os.path.expanduser(str(explicit)))
    else:
        # Check legacy dispatcher path first, then new actionq path.
        legacy = Path(os.path.expanduser(DEFAULT_CONFIG))
        daemon = Path(os.path.expanduser(DAEMON_CONFIG))
        config_path = legacy if legacy.exists() else daemon
    if not config_path.exists():
        raise ConfigError(f"Dispatcher config not found: {config_path}")

    raw = tomllib.loads(config_path.read_text())
    base = config_path.parent
    g = raw.get("global", {})
    worker_env = g.get("worker_env")
    if worker_env is not None and not isinstance(worker_env, dict):
        raise ConfigError("global.worker_env must be a table/object")
    sprintctl_takeup = g.get("sprintctl_takeup") or {}
    if not isinstance(sprintctl_takeup, dict):
        raise ConfigError("global.sprintctl_takeup must be a table/object")
    routing_raw = g.get("routing") or raw.get("routing") or {}
    if not isinstance(routing_raw, dict):
        raise ConfigError("routing must be a table/object")
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
        heartbeat_interval_seconds=int(g.get("heartbeat_interval_seconds", 60)),
        graceful_shutdown_seconds=int(g.get("graceful_shutdown_seconds", 30)),
        session_state_path=(
            _expand_path(g["session_state_path"], base=base)
            if g.get("session_state_path")
            else None
        ),
        audit_enabled=bool(g.get("audit", {}).get("enabled", True)),
        fail_action_on_emit_error=bool(g.get("audit", {}).get("fail_action_on_emit_error", False)),
        sprintctl_takeup=SprintctlTakeupConfig(
            enabled=bool(sprintctl_takeup.get("enabled", False)),
            remote_only=bool(sprintctl_takeup.get("remote_only", True)),
            actor_prefix=str(sprintctl_takeup.get("actor_prefix", "actionq")),
            release_on_sprintctl_error=bool(
                sprintctl_takeup.get("release_on_sprintctl_error", False)
            ),
        ),
    )

    harnesses: dict[str, HarnessConfig] = {}
    for name, item in raw.get("harnesses", {}).items():
        harnesses[name] = HarnessConfig(
            name=name,
            bin=str(item.get("bin", name)),
            kind=str(item.get("kind", name)),
            default_model=str(item.get("default_model", "")),
            default_reasoning=(
                str(item["default_reasoning"])
                if item.get("default_reasoning")
                else None
            ),
            provider=(str(item["provider"]) if item.get("provider") else None),
            transport=(str(item["transport"]) if item.get("transport") else None),
            surface=(str(item["surface"]) if item.get("surface") else None),
        )

    projects = {}
    for name, item in raw.get("projects", {}).items():
        env = item.get("env")
        if env is not None and not isinstance(env, dict):
            raise ConfigError(f"projects.{name}.env must be a table/object")
        sprintctl_path_raw = item.get("sprintctl_path")
        projects[name] = ProjectConfig(
            name=name,
            path=_expand_path(_required(item, "path", f"projects.{name}"), base=base),
            base_ref=item.get("base_ref", "HEAD"),
            env={str(key): str(value) for key, value in (env or {}).items()} or None,
            default_harness=item.get("default_harness") or None,
            default_model=item.get("default_model") or None,
            sprintctl_path=_expand_path(sprintctl_path_raw, base=base) if sprintctl_path_raw else None,
            default_reasoning=item.get("default_reasoning") or None,
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
            default_harness=item.get("default_harness") or None,
            reasoning=item.get("reasoning") or None,
        )

    if not actions:
        raise ConfigError("At least one action config is required")
    routing = RoutingConfig(
        policy_path=(
            _expand_path(str(routing_raw["policy_path"]), base=base)
            if routing_raw.get("policy_path")
            else None
        ),
        default_harness=(str(routing_raw["default_harness"]) if routing_raw.get("default_harness") else None),
        trusted_caller_harness=(
            str(routing_raw["trusted_caller_harness"])
            if routing_raw.get("trusted_caller_harness")
            else None
        ),
        caller_provider=(str(routing_raw["caller_provider"]) if routing_raw.get("caller_provider") else None),
        caller_transport=(
            str(routing_raw["caller_transport"]) if routing_raw.get("caller_transport") else None
        ),
        caller_surface=(str(routing_raw["caller_surface"]) if routing_raw.get("caller_surface") else None),
    )
    return DispatcherConfig(config_path, global_config, projects, actions, harnesses, routing)
