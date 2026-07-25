"""默认 CLI 的模型流事件渲染。"""

from __future__ import annotations

from dataclasses import dataclass, field

from hermes.model_streaming import StreamEvent


@dataclass
class _AttemptDisplay:
    """一个模型尝试在终端上的正文状态。"""

    content_parts: list[str] = field(default_factory=list)
    displayed: bool = False
    line_terminated: bool = False

    @property
    def content(self) -> str:
        return "".join(self.content_parts)


class CLIStreamRenderer:
    """把同步模型流事件显示为默认 CLI 的普通文本输出。"""

    def __init__(self) -> None:
        self._attempts: dict[str, _AttemptDisplay] = {}
        self._current_attempt_id: str | None = None
        self._last_completed_attempt_id: str | None = None
        self._last_completed_content = ""

    def begin_request(self) -> None:
        """开始一次 run_conversation 调用前重置本次请求状态。"""
        self._attempts.clear()
        self._current_attempt_id = None
        self._last_completed_attempt_id = None
        self._last_completed_content = ""

    def handle_event(self, event: StreamEvent) -> None:
        """处理一个已由同步 AgentLoop 完整隔离的流事件。"""
        if event.event_type == "model_turn_started":
            self._attempts[event.attempt_id] = _AttemptDisplay()
            self._current_attempt_id = event.attempt_id
            return

        if event.event_type == "reasoning_delta":
            return

        if event.attempt_id != self._current_attempt_id:
            return
        attempt = self._attempts.get(event.attempt_id)
        if attempt is None:
            return

        if event.event_type == "text_delta":
            self._display_text_delta(attempt, event.delta or "")
        elif event.event_type == "model_turn_interrupted":
            self._display_interruption(attempt)
        elif event.event_type == "model_turn_completed":
            self._complete_attempt(event.attempt_id, attempt)

    def ensure_line_break(self) -> None:
        """让后续审批或普通日志从已显示正文的下一行开始。"""
        if self._current_attempt_id is None:
            return
        attempt = self._attempts.get(self._current_attempt_id)
        if attempt is not None and attempt.displayed and not attempt.line_terminated:
            print(flush=True)
            attempt.line_terminated = True

    def was_final_response_streamed(self, final_response: str) -> bool:
        """判断本次请求最后完成的模型回合是否已显示同一正文。"""
        return (
            self._last_completed_attempt_id is not None
            and self._last_completed_attempt_id == self._current_attempt_id
            and bool(self._last_completed_content)
            and self._last_completed_content == final_response
        )

    def discard_current_response(self) -> None:
        """标记当前已显示正文失效，避免把取消前的片段当作最终回答。"""
        attempt_id = self._current_attempt_id
        attempt = self._attempts.get(attempt_id) if attempt_id is not None else None
        if attempt is not None and attempt.displayed:
            self.ensure_line_break()
            print("[本次响应已停止，以上内容不会保存]", flush=True)
            attempt.line_terminated = True
        self._last_completed_attempt_id = None
        self._last_completed_content = ""

    @staticmethod
    def _display_text_delta(attempt: _AttemptDisplay, text: str) -> None:
        if not text:
            return
        if not attempt.displayed:
            print("\nAssistant: ", end="", flush=True)
            attempt.displayed = True
        print(text, end="", flush=True)
        attempt.content_parts.append(text)
        attempt.line_terminated = False

    def _display_interruption(self, attempt: _AttemptDisplay) -> None:
        if not attempt.displayed:
            return
        self.ensure_line_break()
        print("[本次响应已中断，以上未完成内容不会保存]", flush=True)
        attempt.line_terminated = True

    def _complete_attempt(self, attempt_id: str, attempt: _AttemptDisplay) -> None:
        self._last_completed_attempt_id = attempt_id
        self._last_completed_content = attempt.content
        self.ensure_line_break()
