"""默认 CLI 的单 worker 路由与数据库连接边界。"""

from __future__ import annotations

from collections import deque
import json
import queue
import threading
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Protocol

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


DEFAULT_CLI_MESSAGE_QUEUE_LIMIT = 20


class CLIMessageQueue:
    """由终端主线程使用的有界普通消息队列。"""

    def __init__(self, limit: int = DEFAULT_CLI_MESSAGE_QUEUE_LIMIT) -> None:
        if limit <= 0:
            raise ValueError("CLI message queue limit must be positive")
        self._limit = limit
        self._messages: deque[str] = deque()

    def enqueue(self, message: str) -> bool:
        """在未达到上限时保存一条原始用户文本。"""
        if self.is_full():
            return False
        self._messages.append(message)
        return True

    def peek(self) -> str | None:
        """查看下一条消息，但不改变队列。"""
        return self._messages[0] if self._messages else None

    def dequeue(self) -> str | None:
        """取出最早进入队列的消息。"""
        return self._messages.popleft() if self._messages else None

    def clear(self) -> int:
        """清空尚未提交的消息，并返回清空数量。"""
        count = len(self._messages)
        self._messages.clear()
        return count

    def is_empty(self) -> bool:
        """返回是否没有待处理消息。"""
        return not self._messages

    def is_full(self) -> bool:
        """返回是否已经达到消息上限。"""
        return len(self._messages) >= self._limit

    @property
    def limit(self) -> int:
        """返回当前队列允许保存的最大消息数。"""
        return self._limit

    def __len__(self) -> int:
        return len(self._messages)


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
    cancel_event: threading.Event | None = None


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


class CLIEventType(str, Enum):
    """CLI 内部事件的来源类型。"""

    USER_INPUT = "user_input"
    WORKER_RESULT = "worker_result"
    STREAM_EVENT = "stream_event"
    CANCEL_REQUEST = "cancel_request"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class CLIEvent:
    """交给 CLI controller 串行处理的一项输入或后台通知。"""

    event_type: CLIEventType
    user_input: str = ""
    worker_result: CLIWorkerResult | None = None
    stream_event: object | None = None

    @classmethod
    def user_input_event(cls, user_input: str) -> "CLIEvent":
        return cls(event_type=CLIEventType.USER_INPUT, user_input=user_input)

    @classmethod
    def worker_result_event(cls, result: CLIWorkerResult) -> "CLIEvent":
        return cls(event_type=CLIEventType.WORKER_RESULT, worker_result=result)

    @classmethod
    def stream_event_event(cls, stream_event: object) -> "CLIEvent":
        return cls(event_type=CLIEventType.STREAM_EVENT, stream_event=stream_event)

    @classmethod
    def shutdown_event(cls) -> "CLIEvent":
        return cls(event_type=CLIEventType.SHUTDOWN)

    @classmethod
    def cancel_request_event(cls) -> "CLIEvent":
        return cls(event_type=CLIEventType.CANCEL_REQUEST)


class CLIEventQueue:
    """连接 CLI UI、controller 和 worker 的线程安全事件队列。"""

    def __init__(self) -> None:
        self._events: queue.Queue[CLIEvent] = queue.Queue()

    def put(self, event: CLIEvent) -> None:
        self._events.put(event)

    def get(self) -> CLIEvent:
        return self._events.get()

    def post_user_input(self, user_input: str) -> None:
        self.put(CLIEvent.user_input_event(user_input))

    def post_worker_result(self, result: CLIWorkerResult) -> None:
        self.put(CLIEvent.worker_result_event(result))

    def post_stream_event(self, stream_event: object) -> None:
        self.put(CLIEvent.stream_event_event(stream_event))

    def post_shutdown(self) -> None:
        self.put(CLIEvent.shutdown_event())

    def post_cancel_request(self) -> None:
        self.put(CLIEvent.cancel_request_event())


