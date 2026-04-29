from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import threading
import time
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .clients import ActionctlClient, AuditctlClient, AuditResult, ClientError, SprintctlClient
from .commands import CommandRunner, git_safe_env
from .config import DispatcherConfig, load_config
from .core import CoordinatorError, PreparedSession, ScopeIterateHandler, _ActionSettled
from .session import (
    SessionRecord,
    _now_iso,
    load_session_state,
    make_daemon_id,
    new_session_id,
    save_session_state,
)

_AUDIT_SKIPPED = "skipped"
_AUDIT_OK = "ok"
_AUDIT_FAILED = "failed"
from .routing import RoutingError, resolve_routing
from .worker import FakeCommitWorker, WorkerError

log = logging.getLogger(__name__)


class ProcessLike(Protocol):
    @property
    def pid(self) -> int: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[[list[str], Path, dict[str, str], str | None], ProcessLike]


@dataclass
class _ImmediateProcess:
    """Fake process that has already completed synchronously."""

    _returncode: int
    pid: int = -1

    def __post_init__(self) -> None:
        self.pid = os.getpid()

    def wait(self, timeout: float | None = None) -> int:
        return self._returncode

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


@dataclass
class _SprintTakeupState:
    sprint_id: int
    actor: str
    instance_id: str
    runtime_session_id: str


def _default_process_factory(
    cmd: list[str], cwd: Path, env: dict[str, str], input_text: str | None
) -> ProcessLike:
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.PIPE if input_text else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if input_text:
        proc.stdin.write(input_text.encode())
        proc.stdin.close()
    return proc


