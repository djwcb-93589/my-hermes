"""
AgentLoop:parent agent 与 sub agent 共用的最小循环抽象。

只负责"模型调用 → 解析 assistant → 处理 tool_call → 追加 message →
iterations 计数 → max_iterations 处理"。DB 持久化、token 压缩、
fallback、continuation 等高层行为由调用方包裹,AgentLoop 不绑定。

辅助函数 ``build_assistant_msg_dict`` / ``dispatch_tool_call`` 是更细
粒度的复用单元——conversation.py 主循环只用了前者,delegate 子 agent
直接用 AgentLoop 类。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from hermes.config import client as _default_client


@dataclass
class AgentLoopResult:
    """AgentLoop.run 的返回。"""
    ok: bool
    status: str  # "completed" | "max_iterations" | "tool_error" | "model_error"
    summary: str
    messages: list[dict]
    iterations: int
    tools_used: list[str] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# 共享 helper
# ---------------------------------------------------------------------------

def build_assistant_msg_dict(assistant_msg) -> dict:
    """把 SDK 的 assistant message 对象转成可序列化 dict。

    parent 和 sub 都用同一个组装逻辑,避免两边 drift。
    """
    msg_dict: dict = {
        "role": "assistant",
        "content": assistant_msg.content or "",
    }
    if assistant_msg.tool_calls:
        msg_dict["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in assistant_msg.tool_calls
        ]
    return msg_dict


def dispatch_tool_call(
    tool_call,
    registry,
    *,
    session_key: str | None = None,
    blocked_tools: set[str] | None = None,
) -> tuple[str, str | None, str | None]:
    """处理单个 tool_call。

    返回 ``(tool_message_content, error_status, error_detail)``:
      - 成功: ``(output, None, None)``
      - blocked 工具: ``("(error: ...)", "blocked", "blocked tool invoked: <name>")``
      - JSON 参数解析失败: ``("(error: ...)", "json", "invalid JSON in <name>: <exc>")``
      - dispatch 抛异常: ``("(error: ...)", "dispatch", "tool <name> raised: <exc>")``

    tool_message_content 用于塞回 messages 让模型看到;error_status / error_detail
    让调用方决定是否终止循环。
    """
    tool_name = tool_call.function.name

    if blocked_tools and tool_name in blocked_tools:
        return (
            f"(error: '{tool_name}' is blocked)",
            "blocked",
            f"blocked tool invoked: {tool_name!r}",
        )

    try:
        tool_args = json.loads(tool_call.function.arguments)
    except Exception as exc:
        return (
            f"(error: invalid JSON arguments in {tool_name}: {exc})",
            "json",
            f"invalid JSON in tool_call {tool_name!r}: {exc}",
        )

    try:
        output = registry.dispatch(tool_name, tool_args, session_key=session_key)
    except Exception as exc:
        return (
            f"(error: tool {tool_name} raised: {exc})",
            "dispatch",
            f"tool {tool_name!r} raised: {exc}",
        )

    return output, None, None


# ---------------------------------------------------------------------------
# AgentLoop
# ---------------------------------------------------------------------------

class AgentLoop:
    """最小公共 agent 循环。

    负责模型调用、assistant 解析、tool_call 处理、message 追加、
    iterations 计数、max_iterations 处理。不负责 DB 持久化、压缩、
    fallback、continuation —— 这些由调用方包裹。

    parent agent / sub agent 都能复用;区别仅在 tools / blocked_tools /
    session_key / system_prompt 的传参。
    """

    def __init__(
        self,
        *,
        model: str,
        max_iterations: int,
        tools: list[dict],
        system_prompt: str,
        registry,
        client=_default_client,
        session_key: str | None = None,
        blocked_tools: set[str] | None = None,
    ):
        self.model = model
        self.max_iterations = max_iterations
        self.tools = tools
        self.system_prompt = system_prompt
        self.registry = registry
        self.client = client
        self.session_key = session_key
        self.blocked_tools = set(blocked_tools) if blocked_tools else set()
        self.tools_used: list[str] = []

    @staticmethod
    def _last_assistant_text(messages: list[dict]) -> str:
        """取最后一段 assistant 文本(用于异常 / max_iter 路径的 summary)。"""
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("content"):
                return m["content"]
        return ""

    def run(self, user_message: str) -> AgentLoopResult:
        """跑一次完整循环,从单条 user_message 开始。"""
        messages: list[dict] = [{"role": "user", "content": user_message}]
        iterations = 0

        for iteration in range(self.max_iterations):
            iterations = iteration + 1
            api_messages = (
                [{"role": "system", "content": self.system_prompt}] + messages
            )

            # 模型调用 —— 异常直接当作 model_error 返回
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=api_messages,
                    tools=self.tools if self.tools else None,
                )
            except Exception as exc:
                return AgentLoopResult(
                    ok=False,
                    status="model_error",
                    summary=self._last_assistant_text(messages),
                    messages=messages,
                    iterations=iterations,
                    tools_used=self.tools_used,
                    error=repr(exc),
                )

            assistant_msg = response.choices[0].message
            messages.append(build_assistant_msg_dict(assistant_msg))

            # 模型不再调工具 → 任务完成
            if not assistant_msg.tool_calls:
                return AgentLoopResult(
                    ok=True,
                    status="completed",
                    summary=assistant_msg.content or "",
                    messages=messages,
                    iterations=iterations,
                    tools_used=self.tools_used,
                )

            # 处理本轮 tool_calls
            for tc in assistant_msg.tool_calls:
                output, err_status, err_detail = dispatch_tool_call(
                    tc, self.registry,
                    session_key=self.session_key,
                    blocked_tools=self.blocked_tools,
                )
                if err_status is not None:
                    return AgentLoopResult(
                        ok=False,
                        status="tool_error",
                        summary=self._last_assistant_text(messages),
                        messages=messages,
                        iterations=iterations,
                        tools_used=self.tools_used,
                        error=err_detail,
                    )
                tc_name = tc.function.name
                if tc_name not in self.tools_used:
                    self.tools_used.append(tc_name)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": output,
                })

        # 跑满 max_iterations 仍未完成
        return AgentLoopResult(
            ok=False,
            status="max_iterations",
            summary=self._last_assistant_text(messages),
            messages=messages,
            iterations=iterations,
            tools_used=self.tools_used,
        )
