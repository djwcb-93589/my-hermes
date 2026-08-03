"""Claude Code 受管会话的最小启动与生命周期工作流。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace

from hermes.claude_code.contracts import (
    ClaudeCodeProcessPort,
    ClaudeCodeProcessSnapshot,
    ClaudeCodeReadResult,
    ClaudeCodeRuntimeError,
    ClaudeCodeSessionRef,
    ClaudeCodeSnapshot,
)
from hermes.claude_code.snapshot import ClaudeCodeObservationState


class ClaudeCodeRuntime:
    """保存 SessionRef 与有界解释状态；进程和 cleanup 仍由 Manager 拥有。"""

    def __init__(
        self,
        process_port: ClaudeCodeProcessPort,
        *,
        executable: str = "claude",
        clock: Callable[[], float] = time.time,
    ) -> None:
        required = (
            "preflight_start",
            "start",
            "read",
            "write",
            "submit",
            "status",
            "wait",
            "interrupt",
            "kill",
        )
        if any(
            not callable(getattr(process_port, name, None))
            for name in required
        ):
            raise TypeError("process_port does not implement the required interface")
        if not isinstance(executable, str) or not executable.strip():
            raise ValueError("executable must be a non-empty string")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._port = process_port
        self._executable = executable
        self._clock = clock
        self._lock = threading.RLock()
        # 不复制 Handle、完整日志或 ProcessManager registry；解释缓冲均有硬上限。
        self._sessions: dict[str, ClaudeCodeSessionRef] = {}
        self._active_cwds: dict[str, str] = {}
        self._observations: dict[str, ClaudeCodeObservationState] = {}

    def start(
        self,
        *,
        user_requested: bool,
        session_owner: str,
        cwd: str,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> ClaudeCodeSessionRef:
        """仅在用户明确指定 CC 后启动一个受管后台 PTY。"""

        if user_requested is not True:
            raise ClaudeCodeRuntimeError(
                "explicit_user_request_required",
                "Claude Code requires an explicit current user request",
            )
        self._require_owner(session_owner)
        if not isinstance(cwd, str) or not cwd.strip():
            raise ClaudeCodeRuntimeError(
                "cwd_required",
                "Claude Code requires an explicit working directory",
            )
        normalized_cwd = self._port.preflight_start(
            session_owner=session_owner,
            cwd=cwd,
            executable=self._executable,
        )

        with self._lock:
            self._assert_cwd_available_locked(normalized_cwd)
            snapshot = self._port.start(
                session_owner=session_owner,
                cwd=normalized_cwd,
                executable=self._executable,
                cancel_checker=cancel_checker,
            )
            try:
                now = float(self._clock())
                if snapshot.process_id in self._sessions:
                    raise RuntimeError("duplicate Claude Code process id")
                if snapshot.cwd != normalized_cwd:
                    raise RuntimeError("Claude Code cwd changed before registration")
                session = ClaudeCodeSessionRef(
                    process_id=snapshot.process_id,
                    session_owner=session_owner,
                    cwd=normalized_cwd,
                    cursor=0,
                    started_at=snapshot.started_at,
                    last_activity_at=now,
                )
                observation = ClaudeCodeObservationState(
                    initial_cursor=session.cursor,
                    initial_process_status=snapshot.status,
                )
                self._sessions[session.process_id] = session
                self._observations[session.process_id] = observation
                if snapshot.active:
                    self._active_cwds[session.cwd] = session.process_id
                return session
            except Exception as registration_error:
                self._discard_session_locked(
                    session_owner=session_owner,
                    process_id=snapshot.process_id,
                )
                try:
                    cleanup_snapshot = self._port.kill(
                        session_owner=session_owner,
                        process_id=snapshot.process_id,
                        grace_seconds=0.0,
                    )
                    if cleanup_snapshot.active:
                        raise ClaudeCodeRuntimeError(
                            "session_registration_failed",
                            "Claude Code registration cleanup remained active",
                        )
                except ClaudeCodeRuntimeError as cleanup_error:
                    raise ClaudeCodeRuntimeError(
                        "session_registration_failed",
                        "Claude Code session registration and cleanup failed",
                    ) from cleanup_error
                raise ClaudeCodeRuntimeError(
                    "session_registration_failed",
                    "Claude Code session registration failed and was rolled back",
                ) from registration_error

    def read(
        self,
        *,
        session_owner: str,
        process_id: str,
        limit: int = 20_000,
    ) -> ClaudeCodeReadResult:
        """只读取已保存 cursor 后的新输出，并原样采用 next_cursor。"""

        with self._lock:
            session = self._require_session_locked(session_owner, process_id)
            observation = self._observation_locked(session)
            page = self._invoke(
                session,
                lambda: self._port.read(
                    session_owner=session_owner,
                    process_id=process_id,
                    cursor=session.cursor,
                    limit=limit,
                ),
            )
            status_changed = observation.note_process_status(page.status)
            has_new_output = (
                page.next_cursor > session.cursor and bool(page.output)
            )
            last_activity_at = session.last_activity_at
            if status_changed or has_new_output:
                last_activity_at = float(self._clock())
            updated = replace(
                session,
                cursor=page.next_cursor,
                last_activity_at=last_activity_at,
            )
            self._sessions[process_id] = updated
            self._release_cwd_if_terminal_locked(updated, page.status)
            return ClaudeCodeReadResult(
                session=updated,
                status=page.status,
                output=page.output,
                requested_cursor=page.requested_cursor,
                available_from_cursor=page.available_from_cursor,
                next_cursor=page.next_cursor,
                output_truncated=page.output_truncated,
                exit_code=page.exit_code,
            )

    def observe(
        self,
        *,
        session_owner: str,
        process_id: str,
        limit: int = 20_000,
    ) -> ClaudeCodeSnapshot:
        """执行一次读取、规范化和状态识别，不循环、不输入也不终止。"""

        with self._lock:
            session = self._require_session_locked(session_owner, process_id)
            observation = self._observation_locked(session)
            timestamp = float(self._clock())
            page = None
            process_snapshot = None
            lost = False
            observation_errors: list[tuple[str, str, str]] = []

            try:
                page = self._port.read(
                    session_owner=session_owner,
                    process_id=process_id,
                    cursor=session.cursor,
                    limit=limit,
                )
            except ClaudeCodeRuntimeError as error:
                observation_errors.append(
                    ("read", error.error_type, error.safe_message)
                )
                lost = error.error_type == "session_not_found"

            if page is not None:
                session = replace(
                    session,
                    cursor=page.next_cursor,
                )
                self._sessions[process_id] = session

            if not lost:
                try:
                    process_snapshot = self._port.status(
                        session_owner=session_owner,
                        process_id=process_id,
                    )
                except ClaudeCodeRuntimeError as error:
                    observation_errors.append(
                        ("status", error.error_type, error.safe_message)
                    )
                    lost = (
                        error.error_type == "session_not_found"
                        or page is None
                    )

            observable_status = (
                process_snapshot.status
                if process_snapshot is not None
                else page.status if page is not None and not lost else None
            )
            if observable_status is not None:
                self._release_cwd_if_terminal_locked(
                    session,
                    observable_status,
                )

            observation_result = observation.build_result(
                session_ref=session,
                page=page,
                process_snapshot=process_snapshot,
                timestamp=timestamp,
                lost=lost,
                observation_errors=tuple(observation_errors),
            )
            snapshot = observation_result.snapshot
            if observation_result.activity_detected:
                session = replace(session, last_activity_at=timestamp)
                self._sessions[process_id] = session
                snapshot = replace(
                    snapshot,
                    session_ref=session,
                    last_activity_at=timestamp,
                )
            return snapshot

    def write(
        self,
        *,
        session_owner: str,
        process_id: str,
        data: str,
    ) -> int:
        """原样写入，不追加 Enter，也不保存输入。"""

        with self._lock:
            session = self._require_session_locked(
                session_owner,
                process_id,
            )
            result = self._invoke(
                session,
                lambda: self._port.write(
                    session_owner=session_owner,
                    process_id=process_id,
                    data=data,
                ),
            )
            self._record_outbound_input_locked(
                session,
                data=data,
                input_kind="write",
                submitted=False,
            )
            return result

    def submit(
        self,
        *,
        session_owner: str,
        process_id: str,
        data: str,
    ) -> int:
        """提交文本和一次 transport Enter，不保存输入。"""

        with self._lock:
            session = self._require_session_locked(
                session_owner,
                process_id,
            )
            try:
                result = self._invoke(
                    session,
                    lambda: self._port.submit(
                        session_owner=session_owner,
                        process_id=process_id,
                        data=data,
                    ),
                )
            except ClaudeCodeRuntimeError as error:
                if error.delivery_unknown:
                    self._record_uncertain_submit_locked(
                        session,
                        data=data,
                    )
                raise
            self._record_outbound_input_locked(
                session,
                data=data,
                input_kind="submit",
                submitted=True,
            )
            return result

    def status(
        self,
        *,
        session_owner: str,
        process_id: str,
    ) -> ClaudeCodeProcessSnapshot:
        """返回 ProcessStatus，不推断 Claude Code 语义状态。"""

        session = self._require_session(session_owner, process_id)
        snapshot = self._invoke(
            session,
            lambda: self._port.status(
                session_owner=session_owner,
                process_id=process_id,
            ),
        )
        self._touch(session, process_status=snapshot.status)
        return snapshot

    def wait(
        self,
        *,
        session_owner: str,
        process_id: str,
        timeout: float = 30.0,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> ClaudeCodeProcessSnapshot:
        """执行一次有界等待；超时不终止进程。"""

        session = self._require_session(session_owner, process_id)
        snapshot = self._invoke(
            session,
            lambda: self._port.wait(
                session_owner=session_owner,
                process_id=process_id,
                timeout=timeout,
                cancel_checker=cancel_checker,
            ),
        )
        self._touch(session, process_status=snapshot.status)
        return snapshot

    def interrupt(
        self,
        *,
        session_owner: str,
        process_id: str,
    ) -> int:
        """请求协作式 Ctrl+C；不承担强制终止。"""

        with self._lock:
            session = self._require_session_locked(
                session_owner,
                process_id,
            )
            try:
                result = self._invoke(
                    session,
                    lambda: self._port.interrupt(
                        session_owner=session_owner,
                        process_id=process_id,
                    ),
                )
            except ClaudeCodeRuntimeError as error:
                if error.delivery_unknown:
                    self._mark_interrupt_delivery_unknown_locked(session)
                raise
            self._mark_interrupt_requested_locked(session)
            return result

    def kill(
        self,
        *,
        session_owner: str,
        process_id: str,
        grace_seconds: float = 2.0,
    ) -> ClaudeCodeProcessSnapshot:
        """请求 ProcessManager 协作式中断并按需强制清理进程树。"""

        session = self._require_session(session_owner, process_id)
        snapshot = self._invoke(
            session,
            lambda: self._port.kill(
                session_owner=session_owner,
                process_id=process_id,
                grace_seconds=grace_seconds,
            ),
        )
        self._touch(session, process_status=snapshot.status)
        return snapshot

    def _assert_cwd_available_locked(self, cwd: str) -> None:
        """拒绝同一 runtime 内同 cwd 的第二个活跃 CC。"""

        process_id = self._active_cwds.get(cwd)
        if process_id is None:
            return
        session = self._sessions.get(process_id)
        if session is None:
            self._active_cwds.pop(cwd, None)
            return
        try:
            snapshot = self._port.status(
                session_owner=session.session_owner,
                process_id=session.process_id,
            )
        except ClaudeCodeRuntimeError as error:
            if error.error_type == "session_not_found":
                self._discard_session_locked(
                    session_owner=session.session_owner,
                    process_id=session.process_id,
                )
                return
            raise ClaudeCodeRuntimeError(
                "cwd_session_active",
                "An existing managed Claude Code session could not be cleared",
            ) from error
        if snapshot.active:
            raise ClaudeCodeRuntimeError(
                "cwd_session_active",
                "A managed Claude Code session is already active for this cwd",
            )
        self._active_cwds.pop(cwd, None)

    def _require_session(
        self,
        session_owner: str,
        process_id: str,
    ) -> ClaudeCodeSessionRef:
        """验证调用 owner、SessionRef 与 process id 的一致性。"""

        with self._lock:
            return self._require_session_locked(session_owner, process_id)

    def _require_session_locked(
        self,
        session_owner: str,
        process_id: str,
    ) -> ClaudeCodeSessionRef:
        """在 runtime 锁内读取最小引用，不查询系统 PID。"""

        self._require_owner(session_owner)
        if not isinstance(process_id, str) or not process_id.strip():
            raise ClaudeCodeRuntimeError(
                "session_not_found",
                "Claude Code session was not found",
            )
        session = self._sessions.get(process_id)
        if session is None:
            raise ClaudeCodeRuntimeError(
                "session_not_found",
                "Claude Code session was not found",
            )
        if session.session_owner != session_owner:
            raise ClaudeCodeRuntimeError(
                "session_owner_mismatch",
                "Claude Code session belongs to a different owner",
            )
        return session

    def _observation_locked(
        self,
        session: ClaudeCodeSessionRef,
    ) -> ClaudeCodeObservationState:
        """读取当前会话的有界解释状态，不复制 ProcessManager 日志。"""

        observation = self._observations.get(session.process_id)
        if observation is None:
            observation = ClaudeCodeObservationState(
                initial_cursor=session.cursor
            )
            self._observations[session.process_id] = observation
        return observation

    @staticmethod
    def _require_owner(session_owner: str) -> None:
        """拒绝缺失的调用 session owner。"""

        if not isinstance(session_owner, str) or not session_owner.strip():
            raise ClaudeCodeRuntimeError(
                "session_owner_mismatch",
                "Claude Code requires a valid owning session",
            )

    def _invoke(self, session: ClaudeCodeSessionRef, operation: Callable):
        """在 Manager 记录消失时同步丢弃对应的非资源引用。"""

        try:
            return operation()
        except ClaudeCodeRuntimeError as error:
            if error.error_type == "session_not_found":
                with self._lock:
                    self._discard_session_locked(
                        session_owner=session.session_owner,
                        process_id=session.process_id,
                    )
            raise

    def _touch(
        self,
        session: ClaudeCodeSessionRef,
        *,
        process_status: str | None = None,
    ) -> None:
        """只有 ProcessStatus 实质变化时才更新时间。"""

        with self._lock:
            current = self._sessions.get(session.process_id)
            if current is None or current.session_owner != session.session_owner:
                return
            updated = current
            if (
                process_status is not None
                and self._observation_locked(current).note_process_status(
                    process_status
                )
            ):
                updated = replace(
                    current,
                    last_activity_at=float(self._clock()),
                )
                self._sessions[session.process_id] = updated
            if process_status is not None:
                self._release_cwd_if_terminal_locked(updated, process_status)

    def _record_outbound_input_locked(
        self,
        session: ClaudeCodeSessionRef,
        *,
        data: str,
        input_kind: str,
        submitted: bool,
    ) -> None:
        """原子记录安全 input evidence，并把成功输入计为活动。"""

        current = self._sessions.get(session.process_id)
        if current is None or current.session_owner != session.session_owner:
            return
        timestamp = float(self._clock())
        observation = self._observation_locked(current)
        observation.record_outbound_input(
            data,
            input_kind=input_kind,
            sent_at=timestamp,
            cursor_before=session.cursor,
            cursor_after=current.cursor,
        )
        if submitted:
            observation.mark_input_submitted()
        self._sessions[session.process_id] = replace(
            current,
            last_activity_at=timestamp,
        )

    def _record_uncertain_submit_locked(
        self,
        session: ClaudeCodeSessionRef,
        *,
        data: str,
    ) -> None:
        """未知送达只保留短期 echo 指纹，并使当前提示在事实源失效。"""

        current = self._sessions.get(session.process_id)
        if current is None or current.session_owner != session.session_owner:
            return
        observation = self._observation_locked(current)
        observation.record_outbound_input(
            data,
            input_kind="submit",
            sent_at=float(self._clock()),
            cursor_before=session.cursor,
            cursor_after=current.cursor,
        )
        observation.mark_input_delivery_unknown()

    def _mark_interrupt_requested_locked(
        self,
        session: ClaudeCodeSessionRef,
    ) -> None:
        """原子记录明确送达的 interrupt，并更新活动时间。"""

        current = self._sessions.get(session.process_id)
        if current is None or current.session_owner != session.session_owner:
            return
        self._observation_locked(current).mark_interrupt_requested()
        self._sessions[session.process_id] = replace(
            current,
            last_activity_at=float(self._clock()),
        )

    def _mark_interrupt_delivery_unknown_locked(
        self,
        session: ClaudeCodeSessionRef,
    ) -> None:
        """中断未能确认送达时只清除旧提示，不伪造活动时间。"""

        current = self._sessions.get(session.process_id)
        if current is None or current.session_owner != session.session_owner:
            return
        self._observation_locked(current).mark_interrupt_delivery_unknown()

    def _release_cwd_if_terminal_locked(
        self,
        session: ClaudeCodeSessionRef,
        process_status: str,
    ) -> None:
        """终态只释放并发占用；ProcessManager 仍保留生命周期记录。"""

        if process_status in {"starting", "running"}:
            return
        if self._active_cwds.get(session.cwd) == session.process_id:
            self._active_cwds.pop(session.cwd, None)

    def _discard_session_locked(
        self,
        *,
        session_owner: str,
        process_id: str,
    ) -> None:
        """只丢弃 SessionRef；不执行任何进程或 Backend cleanup。"""

        session = self._sessions.get(process_id)
        if session is None or session.session_owner != session_owner:
            return
        self._sessions.pop(process_id, None)
        self._observations.pop(process_id, None)
        if self._active_cwds.get(session.cwd) == process_id:
            self._active_cwds.pop(session.cwd, None)


__all__ = ["ClaudeCodeRuntime"]
