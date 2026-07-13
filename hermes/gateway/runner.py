"""
GatewayRunner:启动 adapter,路由入站消息,跑 agent,回发结果。

核心设计:
  - 每条 route_key 串行处理(busy 原子设置 + deque 排队),不同 route_key 并行。
  - 同一会话收到新消息时,先取消当前模型 Task,再排队。
  - busy / pending 消息持久化到 SQLite,重启后按接收顺序恢复。
  - 全局 semaphore 限制不同会话同时调用 LLM 的数量。
  - Gateway 使用 ``run_conversation_async``,模型 HTTP 请求由 asyncio.Task 管理。
  - /stop、/new 或后续消息可直接取消当前 Task,不再等待同步线程返回。
  - cancel_checker 仍作为协作式取消兜底,保持旧调用链兼容。
"""

from __future__ import annotations

import asyncio
import json
import os

from hermes.db import (
    complete_gateway_message,
    delete_gateway_messages,
    enqueue_gateway_message,
    get_gateway_queued_messages,
    init_db,
    mark_gateway_message_processing,
    reset_gateway_processing_messages,
)
from hermes.gateway.adapters import BasePlatformAdapter
from hermes.gateway.session_store import SessionStore
from hermes.gateway.types import (
    MessageEvent,
    MessageType,
    SendResult,
    SessionSource,
    build_session_key,
)
from hermes.prompt import build_system_prompt


