"""默认 CLI 的终端输入输出与事件唤醒边界。"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
import sys
import threading
from typing import TYPE_CHECKING, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from hermes.cli_streaming import CLIStreamRenderer

if TYPE_CHECKING:
    from hermes.cli_state_machine import CLIWorkerResult


PROMPT_TEXT = "You: "
CLI_REPLAY_MESSAGE_LIMIT = 4000


def _is_interactive_terminal() -> bool:
    """判断当前标准输入和输出是否都可用于交互式终端。"""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, OSError):
        return False


class CLIInput:
    """复用一个交互式输入会话，并在非终端环境回退到原生输入。"""

    def __init__(self) -> None:
        self._session = PromptSession() if _is_interactive_terminal() else None
        self._interrupt_callback: Callable[[], None] | None = None
        self._interrupt_lock = threading.Lock()
        self._programmatic_cancel = threading.Event()

    def set_interrupt_callback(self, callback: Callable[[], None]) -> None:
        """设置由输入线程检测到 Ctrl+C 时使用的线程安全通知入口。"""
        with self._interrupt_lock:
            self._interrupt_callback = callback

    def cancel_current_input(self) -> bool:
        """从 controller 线程安全地取消当前 prompt_toolkit 输入编辑。"""
        if self._session is None:
            return False
        app = getattr(self._session, "app", None)
        loop = getattr(app, "loop", None)
        if app is None or loop is None or not loop.is_running():
            return False

        self._programmatic_cancel.set()

        def interrupt_prompt() -> None:
            if app.is_running:
                app.exit(exception=KeyboardInterrupt)

        try:
            loop.call_soon_threadsafe(interrupt_prompt)
        except RuntimeError:
            self._programmatic_cancel.clear()
            return False
        return True

    def prompt(self) -> str:
        """读取一条用户输入；Ctrl+C 仅取消本次输入并重新提示。"""
        while True:
            try:
                if self._session is not None:
                    return self._session.prompt(PROMPT_TEXT)
                return input(PROMPT_TEXT)
            except KeyboardInterrupt:
                if not self._programmatic_cancel.is_set():
                    self._notify_interrupt()
                self._programmatic_cancel.clear()
                print()
            except EOFError:
                return ""

    def _notify_interrupt(self) -> None:
        with self._interrupt_lock:
            callback = self._interrupt_callback
        if callback is not None:
            callback()


class CLIUI:
    """持续读取终端输入，并在 controller 指示下显示 CLI 输出。"""

    def __init__(
        self,
        *,
        cli_input: CLIInput,
        post_user_input: Callable[[str], None],
        post_shutdown: Callable[[], None],
        post_cancel_request: Callable[[], None],
    ) -> None:
        self._cli_input = cli_input
        self._post_user_input = post_user_input
        self._post_shutdown = post_shutdown
        cli_input.set_interrupt_callback(post_cancel_request)
        self._renderer = CLIStreamRenderer()
        self._stop_input = threading.Event()
        self._allow_next_input = threading.Event()
        self._input_thread = threading.Thread(
            target=self._read_input,
            name="hermes-cli-input",
            daemon=True,
        )

    def start_input(self) -> None:
        """启动持续读取输入的 CLI 专用线程。"""
        self._input_thread.start()

    def allow_next_input(self) -> None:
        """允许输入线程在当前文本已路由后显示下一次提示。"""
        if not self._stop_input.is_set():
            self._allow_next_input.set()

    def stop_input(self) -> None:
        """阻止输入线程在当前事件完成后继续请求新输入。"""
        self._stop_input.set()
        self._allow_next_input.set()

    def cancel_current_input(self) -> None:
        """请求输入组件清除当前正在编辑的文本，不直接访问其内部 Buffer。"""
        self._cli_input.cancel_current_input()

    def begin_stream_request(self) -> None:
        """为下一次模型请求重置流式正文显示状态。"""
        self._renderer.begin_request()

    def handle_stream_event(self, event: object) -> None:
        """在 controller 所在线程显示已转交的模型流事件。"""
        self._renderer.handle_event(event)

    def show_startup(
        self,
        *,
        profile: object,
        model: object,
        base_url: object,
        prompt_length: int,
    ) -> None:
        """显示保留的 CLI 启动信息。"""
        print(f"Profile (HERMES_HOME): {profile}")
        print(f"Model: {model} | Base URL: {base_url}")
        print(f"System prompt: {prompt_length} chars")
        print(
            "Type '/quit' to exit. Use /sessions, /resume <number>, or /new. "
            "Use /approve [once|session] or /deny when prompted.\n"
        )

    def show_message(self, message: str) -> None:
        """显示一条非流式 CLI 提示。"""
        print(f"\nAssistant: {message}\n")

    def show_worker_result(self, worker_result: "CLIWorkerResult") -> None:
        """显示 controller 已接收的完整 worker 结果。"""
        if worker_result.error is not None:
            self.show_message(worker_result.error)
            return
        if worker_result.kind == "list_sessions":
            self._show_cli_sessions(
                worker_result.sessions,
                worker_result.current_session_id,
            )
            return
        if worker_result.kind == "resume":
            self.show_message(f"resumed session {worker_result.session_id}")
            self._show_resumed_context(worker_result.messages)
            return
        if worker_result.kind == "deny":
            self.show_message("approval denied")
            return

        result = worker_result.conversation_result
        if not isinstance(result, dict):
            self.show_message("worker returned an invalid result")
            return
        if result.get("status") == "awaiting_approval":
            self._show_approval_prompt(result)
            return
        if result.get("status") == "cancelled":
            self._renderer.discard_current_response()
        final_response = str(result.get("final_response", ""))
        if not self._renderer.was_final_response_streamed(final_response):
            self.show_message(final_response)

    def _read_input(self) -> None:
        while not self._stop_input.is_set():
            raw_user_input = self._cli_input.prompt()
            if self._stop_input.is_set():
                return
            if not raw_user_input:
                self._post_shutdown()
                return
            self._post_user_input(raw_user_input)
            self._allow_next_input.wait()
            self._allow_next_input.clear()

    def _show_approval_prompt(self, result: dict) -> None:
        self._renderer.ensure_line_break()
        request = result.get("approval_request")
        if not isinstance(request, dict):
            self.show_message("approval request is invalid")
            return
        scopes = request.get("details", {}).get("allowed_grant_scopes", [])
        summary = str(request.get("summary", "需要批准的工具操作"))
        print(
            "\nAssistant: "
            f"{summary}\nApprove with /approve"
            f" (available scopes: {', '.join(scopes)}) or /deny\n"
        )

    @staticmethod
    def _format_cli_session_time(timestamp: object) -> str:
        try:
            return datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, OSError, OverflowError):
            return "unknown time"

    @staticmethod
    def _format_cli_preview(content: object, limit: int = 120) -> str:
        preview = " ".join(str(content or "").split())
        if len(preview) > limit:
            return f"{preview[:limit - 3]}..."
        return preview

    def _show_cli_sessions(
        self,
        sessions: tuple[dict, ...],
        current_session_id: str | None,
    ) -> None:
        if not sessions:
            self.show_message("no saved CLI sessions")
            return

        print("\nSessions:")
        for index, session in enumerate(sessions, start=1):
            session_id = str(session["session_id"])
            current_marker = " [current]" if session_id == current_session_id else ""
            print(
                f"  {index}. {self._format_cli_preview(session['preview'])} "
                f"({self._format_cli_session_time(session['timestamp'])}){current_marker}"
            )
        print("Use /resume <number> to restore a session.\n")

    @staticmethod
    def _format_replayed_content(content: object) -> str:
        text = str(content or "")
        if len(text) <= CLI_REPLAY_MESSAGE_LIMIT:
            return text
        return f"{text[:CLI_REPLAY_MESSAGE_LIMIT]}\n[message truncated]"

    @staticmethod
    def _tool_call_names(tool_calls: object) -> list[str]:
        if not isinstance(tool_calls, list):
            return []
        names: list[str] = []
        for tool_call in tool_calls:
            if isinstance(tool_call, dict):
                function = tool_call.get("function")
                name = function.get("name") if isinstance(function, dict) else None
            else:
                function = getattr(tool_call, "function", None)
                name = getattr(function, "name", None)
            if name:
                names.append(str(name))
        return names

    def _show_resumed_context(self, messages: tuple[dict, ...]) -> None:
        if not messages:
            print("Restored conversation: no messages\n")
            return
        print("Restored conversation:")
        for message in messages:
            role = message.get("role")
            content = self._format_replayed_content(message.get("content"))
            if role == "user":
                print(f"You: {content}")
                continue
            if role == "assistant":
                if content:
                    print(f"Assistant: {content}")
                tool_names = self._tool_call_names(message.get("tool_calls"))
                if tool_names:
                    print(f"Assistant: [tool: {', '.join(tool_names)}]")
                elif not content and message.get("tool_calls"):
                    print("Assistant: [tool call]")
                elif not content:
                    print("Assistant: [empty message]")
                continue
            if role == "tool":
                print("Tool: [result omitted]")
        print()


def patched_cli_stdout():
    """为整个交互式 CLI 会话协调普通输出与正在编辑的输入行。"""
    if _is_interactive_terminal():
        return patch_stdout()
    return nullcontext()
