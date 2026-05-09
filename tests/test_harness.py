"""Unit tests for HarnessAdapter protocol and ClaudeAdapter (C-7), CodexAdapter and OpenCodeAdapter (C-8)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from actionq_dispatcher.acl import ACL
from actionq_dispatcher.config import ActionConfig
from actionq_dispatcher.harness import ClaudeAdapter, CodexAdapter, HarnessAdapter, OpenCodeAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _acl(*, allowed_tools=None, bash_allowlist=None, bash_denylist=None):
    return ACL(
        version=1,
        allowed_tools=allowed_tools or [],
        path_allowlist=[],
        path_denylist=[],
        bash_allowlist=bash_allowlist or [],
        bash_denylist=bash_denylist or [],
        network="none",
    )


def _action_config(model="claude-sonnet-4-6"):
    return ActionConfig(
        name="test-action",
        model=model,
        runner="local",
        prompt_template=Path("/dev/null"),
        working_dir="{worktree_root}/test",
        tool_acl=Path("/dev/null"),
        timeout_minutes=5,
        max_cost_usd=0.0,
        test_command="pytest",
        pre_gates=[],
        post_gates=[],
    )


def _prepared(worktree: Path, *, prompt="do the thing", acl=None):
    prep = MagicMock()
    prep.worktree = worktree
    prep.prompt = prompt
    prep.acl = acl or _acl()
    return prep


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_claude_adapter_satisfies_harness_adapter_protocol():
    # Runtime check: ClaudeAdapter instances are accepted as HarnessAdapter.
    adapter = ClaudeAdapter(bin="claude", worker_env=None)
    # Protocol is structural — isinstance check via runtime_checkable would
    # require @runtime_checkable; validate structurally by calling the method.
    assert callable(adapter.start)


# ---------------------------------------------------------------------------
# ClaudeAdapter.start — command construction
# ---------------------------------------------------------------------------


def test_start_builds_base_claude_command(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured = {}

    def fake_factory(cmd, cwd, env, prompt):
        captured.update(cmd=cmd, cwd=cwd, env=env, prompt=prompt)
        proc = MagicMock()
        proc.pid = 1
        return proc

    adapter = ClaudeAdapter(bin="claude", worker_env=None)
    adapter.start(_action_config(), _prepared(worktree), fake_factory)

    cmd = captured["cmd"]
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "--no-session-persistence" in cmd
    assert "--add-dir" in cmd
    assert cmd[cmd.index("--add-dir") + 1] == str(worktree)


def test_start_uses_custom_bin(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured = {}

    def fake_factory(cmd, cwd, env, prompt):
        captured["cmd"] = cmd
        proc = MagicMock()
        proc.pid = 1
        return proc

    adapter = ClaudeAdapter(bin="/usr/local/bin/claude-dev", worker_env=None)
    adapter.start(_action_config(), _prepared(worktree), fake_factory)

    assert captured["cmd"][0] == "/usr/local/bin/claude-dev"


def test_start_passes_prompt_to_factory(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured = {}

    def fake_factory(cmd, cwd, env, prompt):
        captured["prompt"] = prompt
        proc = MagicMock()
        proc.pid = 1
        return proc

    adapter = ClaudeAdapter(bin="claude", worker_env=None)
    adapter.start(_action_config(), _prepared(worktree, prompt="implement feature X"), fake_factory)

    assert captured["prompt"] == "implement feature X"


def test_start_passes_worktree_as_cwd(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured = {}

    def fake_factory(cmd, cwd, env, prompt):
        captured["cwd"] = cwd
        proc = MagicMock()
        proc.pid = 1
        return proc

    adapter = ClaudeAdapter(bin="claude", worker_env=None)
    adapter.start(_action_config(), _prepared(worktree), fake_factory)

    assert captured["cwd"] == worktree


# ---------------------------------------------------------------------------
# ACL → --allowedTools / --disallowedTools
# ---------------------------------------------------------------------------


def test_start_includes_allowed_tools_when_present(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured = {}

    def fake_factory(cmd, cwd, env, prompt):
        captured["cmd"] = cmd
        proc = MagicMock()
        proc.pid = 1
        return proc

    acl = _acl(allowed_tools=["read", "edit"])
    adapter = ClaudeAdapter(bin="claude", worker_env=None)
    adapter.start(_action_config(), _prepared(worktree, acl=acl), fake_factory)

    cmd = captured["cmd"]
    assert "--allowedTools" in cmd
    tools_value = cmd[cmd.index("--allowedTools") + 1]
    assert "Read" in tools_value
    assert "Edit" in tools_value


def test_start_omits_allowed_tools_when_empty(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured = {}

    def fake_factory(cmd, cwd, env, prompt):
        captured["cmd"] = cmd
        proc = MagicMock()
        proc.pid = 1
        return proc

    adapter = ClaudeAdapter(bin="claude", worker_env=None)
    adapter.start(_action_config(), _prepared(worktree, acl=_acl()), fake_factory)

    assert "--allowedTools" not in captured["cmd"]


def test_start_includes_disallowed_tools_from_bash_denylist(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured = {}

    def fake_factory(cmd, cwd, env, prompt):
        captured["cmd"] = cmd
        proc = MagicMock()
        proc.pid = 1
        return proc

    acl = _acl(bash_denylist=["rm -rf"])
    adapter = ClaudeAdapter(bin="claude", worker_env=None)
    adapter.start(_action_config(), _prepared(worktree, acl=acl), fake_factory)

    cmd = captured["cmd"]
    assert "--disallowedTools" in cmd
    denied_value = cmd[cmd.index("--disallowedTools") + 1]
    assert "rm -rf" in denied_value


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def test_start_env_includes_git_safe_directory(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured = {}

    def fake_factory(cmd, cwd, env, prompt):
        captured["env"] = env
        proc = MagicMock()
        proc.pid = 1
        return proc

    adapter = ClaudeAdapter(bin="claude", worker_env=None)
    adapter.start(_action_config(), _prepared(worktree), fake_factory)

    env = captured["env"]
    # git_safe_env injects GIT_CONFIG_KEY_*/GIT_CONFIG_VALUE_* entries.
    safe_dirs = [v for k, v in env.items() if k.startswith("GIT_CONFIG_VALUE_")]
    assert str(worktree) in safe_dirs


def test_start_merges_worker_env(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured = {}

    def fake_factory(cmd, cwd, env, prompt):
        captured["env"] = env
        proc = MagicMock()
        proc.pid = 1
        return proc

    adapter = ClaudeAdapter(bin="claude", worker_env={"MY_TOKEN": "secret"})
    adapter.start(_action_config(), _prepared(worktree), fake_factory)

    assert captured["env"].get("MY_TOKEN") == "secret"


def test_start_returns_process_from_factory(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    sentinel = MagicMock()
    sentinel.pid = 42

    adapter = ClaudeAdapter(bin="claude", worker_env=None)
    result = adapter.start(
        _action_config(),
        _prepared(worktree),
        lambda cmd, cwd, env, prompt: sentinel,
    )

    assert result is sentinel


# ---------------------------------------------------------------------------
# CodexAdapter (C-8)
# ---------------------------------------------------------------------------


def test_codex_adapter_satisfies_harness_adapter_protocol():
    adapter = CodexAdapter(bin="codex", worker_env=None)
    assert callable(adapter.start)


def test_codex_start_builds_command(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured = {}

    def fake_factory(cmd, cwd, env, prompt):
        captured.update(cmd=cmd, cwd=cwd, prompt=prompt)
        proc = MagicMock()
        proc.pid = 1
        return proc

    adapter = CodexAdapter(bin="codex", worker_env=None)
    adapter.start(_action_config(model="gpt-4o"), _prepared(worktree, prompt="fix the tests"), fake_factory)

    cmd = captured["cmd"]
    assert cmd[0] == "codex"
    assert "--approval-mode" in cmd
    assert cmd[cmd.index("--approval-mode") + 1] == "full-auto"
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "gpt-4o"
    assert cmd[-1] == "fix the tests"


def test_codex_start_passes_prompt_as_positional_arg(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured = {}

    def fake_factory(cmd, cwd, env, prompt):
        captured.update(cmd=cmd, prompt=prompt)
        proc = MagicMock()
        proc.pid = 1
        return proc

    adapter = CodexAdapter(bin="codex", worker_env=None)
    adapter.start(_action_config(), _prepared(worktree, prompt="refactor auth"), fake_factory)

    # Prompt is the last positional arg, NOT passed via stdin.
    assert captured["cmd"][-1] == "refactor auth"
    assert captured["prompt"] is None


def test_codex_start_uses_worktree_as_cwd(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured = {}

    def fake_factory(cmd, cwd, env, prompt):
        captured["cwd"] = cwd
        proc = MagicMock()
        proc.pid = 1
        return proc

    CodexAdapter(bin="codex", worker_env=None).start(
        _action_config(), _prepared(worktree), fake_factory
    )

    assert captured["cwd"] == worktree


def test_codex_start_merges_worker_env(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured = {}

    def fake_factory(cmd, cwd, env, prompt):
        captured["env"] = env
        proc = MagicMock()
        proc.pid = 1
        return proc

    CodexAdapter(bin="codex", worker_env={"OPENAI_API_KEY": "sk-test"}).start(
        _action_config(), _prepared(worktree), fake_factory
    )

    assert captured["env"].get("OPENAI_API_KEY") == "sk-test"


def test_codex_start_uses_custom_bin(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured = {}

    def fake_factory(cmd, cwd, env, prompt):
        captured["cmd"] = cmd
        proc = MagicMock()
        proc.pid = 1
        return proc

    CodexAdapter(bin="/usr/local/bin/codex-cli", worker_env=None).start(
        _action_config(), _prepared(worktree), fake_factory
    )

    assert captured["cmd"][0] == "/usr/local/bin/codex-cli"


# ---------------------------------------------------------------------------
# OpenCodeAdapter (C-8)
# ---------------------------------------------------------------------------


def test_opencode_adapter_satisfies_harness_adapter_protocol():
    adapter = OpenCodeAdapter(bin="opencode", worker_env=None)
    assert callable(adapter.start)


def test_opencode_start_builds_command(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured = {}

    def fake_factory(cmd, cwd, env, prompt):
        captured.update(cmd=cmd, cwd=cwd, prompt=prompt)
        proc = MagicMock()
        proc.pid = 1
        return proc

    adapter = OpenCodeAdapter(bin="opencode", worker_env=None)
    adapter.start(_action_config(model="codestral"), _prepared(worktree, prompt="add feature"), fake_factory)

    cmd = captured["cmd"]
    assert cmd[0] == "opencode"
    assert cmd[1] == "run"
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "codestral"


def test_opencode_start_passes_prompt_via_stdin(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured = {}

    def fake_factory(cmd, cwd, env, prompt):
        captured["prompt"] = prompt
        proc = MagicMock()
        proc.pid = 1
        return proc

    OpenCodeAdapter(bin="opencode", worker_env=None).start(
        _action_config(), _prepared(worktree, prompt="implement login"), fake_factory
    )

    assert captured["prompt"] == "implement login"


def test_opencode_start_uses_worktree_as_cwd(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured = {}

    def fake_factory(cmd, cwd, env, prompt):
        captured["cwd"] = cwd
        proc = MagicMock()
        proc.pid = 1
        return proc

    OpenCodeAdapter(bin="opencode", worker_env=None).start(
        _action_config(), _prepared(worktree), fake_factory
    )

    assert captured["cwd"] == worktree


def test_opencode_start_merges_worker_env(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured = {}

    def fake_factory(cmd, cwd, env, prompt):
        captured["env"] = env
        proc = MagicMock()
        proc.pid = 1
        return proc

    OpenCodeAdapter(bin="opencode", worker_env={"ANTHROPIC_API_KEY": "ak-test"}).start(
        _action_config(), _prepared(worktree), fake_factory
    )

    assert captured["env"].get("ANTHROPIC_API_KEY") == "ak-test"


def test_opencode_start_uses_custom_bin(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    captured = {}

    def fake_factory(cmd, cwd, env, prompt):
        captured["cmd"] = cmd
        proc = MagicMock()
        proc.pid = 1
        return proc

    OpenCodeAdapter(bin="/opt/opencode/bin/opencode", worker_env=None).start(
        _action_config(), _prepared(worktree), fake_factory
    )

    assert captured["cmd"][0] == "/opt/opencode/bin/opencode"