class GatewayRunner:
    """启动 adapter、路由消息、跑 agent、回发结果。"""

    def __init__(self, config: dict, db_path: str):
        self.config = config
        self.db_path = db_path
        self.adapters: dict[str, BasePlatformAdapter] = {}
        self.agent_name = config.get("gateway", {}).get("agent_name", "main")
        idle_timeout = config.get("gateway", {}).get("session_idle_timeout", 86400)
        max_pending = config.get("gateway", {}).get("max_pending_messages", 20)
        max_concurrent = config.get(
            "gateway", {},
        ).get("max_concurrent_llm_requests", 4)
        self.sessions = SessionStore(
            idle_timeout=idle_timeout,
            db_path=db_path,
            max_pending_messages=max_pending,
        )
        self.max_concurrent_llm_requests = max(1, int(max_concurrent))
        self._llm_semaphore = asyncio.Semaphore(
            self.max_concurrent_llm_requests
        )
        self._accepted_messages: set[tuple[str, str]] = set()
        # 异步模型客户端按需创建,Gateway 停止时统一关闭。
        self._async_client = None

    def add_adapter(self, adapter: BasePlatformAdapter):
        adapter._on_message = self._handle_message
        self.adapters[adapter.platform_name] = adapter

    async def start(self):
        """逐个连接 adapter,然后恢复上次未完成的 Runner 消息。"""
        for name, adapter in self.adapters.items():
            try:
                ok = await adapter.connect()
                if ok:
                    print(f"  [gateway] {name} connected")
                else:
                    print(f"  [gateway] {name} FAILED to connect")
            except Exception as exc:
                print(f"  [gateway] {name} crashed on connect: {type(exc).__name__}")
        await self._restore_queued_messages()

    async def stop(self):
        """取消运行中任务,断开 adapter,关闭模型客户端并清理 backend。"""
        active_tasks = self.sessions.cancel_all(reason="shutdown")
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        for adapter in self.adapters.values():
            try:
                await adapter.disconnect()
            except Exception:
                pass
        if self._async_client is not None:
            try:
                await self._async_client.close()
            except Exception:
                pass
            finally:
                self._async_client = None
        from hermes.backends import cleanup_all_backends
        cleanup_all_backends()

    # ----- 消息路由 -----

    @staticmethod
    def _serialize_event(event: MessageEvent) -> str:
        """把平台无关事件序列化后写入 Runner 恢复队列。"""
        source = event.source
        payload = {
            "message_id": event.message_id,
            "text": event.text,
            "message_type": event.message_type.value,
            "media_urls": event.media_urls,
            "reply_to_message_id": event.reply_to_message_id,
            "attachments": event.attachments,
            "metadata": event.metadata,
            "source": {
                "platform": source.platform,
                "account_id": source.account_id,
                "chat_id": source.chat_id,
                "chat_type": source.chat_type,
                "user_id": source.user_id,
                "user_id_alt": source.user_id_alt,
                "user_name": source.user_name,
                "thread_id": source.thread_id,
            },
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _deserialize_event(raw: str) -> MessageEvent:
        """从 Runner 恢复队列重建 MessageEvent。"""
        payload = json.loads(raw)
        source = payload["source"]
        return MessageEvent(
            message_id=str(payload["message_id"]),
            text=str(payload["text"]),
            message_type=MessageType(payload.get("message_type", "text")),
            media_urls=list(payload.get("media_urls", [])),
            reply_to_message_id=payload.get("reply_to_message_id"),
            attachments=list(payload.get("attachments", [])),
            metadata=dict(payload.get("metadata", {})),
            source=SessionSource(
                platform=str(source["platform"]),
                account_id=str(source.get("account_id", "")),
                chat_id=str(source.get("chat_id", "")),
                chat_type=str(source.get("chat_type", "dm")),
                user_id=str(source.get("user_id", "")),
                user_id_alt=str(source.get("user_id_alt", "")),
                user_name=str(source.get("user_name", "")),
                thread_id=source.get("thread_id"),
            ),
        )

    def _persist_event(self, route_key: str, event: MessageEvent) -> None:
        """消息进入内存 busy / pending 前先持久化。"""
        conn = init_db(self.db_path)
        try:
            enqueue_gateway_message(
                conn,
                route_key,
                event.message_id,
                self._serialize_event(event),
            )
        finally:
            conn.close()
        self._accepted_messages.add((route_key, event.message_id))

    def _mark_event_processing(self, route_key: str, event: MessageEvent) -> None:
        conn = init_db(self.db_path)
        try:
            mark_gateway_message_processing(
                conn, route_key, event.message_id,
            )
        finally:
            conn.close()

    def _complete_event(self, route_key: str, event: MessageEvent) -> bool:
        """处理结束后删除恢复记录;失败时保留到下次重启。"""
        try:
            conn = init_db(self.db_path)
            try:
                complete_gateway_message(
                    conn, route_key, event.message_id,
                )
            finally:
                conn.close()
        except Exception as exc:
            print(
                f"  [gateway] {route_key}: queue completion failed "
                f"({type(exc).__name__})"
            )
            return False
        self._accepted_messages.discard((route_key, event.message_id))
        return True

    def _drop_events(self, route_key: str, events: list[MessageEvent]) -> None:
        """持久化删除被 /new 明确取消的旧 pending。"""
        message_ids = [event.message_id for event in events]
        conn = init_db(self.db_path)
        try:
            delete_gateway_messages(conn, route_key, message_ids)
        finally:
            conn.close()
        for message_id in message_ids:
            self._accepted_messages.discard((route_key, message_id))

    async def _restore_queued_messages(self) -> None:
        """Adapter 就绪后恢复 queued / processing 消息。"""
        conn = init_db(self.db_path)
        try:
            reset_gateway_processing_messages(conn)
            rows = get_gateway_queued_messages(conn)
        finally:
            conn.close()

        restored = 0
        for row in rows:
            try:
                event = self._deserialize_event(row["event_json"])
                route_key = build_session_key(event.source, self.agent_name)
                if route_key != row["route_key"]:
                    raise ValueError("route key mismatch")
                key = (route_key, event.message_id)
                if key in self._accepted_messages:
                    continue
                self._accepted_messages.add(key)
                await self._handle_message(event, from_queue=True)
                restored += 1
            except Exception as exc:
                print(
                    "  [gateway] queued message recovery failed: "
                    f"{type(exc).__name__}"
                )
        if restored:
            print(f"  [gateway] restored queued messages: {restored}")

    async def _handle_message(
        self,
        event: MessageEvent,
        *,
        from_queue: bool = False,
    ):
        """所有 adapter 的入站消息在此汇聚。"""
        route_key = build_session_key(event.source, self.agent_name)
        queue_key = (route_key, event.message_id)
        if not from_queue and queue_key in self._accepted_messages:
            return

        # slash 命令(所有平台通用)
        cmd = (event.text or "").strip().lower()
        if cmd == "/new":
            ctx = self.sessions.get_or_create(
                route_key, build_system_prompt(os.getcwd()),
            )
            if ctx.busy:
                # /new 作为串行屏障:丢弃命令前尚未执行的旧消息,
                # 等当前 worker 完全退出后再切换 conversation_id。
                dropped_events = list(ctx.pending)
                self._drop_events(route_key, dropped_events)
                ctx.pending.clear()
                if not from_queue:
                    self._persist_event(route_key, event)
                self.sessions.enqueue(ctx, event)
                self.sessions.request_cancel(route_key, reason="new")
                print(
                    f"  [gateway] {route_key}: /new queued "
                    f"({len(dropped_events)} old pending dropped)"
                )
                return
            if not from_queue:
                self._persist_event(route_key, event)
            try:
                self.sessions.new_conversation(
                    route_key, build_system_prompt(os.getcwd()),
                )
                await self._reply(event, "(new conversation started)")
            except Exception:
                raise
            else:
                self._complete_event(route_key, event)
            await self._dispatch_next(ctx)
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
            # 正在处理 → 在单会话上限内排队。
            if (
                not from_queue
                and len(ctx.pending) >= self.sessions.max_pending_messages
            ):
                print(f"  [gateway] {route_key}: queue full")
                await self._reply(
                    event,
                    "(queue full: please wait for pending messages)",
                )
                return
            if not from_queue:
                self._persist_event(route_key, event)
            if from_queue:
                # 已持久化消息必须全部恢复,不能因重启后的新上限丢失。
                ctx.pending.append(event)
            else:
                self.sessions.enqueue(ctx, event)
            # 重启恢复的历史队列按原顺序完整执行,不能让后一条恢复消息
            # 取消前一条;只有新到达的实时消息才覆盖当前请求。
            if not from_queue:
                self.sessions.request_cancel(route_key, reason="superseded")
            print(f"  [gateway] {route_key}: queued ({len(ctx.pending)} pending)")
            return

        # 原子设置 busy,避免竞态:create_task 不会立即执行,_rocess 也没
        # 机会在 _handle_message 返回前跑。所以在 _handle_message 里设 busy
        # 就能保证同一 route_key 只有一个 worker。
        if not from_queue:
            self._persist_event(route_key, event)
        self._mark_event_processing(route_key, event)
        ctx.busy = True
        ctx.cancel_requested = False
        ctx.cancel_reason = None
        # 模型 Task 与串行收尾 worker 分开管理。即使模型 Task 在首次运行前
        # 就被取消,worker 仍会启动并清理 busy / 持久队列。
        agent_task = asyncio.create_task(self._run_agent(event, ctx))
        ctx.active_task = agent_task
        worker_task = asyncio.create_task(
            self._process(route_key, event, agent_task),
        )
        ctx.worker_task = worker_task

    async def _process(
        self,
        route_key: str,
        event: MessageEvent,
        agent_task: asyncio.Task | None = None,
    ):
        """串行处理一条消息,然后检查队列。"""
        ctx = self.sessions.get_or_create(
            route_key, build_system_prompt(os.getcwd()),
        )

        cancel_reason = None
        try:
            if agent_task is None:
                response = await self._run_agent(event, ctx)
            else:
                response = await agent_task
            # worker 返回到事件循环后做最后一道取消检查。即使取消恰好
            # 发生在模型响应检查之后,也不能把旧回复发送给用户。
            if ctx.cancel_requested:
                print(f"  [gateway] {route_key}: stale response discarded")
            elif response:
                await self._reply(event, response)
        except asyncio.CancelledError:
            cancel_reason = ctx.cancel_reason or "cancelled"
            print(f"  [gateway] {route_key}: task cancelled ({cancel_reason})")
        except Exception as exc:
            print(f"  [gateway] {route_key} error: {type(exc).__name__}")
            try:
                await self._reply(event, f"(internal error: {type(exc).__name__})")
            except Exception:
                pass
        finally:
            cancel_reason = cancel_reason or ctx.cancel_reason
            ctx.busy = False
            if agent_task is not None and ctx.active_task is agent_task:
                ctx.active_task = None
            if ctx.worker_task is asyncio.current_task():
                ctx.worker_task = None
            # 用户取消 /new / 后续消息覆盖表示这条消息已经明确放弃;
            # shutdown 则保留 processing 记录,下次启动恢复。
            if cancel_reason != "shutdown":
                self._complete_event(route_key, event)

        if cancel_reason != "shutdown":
            await self._dispatch_next(ctx)

    async def _dispatch_next(self, ctx) -> None:
        """按入队顺序分发下一条消息,命令也走同一串行入口。"""
        if ctx.busy or not ctx.pending:
            return
        next_event = ctx.pending.popleft()
        await self._handle_message(next_event, from_queue=True)

    async def _run_agent(self, event: MessageEvent, ctx) -> str | None:
        """在全局并发限制内运行异步主会话。"""
        # 所有 route_key 共用同一信号量,避免不同会话同时打满模型服务。
        async with self._llm_semaphore:
            return await self._run_agent_async(event, ctx)

    async def _run_agent_async(self, event: MessageEvent, ctx) -> str | None:
        """使用 AsyncOpenAI 跑主会话,SQLite 仍复用现有同步接口。"""
        from hermes.db import ensure_session
        from hermes.conversation import run_conversation_async

        cancel_checker = lambda: ctx.cancel_requested  # noqa: E731
        conn = init_db(self.db_path)
        try:
            ensure_session(
                conn,
                ctx.conversation_id,
                source=event.source.platform,
            )
            result = await run_conversation_async(
                event.text,
                conn,
                ctx.conversation_id,
                ctx.system_prompt,
                ctx.conversation_id,
                cancel_checker,
                async_client=self._get_async_client(),
            )
            return result.get("final_response")
        finally:
            conn.close()

    def _get_async_client(self):
        """按需创建 Runner 独占的异步模型客户端。"""
        if self._async_client is None:
            from hermes.config import create_async_client
            self._async_client = create_async_client()
        return self._async_client

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
