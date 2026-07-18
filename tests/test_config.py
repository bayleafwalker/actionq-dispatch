from pathlib import Path

from actionq_dispatcher.config import load_config


def test_smoke_config_loads():
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "examples" / "config.smoke.toml")

    project = config.projects["sprintctl"]
    action = config.actions["scope-iterate"]

    assert project.env["SPRINTCTL_DB"] == "/projects/dev/sprintctl/.sprintctl/sprintctl.db"
    assert action.runner == "fake"
    assert action.fake_commit_path == "docs/actionq-dispatch-smoke.md"
    assert config.global_config.worker_env is None
    assert config.global_config.sprintctl_takeup.enabled is True
    assert config.global_config.sprintctl_takeup.remote_only is True
    assert config.global_config.sprintctl_takeup.actor_prefix == "actionq"
    assert config.global_config.sprintctl_takeup.release_on_sprintctl_error is False
    assert action.test_command == "python3 -c pass"


def test_config_loads_reasoning_defaults(tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("{action_json}", encoding="utf-8")
    acl = tmp_path / "acl.json"
    acl.write_text("{}", encoding="utf-8")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[harnesses.claude]
bin = "claude"
kind = "claude"
default_model = "claude-sonnet-5"
default_reasoning = "medium"

[projects.demo]
path = "{tmp_path / 'repo'}"
default_harness = "claude"
default_reasoning = "high"

[actions.scope-iterate]
model = "claude-haiku-4-5-20251001"
reasoning = "low"
prompt_template = "{prompt}"
tool_acl = "{acl}"
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.harnesses["claude"].default_reasoning == "medium"
    assert config.projects["demo"].default_reasoning == "high"
    assert config.actions["scope-iterate"].reasoning == "low"
