"""Step 1 daemon tests: poll loop, session lifecycle events, signal handling."""
from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from actionq_dispatcher.config import (
    ActionConfig,
    DispatcherConfig,
    GlobalConfig,
    ProjectConfig,
)
from actionq_dispatcher.daemon import ActionqDaemon, _ImmediateProcess
from actionq_dispatcher.session import SessionRecord, load_session_state, save_session_state


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class FakeActionctl:
    def __init__(self, action=None, *, active_sessions=None):
        self.action = action
        self.active_sessions = active_sessions or []
        self.emits: list[tuple[str, dict, str, int | None]] = []
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

    def sessions(self, *, project=None, active_only=False, limit=100):
        rows = self.active_sessions[:limit]
        if project is not None:
            rows = [row for row in rows if row.get("project") == project]
        if active_only:
            rows = [row for row in rows if row.get("status") != "exited"]
        return rows


class FakeSprintctl:
    def __init__(self, *, takeup_error=None, takeup_release_error=None):
        self.takeup_error = takeup_error
        self.takeup_release_error = takeup_release_error
        self.takeups: list[dict] = []
        self.takeup_releases: list[dict] = []

    def item_show(self, item_id):
        return {
            "item": {"id": item_id, "sprint_id": 1, "status": "pending"},
            "active_claims": [],
        }

    def takeup_take(
        self,
        *,
        sprint_id,
        actor,
        instance_id,
        runtime_session_id,
        context=None,
    ):
        if self.takeup_error is not None:
            raise self.takeup_error
        row = {
            "sprint_id": sprint_id,
            "actor": actor,
            "instance_id": instance_id,
            "runtime_session_id": runtime_session_id,
            "context": context,
        }
        self.takeups.append(row)
        return row

    def takeup_release(
        self,
        *,
        sprint_id,
        actor,
        instance_id,
        runtime_session_id,
        reason=None,
    ):
        if self.takeup_release_error is not None:
            raise self.takeup_release_error
        row = {
            "sprint_id": sprint_id,
            "actor": actor,
            "instance_id": instance_id,
            "runtime_session_id": runtime_session_id,
            "reason": reason,
        }
        self.takeup_releases.append(row)
        return row

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


class SlowProcess:
    """Process that takes `total` seconds to complete, pausing in check_interval chunks."""

    def __init__(self, returncode: int = 0, total: float = 0.5):
        self.pid = 99999
        self._returncode = returncode
        self._remaining = total

    def wait(self, timeout: float | None = None) -> int:
        if timeout is None or self._remaining <= 0:
            return self._returncode
        effective = min(timeout, self._remaining)
        time.sleep(effective)
        self._remaining -= effective
        if self._remaining > 0:
            raise subprocess.TimeoutExpired([], timeout)
        return self._returncode

    def terminate(self):
        self._remaining = 0

    def kill(self):
        self._remaining = 0


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


