"""主会话 agent:ConversationAgentLoop + run_conversation 入口。

ConversationAgentLoop 继承 AgentLoop,覆盖 hooks 注入主会话专有行为:
  - compression(每轮模型调用前)
  - classify_error / fallback / jittered_backoff(模型异常时)
  - finish_reason == "length" 的 continuation
  - 普通 assistant / continuation message 的 add_messages 持久化
  - assistant tool_call + tool results 的 batch 原子持久化
  - tool_call 处理的 print 日志 + 错误 tool message 包装

run_conversation() 保持原有同步签名;run_conversation_async() 为 Gateway
提供可取消的异步入口,两者返回 dict 格式一致。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time

from hermes.agent_loop import (
    AgentLoop,
    AgentLoopResult,
    AsyncAgentLoop,
    _sanitize_error_message,
)
from hermes.config import (
    client,
    create_async_client,
    MODEL,
    MAX_ITERATIONS,
    MAX_RETRIES,
    MAX_CONTINUATIONS,
    COMPRESSION_THRESHOLD,
    CONTINUE_MESSAGE,
)
from hermes.db import (
    add_messages,
    get_gateway_visible_session_messages,
    get_session_messages,
)
from hermes.errors import (
    classify_error,
    jittered_backoff,
    switch_to_async_fallback,
    switch_to_fallback,
)
from hermes.tokens import compress, compress_async, estimate_tokens
from hermes.tools import registry


ENABLED_TOOLSETS = ["terminal", "file", "memory", "skill", "delegate", "cron"]


def _dispatch_conversation_tool_call(loop, tool_call):
    """主会话工具分发共享实现,供同步 / 异步循环复用。"""
    tool_name = tool_call.function.name
    try:
        tool_args = json.loads(tool_call.function.arguments)
    except Exception as exc:
        short = _sanitize_error_message(exc, max_len=200)
        return (
            f"(error: invalid JSON arguments in {tool_name}: {short})",
            "json",
            f"invalid JSON in tool_call {tool_name!r}: {short}",
        )
    print(
        f"  [tool] {tool_name}: "
        f"{json.dumps(tool_args, ensure_ascii=False)[:120]}"
    )
    try:
        output = loop.registry.dispatch(
            tool_name, tool_args,
            session_key=loop.session_key,
        )
    except Exception as exc:
        short = _sanitize_error_message(exc, max_len=200)
        return (
            f"(error: tool {tool_name} failed: {short})",
            "dispatch",
            f"tool {tool_name!r} raised: {short}",
        )
    return output, None, None


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
        cancel_checker=None,
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
            cancel_checker=cancel_checker,
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
        # fallback 只能从 primary 切换一次。已经切到 fallback 后再失败,
        # 不再二次切换、不重置 retry_count,直接 abort 避免 max_iterations 拖延。
        self._using_fallback = False

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
        """模型异常分类:retry → fallback → abort,不抛异常到最外层。

        策略:
          - context_overflow:压缩后重试(无副作用)
          - should_fallback(auth / model_not_found):不做无意义 retry,直接切 fallback
          - retryable(429 / 5xx / network / timeout):先按 max_retries 重试
          - retry 耗尽或 unknown:尝试 fallback;没 fallback 就 abort
        """
        status = getattr(exc, "status_code", None)
        classified = classify_error(status, str(exc))
        print(f"  [error] {classified['reason']} (status={status})")

        if classified["should_compress"]:
            # 触发压缩后让循环重试本轮
            # 直接 mutate messages,让下一轮 pre_model_call 拿到压缩后的版本
            compressed = compress(messages)
            messages.clear()
            messages.extend(compressed)
            return "retry"

        # auth / model_not_found:不重试,直接尝试 fallback
        if classified["should_fallback"]:
            return self._try_fallback_or_abort()

        # 可重试错误(429 / 5xx / network / timeout):先按 max_retries 重试
        if classified["retryable"] and self._retry_count < self.max_retries:
            self._retry_count += 1
            time.sleep(jittered_backoff(self._retry_count))
            return "retry"

        # retry 耗尽 / unknown:最后尝试 fallback,没 fallback 就 abort
        return self._try_fallback_or_abort()

    def _try_fallback_or_abort(self) -> str:
        """尝试切到 fallback 模型。配置了就 retry,没配置 / 已切换过就 abort。

        - 已经在用 fallback 还失败:直接 abort,不再二次切换、不重置
          retry_count,避免 fallback 反复重启或靠 max_iterations 拖延。
        - 首次切换:设置 _using_fallback=True,重置 retry_count 一次,
          让 fallback 也有完整的 max_retries 重试机会。
        - 没配置 fallback:直接 abort。

        返回 abort(而非 raise)让 AgentLoop.run 返结构化 model_error,
        避免底层 openai / http 异常冒到最外层。
        """
        if self._using_fallback:
            return "abort"
        fallback_client, fallback_model = switch_to_fallback()
        if fallback_client:
            self.client = fallback_client
            self.model = fallback_model
            self._using_fallback = True
            # 切到 fallback 后重置 retry_count 一次,让 fallback 有重试机会。
            # 后续 fallback 自己失败时不再重置(上面的 _using_fallback 守卫)。
            self._retry_count = 0
            return "retry"
        return "abort"

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
        output 回写给模型时走统一脱敏(密钥 / 外部路径 / traceback),
        复用 agent_loop._sanitize_error_message 避免重复实现。
        """
        tool_name = tool_call.function.name
        try:
            tool_args = json.loads(tool_call.function.arguments)
        except Exception as exc:
            short = _sanitize_error_message(exc, max_len=200)
            return (
                f"(error: invalid JSON arguments in {tool_name}: {short})",
                "json",
                f"invalid JSON in tool_call {tool_name!r}: {short}",
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
            short = _sanitize_error_message(exc, max_len=200)
            return (
                f"(error: tool {tool_name} failed: {short})",
                "dispatch",
                f"tool {tool_name!r} raised: {short}",
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


class AsyncConversationAgentLoop(AsyncAgentLoop):
    """Gateway 使用的异步主会话循环。

    会话策略与 ``ConversationAgentLoop`` 保持一致,模型、压缩和重试等待
    使用异步调用,从而允许 Runner 直接取消正在等待的 HTTP 请求。
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
        cancel_checker=None,
        final_message_callback=None,
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
            cancel_checker=cancel_checker,
        )
        self.conn = conn
        self.db_session_id = db_session_id
        self.max_retries = max_retries
        self.max_continuations = max_continuations
        self.compression_threshold = compression_threshold
        self._existing_messages = existing_messages
        self._retry_count = 0
        self._continuation_count = 0
        self._using_fallback = False
        # Gateway 可注入回调,把最终消息和 outbox 放进同一事务。
        self.final_message_callback = final_message_callback
        # fallback 客户端由本循环创建,结束时单独关闭;主客户端归 Runner 管理。
        self._fallback_client = None

    def init_messages(self, user_message: str) -> list[dict]:
        return list(self._existing_messages) + [
            {"role": "user", "content": user_message},
        ]

    async def pre_model_call(self, messages: list[dict]) -> list[dict]:
        if estimate_tokens(messages) >= self.compression_threshold:
            messages = await compress_async(messages, self.client, self.model)
        return messages

    async def handle_model_error(self, exc, messages) -> str:
        """异步模型异常策略,取消不会进入普通重试 / fallback。"""
        status = getattr(exc, "status_code", None)
        classified = classify_error(status, str(exc))
        print(f"  [error] {classified['reason']} (status={status})")

        if classified["should_compress"]:
            compressed = await compress_async(
                messages, self.client, self.model,
            )
            messages.clear()
            messages.extend(compressed)
            return "retry"

        if classified["should_fallback"]:
            return self._try_fallback_or_abort()

        if classified["retryable"] and self._retry_count < self.max_retries:
            self._retry_count += 1
            await asyncio.sleep(jittered_backoff(self._retry_count))
            return "retry"

        return self._try_fallback_or_abort()

    def _try_fallback_or_abort(self) -> str:
        if self._using_fallback:
            return "abort"
        fallback_client, fallback_model = switch_to_async_fallback()
        if fallback_client:
            self.client = fallback_client
            self.model = fallback_model
            self._fallback_client = fallback_client
            self._using_fallback = True
            self._retry_count = 0
            return "retry"
        return "abort"

    async def close(self) -> None:
        """只关闭本循环创建的 fallback 客户端。"""
        if self._fallback_client is None:
            return
        try:
            await self._fallback_client.close()
        except Exception:
            # 清理失败不应覆盖已经生成的会话结果或取消状态。
            pass
        finally:
            self._fallback_client = None

    async def on_assistant_message(self, msg_dict: dict, response) -> None:
        add_messages(self.conn, self.db_session_id, [msg_dict])
        self._retry_count = 0

    async def on_final_assistant_message(
        self,
        msg_dict: dict,
        response,
    ) -> None:
        if self.final_message_callback is None:
            add_messages(self.conn, self.db_session_id, [msg_dict])
        else:
            self.final_message_callback(
                self.conn,
                self.db_session_id,
                msg_dict,
            )
        self._retry_count = 0

    def should_continue(self, finish_reason: str, messages: list[dict]) -> bool:
        if finish_reason == "length" and self._continuation_count < self.max_continuations:
            self._continuation_count += 1
            return True
        return False

    def continuation_message(self) -> dict:
        return {"role": "user", "content": CONTINUE_MESSAGE}

    async def on_continuation_message(self, cont_msg: dict) -> None:
        add_messages(self.conn, self.db_session_id, [cont_msg])

    def on_tool_dispatch_start(self) -> None:
        self._continuation_count = 0

    async def dispatch_one(
        self,
        tool_call,
    ) -> tuple[str, str | None, str | None]:
        return await asyncio.to_thread(
            _dispatch_conversation_tool_call, self, tool_call,
        )

    async def on_tool_message(
        self,
        tool_call,
        tool_msg: dict,
        output: str,
    ) -> None:
        pass

    async def on_tool_messages_batch(
        self,
        assistant_msg: dict,
        tool_messages: list[dict],
        response,
    ) -> None:
        add_messages(
            self.conn,
            self.db_session_id,
            [assistant_msg, *tool_messages],
        )
        self._retry_count = 0


# ---------------------------------------------------------------------------
# 对外入口(签名 / 返回格式与原版完全一致)
# ---------------------------------------------------------------------------

def _short_db_error(exc) -> str:
    """DB 异常简短描述,统一脱敏且不带完整 traceback。"""
    return _sanitize_error_message(exc, max_len=200)


def _persistence_error_response(exc) -> dict:
    """run_conversation 入口 DB 读写失败时的结构化返回。

    不启动 AgentLoop —— 历史都读不出来 / user msg 写不进去时,继续跑模型
    没有意义。fatal=True / retryable=False:调用方不应盲目重试整个 agent。
    """
    detail = _short_db_error(exc)
    return {
        "final_response": (
            f"(agent error: persistence_error; fatal=True; "
            f"retryable=False; detail={detail})"
        ),
        "messages": [],
        "ok": False,
        "status": "error",
        "error_type": "persistence_error",
        "fatal": True,
        "retryable": False,
    }


def _conversation_result_response(result: AgentLoopResult) -> dict:
    """把同步 / 异步循环结果映射为统一的对外返回格式。"""
    if result.status == "completed":
        final = result.summary
    elif result.status == "max_iterations":
        final = "(max iterations reached)"
    elif result.status == "cancelled":
        final = "(cancelled)"
    elif result.status == "model_error":
        final = (
            f"(agent error: model_error; fatal={result.fatal}; "
            f"retryable={result.retryable}; detail={result.error})"
        )
    elif result.status == "tool_error":
        final = (
            f"(agent error: tool_error; fatal={result.fatal}; "
            f"detail={result.error})"
        )
    elif result.status == "error":
        final = (
            f"(agent error: {result.error_type}; fatal={result.fatal}; "
            f"retryable={result.retryable}; detail={result.error})"
        )
    else:
        final = f"(agent ended: status={result.status}, error={result.error})"

    return {
        "final_response": final,
        "messages": result.messages,
        "ok": result.ok,
        "status": result.status,
        "error_type": result.error_type,
        "fatal": result.fatal,
        "retryable": result.retryable,
    }


def run_conversation(
    user_message: str,
    conn: sqlite3.Connection,
    session_id: str,
    cached_prompt: str,
    session_key: str | None = None,
    cancel_checker=None,
) -> dict:
    """主会话 agent 入口。委托给 ConversationAgentLoop。

    返回 ``{"final_response": str, "messages": list[dict]}``,
    与原版完全一致;额外带 ok / status / error_type / fatal / retryable
    结构化字段,供调用方精确判断错误。
    """
    # 关键顺序:先读历史(不含当前 user_msg),再 add_messages 当前 user_msg。
    # 这样 ConversationAgentLoop.init_messages 拼 existing + [user_msg] 时,
    # user message 在 API 调用 和 DB 里都只出现一次。
    #
    # DB 读写失败时直接返结构化 persistence_error,不启动 AgentLoop ——
    # 历史读不出 / user msg 写不进时,跑模型无意义。
    try:
        existing = get_session_messages(conn, session_id)
        user_msg = {"role": "user", "content": user_message}
        add_messages(conn, session_id, [user_msg])
    except Exception as exc:
        return _persistence_error_response(exc)

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
        cancel_checker=cancel_checker,
    )
    result: AgentLoopResult = loop.run(user_message)

    # 同步 / 异步入口使用同一映射,避免两条链路返回格式漂移。
    return _conversation_result_response(result)


async def run_conversation_async(
    user_message: str,
    conn: sqlite3.Connection,
    session_id: str,
    cached_prompt: str,
    session_key: str | None = None,
    cancel_checker=None,
    *,
    async_client=None,
    final_message_callback=None,
) -> dict:
    """Gateway 异步主会话入口,返回格式与 ``run_conversation`` 一致。"""
    owns_client = async_client is None
    if async_client is None:
        async_client = create_async_client()

    loop = None
    try:
        try:
            existing = get_gateway_visible_session_messages(conn, session_id)
            user_msg = {"role": "user", "content": user_message}
            add_messages(conn, session_id, [user_msg])
        except Exception as exc:
            return _persistence_error_response(exc)

        loop = AsyncConversationAgentLoop(
            model=MODEL,
            max_iterations=MAX_ITERATIONS,
            tools=registry.get_definitions(ENABLED_TOOLSETS),
            system_prompt=cached_prompt,
            registry=registry,
            client=async_client,
            session_key=session_key or session_id,
            conn=conn,
            db_session_id=session_id,
            existing_messages=existing,
            max_retries=MAX_RETRIES,
            max_continuations=MAX_CONTINUATIONS,
            compression_threshold=COMPRESSION_THRESHOLD,
            model_kwargs=None,
            cancel_checker=cancel_checker,
            final_message_callback=final_message_callback,
        )
        result: AgentLoopResult = await loop.run(user_message)
        return _conversation_result_response(result)
    finally:
        if loop is not None:
            await loop.close()
        if owns_client:
            try:
                await async_client.close()
            except Exception:
                pass
