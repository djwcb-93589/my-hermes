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
    status: str  # completed | max_iterations | tool_error | model_error | error | cancelled
    summary: str
    messages: list[dict]
    iterations: int
    tools_used: list[str] = field(default_factory=list)
    error: str | None = None
    # 错误分类字段(只在 ok=False 时有意义):
    #   error_type: 具体类型(model_error / persistence_error / tool_error /
    #               internal_error / cancelled / 具体工具 error_type)
    #   fatal: True 表示调用方不应盲目重试整个 agent
    #   retryable: True 表示瞬时可重试(模型临时不可用等)
    error_type: str | None = None
    fatal: bool = False
    retryable: bool = True


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
      - ``on_assistant_message``        普通 assistant msg 追加后
      - ``should_continue``             是否触发 continuation
      - ``continuation_message``        续写 prompt
      - ``on_continuation_message``     continuation 追加后(主会话 add_messages)
      - ``on_tool_dispatch_start``      即将处理 tool_calls(主会话重置 continuation_count)
      - ``dispatch_one``                处理单个 tool_call
      - ``on_tool_message``             单条 tool msg 追加后
      - ``on_tool_messages_batch``      assistant tool_call + tool results 完成后
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
            error_type="cancelled", fatal=True, retryable=False,
        )

    # --- 边界错误结果 helper(让 run() 调用点统一) ---

    def _model_error_result(self, messages, error):
        """模型最终失败:不可继续 loop,但调用方可能下次能重试整个 agent。"""
        return self._result(
            ok=False, status="model_error",
            summary=self.last_assistant_text(messages),
            messages=messages, error=error,
            error_type="model_error", fatal=True, retryable=True,
        )

    def _persistence_error_result(self, messages, error):
        """DB 持久化失败:数据完整性问题,不重试。"""
        return self._result(
            ok=False, status="error",
            summary=self.last_assistant_text(messages),
            messages=messages, error=error,
            error_type="persistence_error", fatal=True, retryable=False,
        )

    def _internal_error_result(self, messages, error):
        """未预期异常:兜底,避免原始异常冒到最外层。"""
        return self._result(
            ok=False, status="error",
            summary=self.last_assistant_text(messages),
            messages=messages, error=error,
            error_type="internal_error", fatal=True, retryable=False,
        )

    # --- 工具错误致命判定 ---

    # 致命工具错误集合:模型即使看到错误也无法修正,继续 loop 只会无限循环。
    # 安全 / 权限 / 路径逃逸 / DB / 取消 都属于这一类。
    _FATAL_TOOL_ERROR_TYPES = frozenset({
        "forbidden",
        "permission_denied",
        "path_escape",
        "safety_blocked",
        "cancelled",
        "persistence_error",
        "internal_error",
    })

    def _classify_tool_error(
        self,
        output: str,
        err_status: str | None,
    ) -> tuple[bool, str]:
        """判断工具错误是否致命。返回 (fatal, error_type)。

        优先看 err_status(blocked 一定致命);
        其次尝试解析 output JSON 里的 error_type 字段;
        显式标记 fatal=true 的也认致命;
        其它默认非致命,让模型有机会修正参数或换做法。

        为什么允许非致命错误继续 loop:模型可能传错参数 / 调不存在文件,
        看到错误后能调整。直接终止会让简单工具错误升级成整个 agent 失败。
        """
        if err_status == "blocked":
            return True, "blocked"

        if err_status in ("json", "dispatch"):
            # 调用层错误(参数 JSON 非法 / 工具抛异常):非致命,让模型修正
            return False, err_status

        if isinstance(output, str):
            try:
                obj = json.loads(output)
            except (ValueError, TypeError):
                obj = None
            if isinstance(obj, dict):
                err_type = obj.get("error_type")
                if obj.get("fatal") is True:
                    return True, err_type or "fatal_flagged"
                if err_type in self._FATAL_TOOL_ERROR_TYPES:
                    return True, err_type
                if err_type:
                    return False, err_type

        return False, ""

    # ===================== 模板方法 =====================

    def run(self, user_message: str) -> AgentLoopResult:
        """跑一次完整循环。从单条 user_message 开始。

        顶层 try/except 兜底:任何未预期异常都包装成 internal_error,
        不让原始异常(openai client / sqlite3 / json)冒到最外层。
        """
        try:
            return self._run_inner(user_message)
        except Exception as exc:
            # 内部 _run_inner 已经处理了 model / persistence / tool 等已知
            # 异常,真到这里说明是未预期 bug,统一标 internal_error
            return self._internal_error_result(
                messages=[], error=f"unhandled exception: {exc!r}",
            )

    def _run_inner(self, user_message: str) -> AgentLoopResult:
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
                    # 模型最终失败:返回结构化 model_error,不抛异常
                    return self._model_error_result(messages, repr(exc))
                # "raise" 或任何未知返回值都重新抛,但被顶层兜底 catch
                raise

            assistant_msg = response.choices[0].message
            finish_reason = response.choices[0].finish_reason

            msg_dict = build_assistant_msg_dict(assistant_msg)
            messages.append(msg_dict)

            if assistant_msg.tool_calls:
                # assistant tool_call 必须等对应 tool result 生成后一起持久化,
                # 避免数据库里出现只有 tool_call 没有 tool result 的半截历史。
                self.on_tool_dispatch_start()
                # 3) tool 调用前检查取消
                if self._is_cancelled():
                    return self._cancel_result(messages)
                try:
                    tool_messages, tool_error = self.process_tool_calls(
                        assistant_msg.tool_calls, messages
                    )
                except Exception as exc:
                    # 工具分发过程中的持久化 / 结构异常
                    return self._persistence_error_result(messages, repr(exc))
                try:
                    self.on_tool_messages_batch(msg_dict, tool_messages, response)
                except Exception as exc:
                    # DB 写入失败:assistant + tool_messages 整组未落盘,停止 loop
                    return self._persistence_error_result(messages, repr(exc))
                if tool_error is not None:
                    return tool_error
                continue

            # continuation hook(主会话:finish_reason == "length")
            if self.should_continue(finish_reason, messages):
                try:
                    self.on_assistant_message(msg_dict, response)
                except Exception as exc:
                    return self._persistence_error_result(messages, repr(exc))
                cont_msg = self.continuation_message()
                messages.append(cont_msg)
                try:
                    self.on_continuation_message(cont_msg)
                except Exception as exc:
                    return self._persistence_error_result(messages, repr(exc))
                continue

            # 模型不再调工具 → 任务完成
            try:
                self.on_assistant_message(msg_dict, response)
            except Exception as exc:
                return self._persistence_error_result(messages, repr(exc))
            return self._result(
                ok=True, status="completed",
                summary=assistant_msg.content or "",
                messages=messages,
            )

        # 跑满 max_iterations 仍未完成
        return self._result(
            ok=False, status="max_iterations",
            summary=self.last_assistant_text(messages),
            messages=messages,
        )

    def process_tool_calls(
        self,
        tool_calls,
        messages,
    ) -> tuple[list[dict], AgentLoopResult | None]:
        """处理本轮所有 tool_calls,返回生成的 tool messages 和可选错误结果。

        错误分类策略:
          - 非致命错误(参数非法 / 工具异常 / file_not_found / ambiguous 等):
            包装成合法 tool message 追加到上下文,让模型有机会在下一轮
            修正参数或换做法。loop 继续。
          - 致命错误(forbidden / safety_blocked / permission_denied /
            path_escape / cancelled / persistence_error):终止 loop,
            返回结构化 tool_error。
        """
        tool_messages: list[dict] = []
        fatal_detail: str | None = None
        for tc in tool_calls:
            try:
                output, err_status, err_detail = self.dispatch_one(tc)
            except Exception as exc:
                tool_name = self._tool_call_name(tc)
                output = f"(error: tool {tool_name} raised: {exc})"
                err_status = "dispatch"
                err_detail = f"tool {tool_name!r} raised: {exc}"

            tc_name = self._tool_call_name(tc)
            if tc_name not in self.tools_used:
                self.tools_used.append(tc_name)
            tool_msg = {
                "role": "tool",
                "tool_call_id": tc.id,
                "content": output,
            }
            messages.append(tool_msg)
            tool_messages.append(tool_msg)
            self.on_tool_message(tc, tool_msg, output)

            # 只记第一个致命错误,继续把后续 tool_call 的结果也生成
            # (整批 tool_messages 都要持久化,避免残缺历史)
            if fatal_detail is None:
                fatal, err_type = self._classify_tool_error(output, err_status)
                if fatal:
                    fatal_detail = (
                        err_detail
                        or f"fatal tool error ({err_type}) in {tc_name!r}"
                    )

        if fatal_detail is not None:
            return tool_messages, self._result(
                ok=False, status="tool_error",
                summary=self.last_assistant_text(messages),
                messages=messages, error=fatal_detail,
                error_type="tool_error", fatal=True, retryable=False,
            )
        return tool_messages, None

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
        """普通 assistant msg 追加后调用。默认空。"""
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

        返回值里的 error_status 表示工具执行失败,但调用方仍会生成
        合法 tool message,再由 batch hook 原子持久化。
        """
        return dispatch_tool_call(
            tool_call, self.registry,
            session_key=self.session_key,
            blocked_tools=self.blocked_tools,
        )

    def on_tool_message(self, tool_call, tool_msg: dict, output: str) -> None:
        """单条 tool msg 追加后调用。默认空。"""
        pass

    def on_tool_messages_batch(
        self,
        assistant_msg: dict,
        tool_messages: list[dict],
        response,
    ) -> None:
        """assistant tool_call 与对应 tool results 全部生成后调用。默认空。"""
        pass

    # ===================== 辅助 =====================

    @staticmethod
    def _tool_call_name(tool_call) -> str:
        function = getattr(tool_call, "function", None)
        return getattr(function, "name", "<unknown>")

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
        error_type: str | None = None,
        fatal: bool = False,
        retryable: bool = True,
    ) -> AgentLoopResult:
        """统一构造结果对象。"""
        return AgentLoopResult(
            ok=ok, status=status, summary=summary,
            messages=messages, iterations=self.iterations,
            tools_used=list(self.tools_used),
            error=error, error_type=error_type,
            fatal=fatal, retryable=retryable,
        )
