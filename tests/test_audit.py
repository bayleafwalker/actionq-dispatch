"""Tests for AuditctlClient and daemon audit integration."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from actionq_dispatcher.clients import AuditResult, AuditctlClient
from actionq_dispatcher.commands import CommandResult, CommandRunner
from actionq_dispatcher.config import (
    ActionConfig,
    DispatcherConfig,
    GlobalConfig,
    ProjectConfig,
)
from actionq_dispatcher.daemon import ActionqDaemon, _ImmediateProcess
from actionq_dispatcher.session import load_session_state


# ---------------------------------------------------------------------------
# AuditctlClient unit tests (mock subprocess.run)
# ---------------------------------------------------------------------------

_GOOD_RESPONSE = json.dumps({
    "id": "ad:01ABCDEFGHJKMNPQRSTVWXYZ01",
    "ts": "2026-04-28T09:00:00Z",
    "type": "session.start",
    "source": "actionq-daemon",
    "repo_id": "demo",
    "ndjson_path": "/tmp/events-2026-04-28.ndjson",
})


def _mock_proc(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _client(tmp_path: Path) -> AuditctlClient:
    return AuditctlClient(tmp_path, CommandRunner())


def test_auditctl_success(tmp_path: Path) -> None:
    with patch("actionq_dispatcher.commands.subprocess.run", return_value=_mock_proc(0, _GOOD_RESPONSE)):
        result = _client(tmp_path).add(
            type_="session.start",
            actor="actionq:aqs:abc",
            summary="test session",
        )
    assert result.ok is True
    assert result.event_id == "ad:01ABCDEFGHJKMNPQRSTVWXYZ01"
    assert result.error is None


def test_auditctl_nonzero_exit(tmp_path: Path) -> None:
    with patch("actionq_dispatcher.commands.subprocess.run", return_value=_mock_proc(1, stderr="AUDITCTL_ARTIFACTS_ROOT is required")):
        result = _client(tmp_path).add(
            type_="session.start",
            actor="actionq:aqs:abc",
            summary="test",
        )
    assert result.ok is False
    assert result.event_id is None
    assert "AUDITCTL_ARTIFACTS_ROOT" in (result.error or "")


def test_auditctl_timeout(tmp_path: Path) -> None:
    with patch(
        "actionq_dispatcher.commands.subprocess.run",
        side_effect=subprocess.TimeoutExpired([], 15),
    ):
        result = _client(tmp_path).add(
            type_="session.start",
            actor="actionq:aqs:abc",
            summary="test",
            timeout=15,
        )
    assert result.ok is False
    assert result.event_id is None
    assert "timed out" in (result.error or "")


def test_auditctl_invalid_json_response(tmp_path: Path) -> None:
    with patch("actionq_dispatcher.commands.subprocess.run", return_value=_mock_proc(0, "not-json")):
        result = _client(tmp_path).add(
            type_="session.start",
            actor="actionq:aqs:abc",
            summary="test",
        )
    assert result.ok is False
    assert "invalid JSON" in (result.error or "")


def test_auditctl_builds_correct_command(tmp_path: Path) -> None:
    captured: list = []

    def fake_run(args, **kwargs):
        captured.append(args)
        return _mock_proc(0, _GOOD_RESPONSE)

    with patch("actionq_dispatcher.commands.subprocess.run", side_effect=fake_run):
        _client(tmp_path).add(
            type_="session.start",
            actor="actionq:aqs:abc",
            summary="test summary",
            refs=["wi:42", "sprint:1"],
            metadata={"action_id": 99},
            source="actionq-daemon",
        )

    cmd = captured[0]
    assert "direnv" in cmd
    assert "exec" in cmd
    assert "--type" in cmd and "session.start" in cmd
    assert "--source" in cmd and "actionq-daemon" in cmd
    assert "--ref" in cmd
    # Both refs present
    ref_indices = [i for i, x in enumerate(cmd) if x == "--ref"]
    assert len(ref_indices) == 2
    assert cmd[ref_indices[0] + 1] == "wi:42"
    assert cmd[ref_indices[1] + 1] == "sprint:1"
    assert "--json" in cmd
    assert "--metadata" in cmd


# ---------------------------------------------------------------------------
# Daemon integration tests with fake audit factory
# ---------------------------------------------------------------------------


class FakeActionctl:
    def __init__(self, action=None):
        self.action = action
        self.emits: list[tuple] = []
        self.completions: list[tuple] = []
        self.failures: list[tuple] = []
        self.rejections: list[tuple] = []
        self.claims = 0

    def claim(self, *, worker, timeout_minutes):
        self.claims += 1
        return self.action

    def complete(self, action_id, result_ref, actor):
        self.completions.append((action_id, result_ref, actor))
        return {}

    def fail(self, action_id, reason, actor):
        self.failures.append((action_id, reason, actor))
        return {}

    def reject(self, action_id, reason, validator, actor):
        self.rejections.append((action_id, reason, validator, actor))
        return {}

    def emit(self, event_type, *, payload, actor, action_id=None):
        self.emits.append((event_type, payload, actor, action_id))
        return {}

    def events(self, *, event_type=None, since=None, limit=1000):
        return []


class FakeSprintctl:
    def item_show(self, item_id):
        return {"item": {"id": item_id, "sprint_id": 1, "status": "pending"}, "active_claims": []}

    def claim_start(self, *, item_id, actor, ttl_seconds, branch, worktree, runtime_session_id=None):
        return {"claim_id": 1, "claim_token": "tok"}

    def done_from_claim(self, *, item_id, claim_id, claim_token, actor):
        return {}

    def block_with_claim(self, *, item_id, claim_id, claim_token, actor):
        return {}

    def release_claim(self, *, claim_id, claim_token, actor):
        pass

    def coordination_failure(self, **kwargs):
        return {}


class FakeAuditctl:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self._fail = fail
        self._counter = 0

    def add(self, *, type_, actor, summary, refs=None, metadata=None, detail=None, source="actionq-daemon", timeout=15) -> AuditResult:
        self._counter += 1
        self.calls.append({"type_": type_, "actor": actor, "summary": summary, "refs": refs, "metadata": metadata})
        if self._fail:
            return AuditResult(ok=False, event_id=None, error="mock audit failure")
        event_id = f"ad:{'0' * 25}{self._counter}"
        return AuditResult(ok=True, event_id=event_id, error=None)


def _git(repo: Path, *args):
    import subprocess as sp
    sp.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# demo\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


def _config(tmp_path: Path, repo: Path) -> DispatcherConfig:
    prompt = tmp_path / "prompt.md"
    prompt.write_text(
        "Action:\n{action_json}\nSprint:\n{sprint_item_json}\n"
        "Worktree: {working_dir}\nBranch: {branch_name}\n"
        "Scope:\n{allowed_scope}\nTests: {test_command}\n"
    )
    acl = tmp_path / "acl.json"
    acl.write_text(
        '{"version":1,"allowed_tools":["read"],'
        '"path_allowlist":["{working_dir}/**"],'
        '"path_denylist":[],'
        '"bash_allowlist":[],"bash_denylist":[],"network":"none"}'
    )
    return DispatcherConfig(
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
            heartbeat_interval_seconds=1,
            graceful_shutdown_seconds=2,
            session_state_path=tmp_path / "sessions.json",
            audit_enabled=True,
            fail_action_on_emit_error=False,
        ),
        projects={"demo": ProjectConfig("demo", repo, "HEAD", env=None)},
        actions={
            "scope-iterate": ActionConfig(
                name="scope-iterate",
                model="fake",
                runner="fake",
                prompt_template=prompt,
                working_dir="{worktree_root}/{project}/{action.id}",
                tool_acl=acl,
                timeout_minutes=5,
                max_cost_usd=0.0,
                test_command="python3 -c pass",
                pre_gates=[],
                post_gates=[],
                fake_commit_path="docs/actionq-dispatch-smoke.md",
            )
        },
    )


def _action():
    return {"id": 42, "action_type": "scope-iterate", "project": "demo", "target_ref": "1"}


def _make_daemon(config, actionctl, sprintctl, audit_factory=None):
    daemon = ActionqDaemon(
        config,
        daemon_id="devbox:testhost:1:abc",
        runner=CommandRunner(),
        sprintctl_factory=lambda project: sprintctl,
        audit_factory=audit_factory,
    )
    daemon._actionctl = actionctl
    return daemon


def test_audit_events_emitted_in_order(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)
    fake_audit = FakeAuditctl()

    actionctl = FakeActionctl(action=_action())
    daemon = _make_daemon(config, actionctl, FakeSprintctl(), audit_factory=lambda _: fake_audit)
    daemon.run(max_iters=1)

    types = [c["type_"] for c in fake_audit.calls]
    assert types.index("dispatch.queued") < types.index("dispatch.started")
    assert types.index("dispatch.started") < types.index("session.start")
    assert types.index("session.start") < types.index("session.exit")


def test_session_exit_cross_refs_session_start(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)
    fake_audit = FakeAuditctl()

    actionctl = FakeActionctl(action=_action())
    daemon = _make_daemon(config, actionctl, FakeSprintctl(), audit_factory=lambda _: fake_audit)
    daemon.run(max_iters=1)

    start_call = next(c for c in fake_audit.calls if c["type_"] == "session.start")
    start_idx = fake_audit.calls.index(start_call)
    expected_event_id = f"ad:{'0' * 25}{start_idx + 1}"

    exit_call = next(c for c in fake_audit.calls if c["type_"] == "session.exit")
    assert exit_call.get("refs") == [expected_event_id], (
        f"session.exit should ref session.start event_id, got {exit_call.get('refs')}"
    )


def test_audit_status_ok_in_session_exited_payload(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)
    fake_audit = FakeAuditctl()

    actionctl = FakeActionctl(action=_action())
    daemon = _make_daemon(config, actionctl, FakeSprintctl(), audit_factory=lambda _: fake_audit)
    daemon.run(max_iters=1)

    exited = next(e for e in actionctl.emits if e[0] == "session.exited")
    assert exited[1]["audit_status"] == "ok"
    assert "audit_error" not in exited[1]


def test_audit_status_failed_when_audit_fails(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)
    fake_audit = FakeAuditctl(fail=True)

    actionctl = FakeActionctl(action=_action())
    daemon = _make_daemon(config, actionctl, FakeSprintctl(), audit_factory=lambda _: fake_audit)
    daemon.run(max_iters=1)

    # Action still completes despite audit failure.
    assert actionctl.completions, "action should complete even when audit fails"

    exited = next(e for e in actionctl.emits if e[0] == "session.exited")
    assert exited[1]["audit_status"] == "failed"
    assert "mock audit failure" in (exited[1].get("audit_error") or "")


def test_fail_on_audit_error_rejects_before_harness(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)
    config.global_config.fail_action_on_emit_error = True
    # Re-set on daemon since __init__ reads it at construction:
    fake_audit = FakeAuditctl(fail=True)

    actionctl = FakeActionctl(action=_action())
    daemon = _make_daemon(config, actionctl, FakeSprintctl(), audit_factory=lambda _: fake_audit)
    daemon._fail_on_audit_error = True
    daemon.run(max_iters=1)

    # Should reject, not complete, and not start a harness.
    assert actionctl.rejections, "expected rejection when dispatch.queued audit fails with fail_on_error"
    assert not actionctl.completions

    # Only dispatch.queued should have been called before the rejection.
    types = [c["type_"] for c in fake_audit.calls]
    assert types == ["dispatch.queued"]


def test_no_audit_when_disabled(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)
    config.global_config.audit_enabled = False
    fake_audit = FakeAuditctl()

    actionctl = FakeActionctl(action=_action())
    daemon = _make_daemon(config, actionctl, FakeSprintctl(), audit_factory=lambda _: fake_audit)
    daemon._audit_enabled = False
    daemon.run(max_iters=1)

    assert fake_audit.calls == [], "no audit calls expected when audit is disabled"

    exited = next(e for e in actionctl.emits if e[0] == "session.exited")
    assert exited[1]["audit_status"] == "skipped"


def test_session_exited_payload_skipped_when_no_project(tmp_path):
    """Actions without a recognised project get audit_status=skipped."""
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)
    fake_audit = FakeAuditctl()

    action = {"id": 7, "action_type": "scope-iterate", "project": "unknown-project", "target_ref": "1"}
    actionctl = FakeActionctl(action=action)
    daemon = _make_daemon(config, actionctl, FakeSprintctl(), audit_factory=lambda _: fake_audit)
    daemon.run(max_iters=1)

    exited_events = [e for e in actionctl.emits if e[0] == "session.exited"]
    if exited_events:
        assert exited_events[0][1]["audit_status"] == "skipped"


def test_pr_open_emitted_after_completed_session(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)
    fake_audit = FakeAuditctl()

    actionctl = FakeActionctl(action=_action())
    daemon = _make_daemon(config, actionctl, FakeSprintctl(), audit_factory=lambda _: fake_audit)
    daemon._query_pull_request = lambda project_path, branch, env: {  # type: ignore[method-assign]
        "number": 17,
        "state": "OPEN",
        "headRefName": branch,
    }
    daemon.run(max_iters=1)

    types = [c["type_"] for c in fake_audit.calls]
    assert "pr.open" in types
    assert "pr.merge" not in types
    assert types.index("session.exit") < types.index("pr.open")

    pr_open = next(c for c in fake_audit.calls if c["type_"] == "pr.open")
    assert pr_open["metadata"]["pr_number"] == 17
    assert pr_open["metadata"]["branch"] == "agent/scope-iterate/42"


def test_pr_merge_emits_open_and_merge(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)
    fake_audit = FakeAuditctl()

    actionctl = FakeActionctl(action=_action())
    daemon = _make_daemon(config, actionctl, FakeSprintctl(), audit_factory=lambda _: fake_audit)
    daemon._query_pull_request = lambda project_path, branch, env: {  # type: ignore[method-assign]
        "number": 18,
        "state": "MERGED",
        "headRefName": branch,
    }
    daemon.run(max_iters=1)

    types = [c["type_"] for c in fake_audit.calls]
    assert "pr.open" in types
    assert "pr.merge" in types
    assert types.index("pr.open") < types.index("pr.merge")


def test_no_pr_audit_events_when_branch_has_no_pr(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)
    fake_audit = FakeAuditctl()

    actionctl = FakeActionctl(action=_action())
    daemon = _make_daemon(config, actionctl, FakeSprintctl(), audit_factory=lambda _: fake_audit)
    daemon._query_pull_request = lambda project_path, branch, env: None  # type: ignore[method-assign]
    daemon.run(max_iters=1)

    types = [c["type_"] for c in fake_audit.calls]
    assert "pr.open" not in types
    assert "pr.merge" not in types


def test_pr_audit_events_not_emitted_when_failed(tmp_path):
    """PR audit events are gated on outcome=completed; a failed session emits nothing."""
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)
    fake_audit = FakeAuditctl()

    actionctl = FakeActionctl(action=_action())
    daemon = _make_daemon(config, actionctl, FakeSprintctl(), audit_factory=lambda _: fake_audit)
    # Wire up a PR so the gate is the outcome check, not an absent PR.
    daemon._query_pull_request = lambda project_path, branch, env: {  # type: ignore[method-assign]
        "number": 19,
        "state": "OPEN",
        "headRefName": branch,
    }
    # Force exit_code=1 → settle returns "failed".
    daemon._start_harness = lambda action_config, prepared, is_fake, *, harness_name: _ImmediateProcess(1)  # type: ignore[method-assign]
    daemon.run(max_iters=1)

    types = [c["type_"] for c in fake_audit.calls]
    assert "pr.open" not in types
    assert "pr.merge" not in types
    assert "session.exit" in types


def test_pr_events_cross_ref_session_exit(tmp_path):
    """pr.open and pr.merge refs contain the session.exit event_id (not a double-prefixed copy)."""
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)
    fake_audit = FakeAuditctl()

    actionctl = FakeActionctl(action=_action())
    daemon = _make_daemon(config, actionctl, FakeSprintctl(), audit_factory=lambda _: fake_audit)
    daemon._query_pull_request = lambda project_path, branch, env: {  # type: ignore[method-assign]
        "number": 20,
        "state": "MERGED",
        "headRefName": branch,
    }
    daemon.run(max_iters=1)

    exit_call = next(c for c in fake_audit.calls if c["type_"] == "session.exit")
    exit_idx = fake_audit.calls.index(exit_call)
    expected_exit_event_id = f"ad:{'0' * 25}{exit_idx + 1}"

    pr_open = next(c for c in fake_audit.calls if c["type_"] == "pr.open")
    pr_merge = next(c for c in fake_audit.calls if c["type_"] == "pr.merge")
    assert pr_open.get("refs") == [expected_exit_event_id], (
        f"pr.open should ref session.exit event_id, got {pr_open.get('refs')}"
    )
    assert pr_merge.get("refs") == [expected_exit_event_id], (
        f"pr.merge should ref session.exit event_id, got {pr_merge.get('refs')}"
    )