class ActionqDaemon:
    def __init__(
        self,
        config: DispatcherConfig,
        *,
        daemon_id: str | None = None,
        process_factory: ProcessFactory | None = None,
        runner: CommandRunner | None = None,
        sprintctl_factory: Callable | None = None,
        audit_factory: Callable | None = None,
    ) -> None:
        self.config = config
        self.runner = runner or CommandRunner()
        self.daemon_id = daemon_id or make_daemon_id()
        self.actor = f"actionq-daemon:{socket.gethostname()}:{os.getpid()}"
        self._process_factory = process_factory or _default_process_factory
        self._sprintctl_factory = sprintctl_factory or (
            lambda project: SprintctlClient(project.path, self.runner, project_env=project.env)
        )
        self._audit_factory = audit_factory or (
            lambda project: AuditctlClient(project.path, self.runner, project_env=project.env)
        )
        self._shutdown = threading.Event()
        self._active_session: SessionRecord | None = None
        self._reload_pending = False

        g = config.global_config
        self._poll_interval = g.poll_interval_seconds
        self._heartbeat_interval = g.heartbeat_interval_seconds
        self._graceful_shutdown_seconds = g.graceful_shutdown_seconds
        self._session_state_path: Path = g.session_state_path or (
            Path.home() / ".local/state/actionq/sessions.json"
        )
        self._audit_enabled = g.audit_enabled
        self._fail_on_audit_error = g.fail_action_on_emit_error
        self._actionctl = ActionctlClient(g.actionctl_bin, self.runner)

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGHUP, self._handle_sighup)

    def _handle_signal(self, signum: int, frame: object) -> None:
        log.info("received signal %s, initiating shutdown", signum)
        self._shutdown.set()

    def _handle_sighup(self, signum: int, frame: object) -> None:
        if self._active_session is None:
            log.info("SIGHUP: reloading config")
            try:
                self.config = load_config(self.config.path)
            except Exception as exc:
                log.warning("config reload failed: %s", exc)
        else:
            log.info("SIGHUP: pending reload (active session in progress)")
            self._reload_pending = True

    def run(self, max_iters: int | None = None) -> None:
        self._recover_stale_sessions()
        iters = 0
        while not self._shutdown.is_set():
            try:
                self._loop_once()
            except Exception as exc:
                log.error("unhandled loop error: %s", exc, exc_info=True)
            iters += 1
            if max_iters is not None and iters >= max_iters:
                break

    def _recover_stale_sessions(self) -> None:
        sessions = load_session_state(self._session_state_path)
        seen_session_ids: set[str] = set()
        for session in sessions:
            if session.pid is None or session.outcome is not None:
                continue
            seen_session_ids.add(session.session_id)
            live = self._pid_is_live(session.pid)
            if not live:
                self._emit_recovered_session(session)

        try:
            active_sessions = self._actionctl.sessions(active_only=True, limit=1000)
        except Exception as exc:
            log.warning("failed to query actionctl sessions for recovery: %s", exc)
            active_sessions = []

        for summary in active_sessions:
            session_id = summary.get("session_id")
            if not session_id or session_id in seen_session_ids:
                continue
            if not self._daemon_summary_is_local(summary):
                continue
            session = self._session_from_summary(summary)
            if session is None or session.pid is None or session.outcome is not None:
                continue
            if not self._pid_is_live(session.pid):
                self._emit_recovered_session(session)

        save_session_state(self._session_state_path, [])

    def _pid_is_live(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _emit_recovered_session(self, session: SessionRecord) -> None:
        log.warning(
            "stale session %s (pid=%s) found at startup; emitting recovery event",
            session.session_id,
            session.pid,
        )
        try:
            self._actionctl.emit(
                "session.exited",
                action_id=session.action_id,
                actor=self.actor,
                payload=session.exited_payload(
                    exit_code=-1,
                    duration_seconds=0,
                    outcome="daemon-recovered",
                    failure_reason="daemon restarted with unresolved session",
                ),
            )
        except Exception as exc:
            log.warning("failed to emit stale session recovery event: %s", exc)

    def _daemon_summary_is_local(self, summary: dict) -> bool:
        daemon_id = summary.get("daemon_id")
        if not isinstance(daemon_id, str):
            return False
        parts = daemon_id.split(":")
        if len(parts) < 4:
            return False
        return parts[1] == socket.gethostname()

    def _session_from_summary(self, summary: dict) -> SessionRecord | None:
        try:
            action_id = int(summary["action_id"])
            session_id = str(summary["session_id"])
            action_type = str(summary["action_type"])
            daemon_id = str(summary["daemon_id"])
        except (KeyError, TypeError, ValueError):
            return None

        pid = summary.get("pid")
        try:
            pid_value = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            pid_value = None

        return SessionRecord(
            session_id=session_id,
            daemon_id=daemon_id,
            action_id=action_id,
            action_type=action_type,
            project=summary.get("project"),
            target_ref=summary.get("target_ref"),
            harness=str(summary.get("harness") or "unknown"),
            model=str(summary.get("model") or "unknown"),
            worktree=str(summary.get("worktree") or ""),
            branch=str(summary.get("branch") or ""),
            pid=pid_value,
            started_at=summary.get("started_at"),
            exited_at=summary.get("exited_at"),
            last_heartbeat_at=summary.get("last_heartbeat_at"),
            outcome=summary.get("outcome"),
        )

    def _loop_once(self) -> None:
        pause_file = self.config.global_config.pause_file
        if pause_file.exists():
            try:
                self._actionctl.emit(
                    "coordinator_paused",
                    actor=self.actor,
                    payload={"daemon_id": self.daemon_id, "pause_file": str(pause_file)},
                )
            except Exception as exc:
                log.warning("failed to emit coordinator_paused: %s", exc)
            self._shutdown.wait(self._poll_interval)
            return

        action = self._actionctl.claim(
            worker=self.actor,
            timeout_minutes=self.config.global_config.default_timeout_minutes,
        )
        if action is None:
            self._shutdown.wait(self._poll_interval)
            return

        if self._reload_pending:
            self._reload_pending = False
            try:
                self.config = load_config(self.config.path)
            except Exception as exc:
                log.warning("deferred config reload failed: %s", exc)

        self._dispatch_and_run(action)

    def _emit_audit(
        self,
        audit: AuditctlClient | None,
        event_type: str,
        *,
        actor: str,
        summary: str,
        refs: list[str] | None = None,
        metadata: dict | None = None,
    ) -> AuditResult | None:
        if audit is None:
            return None
        result = audit.add(type_=event_type, actor=actor, summary=summary, refs=refs, metadata=metadata)
        if not result.ok:
            log.warning("audit emit failed for %s: %s", event_type, result.error)
        return result

    def _query_pull_request(self, project_path: Path, branch: str, env: dict[str, str] | None) -> dict | None:
        cmd = [
            "direnv",
            "exec",
            str(project_path),
            "gh",
            "pr",
            "view",
            branch,
            "--json",
            "number,state,headRefName",
        ]
        result = self.runner.run(cmd, cwd=project_path, env=env, timeout=15)
        if result.returncode != 0:
            log.info("no PR found for branch %s: %s", branch, result.stderr.strip())
            return None
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            log.warning("invalid JSON from gh pr view for branch %s", branch)
            return None
        if not isinstance(payload, dict):
            log.warning("unexpected gh pr view payload type for branch %s", branch)
            return None
        if payload.get("headRefName") != branch:
            log.warning(
                "gh pr view returned headRefName=%s for branch %s",
                payload.get("headRefName"),
                branch,
            )
            return None
        return payload

    def _project_uses_remote_sprintctl(self, project_env: dict[str, str] | None) -> bool:
        effective_env = os.environ.copy()
        if project_env:
            effective_env.update(project_env)
        return effective_env.get("SPRINTCTL_BACKEND") == "remote"

    def _take_sprint_takeup(
        self,
        *,
        prepared: PreparedSession,
        session_id: str,
        action_id: int,
        action_type: str,
        harness: str,
        model: str,
    ) -> _SprintTakeupState | None:
        takeup_config = self.config.global_config.sprintctl_takeup
        if not takeup_config.enabled:
            return None
        if takeup_config.remote_only and not self._project_uses_remote_sprintctl(
            prepared.project.env
        ):
            return None

        item = prepared.item_bundle.get("item", prepared.item_bundle)
        sprint_id = int(item["sprint_id"])
        actor = f"{takeup_config.actor_prefix}:{session_id}"
        context = f"actionq action {action_id} {action_type} via {harness}/{model}"
        prepared.sprintctl.takeup_take(
            sprint_id=sprint_id,
            actor=actor,
            instance_id=session_id,
            runtime_session_id=session_id,
            context=context,
        )
        return _SprintTakeupState(
            sprint_id=sprint_id,
            actor=actor,
            instance_id=session_id,
            runtime_session_id=session_id,
        )

    def _release_sprint_takeup(
        self,
        *,
        prepared: PreparedSession,
        takeup: _SprintTakeupState | None,
        reason: str,
    ) -> tuple[str, str | None]:
        if takeup is None:
            return "skipped", None
        try:
            prepared.sprintctl.takeup_release(
                sprint_id=takeup.sprint_id,
                actor=takeup.actor,
                instance_id=takeup.instance_id,
                runtime_session_id=takeup.runtime_session_id,
                reason=reason,
            )
        except Exception as exc:
            log.warning("failed to release sprint takeup for action #%s: %s", prepared.action_id, exc)
            return "failed", str(exc)
        return "released", None

    def _emit_pr_audit_events(
        self,
        *,
        audit: AuditctlClient | None,
        project_path: Path,
        project_env: dict[str, str] | None,
        branch: str,
        action_id: int,
        session_id: str,
        session_exit_event_id: str | None,
    ) -> None:
        pr = self._query_pull_request(project_path, branch, project_env)
        if pr is None:
            return

        refs = [session_exit_event_id] if session_exit_event_id else None
        pr_number = pr.get("number")
        state = pr.get("state")

        self._emit_audit(
            audit,
            "pr.open",
            actor=self.actor,
            summary=f"actionq PR open for action #{action_id}: #{pr_number}",
            refs=refs,
            metadata={
                "session_id": session_id,
                "action_id": action_id,
                "branch": branch,
                "pr_number": pr_number,
                "state": state,
            },
        )

        if state == "MERGED":
            self._emit_audit(
                audit,
                "pr.merge",
                actor=self.actor,
                summary=f"actionq PR merged for action #{action_id}: #{pr_number}",
                refs=refs,
                metadata={
                    "session_id": session_id,
                    "action_id": action_id,
                    "branch": branch,
                    "pr_number": pr_number,
                    "state": state,
                },
            )

    def _dispatch_and_run(self, action: dict) -> None:
        action_id = int(action["id"])
        action_type = action["action_type"]

        if action_type not in self.config.actions:
            try:
                self._actionctl.reject(
                    action_id,
                    f"No daemon config for action type {action_type}",
                    "action-type-config",
                    self.actor,
                )
            except Exception as exc:
                log.error("failed to reject unknown action type: %s", exc)
            return

        if action_type != "scope-iterate":
            try:
                self._actionctl.reject(
                    action_id,
                    f"Action type {action_type} not yet implemented in daemon",
                    "action-type-implemented",
                    self.actor,
                )
            except Exception as exc:
                log.error("failed to reject unimplemented action type: %s", exc)
            return

        action_config = self.config.actions[action_type]
        is_fake = action_config.runner in ("fake", "fake-commit")

        project_name = action.get("project") or ""
        project_config = self.config.projects.get(project_name)

        # Resolve harness routing before any expensive work.
        try:
            routing = resolve_routing(action, action_config, project_config, self.config)
        except RoutingError as exc:
            log.error("harness routing failed for action #%s: %s", action_id, exc)
            try:
                self._actionctl.reject(action_id, str(exc), "harness-routing", self.actor)
            except Exception as e2:
                log.error("failed to reject action after routing error: %s", e2)
            return

        # Generate session_id early so dispatch.queued can include it.
        session_id = new_session_id()
        audit: AuditctlClient | None = (
            self._audit_factory(project_config)
            if project_config is not None and self._audit_enabled
            else None
        )

        # dispatch.queued — before any expensive work; enforce fail_on_error here.
        dq_result = self._emit_audit(
            audit,
            "dispatch.queued",
            actor=self.actor,
            summary=f"actionq action queued: {action_type} #{action_id}",
            metadata={
                "session_id": session_id,
                "action_id": action_id,
                "action_type": action_type,
                "harness": routing.harness,
                "model": routing.model,
            },
        )
        if dq_result is not None and not dq_result.ok and self._fail_on_audit_error:
            log.error("audit dispatch.queued failed and fail_action_on_emit_error is set; rejecting")
            try:
                self._actionctl.reject(
                    action_id,
                    f"audit dispatch.queued failed: {dq_result.error}",
                    "audit-required",
                    self.actor,
                )
            except Exception as exc:
                log.error("failed to reject after audit failure: %s", exc)
            return

        handler = ScopeIterateHandler(
            self.config,
            self.runner,
            self._sprintctl_factory,
            worker=None,
            actionctl=self._actionctl,
            actor=self.actor,
        )

        try:
            prepared = handler.prepare(action, action_config, runtime_session_id=session_id)
        except _ActionSettled:
            return
        except CoordinatorError as exc:
            log.error("coordinator error in prepare: %s", exc)
            try:
                self._actionctl.fail(action_id, f"coordinator error: {exc}", self.actor)
            except Exception:
                pass
            return
        except Exception as exc:
            log.error("unexpected error in prepare: %s", exc, exc_info=True)
            try:
                self._actionctl.fail(action_id, f"internal error: {exc}", self.actor)
            except Exception:
                pass
            return

        # dispatch.started — worktree and branch are now known.
        dq_event_id = dq_result.event_id if dq_result else None
        self._emit_audit(
            audit,
            "dispatch.started",
            actor=self.actor,
            summary=f"actionq session dispatched: {action_type} #{action_id}",
            refs=[dq_event_id] if dq_event_id else None,
            metadata={
                "session_id": session_id,
                "action_id": action_id,
                "worktree": str(prepared.worktree),
                "branch": prepared.branch,
                "harness": routing.harness,
                "model": routing.model,
            },
        )

        session = SessionRecord(
            session_id=session_id,
            daemon_id=self.daemon_id,
            action_id=action_id,
            action_type=action_type,
            project=action.get("project"),
            target_ref=action.get("target_ref"),
            harness=routing.harness,
            model=routing.model,
            worktree=str(prepared.worktree),
            branch=prepared.branch,
            runtime_session_id=session_id,
            ttl_seconds=max(self._heartbeat_interval * 2, 1),
            claim_id=prepared.claim_id,
            work_item_id=prepared.item_id,
            claim_type="execute",
        )
        self._active_session = session
        takeup: _SprintTakeupState | None = None

        try:
            takeup = self._take_sprint_takeup(
                prepared=prepared,
                session_id=session_id,
                action_id=action_id,
                action_type=action_type,
                harness=session.harness,
                model=session.model,
            )
        except Exception as exc:
            log.error("sprint takeup failed before harness start: %s", exc)
            try:
                handler._fail_after_claim(
                    action_id,
                    prepared.item_bundle,
                    prepared.sprintctl,
                    prepared.item_id,
                    prepared.claim_id,
                    prepared.claim_token,
                    f"sprint takeup failed: {exc}",
                )
            except Exception:
                pass
            self._active_session = None
            return

        try:
            self._actionctl.emit(
                "session.dispatch",
                action_id=action_id,
                actor=self.actor,
                payload=session.dispatch_payload(routing_source=routing.routing_source),
            )
        except Exception as exc:
            log.warning("failed to emit session.dispatch: %s", exc)

        proc = self._start_harness(action_config, prepared, is_fake)
        session.pid = proc.pid
        session.started_at = _now_iso()
        self._persist_session(session)

        try:
            self._actionctl.emit(
                "session.started",
                action_id=action_id,
                actor=self.actor,
                payload=session.started_payload(),
            )
        except Exception as exc:
            log.warning("failed to emit session.started: %s", exc)

        # session.start — child PID exists; store event_id for cross-ref in session.exit.
        ss_result = self._emit_audit(
            audit,
            "session.start",
            actor=self.actor,
            summary=f"actionq session process started: {action_type} #{action_id} pid={session.pid}",
            refs=[dq_event_id] if dq_event_id else None,
            metadata={
                "session_id": session_id,
                "action_id": action_id,
                "pid": session.pid,
                "harness": session.harness,
                "model": session.model,
            },
        )
        session_start_event_id: str | None = ss_result.event_id if ss_result else None

        start_monotonic = time.monotonic()
        exit_code = self._run_with_heartbeats(proc, session, action_id, start_monotonic, audit=audit)
        duration = time.monotonic() - start_monotonic
        session.exited_at = _now_iso()
        sprint_release_status = "skipped"
        sprint_release_error: str | None = None

        try:
            outcome = handler.settle(prepared, action_config, exit_code=exit_code)
        except Exception as exc:
            log.error("settle error: %s", exc, exc_info=True)
            try:
                self._actionctl.fail(action_id, f"settle error: {exc}", self.actor)
            except Exception:
                pass
            outcome = "failed"
            if (
                takeup is not None
                and isinstance(exc, ClientError)
                and self.config.global_config.sprintctl_takeup.release_on_sprintctl_error
            ):
                sprint_release_status, sprint_release_error = self._release_sprint_takeup(
                    prepared=prepared,
                    takeup=takeup,
                    reason="shutdown" if self._shutdown.is_set() else outcome,
                )
        else:
            sprint_release_status, sprint_release_error = self._release_sprint_takeup(
                prepared=prepared,
                takeup=takeup,
                reason="shutdown" if self._shutdown.is_set() else outcome,
            )

        session.outcome = outcome

        # session.exit — cross-ref back to session.start event.
        se_result = self._emit_audit(
            audit,
            "session.exit",
            actor=self.actor,
            summary=f"actionq session exited: {action_type} #{action_id} outcome={outcome}",
            refs=[session_start_event_id] if session_start_event_id else None,
            metadata={
                "session_id": session_id,
                "action_id": action_id,
                "exit_code": exit_code,
                "duration_seconds": int(duration),
                "outcome": outcome,
            },
        )
        if audit is not None:
            audit_status = _AUDIT_OK if (se_result and se_result.ok) else _AUDIT_FAILED
            audit_error = se_result.error if se_result else None
        else:
            audit_status = _AUDIT_SKIPPED
            audit_error = None

        if audit is not None and outcome == "completed" and project_config is not None:
            self._emit_pr_audit_events(
                audit=audit,
                project_path=project_config.path,
                project_env=project_config.env,
                branch=prepared.branch,
                action_id=action_id,
                session_id=session_id,
                session_exit_event_id=se_result.event_id if se_result else None,
            )

        try:
            self._actionctl.emit(
                "session.exited",
                action_id=action_id,
                actor=self.actor,
                payload=session.exited_payload(
                    exit_code=exit_code,
                    duration_seconds=duration,
                    outcome=outcome,
                    audit_status=audit_status,
                    audit_error=audit_error,
                    sprint_release_status=sprint_release_status,
                    sprint_release_error=sprint_release_error,
                ),
            )
        except Exception as exc:
            log.warning("failed to emit session.exited: %s", exc)

        self._active_session = None
        self._persist_session(None)

    def _start_harness(
        self,
        action_config,
        prepared: PreparedSession,
        is_fake: bool,
    ) -> ProcessLike:
        if is_fake:
            fake = FakeCommitWorker(self.runner)
            returncode = 0
            try:
                fake.invoke(
                    action_config=action_config,
                    worktree=prepared.worktree,
                    prompt=prepared.prompt,
                    acl=prepared.acl,
                )
            except WorkerError as exc:
                log.warning("fake worker failed: %s", exc)
                returncode = 1
            return _ImmediateProcess(returncode)

        from .acl import claude_tool_args

        allowed, denied = claude_tool_args(prepared.acl)
        cmd = [
            self.config.global_config.claude_bin,
            "-p",
            "--model",
            action_config.model,
            "--output-format",
            "json",
            "--no-session-persistence",
            "--add-dir",
            str(prepared.worktree),
        ]
        if allowed:
            cmd.extend(["--allowedTools", ",".join(allowed)])
        if denied:
            cmd.extend(["--disallowedTools", ",".join(denied)])

        env = git_safe_env(prepared.worktree, self.config.global_config.worker_env)
        return self._process_factory(cmd, prepared.worktree, env, prepared.prompt)

    def _run_with_heartbeats(
        self,
        proc: ProcessLike,
        session: SessionRecord,
        action_id: int,
        start_monotonic: float,
        *,
        audit: AuditctlClient | None = None,
    ) -> int:
        # Poll every check_interval seconds to stay responsive to shutdown.
        check_interval = min(float(self._heartbeat_interval), 2.0)
        time_in_period = 0.0

        while True:
            try:
                code = proc.wait(timeout=check_interval)
                return code
            except subprocess.TimeoutExpired:
                time_in_period += check_interval

                if self._shutdown.is_set():
                    try:
                        self._actionctl.emit(
                            "session.paused",
                            action_id=action_id,
                            actor=self.actor,
                            payload=session.paused_payload(
                                reason="shutdown", mechanism="checkpoint-and-fail"
                            ),
                        )
                    except Exception as exc:
                        log.warning("failed to emit session.paused: %s", exc)

                    self._emit_audit(
                        audit,
                        "session.pause",
                        actor=self.actor,
                        summary=f"actionq session paused: shutdown action #{action_id}",
                        metadata={
                            "session_id": session.session_id,
                            "action_id": action_id,
                            "reason": "shutdown",
                            "mechanism": "checkpoint-and-fail",
                        },
                    )

                    try:
                        return proc.wait(timeout=self._graceful_shutdown_seconds)
                    except subprocess.TimeoutExpired:
                        log.warning(
                            "graceful shutdown timeout; terminating child pid=%s", session.pid
                        )
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                        try:
                            return proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                            return proc.wait()

                if time_in_period >= self._heartbeat_interval:
                    age = time.monotonic() - start_monotonic
                    try:
                        self._actionctl.emit(
                            "session.heartbeat",
                            action_id=action_id,
                            actor=self.actor,
                            payload=session.heartbeat_payload(age),
                        )
                    except Exception as exc:
                        log.warning("failed to emit heartbeat: %s", exc)
                    session.last_heartbeat_at = _now_iso()
                    self._persist_session(session)
                    time_in_period = 0.0

    def _persist_session(self, session: SessionRecord | None) -> None:
        sessions = [session] if session is not None else []
        try:
            save_session_state(self._session_state_path, sessions)
        except Exception as exc:
            log.warning("failed to persist session state: %s", exc)
