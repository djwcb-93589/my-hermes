"""默认 CLI 的单 worker 路由与数据库连接边界。"""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from typing import Callable

from hermes.cli_approval import execute_cli_approval
from hermes.config import DB_PATH
from hermes.conversation import run_conversation
from hermes.db import (
    create_session,
    get_session_messages,
    init_db,
    list_cli_sessions,
    replace_tool_message_content,
    session_exists,
)


@dataclass(frozen=True)
class CLIWorkerTask:
    """主线程交给 CLI worker 的单项工作。"""

    kind: str
    session_id: str | None = None
    user_input: str = ""
    cached_prompt: str = ""
    tool_policy: object | None = None
    approval_request: dict | None = None
    approval_scope: str = "once"
    current_session_id: str | None = None


@dataclass(frozen=True)
class CLIWorkerResult:
    """worker 完成一项工作后交回主线程的结果。"""

    kind: str
    session_id: str | None = None
    conversation_result: dict | None = None
    sessions: tuple[dict, ...] = ()
    messages: tuple[dict, ...] = ()
    current_session_id: str | None = None
    error: str | None = None


class CLIWorker:
    """串行执行 CLI 工作，并让每项数据库工作在线程内独占连接。"""

    def __init__(
        self,
        *,
        renderer,
        on_result: Callable[[CLIWorkerResult], None],
    ) -> None:
        self._renderer = renderer
        self._on_result = on_result
        self._tasks: queue.Queue[CLIWorkerTask | None] = queue.Queue()
        self._results: queue.Queue[CLIWorkerResult] = queue.Queue()
        self._lock = threading.Lock()
        self._accepting = True
        self._busy = False
        self._thread = threading.Thread(
            target=self._run,
            name="hermes-cli-worker",
            daemon=False,
        )

    def start(self) -> None:
        """启动单一 worker 线程。"""
        self._thread.start()

    def submit(self, task: CLIWorkerTask) -> bool:
        """仅在没有未消费工作结果时接受下一项工作。"""
        with self._lock:
            if not self._accepting or self._busy:
                return False
            self._busy = True
        self._tasks.put(task)
        return True

    def is_busy(self) -> bool:
        """返回 worker 是否仍有运行中或待主线程消费的工作。"""
        with self._lock:
            return self._busy

    def drain_results(self) -> list[CLIWorkerResult]:
        """让主线程消费完成结果，并释放下一次提交资格。"""
        results: list[CLIWorkerResult] = []
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                break
            results.append(result)

        if results:
            with self._lock:
                self._busy = False
        return results

    def shutdown(self) -> None:
        """等待当前工作收尾后停止线程，避免遗留非守护线程。"""
        with self._lock:
            if not self._accepting:
                return
            self._accepting = False
        self._tasks.put(None)
        self._thread.join()

    def _run(self) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                return
            try:
                result = self._execute(task)
            except Exception as exc:
                result = CLIWorkerResult(
                    kind=task.kind,
                    session_id=task.session_id,
                    error=f"worker task failed: {type(exc).__name__}",
                )
            try:
                self._on_result(result)
            except Exception:
                pass
            self._results.put(result)

    def _execute(self, task: CLIWorkerTask) -> CLIWorkerResult:
        """为单项工作创建、使用并关闭 SQLite 连接。"""
        conn = init_db(DB_PATH)
        try:
            if task.kind == "conversation":
                return self._run_conversation_task(conn, task)
            if task.kind == "list_sessions":
                sessions = list_cli_sessions(conn, limit=10, offset=0)
                return CLIWorkerResult(
                    kind=task.kind,
                    sessions=tuple(sessions),
                    current_session_id=task.current_session_id,
                )
            if task.kind == "resume":
                return self._resume_session(conn, task)
            if task.kind == "approve":
                return self._approve_and_resume(conn, task)
            if task.kind == "deny":
                return self._deny_approval(conn, task)
            return CLIWorkerResult(
                kind=task.kind,
                error=f"unsupported CLI worker task: {task.kind}",
            )
        finally:
            conn.close()

    def _run_conversation_task(self, conn, task: CLIWorkerTask) -> CLIWorkerResult:
        session_id = task.session_id or create_session(conn)
        self._renderer.begin_request()
        try:
            result = run_conversation(
                task.user_input,
                conn,
                session_id,
                task.cached_prompt,
                session_key=session_id,
                tool_policy=task.tool_policy,
                stream_sink=self._renderer.handle_event,
            )
        except Exception as exc:
            return CLIWorkerResult(
                kind=task.kind,
                session_id=session_id,
                error=f"worker task failed: {type(exc).__name__}",
            )
        return CLIWorkerResult(
            kind=task.kind,
            session_id=session_id,
            conversation_result=result,
        )

    @staticmethod
    def _resume_session(conn, task: CLIWorkerTask) -> CLIWorkerResult:
        session_id = task.session_id
        if not session_id or not session_exists(conn, session_id, source="cli"):
            return CLIWorkerResult(
                kind=task.kind,
                error=f"session not found: {session_id or ''}",
            )
        return CLIWorkerResult(
            kind=task.kind,
            session_id=session_id,
            messages=tuple(get_session_messages(conn, session_id)),
        )

    def _approve_and_resume(self, conn, task: CLIWorkerTask) -> CLIWorkerResult:
        if task.session_id is None or task.approval_request is None:
            return CLIWorkerResult(
                kind=task.kind,
                error="approval request is invalid",
            )
        try:
            execute_cli_approval(
                conn,
                session_id=task.session_id,
                request=task.approval_request,
                scope=task.approval_scope,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            return CLIWorkerResult(
                kind=task.kind,
                session_id=task.session_id,
                error=f"approval execution failed: {exc}",
            )

        self._renderer.begin_request()
        result = run_conversation(
            "",
            conn,
            task.session_id,
            task.cached_prompt,
            session_key=task.session_id,
            resume_from_history=True,
            tool_policy=task.tool_policy,
            stream_sink=self._renderer.handle_event,
        )
        return CLIWorkerResult(
            kind=task.kind,
            session_id=task.session_id,
            conversation_result=result,
        )

    @staticmethod
    def _deny_approval(conn, task: CLIWorkerTask) -> CLIWorkerResult:
        if task.session_id is None or task.approval_request is None:
            return CLIWorkerResult(
                kind=task.kind,
                error="approval request is invalid",
            )
        denied = json.dumps({
            "ok": False,
            "error_type": "approval_denied",
            "error": "operation was denied by the user",
        }, ensure_ascii=False)
        if not replace_tool_message_content(
            conn,
            task.session_id,
            str(task.approval_request.get("tool_call_id", "")),
            denied,
        ):
            return CLIWorkerResult(
                kind=task.kind,
                session_id=task.session_id,
                error="approval result could not be recorded",
            )
        return CLIWorkerResult(kind=task.kind, session_id=task.session_id)
