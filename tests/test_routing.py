"""Tests for harness routing resolution (step 5)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from actionq_dispatcher.config import (
    ActionConfig,
    DispatcherConfig,
    GlobalConfig,
    HarnessConfig,
    ProjectConfig,
    RoutingConfig,
)
from actionq_dispatcher.routing import RoutingError, resolve_routing, same_provider_fallback


def _global_config(tmp_path: Path) -> GlobalConfig:
    return GlobalConfig(
        poll_interval_seconds=30,
        default_timeout_minutes=30,
        event_jsonl_path=None,
        budget_daily_usd=20.0,
        budget_window_hours=24,
        worktree_root=tmp_path / "worktrees",
        pause_file=tmp_path / "PAUSED",
        actionctl_bin="actionctl",
        claude_bin="claude",
    )


def _action_config(
    runner: str = "local",
    model: str = "claude-sonnet-5",
    default_harness: str | None = None,
    reasoning: str | None = None,
) -> ActionConfig:
    return ActionConfig(
        name="scope-iterate",
        model=model,
        runner=runner,
        prompt_template=Path("/tmp/prompt.md"),
        working_dir="{worktree_root}/{project}/{action.id}",
        tool_acl=Path("/tmp/acl.json"),
        timeout_minutes=30,
        max_cost_usd=0.0,
        test_command="pytest",
        pre_gates=[],
        post_gates=[],
        default_harness=default_harness,
        reasoning=reasoning,
    )


def _project_config(
    path: Path,
    default_harness: str | None = None,
    default_model: str | None = None,
    default_reasoning: str | None = None,
) -> ProjectConfig:
    return ProjectConfig(
        name="myproject",
        path=path,
        default_harness=default_harness,
        default_model=default_model,
        default_reasoning=default_reasoning,
    )


def _config(
    tmp_path: Path,
    harnesses: dict[str, HarnessConfig] | None = None,
    project_config: ProjectConfig | None = None,
    action_config: ActionConfig | None = None,
) -> DispatcherConfig:
    ac = action_config or _action_config()
    pc = project_config or _project_config(tmp_path / "repo")
    return DispatcherConfig(
        path=tmp_path / "config.toml",
        global_config=_global_config(tmp_path),
        projects={"myproject": pc},
        actions={"scope-iterate": ac},
        harnesses=harnesses or {},
    )


def _caller_policy(tmp_path: Path) -> Path:
    path = tmp_path / "model-routing.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "caller_harness_providers": {"claude": "anthropic", "codex": "codex", "kimi": "kimi"},
        "aliases": {
            "fast-build": {
                "anthropic": {"model": "claude-haiku", "verified": True},
                "codex": {
                    "model": "gpt-spark", "fallback": "gpt-luna", "verified": True,
                    "transport": "chatgpt", "surfaces": ["codex-cli"],
                },
            },
            "codex-only": {"codex": {"model": "gpt-sol", "verified": True}},
        },
    }), encoding="utf-8")
    return path


def _caller_config(tmp_path: Path, caller: str | None, *, transport="chatgpt", surface="codex-cli") -> DispatcherConfig:
    config = _config(
        tmp_path,
        harnesses={
            "claude": HarnessConfig("claude", "claude", "claude", ""),
            "codex": HarnessConfig("codex", "codex", "codex", ""),
        },
        project_config=_project_config(tmp_path / "repo", default_harness="caller"),
        action_config=_action_config(model="fast-build"),
    )
    config.routing = RoutingConfig(
        policy_path=_caller_policy(tmp_path), default_harness="caller",
        trusted_caller_harness=caller, caller_transport=transport, caller_surface=surface,
    )
    return config


def test_caller_mode_resolves_claude_provider_branch(tmp_path):
    config = _caller_config(tmp_path, "claude")
    result = resolve_routing({}, config.actions["scope-iterate"], config.projects["myproject"], config)
    assert (result.harness, result.provider, result.model) == ("claude", "anthropic", "claude-haiku")
    assert result.routing_source == "caller-inheritance"


def test_caller_mode_resolves_codex_spark_with_provenance(tmp_path):
    config = _caller_config(tmp_path, "codex")
    result = resolve_routing({}, config.actions["scope-iterate"], config.projects["myproject"], config)
    assert (result.harness, result.provider, result.model) == ("codex", "codex", "gpt-spark")
    assert result.fallback_model == "gpt-luna"
    assert result.transport == "chatgpt"
    assert result.surface == "codex-cli"


def test_explicit_harness_override_wins_caller_mode(tmp_path):
    config = _caller_config(tmp_path, "codex")
    result = resolve_routing(
        {"harness": "claude", "model": "fast-build"},
        config.actions["scope-iterate"], config.projects["myproject"], config,
    )
    assert (result.harness, result.provider, result.model) == ("claude", "anthropic", "claude-haiku")
    assert result.routing_source == "action-explicit"


def test_explicit_harness_uses_its_configured_transport(tmp_path):
    config = _caller_config(tmp_path, "claude", transport="anthropic-cli", surface="claude-code")
    config.harnesses["codex"].transport = "chatgpt"
    config.harnesses["codex"].surface = "codex-cli"
    result = resolve_routing(
        {"harness": "codex", "model": "fast-build"},
        config.actions["scope-iterate"], config.projects["myproject"], config,
    )
    assert result.model == "gpt-spark"
    assert result.fallback_reason is None


def test_caller_mode_without_trusted_identity_fails_with_multiple_harnesses(tmp_path):
    config = _caller_config(tmp_path, None)
    with pytest.raises(RoutingError, match="trusted_caller_harness"):
        resolve_routing({}, config.actions["scope-iterate"], config.projects["myproject"], config)


def test_unsupported_kimi_provider_branch_fails_closed(tmp_path):
    config = _caller_config(tmp_path, "kimi")
    with pytest.raises(RoutingError, match="no verified provider branch"):
        resolve_routing({}, config.actions["scope-iterate"], config.projects["myproject"], config)


def test_spark_transport_incompatibility_selects_same_provider_luna(tmp_path):
    config = _caller_config(tmp_path, "codex", transport="api", surface="api-worker")
    result = resolve_routing({}, config.actions["scope-iterate"], config.projects["myproject"], config)
    assert result.model == "gpt-luna"
    assert result.provider == "codex"
    assert result.fallback_reason == "transport-incompatible"


def test_spark_usage_limit_redispatch_stays_on_codex(tmp_path):
    config = _caller_config(tmp_path, "codex")
    primary = resolve_routing({}, config.actions["scope-iterate"], config.projects["myproject"], config)
    fallback = same_provider_fallback(primary, reason="confirmed Spark usage limit")
    assert (fallback.harness, fallback.provider, fallback.model) == ("codex", "codex", "gpt-luna")
    assert fallback.fallback_reason == "confirmed Spark usage limit"


def test_no_implicit_cross_provider_fallback(tmp_path):
    config = _caller_config(tmp_path, "claude")
    config.actions["scope-iterate"].model = "codex-only"
    with pytest.raises(RoutingError, match="no verified provider branch"):
        resolve_routing({}, config.actions["scope-iterate"], config.projects["myproject"], config)


# ---------------------------------------------------------------------------
# Action explicit
# ---------------------------------------------------------------------------


def test_action_explicit_harness_wins(tmp_path):
    config = _config(
        tmp_path,
        project_config=_project_config(tmp_path / "repo", default_harness="project-harness"),
        action_config=_action_config(runner="local"),
    )
    action = {"id": 1, "action_type": "scope-iterate", "project": "myproject", "target_ref": "1",
               "harness": "explicit-harness", "model": "explicit-model"}
    result = resolve_routing(action, config.actions["scope-iterate"], config.projects["myproject"], config)
    assert result.harness == "explicit-harness"
    assert result.model == "explicit-model"
    assert result.routing_source == "action-explicit"


def test_action_explicit_harness_uses_harness_default_model_when_no_action_model(tmp_path):
    harnesses = {"codex": HarnessConfig(name="codex", bin="codex", kind="codex", default_model="gpt-5")}
    config = _config(tmp_path, harnesses=harnesses)
    action = {"id": 1, "action_type": "scope-iterate", "project": "myproject", "target_ref": "1",
               "harness": "codex"}
    result = resolve_routing(action, config.actions["scope-iterate"], config.projects["myproject"], config)
    assert result.harness == "codex"
    assert result.model == "gpt-5"
    assert result.routing_source == "action-explicit"


def test_action_explicit_reasoning_wins(tmp_path):
    harnesses = {
        "codex": HarnessConfig(
            name="codex",
            bin="codex",
            kind="codex",
            default_model="gpt-5",
            default_reasoning="medium",
        )
    }
    config = _config(tmp_path, harnesses=harnesses, action_config=_action_config(reasoning="low"))
    action = {
        "id": 1,
        "action_type": "scope-iterate",
        "project": "myproject",
        "target_ref": "1",
        "harness": "codex",
        "reasoning": "high",
    }

    result = resolve_routing(action, config.actions["scope-iterate"], config.projects["myproject"], config)

    assert result.reasoning == "high"


# ---------------------------------------------------------------------------
# Project default
# ---------------------------------------------------------------------------


def test_project_default_used_when_no_action_explicit(tmp_path):
    config = _config(
        tmp_path,
        project_config=_project_config(tmp_path / "repo", default_harness="codex", default_model="gpt-5"),
        action_config=_action_config(runner="local"),
    )
    action = {"id": 1, "action_type": "scope-iterate", "project": "myproject", "target_ref": "1"}
    result = resolve_routing(action, config.actions["scope-iterate"], config.projects["myproject"], config)
    assert result.harness == "codex"
    assert result.model == "gpt-5"
    assert result.routing_source == "project-default"


def test_project_default_falls_back_to_action_model_when_no_project_model(tmp_path):
    config = _config(
        tmp_path,
        project_config=_project_config(tmp_path / "repo", default_harness="codex"),
        action_config=_action_config(runner="local", model="action-model"),
    )
    action = {"id": 1, "action_type": "scope-iterate", "project": "myproject", "target_ref": "1"}
    result = resolve_routing(action, config.actions["scope-iterate"], config.projects["myproject"], config)
    assert result.harness == "codex"
    assert result.model == "action-model"


def test_project_default_reasoning_wins_over_harness_and_action_defaults(tmp_path):
    harnesses = {
        "codex": HarnessConfig(
            name="codex",
            bin="codex",
            kind="codex",
            default_model="gpt-5",
            default_reasoning="medium",
        )
    }
    config = _config(
        tmp_path,
        harnesses=harnesses,
        project_config=_project_config(
            tmp_path / "repo", default_harness="codex", default_reasoning="high"
        ),
        action_config=_action_config(reasoning="low"),
    )
    action = {"id": 1, "action_type": "scope-iterate", "project": "myproject", "target_ref": "1"}

    result = resolve_routing(action, config.actions["scope-iterate"], config.projects["myproject"], config)

    assert result.reasoning == "high"


# ---------------------------------------------------------------------------
# Action-kind default
# ---------------------------------------------------------------------------


def test_action_kind_default_used_when_no_project_default(tmp_path):
    config = _config(
        tmp_path,
        project_config=_project_config(tmp_path / "repo"),
        action_config=_action_config(runner="local", model="sonnet"),
    )
    action = {"id": 1, "action_type": "scope-iterate", "project": "myproject", "target_ref": "1"}
    result = resolve_routing(action, config.actions["scope-iterate"], config.projects["myproject"], config)
    assert result.harness == "claude"
    assert result.model == "sonnet"
    assert result.routing_source == "runner-compatibility"


def test_action_kind_default_uses_action_reasoning(tmp_path):
    config = _config(
        tmp_path,
        project_config=_project_config(tmp_path / "repo"),
        action_config=_action_config(runner="local", reasoning="low"),
    )
    action = {"id": 1, "action_type": "scope-iterate", "project": "myproject", "target_ref": "1"}

    result = resolve_routing(action, config.actions["scope-iterate"], config.projects["myproject"], config)

    assert result.reasoning == "low"


def test_runner_local_maps_to_claude(tmp_path):
    config = _config(tmp_path, action_config=_action_config(runner="local"))
    action = {"id": 1, "action_type": "scope-iterate", "project": "myproject", "target_ref": "1"}
    result = resolve_routing(action, config.actions["scope-iterate"], None, config)
    assert result.harness == "claude"
    assert result.routing_source == "runner-compatibility"


def test_runner_fake_maps_to_fake(tmp_path):
    config = _config(tmp_path, action_config=_action_config(runner="fake", model="fake"))
    action = {"id": 1, "action_type": "scope-iterate", "project": "myproject", "target_ref": "1"}
    result = resolve_routing(action, config.actions["scope-iterate"], None, config)
    assert result.harness == "fake"
    assert result.routing_source == "runner-compatibility"


def test_explicit_default_harness_on_action_config(tmp_path):
    config = _config(
        tmp_path,
        action_config=_action_config(runner="local", model="codex-model", default_harness="codex"),
    )
    action = {"id": 1, "action_type": "scope-iterate", "project": "myproject", "target_ref": "1"}
    result = resolve_routing(action, config.actions["scope-iterate"], None, config)
    assert result.harness == "codex"
    assert result.model == "codex-model"
    assert result.routing_source == "action-class-override"


# ---------------------------------------------------------------------------
# Global fallback
# ---------------------------------------------------------------------------


def test_single_global_fallback_used_when_no_other_routing(tmp_path):
    harnesses = {
        "opencode": HarnessConfig(
            name="opencode",
            bin="opencode",
            kind="opencode",
            default_model="codestral",
            default_reasoning="medium",
        )
    }
    config = _config(
        tmp_path,
        harnesses=harnesses,
        project_config=_project_config(tmp_path / "repo"),
        action_config=_action_config(runner="unknown-runner", model="fallback-model"),
    )
    action = {"id": 1, "action_type": "scope-iterate", "project": "myproject", "target_ref": "1"}
    result = resolve_routing(action, config.actions["scope-iterate"], config.projects["myproject"], config)
    assert result.harness == "opencode"
    assert result.model == "codestral"
    assert result.reasoning == "medium"
    assert result.routing_source == "global-fallback"


def test_multiple_harnesses_no_routing_raises(tmp_path):
    harnesses = {
        "claude": HarnessConfig(name="claude", bin="claude", kind="claude", default_model="sonnet"),
        "codex": HarnessConfig(name="codex", bin="codex", kind="codex", default_model="gpt-5"),
    }
    config = _config(
        tmp_path,
        harnesses=harnesses,
        project_config=_project_config(tmp_path / "repo"),
        action_config=_action_config(runner="unknown-runner"),
    )
    action = {"id": 1, "action_type": "scope-iterate", "project": "myproject", "target_ref": "1"}
    with pytest.raises(RoutingError):
        resolve_routing(action, config.actions["scope-iterate"], config.projects["myproject"], config)


def test_no_harnesses_no_routing_raises(tmp_path):
    config = _config(
        tmp_path,
        harnesses={},
        project_config=_project_config(tmp_path / "repo"),
        action_config=_action_config(runner="unknown-runner"),
    )
    action = {"id": 1, "action_type": "scope-iterate", "project": "myproject", "target_ref": "1"}
    with pytest.raises(RoutingError):
        resolve_routing(action, config.actions["scope-iterate"], config.projects["myproject"], config)


# ---------------------------------------------------------------------------
# Priority: action explicit > project > action-kind
# ---------------------------------------------------------------------------


def test_priority_action_explicit_beats_project_default(tmp_path):
    config = _config(
        tmp_path,
        project_config=_project_config(tmp_path / "repo", default_harness="project-h", default_model="project-m"),
        action_config=_action_config(runner="local"),
    )
    action = {"id": 1, "action_type": "scope-iterate", "project": "myproject", "target_ref": "1",
               "harness": "action-h", "model": "action-m"}
    result = resolve_routing(action, config.actions["scope-iterate"], config.projects["myproject"], config)
    assert result.harness == "action-h"
    assert result.routing_source == "action-explicit"


def test_priority_action_class_override_beats_project_default(tmp_path):
    config = _config(
        tmp_path,
        project_config=_project_config(tmp_path / "repo", default_harness="project-h", default_model="project-m"),
        action_config=_action_config(runner="local", default_harness="action-kind-h"),
    )
    action = {"id": 1, "action_type": "scope-iterate", "project": "myproject", "target_ref": "1"}
    result = resolve_routing(action, config.actions["scope-iterate"], config.projects["myproject"], config)
    assert result.harness == "action-kind-h"
    assert result.routing_source == "action-class-override"


# ---------------------------------------------------------------------------
# Daemon integration: routing error rejects with harness-routing validator
# ---------------------------------------------------------------------------


def test_daemon_rejects_with_harness_routing_validator_when_routing_fails(tmp_path):
    """Runner not in known set + no harnesses + no project/action default → routing fails."""
    import subprocess as sp
    from actionq_dispatcher.commands import CommandRunner
    from actionq_dispatcher.config import SprintctlTakeupConfig
    from actionq_dispatcher.daemon import ActionqDaemon

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    sp.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    sp.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("hi")
    sp.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    sp.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    prompt = tmp_path / "prompt.md"
    prompt.write_text("Action:\n{action_json}\nSprint:\n{sprint_item_json}\nWorktree: {working_dir}\n"
                      "Branch: {branch_name}\nScope:\n{allowed_scope}\nTests: {test_command}\n")
    acl = tmp_path / "acl.json"
    acl.write_text('{"version":1,"allowed_tools":["read"],"path_allowlist":["{working_dir}/**"],'
                   '"path_denylist":[],"bash_allowlist":[],"bash_denylist":[],"network":"none"}')

    config = DispatcherConfig(
        path=tmp_path / "config.toml",
        global_config=GlobalConfig(
            poll_interval_seconds=0,
            default_timeout_minutes=5,
            event_jsonl_path=None,
            budget_daily_usd=20.0,
            budget_window_hours=24,
            worktree_root=tmp_path / "worktrees",
            pause_file=tmp_path / "PAUSED",
            actionctl_bin="actionctl",
            claude_bin="claude",
            session_state_path=tmp_path / "sessions.json",
        ),
        projects={"demo": ProjectConfig("demo", repo, "HEAD", env=None)},
        actions={
            "scope-iterate": ActionConfig(
                name="scope-iterate",
                model="some-model",
                runner="unknown-runner",  # not local/fake/fake-commit
                prompt_template=prompt,
                working_dir="{worktree_root}/{project}/{action.id}",
                tool_acl=acl,
                timeout_minutes=5,
                max_cost_usd=0.0,
                test_command="python3 -c pass",
                pre_gates=[],
                post_gates=[],
            )
        },
        harnesses={},  # no harnesses configured → no global fallback
    )

    class FakeActionctl:
        def __init__(self, action):
            self.action = action
            self.claims = 0
            self.emits: list = []
            self.rejections: list = []
            self.failures: list = []
            self.completions: list = []

        def claim(self, *, worker, timeout_minutes):
            self.claims += 1
            return self.action

        def emit(self, event_type, *, payload, actor, action_id=None):
            self.emits.append((event_type, payload, actor, action_id))
            return {}

        def reject(self, action_id, reason, validator, actor):
            self.rejections.append((action_id, reason, validator, actor))
            return {}

        def fail(self, action_id, reason, actor):
            self.failures.append((action_id, reason, actor))
            return {}

        def complete(self, action_id, result_ref, actor):
            self.completions.append((action_id, result_ref, actor))
            return {}

        def sessions(self, *, project=None, active_only=False, limit=100):
            return []

    actionctl = FakeActionctl({"id": 5, "action_type": "scope-iterate", "project": "demo", "target_ref": "1"})

    daemon = ActionqDaemon(
        config,
        daemon_id="devbox:testhost:1:abc",
        runner=CommandRunner(),
    )
    daemon._actionctl = actionctl
    daemon.run(max_iters=1)

    assert actionctl.rejections, "expected rejection due to routing failure"
    assert actionctl.rejections[0][2] == "harness-routing"
    assert not actionctl.completions
