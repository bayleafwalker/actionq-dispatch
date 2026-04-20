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
    assert action.test_command == "python3 -c pass"