class CLIWorker:
    """串行执行 CLI 工作，并让每项数据库工作在线程内独占连接。"""

    def __init__(
        self,
        *,
        stream_sink: Callable[[object], None] | None,
        publish_result: Callable[[CLIWorkerResult], None],
    ) -> None:
        self._stream_sink = stream_sink
        self._publish_result = publish_result
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
            self._results.put(result)
            try:
                self._publish_result(result)
            except Exception:
                pass

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
        try:
            result = run_conversation(
                task.user_input,
                conn,
                session_id,
                task.cached_prompt,
                session_key=session_id,
                cancel_checker=(
                    task.cancel_event.is_set if task.cancel_event is not None else None
                ),
                tool_policy=task.tool_policy,
                stream_sink=self._stream_sink,
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
                cancel_checker=(
                    task.cancel_event.is_set if task.cancel_event is not None else None
                ),
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            return CLIWorkerResult(
                kind=task.kind,
                session_id=task.session_id,
                error=f"approval execution failed: {exc}",
            )

        result = run_conversation(
            "",
            conn,
            task.session_id,
            task.cached_prompt,
            session_key=task.session_id,
            cancel_checker=(
                task.cancel_event.is_set if task.cancel_event is not None else None
            ),
            resume_from_history=True,
            tool_policy=task.tool_policy,
            stream_sink=self._stream_sink,
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


class CLIControllerUI(Protocol):
    """controller 需要的终端显示与输入协调能力。"""

    def begin_stream_request(self) -> None: ...

    def handle_stream_event(self, event: object) -> None: ...

    def show_worker_result(self, result: CLIWorkerResult) -> None: ...

    def show_message(self, message: str) -> None: ...

    def allow_next_input(self) -> None: ...

    def stop_input(self) -> None: ...


class CLIController:
    """串行处理 CLI 事件，并独占会话、审批和普通消息队列状态。"""

    def __init__(
        self,
        *,
        events: CLIEventQueue,
        worker: CLIWorker,
        ui: CLIControllerUI,
        cached_prompt: str,
        tool_policy: object,
    ) -> None:
        self._events = events
        self._worker = worker
        self._ui = ui
        self._cached_prompt = cached_prompt
        self._tool_policy = tool_policy
        self._session_id: str | None = None
        self._pending_approval: dict | None = None
        self._session_choices: dict[str, str] = {}
        self._message_queue = CLIMessageQueue()
        self._running = False
        self._shutting_down = False
        self._current_cancel_event: threading.Event | None = None

    def run(self) -> None:
        """等待输入或 worker 通知；不使用轮询或定时唤醒。"""
        while True:
            try:
                event = self._events.get()
            except KeyboardInterrupt:
                # 保留主线程收到外部 SIGINT 时的事件化兼容兜底。
                self._events.post_cancel_request()
                continue
            self._handle_event(event)
            if event.event_type == CLIEventType.USER_INPUT and not self._shutting_down:
                self._ui.allow_next_input()
            if self._shutting_down and not self._running:
                return

    def _handle_event(self, event: CLIEvent) -> None:
        if event.event_type == CLIEventType.USER_INPUT:
            self._handle_user_input(event.user_input)
            return
        if event.event_type == CLIEventType.WORKER_RESULT:
            self._handle_worker_results()
            return
        if event.event_type == CLIEventType.STREAM_EVENT:
            if (
                event.stream_event is not None
                and (
                    self._current_cancel_event is None
                    or not self._current_cancel_event.is_set()
                )
            ):
                self._ui.handle_stream_event(event.stream_event)
            return
        if event.event_type == CLIEventType.CANCEL_REQUEST:
            self._handle_cancel_request(announce_idle=False)
            return
        if event.event_type == CLIEventType.SHUTDOWN:
            self._begin_shutdown()

    def _handle_user_input(self, raw_user_input: str) -> None:
        stripped_user_input = raw_user_input.lstrip()
        literal_input = (
            raw_user_input.startswith("//") or raw_user_input[:1].isspace()
        )
        user_input = (
            stripped_user_input[1:]
            if literal_input and stripped_user_input.startswith("//")
            else raw_user_input.strip()
        )
        if not user_input or (
            not literal_input and user_input.lower() in ("quit", "exit")
        ):
            self._begin_shutdown()
            return

        command, _, command_argument = user_input.partition(" ")
        command = "" if literal_input else command.lower()
        if command in {"/quit", "/exit"}:
            self._begin_shutdown()
            return
        if command == "/stop":
            self._handle_cancel_request(announce_idle=True)
            return

        if command in {"/sessions", "/resume", "/new"} and (
            self._running or not self._message_queue.is_empty()
        ):
            self._ui.show_message(
                "cannot change sessions while agent is running or messages are queued."
            )
            return

        if self._pending_approval is not None:
            self._handle_pending_approval(command, command_argument)
            return

        if command in {"/approve", "/deny"}:
            self._ui.show_message("no approval is pending")
            return

        if command == "/resume":
            selection = command_argument.strip()
            task = (
                CLIWorkerTask(
                    kind="list_sessions",
                    current_session_id=self._session_id,
                )
                if not selection
                else CLIWorkerTask(
                    kind="resume",
                    session_id=self._session_choices.get(selection, selection),
                )
            )
            self._submit_task(task)
            return
        if command == "/new":
            self._session_id = None
            self._ui.show_message("new session will start with your next message")
            return
        if command == "/sessions":
            self._submit_task(
                CLIWorkerTask(
                    kind="list_sessions",
                    current_session_id=self._session_id,
                )
            )
            return
        if command.startswith("/"):
            self._ui.show_message(f"unknown command: {command}")
            return

        self._submit_or_queue_message(user_input)

    def _handle_pending_approval(self, command: str, argument: str) -> None:
        if self._running:
            self._ui.show_message("agent is running; approval is still pending.")
            return
        if command == "/deny":
            task = CLIWorkerTask(
                kind="deny",
                session_id=self._session_id,
                approval_request=self._pending_approval,
            )
        elif command == "/approve":
            task = CLIWorkerTask(
                kind="approve",
                session_id=self._session_id,
                cached_prompt=self._cached_prompt,
                tool_policy=self._tool_policy,
                approval_request=self._pending_approval,
                approval_scope=argument.strip().lower() or "once",
            )
        else:
            self._ui.show_message("enter /approve [once|session] or /deny")
            return
        if self._session_id is None or not self._submit_task(task, begins_stream=True):
            self._ui.show_message("agent is running; approval is still pending.")

    def _submit_or_queue_message(self, user_input: str) -> None:
        if self._running or not self._message_queue.is_empty():
            if self._message_queue.enqueue(user_input):
                self._ui.show_message("message queued.")
            else:
                self._ui.show_message(
                    f"message queue is full (limit: {self._message_queue.limit})."
                )
            return
        task = self._conversation_task(user_input)
        if not self._submit_task(task, begins_stream=True):
            if self._message_queue.enqueue(user_input):
                self._ui.show_message("message queued.")
            else:
                self._ui.show_message(
                    f"message queue is full (limit: {self._message_queue.limit})."
                )

    def _conversation_task(self, user_input: str) -> CLIWorkerTask:
        return CLIWorkerTask(
            kind="conversation",
            session_id=self._session_id,
            user_input=user_input,
            cached_prompt=self._cached_prompt,
            tool_policy=self._tool_policy,
        )

    def _submit_task(self, task: CLIWorkerTask, *, begins_stream: bool = False) -> bool:
        if self._shutting_down or self._running:
            return False
        cancel_event = (
            threading.Event() if task.kind in {"conversation", "approve"} else None
        )
        if cancel_event is not None:
            task = replace(task, cancel_event=cancel_event)
        if begins_stream:
            self._ui.begin_stream_request()
        if not self._worker.submit(task):
            return False
        self._running = True
        self._current_cancel_event = cancel_event
        return True

    def _handle_worker_results(self) -> None:
        for result in self._worker.drain_results():
            self._running = False
            self._current_cancel_event = None
            self._apply_worker_result(result)
            self._ui.show_worker_result(result)
            self._submit_next_queued_message()

    def _apply_worker_result(self, result: CLIWorkerResult) -> None:
        if result.kind == "list_sessions":
            if result.error is None:
                self._session_choices = {
                    str(index): str(session["session_id"])
                    for index, session in enumerate(result.sessions, start=1)
                }
            return
        if result.kind == "resume":
            if result.error is None:
                self._session_id = result.session_id
            return
        if result.session_id is not None:
            self._session_id = result.session_id
        if result.kind == "deny" and result.error is None:
            self._pending_approval = None
            return
        conversation_result = result.conversation_result
        if not isinstance(conversation_result, dict):
            return
        if conversation_result.get("status") == "awaiting_approval":
            request = conversation_result.get("approval_request")
            self._pending_approval = request if isinstance(request, dict) else None
        else:
            self._pending_approval = None

    def _submit_next_queued_message(self) -> None:
        if (
            self._shutting_down
            or self._running
            or self._pending_approval is not None
        ):
            return
        user_input = self._message_queue.peek()
        if user_input is None:
            return
        if self._submit_task(self._conversation_task(user_input), begins_stream=True):
            self._message_queue.dequeue()

    def _begin_shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        cleared = self._message_queue.clear()
        if cleared:
            self._ui.show_message(f"discarded {cleared} queued message(s)")
        self._request_current_cancellation()
        self._ui.stop_input()

    def _handle_cancel_request(
        self,
        *,
        announce_idle: bool,
    ) -> None:
        if self._pending_approval is not None and not self._running:
            self._ui.show_message(
                "当前任务正在等待审批，没有运行中的操作。\n请使用 /deny 拒绝审批。"
            )
            return
        if not self._running or self._current_cancel_event is None:
            if announce_idle:
                self._ui.show_message("当前没有正在运行的任务。")
            return
        if self._current_cancel_event.is_set():
            if announce_idle:
                self._ui.show_message("当前任务已经请求停止。")
            return
        self._current_cancel_event.set()
        self._ui.show_message("已请求停止当前任务")

    def _request_current_cancellation(self) -> None:
        if self._current_cancel_event is not None:
            self._current_cancel_event.set()
