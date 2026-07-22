from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


def new_session_id() -> str:
    return f"aqs:{uuid.uuid4().hex}"


def make_daemon_id(pid: int | None = None) -> str:
    pid = pid or os.getpid()
    boot_uuid = uuid.uuid4().hex[:8]
    return f"devbox:{socket.gethostname()}:{pid}:{boot_uuid}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class SessionRecord:
    session_id: str
    daemon_id: str
    action_id: int
    action_type: str
    project: str | None
    target_ref: str | None
    harness: str
    model: str
    worktree: str
    branch: str
    pid: int | None = None
    started_at: str | None = None
    exited_at: str | None = None
    last_heartbeat_at: str | None = None
    outcome: str | None = None
    runtime_session_id: str | None = None
    ttl_seconds: int | None = None
    claim_id: int | None = None
    work_item_id: int | None = None
    claim_type: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def dispatch_payload(self, routing_source: str = "action-kind-default", routing: dict | None = None) -> dict:
        payload = {
            "session_id": self.session_id,
            "runtime_session_id": self.runtime_session_id or self.session_id,
            "daemon_id": self.daemon_id,
            "action_id": self.action_id,
            "action_type": self.action_type,
            "project": self.project,
            "target_ref": self.target_ref,
            "harness": self.harness,
            "model": self.model,
            "routing_source": routing_source,
            "worktree": self.worktree,
            "branch": self.branch,
            "ttl_seconds": self.ttl_seconds,
            "claim": {
                "claim_id": self.claim_id,
                "work_item_id": self.work_item_id,
                "claim_type": self.claim_type,
            },
        }
        if routing:
            payload["routing"] = routing
        return payload

    def started_payload(self) -> dict:
        return {
            "session_id": self.session_id,
            "runtime_session_id": self.runtime_session_id or self.session_id,
            "pid": self.pid,
            "started_at": self.started_at,
            "harness": self.harness,
            "model": self.model,
            "ttl_seconds": self.ttl_seconds,
            "claim": {
                "claim_id": self.claim_id,
                "work_item_id": self.work_item_id,
                "claim_type": self.claim_type,
            },
        }

    def heartbeat_payload(self, monotonic_age_seconds: float) -> dict:
        return {
            "session_id": self.session_id,
            "runtime_session_id": self.runtime_session_id or self.session_id,
            "pid": self.pid,
            "monotonic_age_seconds": int(monotonic_age_seconds),
            "status": "running",
            "worktree": self.worktree,
            "ttl_seconds": self.ttl_seconds,
            "claim": {
                "claim_id": self.claim_id,
                "work_item_id": self.work_item_id,
                "claim_type": self.claim_type,
            },
        }

    def exited_payload(
        self,
        *,
        exit_code: int,
        duration_seconds: float,
        outcome: str,
        failure_reason: str | None = None,
        result_ref: str | None = None,
        audit_status: str = "skipped",
        audit_error: str | None = None,
        sprint_release_status: str = "skipped",
        sprint_release_error: str | None = None,
    ) -> dict:
        payload: dict = {
            "session_id": self.session_id,
            "runtime_session_id": self.runtime_session_id or self.session_id,
            "pid": self.pid,
            "exit_code": exit_code,
            "duration_seconds": int(duration_seconds),
            "outcome": outcome,
            "result_ref": result_ref,
            "failure_reason": failure_reason,
            "audit_status": audit_status,
            "sprint_release_status": sprint_release_status,
            "ttl_seconds": self.ttl_seconds,
            "claim": {
                "claim_id": self.claim_id,
                "work_item_id": self.work_item_id,
                "claim_type": self.claim_type,
            },
        }
        if audit_error is not None:
            payload["audit_error"] = audit_error
        if sprint_release_error is not None:
            payload["sprint_release_error"] = sprint_release_error
        return payload

    def paused_payload(self, *, reason: str, mechanism: str, resumable: bool = False) -> dict:
        return {
            "session_id": self.session_id,
            "runtime_session_id": self.runtime_session_id or self.session_id,
            "reason": reason,
            "mechanism": mechanism,
            "resumable": resumable,
            "ttl_seconds": self.ttl_seconds,
            "claim": {
                "claim_id": self.claim_id,
                "work_item_id": self.work_item_id,
                "claim_type": self.claim_type,
            },
        }


def load_session_state(path: Path) -> list[SessionRecord]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, list):
            return []
        result = []
        for item in data:
            if isinstance(item, dict):
                known = {k: item[k] for k in SessionRecord.__dataclass_fields__ if k in item}
                result.append(SessionRecord(**known))
        return result
    except (json.JSONDecodeError, TypeError, KeyError):
        return []


def save_session_state(path: Path, sessions: list[SessionRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([s.to_dict() for s in sessions], indent=2))
