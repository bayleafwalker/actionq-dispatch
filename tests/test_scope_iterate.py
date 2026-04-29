import subprocess
from pathlib import Path

from actionq_dispatcher.acl import load_acl
from actionq_dispatcher.commands import CommandRunner
from actionq_dispatcher.clients import SprintctlClient
from actionq_dispatcher.config import (
    ActionConfig,
    DispatcherConfig,
    GlobalConfig,
    ProjectConfig,
)
from actionq_dispatcher.core import Dispatcher
from actionq_dispatcher.worker import ConfiguredWorker
from actionq_dispatcher.worker import ClaudeWorker
from actionq_dispatcher.worker import WorkerError

from .test_dispatcher_core import FakeActionctl


class FakeSprintctl:
    def __init__(self, *, item_status="pending", active_claims=None, release_error=None):
        self.done = []
        self.blocked = []
        self.released = []
        self.failures = []
        self.item_status = item_status
        self.active_claims = active_claims or []
        self.release_error = release_error

    def item_show(self, item_id):
        return {
            "item": {
                "id": item_id,
                "sprint_id": 1,
                "status": self.item_status,
                "title": "Test item",
            },
            "events": [],
            "active_claims": self.active_claims,
            "refs": [],
            "deps": {"blocked_by": [], "blocks": []},
        }

    def claim_start(self, *, item_id, actor, ttl_seconds, branch, worktree, runtime_session_id=None):
        return {"claim_id": 7, "claim_token": "secret"}

    def done_from_claim(self, *, item_id, claim_id, claim_token, actor):
        self.done.append((item_id, claim_id, claim_token, actor))
        return {}

    def block_with_claim(self, *, item_id, claim_id, claim_token, actor):
        self.blocked.append((item_id, claim_id, claim_token, actor))
        return {}

    def release_claim(self, *, claim_id, claim_token, actor):
        if self.release_error is not None:
            raise self.release_error
        self.released.append((claim_id, claim_token, actor))

    def coordination_failure(self, *, sprint_id, item_id, actor, summary, detail):
        self.failures.append((sprint_id, item_id, actor, summary, detail))
        return {}


