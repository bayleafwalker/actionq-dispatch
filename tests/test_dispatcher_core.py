from pathlib import Path

from actionq_dispatcher.config import (
    ActionConfig,
    DispatcherConfig,
    GlobalConfig,
    ProjectConfig,
)
from actionq_dispatcher.core import Dispatcher


class FakeActionctl:
    def __init__(self, action=None):
        self.action = action
        self.event_rows = []
        self.rejections = []
        self.failures = []
        self.completions = []
        self.emits = []
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
        rows = self.event_rows
        if event_type is not None:
            rows = [row for row in rows if row.get("event_type") == event_type]
        return rows[:limit]


def _config(tmp_path: Path) -> DispatcherConfig:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("{action_json}")
    acl = tmp_path / "acl.json"
    acl.write_text(
        '{"version":1,"allowed_tools":["read"],"path_allowlist":["{working_dir}/**"],"path_denylist":[]}'
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
        projects={
            "demo": ProjectConfig("demo", tmp_path / "repo", "HEAD", env=None),
        },
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


def test_no_claim_emits_cycle(tmp_path):
    actionctl = FakeActionctl(action=None)
    result = Dispatcher(_config(tmp_path), actionctl=actionctl, sprintctl_factory=lambda _p: None, worker=None).run_once()
    assert result.result == "no_action"
    assert actionctl.emits[0][0] == "coordinator_cycle"
    assert actionctl.emits[0][1]["claimed"] is False


def test_pause_file_emits_paused_without_claim(tmp_path):
    config = _config(tmp_path)
    config.global_config.pause_file.write_text("paused")
    actionctl = FakeActionctl(action={"id": 1})
    result = Dispatcher(config, actionctl=actionctl, sprintctl_factory=lambda _p: None, worker=None).run_once()
    assert result.result == "paused"
    assert actionctl.claims == 0
    assert actionctl.emits[0][0] == "coordinator_paused"


def test_unknown_action_type_rejected(tmp_path):
    actionctl = FakeActionctl(action={"id": 9, "action_type": "unknown"})
    result = Dispatcher(_config(tmp_path), actionctl=actionctl, sprintctl_factory=lambda _p: None, worker=None).run_once()
    assert result.result == "rejected"
    assert actionctl.rejections[0][2] == "action-type-config"
