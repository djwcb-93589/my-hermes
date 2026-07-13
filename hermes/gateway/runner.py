"""
GatewayRunner:启动 adapter,路由入站消息,跑 agent,回发结果。

核心设计:
  - 每条 route_key 串行处理(busy 原子设置 + deque 排队),不同 route_key 并行。
  - 同一会话收到新消息时,先 cancel 当前任务(cancel_checker),再排队。
  - ``run_conversation`` 是同步函数,通过 ``asyncio.to_thread`` 跑在线程池。
    SQLite 连接在线程函数内部创建 / 使用 / 关闭,不跨线程传递。
  - cancel_checker 透传到 ``run_conversation → ConversationAgentLoop → AgentLoop``。
"""

from __future__ import annotations

import asyncio
import os

from hermes.gateway.adapters import BasePlatformAdapter
from hermes.gateway.session_store import SessionStore
from hermes.gateway.types import MessageEvent, SendResult, build_session_key
from hermes.prompt import build_system_prompt


class GatewayRunner:
    """启动 adapter、路由消息、跑 agent、回发结果。"""

    def __init__(self, config: dict, db_path: str):
        self.config = config
        self.db_path = db_path
        self.adapters: dict[str, BasePlatformAdapter] = {}
        self.agent_name = config.get("gateway", {}).get("agent_name", "main")
        idle_timeout = config.get("gateway", {}).get("session_idle_timeout", 86400)
        self.sessions = SessionStore(idle_timeout=idle_timeout, db_path=db_path)

    def add_adapter(self, adapter: BasePlatformAdapter):
        adapter._on_message = self._handle_message
        self.adapters[adapter.platform_name] = adapter

    async def start(self):
        """逐个连接 adapter,单个失败不阻止其他。"""
        for name, adapter in self.adapters.items():
            try:
                ok = await adapter.connect()
                if ok:
                    print(f"  [gateway] {name} connected")
                else:
                    print(f"  [gateway] {name} FAILED to connect")
            except Exception as exc:
                print(f"  [gateway] {name} crashed on connect: {type(exc).__name__}")

    async def stop(self):
        """断开所有 adapter + 清理 backend。"""
        for adapter in self.adapters.values():
            try:
                await adapter.disconnect()
            except Exception:
                pass
        from hermes.backends import cleanup_all_backends
        cleanup_all_backends()

    # ----- 消息路由 -----

    async def _handle_message(self, event: MessageEvent):
        """所有 adapter 的入站消息在此汇聚。"""
        route_key = build_session_key(event.source, self.agent_name)

        # slash 命令(所有平台通用)
        cmd = (event.text or "").strip().lower()
        if cmd == "/new":
            self.sessions.new_conversation(
                route_key, build_system_prompt(os.getcwd()),
            )
            await self._reply(event, "(new conversation started)")
            return
        if cmd == "/stop":
            ok = self.sessions.request_cancel(route_key)
            await self._reply(
                event,
                "(cancel requested)" if ok else "(no active task)",
            )
            return
        if cmd == "/status":
            status = self.sessions.get_status(route_key)
            if status:
                await self._reply(event, f"({status})")
            else:
                await self._reply(event, "(no session)")
            return

        ctx = self.sessions.get_or_create(
            route_key, build_system_prompt(os.getcwd()),
        )

        if ctx.busy:
            # 正在处理 → 排队(保留全部,不只最后一条)
            ctx.pending.append(event)
            ctx.cancel_requested = True
            print(f"  [gateway] {route_key}: queued ({len(ctx.pending)} pending)")
            return

        # 原子设置 busy,避免竞态:create_task 不会立即执行,_rocess 也没
        # 机会在 _handle_message 返回前跑。所以在 _handle_message 里设 busy
        # 就能保证同一 route_key 只有一个 worker。
        ctx.busy = True
        ctx.cancel_requested = False
        asyncio.create_task(self._process(route_key, event))

    async def _process(self, route_key: str, event: MessageEvent):
        """串行处理一条消息,然后检查队列。"""
        ctx = self.sessions.get_or_create(
            route_key, build_system_prompt(os.getcwd()),
        )

        try:
            response = await self._run_agent(event, ctx)
            if response:
                await self._reply(event, response)
        except Exception as exc:
            print(f"  [gateway] {route_key} error: {type(exc).__name__}")
            try:
                await self._reply(event, f"(internal error: {type(exc).__name__})")
            except Exception:
                pass
        finally:
            ctx.busy = False

        # 处理队列中的下一条(在同一事件循环里,无竞态)
        if ctx.pending:
            next_event = ctx.pending.popleft()
            ctx.busy = True
            ctx.cancel_requested = False
            asyncio.create_task(self._process(route_key, next_event))

    async def _run_agent(self, event: MessageEvent, ctx) -> str | None:
        """在线程池跑同步的 ``run_conversation``,不阻塞事件循环。

        关键:SQLite 连接必须在线程函数内部创建 / 使用 / 关闭,
        不跨线程传递(asyncio.to_thread 在 worker 线程跑,如果 conn
        在事件循环线程创建,SQLite 会抛 ProgrammingError)。
        """
        cancel_checker = lambda: ctx.cancel_requested  # noqa: E731

        def _worker() -> str | None:
            # 全部在 worker 线程内完成:建连接 → 确保 session → 跑 agent → 关连接
            from hermes.db import init_db, ensure_session
            from hermes.conversation import run_conversation

            conn = init_db(self.db_path)
            try:
                ensure_session(conn, ctx.conversation_id, source=event.source.platform)
                result = run_conversation(
                    event.text,
                    conn,
                    ctx.conversation_id,
                    ctx.system_prompt,
                    ctx.conversation_id,
                    cancel_checker,
                )
                return result.get("final_response")
            finally:
                conn.close()

        return await asyncio.to_thread(_worker)

    async def _reply(self, event: MessageEvent, content: str):
        """通过来源 adapter 回发。检查 SendResult。"""
        adapter = self.adapters.get(event.source.platform)
        if not adapter:
            return
        try:
            result: SendResult = await adapter.send(
                event.source.chat_id,
                content,
                reply_to_message_id=event.message_id,
                thread_id=event.source.thread_id,
            )
            if not result.success:
                # 脱敏:只输出错误类型 + 简短描述,不含完整响应体 / token
                err = (result.error or "unknown error")[:120]
                print(f"  [gateway] send failed on {event.source.platform}: {err}")
        except Exception as exc:
            print(f"  [gateway] send exception on {event.source.platform}: {type(exc).__name__}")