class CommitWorker:
    def __init__(self, rel_path="allowed.py", content="print('ok')\n"):
        self.rel_path = rel_path
        self.content = content

    def invoke(self, *, action_config, worktree, prompt, acl):
        path = Path(worktree) / self.rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.content)
        subprocess.run(["git", "add", self.rel_path], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-m", "worker change"], cwd=worktree, check=True)
        return object()


class FailingWorker:
    def invoke(self, *, action_config, worktree, prompt, acl):
        raise WorkerError("boom")


def _git(repo: Path, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


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
    prompt = tmp_path / "scope-iterate.md"
    prompt.write_text(
        "Action:\n{action_json}\nSprint:\n{sprint_item_json}\nWorktree: {working_dir}\nBranch: {branch_name}\nScope:\n{allowed_scope}\nTests: {test_command}\n"
    )
    acl = tmp_path / "acl.json"
    acl.write_text(
        """
        {
          "version": 1,
          "allowed_tools": ["read", "edit", "grep", "bash"],
          "path_allowlist": ["{working_dir}/**"],
          "path_denylist": ["{working_dir}/.sprintctl/**"],
          "bash_allowlist": ["git add", "git commit", "python3"],
          "bash_denylist": ["git push", "ssh", "curl", "wget", "rm"],
          "network": "none"
        }
        """
    )
    return DispatcherConfig(
        path=tmp_path / "config.toml",
        global_config=GlobalConfig(
            poll_interval_seconds=30,
            default_timeout_minutes=30,
            event_jsonl_path=None,
            budget_daily_usd=20.0,
            budget_window_hours=24,
            worktree_root=tmp_path / "worktrees",
            pause_file=tmp_path / "PAUSED",
            actionctl_bin="actionctl",
            claude_bin="claude",
        ),
        projects={"demo": ProjectConfig("demo", repo, "HEAD", env=None)},
        actions={
            "scope-iterate": ActionConfig(
                name="scope-iterate",
                model="sonnet",
                runner="local",
                prompt_template=prompt,
                working_dir="{worktree_root}/{project}/{action.id}",
                tool_acl=acl,
                timeout_minutes=5,
                max_cost_usd=1.0,
                test_command="python3 -c pass",
                pre_gates=[],
                post_gates=[],
                fake_commit_path="docs/actionq-dispatch-smoke.md",
            )
        },
    )


def _action():
    return {
        "id": 11,
        "action_type": "scope-iterate",
        "project": "demo",
        "target_ref": "42",
    }


def test_scope_iterate_completes_with_committed_allowed_change(tmp_path):
    repo = _repo(tmp_path)
    actionctl = FakeActionctl(action=_action())
    sprintctl = FakeSprintctl()

    result = Dispatcher(
        _config(tmp_path, repo),
        runner=CommandRunner(),
        actionctl=actionctl,
        sprintctl_factory=lambda _p: sprintctl,
        worker=CommitWorker(),
    ).run_once()

    assert result.result == "completed"
    assert actionctl.completions
    assert "branch=agent/scope-iterate/11" in actionctl.completions[0][1]
    assert sprintctl.done


def test_scope_iterate_rejects_denied_path(tmp_path):
    repo = _repo(tmp_path)
    actionctl = FakeActionctl(action=_action())
    sprintctl = FakeSprintctl()

    result = Dispatcher(
        _config(tmp_path, repo),
        runner=CommandRunner(),
        actionctl=actionctl,
        sprintctl_factory=lambda _p: sprintctl,
        worker=CommitWorker(".sprintctl/state", "no\n"),
    ).run_once()

    assert result.result == "rejected"
    assert actionctl.rejections[-1][2] == "diff-scope-respected"
    assert sprintctl.blocked
    assert sprintctl.released


def test_scope_iterate_worker_failure_marks_failed_and_blocks(tmp_path):
    repo = _repo(tmp_path)
    actionctl = FakeActionctl(action=_action())
    sprintctl = FakeSprintctl()

    result = Dispatcher(
        _config(tmp_path, repo),
        runner=CommandRunner(),
        actionctl=actionctl,
        sprintctl_factory=lambda _p: sprintctl,
        worker=FailingWorker(),
    ).run_once()

    assert result.result == "failed"
    assert actionctl.failures
    assert sprintctl.failures
    assert sprintctl.blocked


def test_budget_gate_rejects_before_worktree_creation(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)
    config.actions["scope-iterate"].pre_gates = ["budget"]
    config.global_config.budget_daily_usd = 2.0
    actionctl = FakeActionctl(action=_action())
    actionctl.event_rows = [
        {
            "event_type": "coordinator_cycle",
            "payload": {"cost_usd": 1.5},
        }
    ]
    sprintctl = FakeSprintctl()

    result = Dispatcher(
        config,
        runner=CommandRunner(),
        actionctl=actionctl,
        sprintctl_factory=lambda _p: sprintctl,
        worker=CommitWorker(),
    ).run_once()

    assert result.result == "rejected"
    assert actionctl.rejections[-1][2] == "budget"
    assert not (config.global_config.worktree_root / "demo" / "11").exists()
    assert actionctl.emits[-1][1]["cost_usd"] == 0.0


def test_non_pending_item_rejected_before_claim(tmp_path):
    repo = _repo(tmp_path)
    actionctl = FakeActionctl(action=_action())
    sprintctl = FakeSprintctl(item_status="done")

    result = Dispatcher(
        _config(tmp_path, repo),
        runner=CommandRunner(),
        actionctl=actionctl,
        sprintctl_factory=lambda _p: sprintctl,
        worker=CommitWorker(),
    ).run_once()

    assert result.result == "rejected"
    assert actionctl.rejections[-1][2] == "sprint-item-ready"
    assert not sprintctl.done


def test_release_error_does_not_mask_rejection(tmp_path):
    repo = _repo(tmp_path)
    actionctl = FakeActionctl(action=_action())
    sprintctl = FakeSprintctl(release_error=RuntimeError("release failed"))

    result = Dispatcher(
        _config(tmp_path, repo),
        runner=CommandRunner(),
        actionctl=actionctl,
        sprintctl_factory=lambda _p: sprintctl,
        worker=CommitWorker(".sprintctl/state", "no\n"),
    ).run_once()

    assert result.result == "rejected"
    assert actionctl.rejections[-1][2] == "diff-scope-respected"
    assert len(sprintctl.failures) == 2
    assert "claim release failed" in sprintctl.failures[-1][3]


def test_configured_fake_worker_completes_through_real_validation(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)
    config.actions["scope-iterate"].runner = "fake"
    actionctl = FakeActionctl(action=_action())
    sprintctl = FakeSprintctl()

    result = Dispatcher(
        config,
        runner=CommandRunner(),
        actionctl=actionctl,
        sprintctl_factory=lambda _p: sprintctl,
        worker=ConfiguredWorker(config.global_config, CommandRunner()),
    ).run_once()

    assert result.result == "completed"
    result_ref = actionctl.completions[0][1]
    assert "branch=agent/scope-iterate/11" in result_ref
    worktree = config.global_config.worktree_root / "demo" / "11"
    assert (worktree / "docs/actionq-dispatch-smoke.md").exists()


def test_configured_fake_worker_denied_path_rejects(tmp_path):
    repo = _repo(tmp_path)
    config = _config(tmp_path, repo)
    config.actions["scope-iterate"].runner = "fake"
    config.actions["scope-iterate"].fake_commit_path = ".sprintctl/state"
    actionctl = FakeActionctl(action=_action())
    sprintctl = FakeSprintctl()

    result = Dispatcher(
        config,
        runner=CommandRunner(),
        actionctl=actionctl,
        sprintctl_factory=lambda _p: sprintctl,
        worker=ConfiguredWorker(config.global_config, CommandRunner()),
    ).run_once()

    assert result.result == "rejected"
    assert actionctl.rejections[-1][2] == "diff-scope-respected"


class CapturingRunner:
    def __init__(self):
        self.calls = []

    def run(self, args, *, cwd=None, env=None, timeout=None, input_text=None):
        self.calls.append(
            {
                "args": args,
                "cwd": cwd,
                "env": env,
                "timeout": timeout,
                "input_text": input_text,
            }
        )
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": '{"item":{"id":42,"sprint_id":1,"status":"pending"},"active_claims":[]}',
                "stderr": "",
            },
        )()


