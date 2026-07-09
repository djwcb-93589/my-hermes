"""
AgentLoop:parent agent 与 sub agent 共用的循环骨架(模板方法模式)。

``run()`` 是公共骨架:iteration loop → model call → assistant parse →
tool_call dispatch → messages append → stop condition。所有"主会话
专有"行为(DB 持久化、压缩、fallback、continuation 等)通过覆盖下方
hooks 注入,AgentLoop 本身不依赖 conn / session_id / add_messages。

默认实现是一份无副作用的最小循环,delegate 子 agent 直接使用;
主会话通过 ``ConversationAgentLoop``(定义在 conversation.py)覆盖
hooks 注入压缩 / fallback / DB 持久化等行为。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

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
# 共享 helper(也可独立使用)
# ---------------------------------------------------------------------------

def build_assistant_msg_dict(assistant_msg) -> dict:
    """把 SDK 的 assistant message 对象转成可序列化 dict。"""
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
# AgentLoop —— 模板方法基类
# ---------------------------------------------------------------------------

class AgentLoop:
    """公共循环骨架。

    子类通过覆盖下列 hook 注入主会话行为:
      - ``init_messages``               构造初始 messages(默认单条 user)
      - ``pre_model_call``              模型调用前(主会话用来做 compression)
      - ``call_model``                  实际 API 调用
      - ``handle_model_error``          模型异常处理,返回 "retry"/"abort"/"raise"
      - ``on_assistant_message``        assistant msg 追加后(主会话用来 add_messages)
      - ``should_continue``             是否触发 continuation
      - ``continuation_message``        续写 prompt
      - ``on_continuation_message``     continuation 追加后(主会话 add_messages)
      - ``on_tool_dispatch_start``      即将处理 tool_calls(主会话重置 continuation_count)
      - ``dispatch_one``                处理单个 tool_call(主会话保留 raise 行为)
      - ``on_tool_message``             tool msg 追加后(主会话 add_messages)
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
        model_kwargs: dict | None = None,
        cancel_checker: "Callable[[], bool] | None" = None,
    ):
        self.model = model
        self.max_iterations = max_iterations
        self.tools = tools
        self.system_prompt = system_prompt
        self.registry = registry
        self.client = client
        self.session_key = session_key
        self.blocked_tools = set(blocked_tools) if blocked_tools else set()
        # provider-specific 额外参数(如 extra_body / temperature 等)。
        # AgentLoop 只透传,不理解内容;由 ConversationAgentLoop /
        # DelegateAgentLoop 的调用方决定。
        self.model_kwargs = dict(model_kwargs) if model_kwargs else {}
        # 协作式取消检查器:返回 True 表示外部已请求取消,循环应尽快退出。
        # 默认 None = 不检查。后台 delegate 用它实现 cancel。
        self.cancel_checker = cancel_checker
        # 运行期状态(每次 run() 重置)
        self.iterations = 0
        self.tools_used: list[str] = []

    # --- 取消检查(后台 delegate 用) ---

    def _is_cancelled(self) -> bool:
        return self.cancel_checker is not None and bool(self.cancel_checker())

    def _cancel_result(self, messages: list[dict]) -> "AgentLoopResult":
        return self._result(
            ok=False, status="cancelled",
            summary=self.last_assistant_text(messages),
            messages=messages, error="cancel requested",
        )

    # ===================== 模板方法 =====================

    def run(self, user_message: str) -> AgentLoopResult:
        """跑一次完整循环。从单条 user_message 开始。"""
        messages = self.init_messages(user_message)
        self.iterations = 0
        self.tools_used = []

        for iteration in range(self.max_iterations):
            # 1) iteration 开始前检查取消
            if self._is_cancelled():
                return self._cancel_result(messages)

            self.iterations = iteration + 1
            messages = self.pre_model_call(messages)

            # 2) 模型调用前检查取消
            if self._is_cancelled():
                return self._cancel_result(messages)

            # 模型调用 —— 走 handle_model_error 决定后续动作
            try:
                response = self.call_model(messages)
            except Exception as exc:
                decision = self.handle_model_error(exc, messages)
                if decision == "retry":
                    continue
                if decision == "abort":
                    return self._result(
                        ok=False, status="model_error",
                        summary=self.last_assistant_text(messages),
                        messages=messages, error=repr(exc),
                    )
                # "raise" 或任何未知返回值都重新抛
                raise

            assistant_msg = response.choices[0].message
            finish_reason = response.choices[0].finish_reason

            msg_dict = build_assistant_msg_dict(assistant_msg)
            messages.append(msg_dict)
            self.on_assistant_message(msg_dict, response)

            # continuation hook(主会话:finish_reason == "length")
            if self.should_continue(finish_reason, messages):
                cont_msg = self.continuation_message()
                messages.append(cont_msg)
                self.on_continuation_message(cont_msg)
                continue

            # 模型不再调工具 → 任务完成
            if not assistant_msg.tool_calls:
                return self._result(
                    ok=True, status="completed",
                    summary=assistant_msg.content or "",
                    messages=messages,
                )

            # 处理本轮 tool_calls
            self.on_tool_dispatch_start()
            # 3) tool 调用前检查取消
            if self._is_cancelled():
                return self._cancel_result(messages)
            tool_error = self.process_tool_calls(assistant_msg.tool_calls, messages)
            if tool_error is not None:
                return tool_error

        # 跑满 max_iterations 仍未完成
        return self._result(
            ok=False, status="max_iterations",
            summary=self.last_assistant_text(messages),
            messages=messages,
        )

    def process_tool_calls(self, tool_calls, messages) -> AgentLoopResult | None:
        """处理本轮所有 tool_calls。返回 AgentLoopResult 表示终止,None 表示继续。

        默认实现:用 dispatch_one 处理每个 tool_call,任何 error_status 都终止。
        主会话覆盖 dispatch_one 改成 raise 行为后,这里仍正确(None)。
        """
        for tc in tool_calls:
            output, err_status, err_detail = self.dispatch_one(tc)
            if err_status is not None:
                return self._result(
                    ok=False, status="tool_error",
                    summary=self.last_assistant_text(messages),
                    messages=messages, error=err_detail,
                )
            tc_name = tc.function.name
            if tc_name not in self.tools_used:
                self.tools_used.append(tc_name)
            tool_msg = {
                "role": "tool",
                "tool_call_id": tc.id,
                "content": output,
            }
            messages.append(tool_msg)
            self.on_tool_message(tc, tool_msg, output)
        return None

    # ===================== 可覆盖 hooks =====================

    def init_messages(self, user_message: str) -> list[dict]:
        """构造初始 messages。默认单条 user message。"""
        return [{"role": "user", "content": user_message}]

    def pre_model_call(self, messages: list[dict]) -> list[dict]:
        """模型调用前的 hook。返回(可能修改后的)messages。默认无操作。"""
        return messages

    def call_model(self, messages: list[dict]):
        """实际 API 调用。``model_kwargs`` 原样透传给 provider SDK。"""
        api_messages = (
            [{"role": "system", "content": self.system_prompt}] + messages
        )
        return self.client.chat.completions.create(
            model=self.model,
            messages=api_messages,
            tools=self.tools if self.tools else None,
            **self.model_kwargs,
        )

    def handle_model_error(self, exc, messages) -> str:
        """模型调用异常时调用。返回:
          - "retry": 跳过本轮 tool_calls,进下一轮 iteration
          - "abort": 作为 model_error 返回
          - "raise": 重新抛异常(默认)
        """
        return "raise"

    def on_assistant_message(self, msg_dict: dict, response) -> None:
        """assistant msg 追加后(主会话用来 add_messages)。默认空。"""
        pass

    def should_continue(self, finish_reason: str, messages: list[dict]) -> bool:
        """是否触发 continuation(默认不触发)。"""
        return False

    def continuation_message(self) -> dict:
        """continuation 时塞回的 prompt。"""
        return {"role": "user", "content": "Please continue from where you left off."}

    def on_continuation_message(self, cont_msg: dict) -> None:
        """continuation msg 追加后(主会话 add_messages)。默认空。"""
        pass

    def on_tool_dispatch_start(self) -> None:
        """即将进入 tool_call 处理。主会话用来重置 continuation_count。默认空。"""
        pass

    def dispatch_one(self, tool_call) -> tuple[str, str | None, str | None]:
        """处理单个 tool_call。默认走 dispatch_tool_call helper。

        主会话(ConversationAgentLoop)覆盖此方法以保留"json/dispatch
        错误直接 raise"的原行为。
        """
        return dispatch_tool_call(
            tool_call, self.registry,
            session_key=self.session_key,
            blocked_tools=self.blocked_tools,
        )

    def on_tool_message(self, tool_call, tool_msg: dict, output: str) -> None:
        """tool msg 追加后(主会话 add_messages)。默认空。"""
        pass

    # ===================== 辅助 =====================

    @staticmethod
    def last_assistant_text(messages: list[dict]) -> str:
        """取最后一段 assistant 文本(用于异常 / max_iter 路径的 summary)。"""
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("content"):
                return m["content"]
        return ""

    def _result(
        self,
        *,
        ok: bool,
        status: str,
        summary: str,
        messages: list[dict],
        error: str | None = None,
    ) -> AgentLoopResult:
        """统一构造结果对象。"""
        return AgentLoopResult(
            ok=ok, status=status, summary=summary,
            messages=messages, iterations=self.iterations,
            tools_used=list(self.tools_used),
            error=error,
        )