def _config(tmp_path: Path, repo: Path, *, runner: str = "fake") -> DispatcherConfig:
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
        ),
        projects={"demo": ProjectConfig("demo", repo, "HEAD", env=None)},
        actions={
            "scope-iterate": ActionConfig(
                name="scope-iterate",
                model="fake",
                runner=runner,
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
    return {
        "id": 42,
        "action_type": "scope-iterate",
        "project": "demo",
        "target_ref": "1",
    }


def _make_daemon(config, actionctl, sprintctl, *, process_factory=None):
    from actionq_dispatcher.commands import CommandRunner

    daemon = ActionqDaemon(
        config,
        daemon_id="devbox:testhost:1:abc",
        runner=CommandRunner(),
        process_factory=process_factory,
        sprintctl_factory=lambda project: sprintctl,
    )
    daemon._actionctl = actionctl
    return daemon


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pause_file_emits_paused_without_claim(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)
    config.global_config.pause_file.write_text("paused")

    actionctl = FakeActionctl()
    daemon = _make_daemon(config, actionctl, FakeSprintctl())
    daemon.run(max_iters=1)

    assert actionctl.claims == 0
    assert any(e[0] == "coordinator_paused" for e in actionctl.emits)


def test_no_action_loops_without_session_events(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)

    actionctl = FakeActionctl(action=None)
    daemon = _make_daemon(config, actionctl, FakeSprintctl())
    daemon.run(max_iters=1)

    assert actionctl.claims == 1
    session_event_types = {"session.dispatch", "session.started", "session.exited"}
    assert not any(e[0] in session_event_types for e in actionctl.emits)


def test_fake_runner_completes_with_session_lifecycle_events(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo, runner="fake")

    actionctl = FakeActionctl(action=_action())
    sprintctl = FakeSprintctl()
    daemon = _make_daemon(config, actionctl, sprintctl)
    daemon.run(max_iters=1)

    emitted_types = [e[0] for e in actionctl.emits]
    assert "session.dispatch" in emitted_types
    assert "session.started" in emitted_types
    assert "session.exited" in emitted_types
    assert actionctl.completions, "action should be completed"

    exited = next(e for e in actionctl.emits if e[0] == "session.exited")
    assert exited[1]["outcome"] == "completed"
    assert exited[1]["exit_code"] == 0


def test_fake_runner_session_exited_action_id_matches(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo, runner="fake")

    actionctl = FakeActionctl(action=_action())
    sprintctl = FakeSprintctl()
    daemon = _make_daemon(config, actionctl, sprintctl)
    daemon.run(max_iters=1)

    dispatch_event = next(e for e in actionctl.emits if e[0] == "session.dispatch")
    assert dispatch_event[3] == 42  # action_id kwarg
    assert dispatch_event[1]["action_id"] == 42
    assert dispatch_event[1]["session_id"].startswith("aqs:")
    assert dispatch_event[1]["runtime_session_id"] == dispatch_event[1]["session_id"]
    assert dispatch_event[1]["ttl_seconds"] == 2
    assert dispatch_event[1]["claim"]["claim_id"] == 1
    assert dispatch_event[1]["claim"]["work_item_id"] == 1


def test_session_heartbeat_emitted_with_slow_process(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo, runner="fake")
    # heartbeat_interval=1s, process takes 2.5s total → at least 1 heartbeat
    config.global_config.heartbeat_interval_seconds = 1

    actionctl = FakeActionctl(action=_action())
    sprintctl = FakeSprintctl()
    proc = SlowProcess(returncode=0, total=2.5)

    daemon = _make_daemon(config, actionctl, sprintctl)

    # Override _start_harness to inject slow process regardless of runner type.
    def patched_start(action_config, prepared, is_fake):
        return proc

    daemon._start_harness = patched_start  # type: ignore[method-assign]
    daemon.run(max_iters=1)

    heartbeats = [e for e in actionctl.emits if e[0] == "session.heartbeat"]
    assert len(heartbeats) >= 1, "expected at least one heartbeat"
    hb = heartbeats[0][1]
    assert "session_id" in hb
    assert hb["status"] == "running"
    assert hb["monotonic_age_seconds"] >= 0


def test_shutdown_with_no_active_child_exits_cleanly(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)

    actionctl = FakeActionctl(action=None)
    daemon = _make_daemon(config, actionctl, FakeSprintctl())
    daemon._shutdown.set()
    daemon.run(max_iters=1)
    # Should complete without error; no session events
    session_event_types = {"session.dispatch", "session.started", "session.exited"}
    assert not any(e[0] in session_event_types for e in actionctl.emits)


def test_shutdown_during_active_child_emits_paused_and_exits(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo, runner="fake")
    config.global_config.heartbeat_interval_seconds = 1
    config.global_config.graceful_shutdown_seconds = 1

    actionctl = FakeActionctl(action=_action())
    sprintctl = FakeSprintctl()
    proc = SlowProcess(returncode=0, total=60.0)

    daemon = _make_daemon(config, actionctl, sprintctl)

    def patched_start(action_config, prepared, is_fake):
        daemon._shutdown.set()
        return proc

    daemon._start_harness = patched_start  # type: ignore[method-assign]
    daemon.run(max_iters=1)

    emitted_types = [e[0] for e in actionctl.emits]
    assert "session.paused" in emitted_types

    paused = next(e for e in actionctl.emits if e[0] == "session.paused")
    assert paused[1]["reason"] == "shutdown"


def test_unknown_action_type_rejected(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)

    action = {"id": 7, "action_type": "unknown-op", "project": "demo", "target_ref": "1"}
    actionctl = FakeActionctl(action=action)
    daemon = _make_daemon(config, actionctl, FakeSprintctl())
    daemon.run(max_iters=1)

    assert actionctl.rejections
    assert actionctl.rejections[0][2] == "action-type-config"


def test_stale_session_recovery_emits_exited_event(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)

    stale = SessionRecord(
        session_id="aqs:deadbeef",
        daemon_id="devbox:old:1:x",
        action_id=99,
        action_type="scope-iterate",
        project="demo",
        target_ref="1",
        harness="claude",
        model="claude-sonnet-4-6",
        worktree="/tmp/old",
        branch="agent/scope-iterate/99",
        pid=999999999,  # almost certainly not a live PID
        started_at="2026-01-01T00:00:00Z",
        outcome=None,
    )
    save_session_state(config.global_config.session_state_path, [stale])

    actionctl = FakeActionctl(action=None)
    daemon = _make_daemon(config, actionctl, FakeSprintctl())
    daemon._recover_stale_sessions()

    recovery = [e for e in actionctl.emits if e[0] == "session.exited"]
    assert recovery, "expected session.exited recovery event"
    assert recovery[0][1]["outcome"] == "daemon-recovered"
    assert recovery[0][3] == 99  # action_id


def test_actionctl_sessions_recovery_emits_exited_when_local_state_missing(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)

    actionctl = FakeActionctl(
        action=None,
        active_sessions=[
            {
                "session_id": "aqs:from-events",
                "action_id": 77,
                "daemon_id": f"devbox:{socket.gethostname()}:1234:abcd1234",
                "action_type": "scope-iterate",
                "project": "demo",
                "target_ref": "1",
                "harness": "claude",
                "model": "claude-sonnet-4-6",
                "worktree": "/tmp/recovered",
                "branch": "agent/scope-iterate/77",
                "pid": 999999999,
                "started_at": "2026-04-28T09:01:00Z",
                "last_heartbeat_at": "2026-04-28T09:02:00Z",
                "exited_at": None,
                "status": "running",
                "outcome": None,
            }
        ],
    )
    daemon = _make_daemon(config, actionctl, FakeSprintctl())
    daemon._recover_stale_sessions()

    recovery = [e for e in actionctl.emits if e[0] == "session.exited"]
    assert recovery, "expected session.exited recovery event from actionctl sessions"
    assert recovery[0][1]["outcome"] == "daemon-recovered"
    assert recovery[0][3] == 77


def test_actionctl_sessions_recovery_skips_live_pid(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)

    actionctl = FakeActionctl(
        action=None,
        active_sessions=[
            {
                "session_id": "aqs:still-live",
                "action_id": 88,
                "daemon_id": f"devbox:{socket.gethostname()}:{os.getpid()}:abcd1234",
                "action_type": "scope-iterate",
                "project": "demo",
                "target_ref": "1",
                "harness": "claude",
                "model": "claude-sonnet-4-6",
                "worktree": "/tmp/live",
                "branch": "agent/scope-iterate/88",
                "pid": os.getpid(),
                "started_at": "2026-04-28T09:01:00Z",
                "last_heartbeat_at": "2026-04-28T09:02:00Z",
                "exited_at": None,
                "status": "running",
                "outcome": None,
            }
        ],
    )
    daemon = _make_daemon(config, actionctl, FakeSprintctl())
    daemon._recover_stale_sessions()

    recovery = [e for e in actionctl.emits if e[0] == "session.exited"]
    assert not recovery


def test_session_state_persisted_and_cleared(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo, runner="fake")
    state_path = config.global_config.session_state_path

    actionctl = FakeActionctl(action=_action())
    sprintctl = FakeSprintctl()
    daemon = _make_daemon(config, actionctl, sprintctl)
    daemon.run(max_iters=1)

    sessions = load_session_state(state_path)
    assert sessions == [], "session state should be cleared after completion"


def test_sprintctl_takeup_runs_for_remote_projects(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo, runner="fake")
    config.global_config.sprintctl_takeup.enabled = True
    config.projects["demo"].env = {"SPRINTCTL_BACKEND": "remote"}

    actionctl = FakeActionctl(action=_action())
    sprintctl = FakeSprintctl()
    daemon = _make_daemon(config, actionctl, sprintctl)
    daemon.run(max_iters=1)

    assert len(sprintctl.takeups) == 1
    assert len(sprintctl.takeup_releases) == 1
    take = sprintctl.takeups[0]
    release = sprintctl.takeup_releases[0]
    assert take["sprint_id"] == 1
    assert take["actor"].startswith("actionq:aqs:")
    assert take["instance_id"].startswith("aqs:")
    assert "scope-iterate via fake/fake" in (take["context"] or "")
    assert release["reason"] == "completed"

    exited = next(e for e in actionctl.emits if e[0] == "session.exited")
    assert exited[1]["sprint_release_status"] == "released"


def test_sprintctl_takeup_skipped_when_remote_only_project_is_local(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo, runner="fake")
    config.global_config.sprintctl_takeup.enabled = True
    monkeypatch.delenv("SPRINTCTL_BACKEND", raising=False)

    actionctl = FakeActionctl(action=_action())
    sprintctl = FakeSprintctl()
    daemon = _make_daemon(config, actionctl, sprintctl)
    daemon.run(max_iters=1)

    assert sprintctl.takeups == []
    assert sprintctl.takeup_releases == []

    exited = next(e for e in actionctl.emits if e[0] == "session.exited")
    assert exited[1]["sprint_release_status"] == "skipped"


def test_sprintctl_takeup_uses_shell_env_for_remote_detection(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo, runner="fake")
    config.global_config.sprintctl_takeup.enabled = True
    config.projects["demo"].env = {"KCTL_DB": "/tmp/demo.kctl.db"}
    monkeypatch.setenv("SPRINTCTL_BACKEND", "remote")

    actionctl = FakeActionctl(action=_action())
    sprintctl = FakeSprintctl()
    daemon = _make_daemon(config, actionctl, sprintctl)
    daemon.run(max_iters=1)

    assert len(sprintctl.takeups) == 1
    assert len(sprintctl.takeup_releases) == 1


def test_sprintctl_takeup_failure_fails_before_harness_start(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo, runner="fake")
    config.global_config.sprintctl_takeup.enabled = True
    config.projects["demo"].env = {"SPRINTCTL_BACKEND": "remote"}

    actionctl = FakeActionctl(action=_action())
    sprintctl = FakeSprintctl(takeup_error=RuntimeError("takeup boom"))
    daemon = _make_daemon(config, actionctl, sprintctl)
    daemon.run(max_iters=1)

    assert actionctl.failures
    assert "sprint takeup failed: takeup boom" in actionctl.failures[0][1]
    emitted_types = [e[0] for e in actionctl.emits]
    assert "session.started" not in emitted_types
    assert "session.exited" not in emitted_types


def test_sprintctl_takeup_release_failure_does_not_flip_completed_action(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo, runner="fake")
    config.global_config.sprintctl_takeup.enabled = True
    config.projects["demo"].env = {"SPRINTCTL_BACKEND": "remote"}

    actionctl = FakeActionctl(action=_action())
    sprintctl = FakeSprintctl(takeup_release_error=RuntimeError("release boom"))
    daemon = _make_daemon(config, actionctl, sprintctl)
    daemon.run(max_iters=1)

    assert actionctl.completions
    exited = next(e for e in actionctl.emits if e[0] == "session.exited")
    assert exited[1]["outcome"] == "completed"
    assert exited[1]["sprint_release_status"] == "failed"
    assert exited[1]["sprint_release_error"] == "release boom"
