"""复用 AgentLoop 的通用 Review 执行循环。"""

from __future__ import annotations

import copy
import json

from hermes.agent_loop import AgentLoop, _short_error


class ReviewAgentLoop(AgentLoop):
    """在受限工具边界内执行一组 Review 输入消息。"""

    def __init__(
        self,
        *,
        review_messages: list[dict],
        review_instruction: str,
        allowed_tool_names: frozenset[str],
        **kwargs,
    ):
        if not isinstance(review_messages, list):
            raise ValueError("review_messages must be a list")
        if not isinstance(review_instruction, str) or not review_instruction.strip():
            raise ValueError("review_instruction must be a non-empty string")
        if isinstance(allowed_tool_names, str):
            raise ValueError("allowed_tool_names must not be a string")
        try:
            normalized_tool_names = frozenset(allowed_tool_names)
        except TypeError as exc:
            raise ValueError("allowed_tool_names must be iterable") from exc
        if any(
            not isinstance(tool_name, str) or not tool_name.strip()
            for tool_name in normalized_tool_names
        ):
            raise ValueError(
                "allowed_tool_names must contain non-empty strings"
            )

        super().__init__(**kwargs)
        self._review_messages = copy.deepcopy(review_messages)
        self.review_instruction = review_instruction
        self.allowed_tool_names = normalized_tool_names

    def init_messages(self, user_message: str) -> list[dict]:
        messages = copy.deepcopy(self._review_messages)
        messages.append({"role": "user", "content": self.review_instruction})
        return messages

    def handle_model_error(self, exc, messages) -> str:
        """Review 不继承主会话的 fallback 或重试策略。"""
        return "abort"

    def dispatch_one(self, tool_call):
        """拒绝不在本次动态解析能力边界内的工具调用。"""
        if self._is_cancelled():
            tool_name = self._tool_call_name(tool_call)
            return (
                f"(error: tool '{tool_name}' cancelled because review claim expired)",
                "cancelled",
                "review claim expired before tool dispatch",
            )
        tool_name = self._tool_call_name(tool_call)
        if tool_name not in self.allowed_tool_names:
            return (
                f"(error: tool '{tool_name}' is disabled in this review)",
                "disabled",
                f"disabled tool invoked in review: {tool_name!r}",
            )
        return super().dispatch_one(tool_call)

    def _classify_tool_error(
        self,
        output: str,
        err_status: str | None,
    ) -> tuple[bool, str]:
        """把 Review 工具的明确错误提升为本次审视失败。"""
        fatal, error_type = super()._classify_tool_error(output, err_status)
        if fatal:
            return fatal, error_type
        if err_status:
            return True, error_type or err_status

        payload = None
        if isinstance(output, str):
            try:
                payload = json.loads(output)
            except (TypeError, ValueError):
                pass
        if isinstance(payload, dict):
            payload_error_type = payload.get("error_type")
            if (
                payload.get("ok") is False
                or "error" in payload
                or bool(payload_error_type)
            ):
                return True, str(payload_error_type or "tool_error")
        if isinstance(output, str) and output.lstrip().lower().startswith("(error:"):
            return True, "tool_error"
        return False, error_type

    def process_tool_calls(
        self,
        tool_calls,
        messages,
    ):
        """按顺序处理工具；首个明确错误后仅补齐协议消息。"""
        tool_messages: list[dict] = []
        fatal_detail: str | None = None
        fatal_error_type: str | None = None
        skip_remaining = False

        for tool_call in tool_calls:
            tool_name = self._tool_call_name(tool_call)
            if skip_remaining:
                output = "(error: skipped because an earlier review tool failed)"
            else:
                try:
                    output, err_status, err_detail = self.dispatch_one(tool_call)
                except Exception as exc:
                    short = _short_error(exc)
                    output = f"(error: tool {tool_name} failed: {short})"
                    err_status = "dispatch"
                    err_detail = (
                        f"tool {tool_name!r} dispatch raised: {short}"
                    )

            tool_msg = {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            }
            messages.append(tool_msg)
            tool_messages.append(tool_msg)
            self.on_tool_message(tool_call, tool_msg, output)

            if skip_remaining:
                continue
            if tool_name not in self.tools_used:
                self.tools_used.append(tool_name)

            fatal, error_type = self._classify_tool_error(output, err_status)
            if fatal:
                fatal_detail = (
                    err_detail
                    or f"fatal tool error ({error_type}) in {tool_name!r}"
                )
                fatal_error_type = error_type or "tool_error"
                skip_remaining = True
                continue

            if not error_type and not err_status:
                self._clear_tool_error_counts(tool_name)

        if fatal_detail is not None:
            return tool_messages, self._result(
                ok=False,
                status="tool_error",
                summary=self.last_assistant_text(messages),
                messages=messages,
                error=fatal_detail,
                error_type=fatal_error_type or "tool_error",
                fatal=True,
                retryable=False,
            )
        return tool_messages, None
