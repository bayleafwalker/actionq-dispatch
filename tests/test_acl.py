from pathlib import Path

from actionq_dispatcher.acl import ACL, claude_tool_args, validate_changed_paths


def test_claude_tool_args_maps_bash_allow_and_deny():
    acl = ACL(
        version=1,
        allowed_tools=["read", "edit", "grep", "bash"],
        path_allowlist=[],
        path_denylist=[],
        bash_allowlist=["git add", "pytest"],
        bash_denylist=["git push"],
        network="none",
    )
    allowed, denied = claude_tool_args(acl)
    assert "Read" in allowed
    assert "Bash(git add:*)" in allowed
    assert "Bash(git push:*)" in denied


def test_validate_changed_paths_rejects_denied_path(tmp_path):
    acl = ACL(
        version=1,
        allowed_tools=[],
        path_allowlist=[str(tmp_path / "**")],
        path_denylist=[str(tmp_path / ".sprintctl" / "**")],
        bash_allowlist=[],
        bash_denylist=[],
        network="none",
    )
    ok, reason = validate_changed_paths(
        [".sprintctl/state"],
        worktree=tmp_path,
        acl=acl,
    )
    assert not ok
    assert "denied" in reason


def test_validate_changed_paths_rejects_escape(tmp_path):
    acl = ACL(
        version=1,
        allowed_tools=[],
        path_allowlist=[str(tmp_path / "**")],
        path_denylist=[],
        bash_allowlist=[],
        bash_denylist=[],
        network="none",
    )
    ok, reason = validate_changed_paths(["../outside.py"], worktree=tmp_path, acl=acl)
    assert not ok
    assert "escapes" in reason
