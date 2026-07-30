"""
SessionStore:按 route_key 管理每条会话的运行期状态。

区分 route_key(稳定的平台路由标识)和 conversation_id(DB session_id)。
- route_key 不变:feishu:dm:ou_xxx → 永远是同一路。
- conversation_id 可变:/new 切换新 UUID,DB 历史从空开始。

支持 session_idle_timeout、/new、/stop、/status。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from hermes.gateway.types import MessageEvent
from hermes.steering import SteerMailbox


_PENDING_SEQUENCE_METADATA_KEY = "_gateway_pending_sequence"


@dataclass
class SessionContext:
    """单条会话的运行期上下文。"""
    route_key: str
    conversation_id: str          # DB session_id
    system_prompt: str
    last_activity: float = field(default_factory=time.time)
    # 每个实际开始的串行任务都占用一个新世代。取消同样推进世代,让已经
    # 捕获旧值的模型 / Outbox worker 永远不能因共享布尔值被重置而复活。
    generation: int = 0
    # 取消信号:AgentLoop.run 内通过 cancel_checker 检查
    cancel_requested: bool = False
    cancel_generation: int | None = None
    # 当前模型任务。/stop 通过 Task.cancel() 中断模型 HTTP 请求。
    active_task: asyncio.Task | None = field(default=None, repr=False)
    active_generation: int | None = field(default=None, repr=False)
    # 当前入站消息对应的 outbox delivery id,供 Runner 内部跨方法传递。
    delivery_id: str | None = None
    delivery_generation: int | None = field(default=None, repr=False)
    # 串行收尾任务不直接取消,确保模型 Task 即使尚未启动也能完成队列清理。
    worker_task: asyncio.Task | None = field(default=None, repr=False)
    worker_generation: int | None = field(default=None, repr=False)
    # 区分用户取消、/new、后续消息覆盖和 Gateway 关闭。
    cancel_reason: str | None = None
    # 普通取消不粗暴取消收尾 worker；用事件唤醒 retry sleep,再由 worker
    # 按 generation 检查正常退出。每次 begin_task 都会换成一个新事件。
    invalidation_event: asyncio.Event = field(
        default_factory=asyncio.Event,
        repr=False,
    )
    # 待处理消息队列(不止保留最后一条)
    pending: deque = field(default_factory=deque)
    # 是否正在处理(同一会话串行,不同会话并行)
    busy: bool = False
    # worker 已经完成业务收尾、但尚未在 route admission
    # 临界区内接力 pending。该阶段仍要阻止新消息越过队头。
    dispatching: bool = False
    # 最近一次 /sessions 实际展示的序号与完整会话 ID 对应关系。
    # None 表示本路由尚未执行过 /sessions，空字典表示列表已成功展示但没有会话。
    conversation_list_mapping: dict[int, str] | None = None
    active_steer_mailbox: SteerMailbox | None = field(default=None, repr=False)
    steer_generation: int | None = field(default=None, repr=False)
    # 只保存当前 generation 已进入 mailbox 的原始事件，供确认或原序重排队使用。
    inflight_steer_events: dict[str, MessageEvent] = field(
        default_factory=dict,
        repr=False,
    )
    inflight_steer_generations: dict[str, int] = field(
        default_factory=dict,
        repr=False,
    )
    # generation 失效时保留未确认原事件，直到数据库恢复或后续显式收口。
    deferred_steer_events: dict[str, MessageEvent] = field(
        default_factory=dict,
        repr=False,
    )
    deferred_steer_generations: dict[str, int] = field(
        default_factory=dict,
        repr=False,
    )
    pending_sequence: int = 0
    last_steer_sequence: int | None = None


class SessionStore:
    """进程内会话状态管理器。线程安全(asyncio 单线程,锁保护 dict 操作)。"""

    def __init__(
        self,
        idle_timeout: float = 86400,
        db_path: str | None = None,
        max_pending_messages: int = 20,
        persistence=None,
    ):
        self._contexts: dict[str, SessionContext] = {}
        self._context_locks: dict[str, asyncio.Lock] = {}
        self.idle_timeout = idle_timeout
        self.db_path = db_path
        self.persistence = persistence
        self.max_pending_messages = max(1, int(max_pending_messages))

    def _load_conversation_id(self, route_key: str) -> str:
        """恢复当前会话；首次使用时把默认 route_key 会话正式登记。"""
        if not self.db_path:
            return route_key

        from hermes.db import (
            get_gateway_conversation_id,
            init_db,
            set_gateway_conversation_id,
        )

        conn = init_db(self.db_path)
        try:
            persisted = get_gateway_conversation_id(conn, route_key)
            if not persisted:
                # 首次使用该路由：生成 UUID 作为会话 ID，避免把 route_key 当成 conversation_id
                persisted = str(uuid.uuid4())
                set_gateway_conversation_id(conn, route_key, persisted)
            return persisted
        finally:
            conn.close()

    @staticmethod
    def _close_active_steer_mailbox(ctx: SessionContext) -> None:
        """关闭并解除当前 steer mailbox，避免旧 generation 继续接收输入。"""
        mailbox = ctx.active_steer_mailbox
        if mailbox is not None:
            mailbox.close()
        ctx.active_steer_mailbox = None
        ctx.steer_generation = None

    @staticmethod
    def _defer_inflight_steer_events(
        ctx: SessionContext,
        generation: int | None = None,
    ) -> None:
        """generation 失效时转移未确认映射，不静默丢弃原事件。"""
        if not ctx.inflight_steer_events:
            return
        fallback_generation = generation
        if fallback_generation is None:
            fallback_generation = (
                ctx.steer_generation
                if ctx.steer_generation is not None
                else ctx.worker_generation
            )
        if fallback_generation is None:
            fallback_generation = (
                ctx.active_generation
                if ctx.active_generation is not None
                else ctx.generation
            )
        for steer_id, event in ctx.inflight_steer_events.items():
            ctx.deferred_steer_events[steer_id] = event
            ctx.deferred_steer_generations[steer_id] = (
                ctx.inflight_steer_generations.get(
                    steer_id,
                    fallback_generation,
                )
            )
        ctx.inflight_steer_events.clear()
        ctx.inflight_steer_generations.clear()

    @staticmethod
    def defer_steer_events(
        ctx: SessionContext,
        generation: int,
    ) -> None:
        """把指定 generation 的未确认 steer 转入可重试收口区。"""
        for steer_id, event in tuple(ctx.inflight_steer_events.items()):
            event_generation = ctx.inflight_steer_generations.get(
                steer_id,
                generation,
            )
            if event_generation != generation:
                continue
            ctx.deferred_steer_events[steer_id] = event
            ctx.deferred_steer_generations[steer_id] = generation
            ctx.inflight_steer_events.pop(steer_id, None)
            ctx.inflight_steer_generations.pop(steer_id, None)

    @staticmethod
    def get_deferred_steer_events(
        ctx: SessionContext,
        generation: int,
    ) -> tuple[MessageEvent, ...]:
        """返回指定 generation 尚未完成收口的原始 steer 事件。"""
        return tuple(
            event
            for steer_id, event in ctx.deferred_steer_events.items()
            if ctx.deferred_steer_generations.get(
                steer_id,
                generation,
            ) == generation
        )

    @staticmethod
    def get_unconfirmed_steer_event_records(
        ctx: SessionContext,
    ) -> tuple[tuple[int, MessageEvent], ...]:
        """返回所有尚未得到持久确认的 steer generation 与原始事件。"""
        records: dict[str, tuple[int, MessageEvent]] = {}
        for steer_id, event in ctx.deferred_steer_events.items():
            records[steer_id] = (
                ctx.deferred_steer_generations.get(
                    steer_id,
                    ctx.generation,
                ),
                event,
            )
        for steer_id, event in ctx.inflight_steer_events.items():
            records[steer_id] = (
                ctx.inflight_steer_generations.get(
                    steer_id,
                    ctx.generation,
                ),
                event,
            )
        return tuple(records.values())

    @staticmethod
    def resolve_steer_event(
        ctx: SessionContext,
        generation: int,
        steer_id: str,
    ) -> None:
        """仅清除属于指定 generation 且已得到可靠结论的 steer。"""
        if (
            steer_id in ctx.inflight_steer_events
            and ctx.inflight_steer_generations.get(
                steer_id,
                generation,
            ) == generation
        ):
            ctx.inflight_steer_events.pop(steer_id, None)
            ctx.inflight_steer_generations.pop(steer_id, None)
        if (
            steer_id in ctx.deferred_steer_events
            and ctx.deferred_steer_generations.get(
                steer_id,
                generation,
            ) == generation
        ):
            ctx.deferred_steer_events.pop(steer_id, None)
            ctx.deferred_steer_generations.pop(steer_id, None)

    @classmethod
    def _ensure_pending_sequence(
        cls,
        ctx: SessionContext,
        event,
    ) -> int:
        """为事件分配稳定的 route 内部顺序号，并保留在事件元数据中。"""
        metadata = getattr(event, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            event.metadata = metadata
        raw_sequence = metadata.get(_PENDING_SEQUENCE_METADATA_KEY)
        if (
            isinstance(raw_sequence, int)
            and not isinstance(raw_sequence, bool)
            and raw_sequence > 0
        ):
            ctx.pending_sequence = max(ctx.pending_sequence, raw_sequence)
            return raw_sequence
        ctx.pending_sequence += 1
        metadata[_PENDING_SEQUENCE_METADATA_KEY] = ctx.pending_sequence
        return ctx.pending_sequence

    @classmethod
    def event_sequence(cls, ctx: SessionContext, event) -> int:
        """返回事件的 route 内部顺序号。"""
        return cls._ensure_pending_sequence(ctx, event)

    def _save_conversation_id(
        self,
        route_key: str,
        conversation_id: str,
    ) -> None:
        """持久化 route 当前会话映射。"""
        if not self.db_path:
            return

        from hermes.db import init_db, set_gateway_conversation_id

        conn = init_db(self.db_path)
        try:
            set_gateway_conversation_id(conn, route_key, conversation_id)
        finally:
            conn.close()

    def get_or_create(
        self,
        route_key: str,
        system_prompt: str,
    ) -> SessionContext:
        """取现有 context 或新建；已存在时用最新 system_prompt 覆盖。

        system_prompt 里带有 ``Current time`` 等会随时间漂移的环境信息，
        会话持续期间必须每次刷新，否则 LLM 会基于过时时间做判断。
        """
        ctx = self._contexts.get(route_key)
        if ctx is None:
            ctx = SessionContext(
                route_key=route_key,
                conversation_id=self._load_conversation_id(route_key),
                system_prompt=system_prompt,
            )
            self._contexts[route_key] = ctx
        else:
            ctx.system_prompt = system_prompt
        ctx.last_activity = time.time()
        return ctx

    async def get_or_create_async(
        self,
        route_key: str,
        system_prompt: str,
    ) -> SessionContext:
        """异步恢复会话映射，不在事件循环线程等待 SQLite。

        已存在的 context 同样用最新 system_prompt 覆盖，保证 LLM 看到的
        ``Current time`` 等环境信息反映本次消息到达时刻，而不是会话
        第一次创建时的固化值。
        """
        ctx = self._contexts.get(route_key)
        if ctx is not None:
            ctx.system_prompt = system_prompt
            ctx.last_activity = time.time()
            return ctx
        lock = self._context_locks.setdefault(route_key, asyncio.Lock())
        async with lock:
            ctx = self._contexts.get(route_key)
            if ctx is None:
                conversation_id = route_key
                if self.db_path and self.persistence is not None:
                    from hermes.db import (
                        get_gateway_conversation_id,
                        set_gateway_conversation_id,
                    )

                    persisted = await self.persistence.call(
                        get_gateway_conversation_id,
                        route_key,
                    )
                    if persisted:
                        conversation_id = persisted
                    else:
                        # 首次使用该路由：生成 UUID 作为会话 ID，避免把 route_key 当成 conversation_id
                        conversation_id = str(uuid.uuid4())
                        await self.persistence.call(
                            set_gateway_conversation_id,
                            route_key,
                            conversation_id,
                        )
                elif self.db_path:
                    conversation_id = await asyncio.to_thread(
                        self._load_conversation_id,
                        route_key,
                    )
                ctx = SessionContext(
                    route_key=route_key,
                    conversation_id=conversation_id,
                    system_prompt=system_prompt,
                )
                self._contexts[route_key] = ctx
            ctx.last_activity = time.time()
            return ctx

    @staticmethod
    def _conversation_switch_blocked(ctx: SessionContext) -> bool:
        """切换不排队；任何活动 worker、dispatch 或 pending 都直接拒绝。"""
        worker_running = (
            ctx.worker_task is not None
            and not ctx.worker_task.done()
        )
        active_running = (
            ctx.active_task is not None
            and not ctx.active_task.done()
        )
        return bool(
            ctx.busy
            or ctx.dispatching
            or ctx.pending
            or worker_running
            or active_running
        )

    async def switch_conversation_async(
        self,
        route_key: str,
        conversation_id: str,
        system_prompt: str,
    ) -> SessionContext:
        """验证 route 归属后原子切换当前映射，再更新内存上下文。"""
        lock = self._context_locks.setdefault(route_key, asyncio.Lock())
        async with lock:
            ctx = self._contexts.get(route_key)
            if ctx is not None and self._conversation_switch_blocked(ctx):
                raise RuntimeError("conversation switch is blocked by active work")

            from hermes.db import (
                get_gateway_conversation_for_route,
                init_db,
                set_gateway_conversation_id,
            )

            if self.db_path and self.persistence is not None:
                summary = await self.persistence.call(
                    get_gateway_conversation_for_route,
                    route_key,
                    conversation_id,
                )
            elif self.db_path:
                def load_summary():
                    conn = init_db(self.db_path)
                    try:
                        return get_gateway_conversation_for_route(
                            conn,
                            route_key,
                            conversation_id,
                        )
                    finally:
                        conn.close()

                summary = await asyncio.to_thread(load_summary)
            else:
                summary = None
            if summary is None:
                raise ValueError("conversation does not belong to route")

            if ctx is not None and ctx.conversation_id == conversation_id:
                ctx.last_activity = time.time()
                return ctx

            # 数据库写入必须先成功；失败时下方所有内存状态都保持不变。
            if self.db_path and self.persistence is not None:
                await self.persistence.call(
                    set_gateway_conversation_id,
                    route_key,
                    conversation_id,
                )
            elif self.db_path:
                await asyncio.to_thread(
                    self._save_conversation_id,
                    route_key,
                    conversation_id,
                )

            if ctx is None:
                ctx = SessionContext(
                    route_key=route_key,
                    conversation_id=conversation_id,
                    system_prompt=system_prompt,
                )
                self._contexts[route_key] = ctx
            else:
                ctx.invalidation_event.set()
                self._close_active_steer_mailbox(ctx)
                ctx.generation += 1
                ctx.conversation_id = conversation_id
                ctx.system_prompt = system_prompt
                ctx.cancel_requested = False
                ctx.cancel_generation = None
                ctx.cancel_reason = None
                ctx.active_task = None
                ctx.active_generation = None
                ctx.delivery_id = None
                ctx.delivery_generation = None
                ctx.worker_task = None
                ctx.worker_generation = None
                ctx.busy = False
                ctx.dispatching = False
                ctx.invalidation_event = asyncio.Event()
                ctx.last_steer_sequence = None
                self._defer_inflight_steer_events(ctx)
            ctx.last_activity = time.time()
            return ctx

    def new_conversation(self, route_key: str, system_prompt: str) -> SessionContext:
        """/new:仅在会话空闲时切换 UUID,保留命令后的 pending。"""
        ctx = self._contexts.get(route_key)
        if ctx is not None and ctx.busy:
            raise RuntimeError("cannot switch a busy conversation")
        new_id = str(uuid.uuid4())
        # 先落库再切换内存状态,避免写入失败时两边指向不同会话。
        self._save_conversation_id(route_key, new_id)
        if ctx is None:
            ctx = SessionContext(
                route_key=route_key,
                conversation_id=new_id,
                system_prompt=system_prompt,
            )
            self._contexts[route_key] = ctx
        else:
            # /new 本身也是失效边界。即使共享取消标志随后清零,任何意外
            # 残留的旧 worker 仍会因 generation 不匹配而保持失效。
            ctx.invalidation_event.set()
            self._close_active_steer_mailbox(ctx)
            ctx.generation += 1
            ctx.conversation_id = new_id
            ctx.system_prompt = system_prompt
            ctx.cancel_requested = False
            ctx.cancel_generation = None
            ctx.cancel_reason = None
            ctx.invalidation_event = asyncio.Event()
            ctx.last_steer_sequence = None
            self._defer_inflight_steer_events(ctx)
        ctx.last_activity = time.time()
        return ctx

    async def new_conversation_async(
        self,
        route_key: str,
        system_prompt: str,
    ) -> SessionContext:
        """先异步持久化新会话映射，再切换内存上下文。"""
        lock = self._context_locks.setdefault(route_key, asyncio.Lock())
        async with lock:
            ctx = self._contexts.get(route_key)
            if ctx is not None and ctx.busy:
                raise RuntimeError("cannot switch a busy conversation")
            new_id = str(uuid.uuid4())
            if self.db_path and self.persistence is not None:
                from hermes.db import set_gateway_conversation_id

                await self.persistence.call(
                    set_gateway_conversation_id,
                    route_key,
                    new_id,
                )
            if ctx is None:
                ctx = SessionContext(
                    route_key=route_key,
                    conversation_id=new_id,
                    system_prompt=system_prompt,
                )
                self._contexts[route_key] = ctx
            else:
                ctx.invalidation_event.set()
                self._close_active_steer_mailbox(ctx)
                ctx.generation += 1
                ctx.conversation_id = new_id
                ctx.system_prompt = system_prompt
                ctx.cancel_requested = False
                ctx.cancel_generation = None
                ctx.cancel_reason = None
                ctx.invalidation_event = asyncio.Event()
                ctx.last_steer_sequence = None
                self._defer_inflight_steer_events(ctx)
            ctx.last_activity = time.time()
            return ctx

    def get(self, route_key: str) -> SessionContext | None:
        """读取现有运行期上下文，不创建会话或刷新活跃时间。"""
        return self._contexts.get(route_key)

    @staticmethod
    def save_conversation_list_mapping(
        ctx: SessionContext,
        mapping: dict[int, str],
    ) -> None:
        """保存本次列表展示的完整会话 ID，供后续命令精确定位。"""
        ctx.conversation_list_mapping = dict(mapping)

    @staticmethod
    def get_conversation_list_mapping(
        ctx: SessionContext,
    ) -> dict[int, str] | None:
        """读取最近一次列表映射，避免调用方意外修改运行期状态。"""
        if ctx.conversation_list_mapping is None:
            return None
        return dict(ctx.conversation_list_mapping)

    @staticmethod
    def clear_conversation_list_mapping(ctx: SessionContext) -> None:
        """清除已失效的列表映射。"""
        ctx.conversation_list_mapping = None

    def begin_task(self, ctx: SessionContext) -> tuple[int, asyncio.Event]:
        """开始一个串行任务并返回其不可变的世代与失效事件。"""
        self._close_active_steer_mailbox(ctx)
        ctx.generation += 1
        ctx.cancel_requested = False
        ctx.cancel_generation = None
        ctx.cancel_reason = None
        ctx.invalidation_event = asyncio.Event()
        ctx.busy = True
        ctx.active_generation = None
        ctx.last_steer_sequence = None
        self._defer_inflight_steer_events(ctx)
        return ctx.generation, ctx.invalidation_event

    def register_steer_mailbox(
        self,
        ctx: SessionContext,
        generation: int,
        mailbox: SteerMailbox,
    ) -> bool:
        """绑定当前 generation 的 mailbox，拒绝绑定到失效任务。"""
        if not ctx.busy or ctx.generation != generation:
            mailbox.close()
            return False
        if ctx.active_generation not in (None, generation):
            mailbox.close()
            return False
        if ctx.active_steer_mailbox is not None:
            self._close_active_steer_mailbox(ctx)
        ctx.active_generation = generation
        ctx.active_steer_mailbox = mailbox
        ctx.steer_generation = generation
        ctx.last_steer_sequence = None
        return True

    def clear_steer_mailbox(
        self,
        ctx: SessionContext,
        generation: int | None = None,
    ) -> None:
        """关闭当前 mailbox，并只清除属于指定 generation 的绑定。"""
        if generation is not None and ctx.steer_generation != generation:
            return
        self._close_active_steer_mailbox(ctx)

    def submit_steer(self, route_key: str, entry, event=None) -> bool:
        """在短 route 临界区内向当前 generation 提交 steer。"""
        ctx = self._contexts.get(route_key)
        if ctx is None or not ctx.busy:
            return False
        mailbox = ctx.active_steer_mailbox
        generation = ctx.steer_generation
        if (
            mailbox is None
            or generation is None
            or generation != ctx.generation
            or generation != ctx.active_generation
            or ctx.cancel_requested
            or not mailbox.is_active
        ):
            return False
        if not mailbox.submit(entry):
            return False
        ctx.last_steer_sequence = entry.sequence
        if event is not None:
            ctx.inflight_steer_events[entry.steer_id] = event
            ctx.inflight_steer_generations[entry.steer_id] = generation
        return True

    @staticmethod
    def track_steer_event(
        ctx: SessionContext,
        generation: int,
        steer_id: str,
        event,
    ) -> bool:
        """记录已进入当前 generation mailbox 的原始事件。"""
        if (
            ctx.generation != generation
            or ctx.steer_generation != generation
            or ctx.active_steer_mailbox is None
        ):
            return False
        ctx.inflight_steer_events[steer_id] = event
        ctx.inflight_steer_generations[steer_id] = generation
        return True

    @staticmethod
    def acknowledge_steer_events(
        ctx: SessionContext,
        generation: int,
        steer_ids,
    ) -> None:
        """仅在外层数据库事务成功提交后移除已确认的原始事件映射。"""
        for steer_id in tuple(steer_ids):
            SessionStore.resolve_steer_event(
                ctx,
                generation,
                steer_id,
            )

    @staticmethod
    def forget_steer_event(
        ctx: SessionContext,
        generation: int,
        steer_id: str,
    ) -> None:
        """移除已经安全放回普通 pending 的 steer 映射。"""
        SessionStore.resolve_steer_event(
            ctx,
            generation,
            steer_id,
        )

    def rollback_task_begin(
        self,
        ctx: SessionContext,
        generation: int,
    ) -> None:
        """回滚 mailbox 注册前的临时运行状态，保留原 Queue 记录。"""
        if ctx.generation != generation:
            return
        self._close_active_steer_mailbox(ctx)
        ctx.generation = max(0, generation - 1)
        ctx.cancel_requested = False
        ctx.cancel_generation = None
        ctx.cancel_reason = None
        ctx.invalidation_event.set()
        ctx.invalidation_event = asyncio.Event()
        ctx.active_task = None
        ctx.active_generation = None
        ctx.delivery_id = None
        ctx.delivery_generation = None
        ctx.worker_task = None
        ctx.worker_generation = None
        ctx.busy = False
        ctx.dispatching = False
        ctx.last_steer_sequence = None
        self._defer_inflight_steer_events(ctx)

    def enqueue(self, ctx: SessionContext, event, *, force: bool = False) -> bool:
        """在单会话上限内入队,队列已满时返回 False。"""
        if not force and len(ctx.pending) >= self.max_pending_messages:
            return False
        self._ensure_pending_sequence(ctx, event)
        ctx.pending.append(event)
        return True

    def enqueue_ordered(
        self,
        ctx: SessionContext,
        event,
        *,
        force: bool = False,
    ) -> bool:
        """按 route 接收顺序插入事件，避免晚到的内部 steer 越过旧消息。"""
        if not force and len(ctx.pending) >= self.max_pending_messages:
            return False
        sequence = self._ensure_pending_sequence(ctx, event)
        insert_at = len(ctx.pending)
        for index, pending_event in enumerate(ctx.pending):
            if self._ensure_pending_sequence(ctx, pending_event) > sequence:
                insert_at = index
                break
        ctx.pending.insert(insert_at, event)
        return True

    def request_cancel(self, route_key: str, reason: str = "user") -> bool:
        """使当前任务失效；仅 shutdown 可以直接取消收尾 worker。"""
        if reason not in {"user", "new", "superseded", "shutdown"}:
            raise ValueError(f"unsupported cancellation reason: {reason}")
        ctx = self._contexts.get(route_key)
        if ctx is None or not ctx.busy:
            return False

        target_generation = (
            ctx.worker_generation
            if ctx.worker_generation is not None
            else ctx.generation
        )
        previous_reason = (
            ctx.cancel_reason
            if ctx.cancel_requested and ctx.cancel_generation == target_generation
            else None
        )
        # 同一旧 worker 可能先后收到多种取消请求。保留 shutdown 的恢复
        # 语义，也保留已经发生的显式放弃；显式原因之间仅允许更强的
        # /new 或 user 覆盖 superseded，避免 /new 后到的新消息降级原因。
        if previous_reason == "shutdown" or reason == "shutdown":
            effective_reason = "shutdown"
        else:
            priority = {"superseded": 1, "user": 2, "new": 3}
            effective_reason = max(
                (previous_reason, reason),
                key=lambda item: priority.get(item, 0),
            )
        ctx.cancel_requested = True
        ctx.cancel_generation = target_generation
        ctx.cancel_reason = effective_reason
        if ctx.active_steer_mailbox is not None:
            ctx.active_steer_mailbox.close()
        if reason != "shutdown" and ctx.generation == target_generation:
            ctx.generation += 1
        ctx.invalidation_event.set()
        if (
            ctx.active_task is not None
            and not ctx.active_task.done()
            and ctx.active_generation in (None, target_generation)
        ):
            ctx.active_task.cancel()
        if (
            reason == "shutdown"
            and ctx.worker_task is not None
            and not ctx.worker_task.done()
            and ctx.worker_generation in (None, target_generation)
        ):
            # 模型完成后 worker 可能仍在等待 outbox 重试。Gateway 关闭时
            # 也要取消该等待,持久状态会在下次启动恢复。
            ctx.worker_task.cancel()
        return True

    def cancel_all(self, reason: str = "shutdown") -> list[asyncio.Task]:
        """取消所有运行中任务,返回可供调用方等待的 Task。"""
        tasks = []
        for route_key, ctx in self._contexts.items():
            if not ctx.busy:
                continue
            self.request_cancel(route_key, reason=reason)
            task = ctx.worker_task or ctx.active_task
            if task is not None:
                tasks.append(task)
        return tasks

    def get_status(self, route_key: str) -> dict | None:
        """/status:返回当前会话状态快照。"""
        ctx = self._contexts.get(route_key)
        if ctx is None:
            return None
        return {
            "route_key": ctx.route_key,
            "conversation_id": ctx.conversation_id,
            "busy": ctx.busy,
            "cancel_requested": ctx.cancel_requested,
            "cancel_reason": ctx.cancel_reason,
            "generation": ctx.generation,
            "active_generation": ctx.active_generation,
            "steer_available": bool(
                ctx.busy
                and ctx.active_steer_mailbox is not None
                and ctx.steer_generation == ctx.active_generation == ctx.generation
                and not ctx.cancel_requested
                and ctx.active_steer_mailbox.is_active
            ),
            "pending_count": len(ctx.pending),
            "pending_limit": self.max_pending_messages,
            "last_activity": ctx.last_activity,
            "idle_seconds": time.time() - ctx.last_activity,
        }

    def cleanup_idle(
        self,
        protected_route_keys: set[str] | None = None,
    ) -> int:
        """清理真正空闲且没有持久投递负担的会话。"""
        return len(self.cleanup_idle_conversations(protected_route_keys))

    @staticmethod
    def _conversation_is_idle(
        route_key: str,
        ctx: SessionContext,
        *,
        protected_route_keys: set[str],
        now: float,
        idle_timeout: float,
    ) -> bool:
        """按现有运行状态判断会话是否已满足最终清理条件。"""

        return (
            route_key not in protected_route_keys
            and not ctx.busy
            and not ctx.pending
            and (
                ctx.worker_task is None
                or ctx.worker_task.done()
            )
            and (
                ctx.active_task is None
                or ctx.active_task.done()
            )
            and (now - ctx.last_activity) > idle_timeout
        )

    def idle_conversation_candidates(
        self,
        protected_route_keys: set[str] | None = None,
    ) -> tuple[tuple[str, str], ...]:
        """只读返回可清理 route 与 conversation，供异步资源编排使用。"""

        now = time.time()
        protected = protected_route_keys or set()
        return tuple(
            (route_key, ctx.conversation_id)
            for route_key, ctx in self._contexts.items()
            if self._conversation_is_idle(
                route_key,
                ctx,
                protected_route_keys=protected,
                now=now,
                idle_timeout=self.idle_timeout,
            )
        )

    def idle_conversation_is_current(
        self,
        route_key: str,
        conversation_id: str,
        protected_route_keys: set[str] | None = None,
    ) -> bool:
        """重新确认候选仍是同一空闲会话，避免清理新近活动的 route。"""

        ctx = self._contexts.get(route_key)
        if ctx is None or ctx.conversation_id != conversation_id:
            return False
        return self._conversation_is_idle(
            route_key,
            ctx,
            protected_route_keys=protected_route_keys or set(),
            now=time.time(),
            idle_timeout=self.idle_timeout,
        )

    def remove_idle_conversation(
        self,
        route_key: str,
        conversation_id: str,
        protected_route_keys: set[str] | None = None,
    ) -> bool:
        """仅在资源清理成功后移除仍保持空闲的同一会话。"""

        if not self.idle_conversation_is_current(
            route_key,
            conversation_id,
            protected_route_keys,
        ):
            return False
        ctx = self._contexts[route_key]
        self._close_active_steer_mailbox(ctx)
        del self._contexts[route_key]
        self._context_locks.pop(route_key, None)
        return True

    def cleanup_idle_conversations(
        self,
        protected_route_keys: set[str] | None = None,
    ) -> list[str]:
        """清理空闲 route，并返回需要释放授权的 conversation IDs。"""
        conversation_ids: list[str] = []
        for route_key, conversation_id in self.idle_conversation_candidates(
            protected_route_keys,
        ):
            if self.remove_idle_conversation(
                route_key,
                conversation_id,
                protected_route_keys,
            ):
                conversation_ids.append(conversation_id)
        return conversation_ids
