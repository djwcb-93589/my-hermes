"""主会话 agent:ConversationAgentLoop + run_conversation 入口。

ConversationAgentLoop 继承 AgentLoop,覆盖 hooks 注入主会话专有行为:
  - compression(每轮模型调用前)
  - classify_error / fallback / jittered_backoff(模型异常时)
  - finish_reason == "length" 的 continuation
  - 普通 assistant / continuation message 的 add_messages 持久化
  - assistant tool_call + tool results 的 batch 原子持久化
  - tool_call 处理的 print 日志 + 错误 tool message 包装

run_conversation() 保持原有 module-level 签名,内部委托给
ConversationAgentLoop.run(),返回 dict 保持调用方兼容。
"""

from __future__ import annotations

import json
import sqlite3
import time

from hermes.agent_loop import AgentLoop, AgentLoopResult
from hermes.config import (
    client,
    MODEL,
    MAX_ITERATIONS,
    MAX_RETRIES,
    MAX_CONTINUATIONS,
    COMPRESSION_THRESHOLD,
    CONTINUE_MESSAGE,
)
from hermes.db import add_messages, get_session_messages
from hermes.errors import classify_error, jittered_backoff, switch_to_fallback
from hermes.tokens import estimate_tokens, compress
from hermes.tools import registry


ENABLED_TOOLSETS = ["terminal", "file", "memory", "skill", "delegate", "cron"]


class ConversationAgentLoop(AgentLoop):
    """主会话 agent 循环。

    在 AgentLoop 公共骨架基础上注入 compression / fallback / retry /
    continuation / DB 持久化等行为。
    """

    def __init__(
        self,
        *,
        model: str,
        max_iterations: int,
        tools: list[dict],
        system_prompt: str,
        registry,
        client,
        session_key: str,
        conn: sqlite3.Connection,
        db_session_id: str,
        existing_messages: list[dict],
        max_retries: int,
        max_continuations: int,
        compression_threshold: int,
        model_kwargs: dict | None = None,
    ):
        super().__init__(
            model=model,
            max_iterations=max_iterations,
            tools=tools,
            system_prompt=system_prompt,
            registry=registry,
            client=client,
            session_key=session_key,
            model_kwargs=model_kwargs,
        )
        # 主会话专有状态
        self.conn = conn
        self.db_session_id = db_session_id
        self.max_retries = max_retries
        self.max_continuations = max_continuations
        self.compression_threshold = compression_threshold
        self._existing_messages = existing_messages
        self._retry_count = 0
        self._continuation_count = 0

    # --- messages 初始化:主会话从 DB 加载历史 ---

    def init_messages(self, user_message: str) -> list[dict]:
        # 复用调用方已 add_messages 过的 user_msg;这里只负责把历史 + 当前
        # user msg 拼成 messages 列表给循环用。
        return list(self._existing_messages) + [{"role": "user", "content": user_message}]

    # --- 模型调用前:compression ---

    def pre_model_call(self, messages: list[dict]) -> list[dict]:
        if estimate_tokens(messages) >= self.compression_threshold:
            messages = compress(messages)
        return messages

    # --- 模型异常处理:classify → compress / fallback / retry / raise ---

    def handle_model_error(self, exc, messages) -> str:
        status = getattr(exc, "status_code", None)
        classified = classify_error(status, str(exc))
        print(f"  [error] {classified['reason']} (status={status})")

        if classified["should_compress"]:
            # 触发压缩后让循环重试本轮
            # ponytail: 直接 mutate messages 是为了让下一轮 pre_model_call
            # 拿到压缩后的版本(compress 返回新列表,这里覆盖原 list 内容)
            compressed = compress(messages)
            messages.clear()
            messages.extend(compressed)
            return "retry"

        if classified["should_fallback"]:
            fallback_client, fallback_model = switch_to_fallback()
            if fallback_client:
                self.client = fallback_client
                self.model = fallback_model
                return "retry"
            return "raise"

        if classified["retryable"] and self._retry_count < self.max_retries:
            self._retry_count += 1
            time.sleep(jittered_backoff(self._retry_count))
            return "retry"

        return "raise"

    # --- 普通 assistant msg 追加后:add_messages + 重置 retry_count ---

    def on_assistant_message(self, msg_dict: dict, response) -> None:
        add_messages(self.conn, self.db_session_id, [msg_dict])
        # 模型调用成功 → 重置 retry_count(对齐原行为)
        self._retry_count = 0

    # --- continuation ---

    def should_continue(self, finish_reason: str, messages: list[dict]) -> bool:
        if finish_reason == "length" and self._continuation_count < self.max_continuations:
            self._continuation_count += 1
            return True
        return False

    def continuation_message(self) -> dict:
        return {"role": "user", "content": CONTINUE_MESSAGE}

    def on_continuation_message(self, cont_msg: dict) -> None:
        add_messages(self.conn, self.db_session_id, [cont_msg])

    # --- tool_call 处理 ---

    def on_tool_dispatch_start(self) -> None:
        """进入 tool_call 路径时重置 continuation_count(对齐原行为)。"""
        self._continuation_count = 0

    def dispatch_one(self, tool_call) -> tuple[str, str | None, str | None]:
        """主会话保留 print 日志,并把工具异常包装成 tool message。

        工具失败不是 DB 事务失败;真正持久化失败交给 add_messages 抛出。
        """
        tool_name = tool_call.function.name
        try:
            tool_args = json.loads(tool_call.function.arguments)
        except Exception as exc:
            return (
                f"(error: invalid JSON arguments in {tool_name}: {exc})",
                "json",
                f"invalid JSON in tool_call {tool_name!r}: {exc}",
            )
        print(
            f"  [tool] {tool_name}: "
            f"{json.dumps(tool_args, ensure_ascii=False)[:120]}"
        )
        try:
            output = self.registry.dispatch(
                tool_name, tool_args,
                session_key=self.session_key,
            )
        except Exception as exc:
            return (
                f"(error: tool {tool_name} raised: {exc})",
                "dispatch",
                f"tool {tool_name!r} raised: {exc}",
            )
        return output, None, None

    # 单条 tool result 不单独持久化,避免 assistant tool_call 与 tool result
    # 被拆成多次提交。
    def on_tool_message(self, tool_call, tool_msg: dict, output: str) -> None:
        pass

    def on_tool_messages_batch(
        self,
        assistant_msg: dict,
        tool_messages: list[dict],
        response,
    ) -> None:
        # assistant tool_call 与对应 tool result 必须同事务写入,
        # 否则崩溃时会留下只有 tool_call 没有 tool result 的残缺历史。
        add_messages(self.conn, self.db_session_id, [assistant_msg, *tool_messages])
        self._retry_count = 0