def test_sprintctl_client_passes_project_env(tmp_path):
    runner = CapturingRunner()
    client = SprintctlClient(
        tmp_path,
        runner,
        project_env={"SPRINTCTL_DB": "/tmp/project.db"},
    )

    client.item_show(42)

    assert runner.calls[0]["env"] == {"SPRINTCTL_DB": "/tmp/project.db"}
    assert runner.calls[0]["args"][:4] == ["direnv", "exec", str(tmp_path), "sprintctl"]


def test_sprintctl_coordination_failure_does_not_require_json_flag(tmp_path):
    runner = CapturingRunner()
    client = SprintctlClient(tmp_path, runner)

    client.coordination_failure(
        sprint_id=1,
        item_id=42,
        actor="dispatcher:actionq",
        summary="failed",
        detail="boom",
    )

    args = runner.calls[0]["args"]
    assert args[:4] == ["direnv", "exec", str(tmp_path), "sprintctl"]
    assert args[4:6] == ["event", "add"]
    assert "--json" not in args


def test_sprintctl_takeup_take_uses_expected_remote_visibility_args(tmp_path):
    runner = CapturingRunner()
    client = SprintctlClient(
        tmp_path,
        runner,
        project_env={"SPRINTCTL_BACKEND": "remote"},
    )

    client.takeup_take(
        sprint_id=7,
        actor="actionq:aqs:test",
        instance_id="aqs:test",
        runtime_session_id="aqs:test",
        context="actionq action 11 scope-iterate via fake/fake",
    )

    call = runner.calls[0]
    assert call["env"] == {"SPRINTCTL_BACKEND": "remote"}
    assert call["args"][:7] == [
        "direnv",
        "exec",
        str(tmp_path),
        "sprintctl",
        "takeup",
        "take",
        "--sprint-id",
    ]
    assert "--actor-kind" in call["args"]
    assert "--instance-id" in call["args"]
    assert "--runtime-session-id" in call["args"]
    assert "--context" in call["args"]
    assert "--json" in call["args"]


def test_sprintctl_takeup_release_uses_expected_args(tmp_path):
    runner = CapturingRunner()
    client = SprintctlClient(tmp_path, runner)

    client.takeup_release(
        sprint_id=7,
        actor="actionq:aqs:test",
        instance_id="aqs:test",
        runtime_session_id="aqs:test",
        reason="completed",
    )

    args = runner.calls[0]["args"]
    assert args[:6] == ["direnv", "exec", str(tmp_path), "sprintctl", "takeup", "release"]
    assert "--reason" in args
    assert "--json" in args


def test_sprintctl_claim_start_passes_runtime_session_id(tmp_path):
    runner = CapturingRunner()
    client = SprintctlClient(tmp_path, runner)

    client.claim_start(
        item_id=42,
        actor="dispatcher:actionq:42",
        ttl_seconds=900,
        branch="agent/scope-iterate/42",
        worktree=tmp_path / "wt",
        runtime_session_id="aqs:test",
    )

    args = runner.calls[0]["args"]
    assert args[:6] == ["direnv", "exec", str(tmp_path), "sprintctl", "claim", "start"]
    assert "--runtime-session-id" in args
    assert "aqs:test" in args
    assert "--json" in args


def test_claude_worker_passes_prompt_on_stdin(tmp_path):
    runner = CapturingRunner()
    config = _config(tmp_path, tmp_path)
    config.global_config.worker_env = {"ANTHROPIC_API_KEY": ""}
    acl = config.actions["scope-iterate"].tool_acl

    ClaudeWorker(config.global_config, runner).invoke(
        action_config=config.actions["scope-iterate"],
        worktree=tmp_path,
        prompt="do the work",
        acl=load_acl(acl, {"working_dir": str(tmp_path)}),
    )

    call = runner.calls[0]
    assert call["input_text"] == "do the work"
    assert call["env"]["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert call["env"]["GIT_CONFIG_VALUE_0"] == str(tmp_path)
    assert call["env"]["ANTHROPIC_API_KEY"] == ""
    assert "do the work" not in call["args"]