# ---------------------------------------------------------------------------
# 对外入口(签名 / 返回格式与原版完全一致)
# ---------------------------------------------------------------------------

def run_conversation(
    user_message: str,
    conn: sqlite3.Connection,
    session_id: str,
    cached_prompt: str,
    session_key: str | None = None,
) -> dict:
    """主会话 agent 入口。委托给 ConversationAgentLoop。

    返回 ``{"final_response": str, "messages": list[dict]}``,
    与原版完全一致。
    """
    # 关键顺序:先读历史(不含当前 user_msg),再 add_messages 当前 user_msg。
    # 这样 ConversationAgentLoop.init_messages 拼 existing + [user_msg] 时,
    # user message 在 API 调用 和 DB 里都只出现一次。
    existing = get_session_messages(conn, session_id)
    user_msg = {"role": "user", "content": user_message}
    add_messages(conn, session_id, [user_msg])

    loop = ConversationAgentLoop(
        model=MODEL,
        max_iterations=MAX_ITERATIONS,
        tools=registry.get_definitions(ENABLED_TOOLSETS),
        system_prompt=cached_prompt,
        registry=registry,
        client=client,
        session_key=session_key or session_id,
        conn=conn,
        db_session_id=session_id,
        existing_messages=existing,
        max_retries=MAX_RETRIES,
        max_continuations=MAX_CONTINUATIONS,
        compression_threshold=COMPRESSION_THRESHOLD,
        # ponytail: 当前项目 client.chat.completions.create 只传基础三参数
        # (model/messages/tools)。若后续切到 GLM 5.2 / deepseek v4 等需要
        # extra_body / temperature 的 provider,在这里透传即可,无需改 AgentLoop。
        model_kwargs=None,
    )
    result: AgentLoopResult = loop.run(user_message)

    # 把 loop 的结构化结果映射回原 run_conversation 的 dict 输出
    if result.status == "completed":
        final = result.summary
    elif result.status == "max_iterations":
        final = "(max iterations reached)"
    else:
        # model_error 默认 raise;tool_error 会先把错误 tool message 持久化
        # 兜底文案,信息不丢
        final = f"(agent loop ended: status={result.status}, error={result.error})"

    return {"final_response": final, "messages": result.messages}
