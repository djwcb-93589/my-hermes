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


class SessionStore:
    """进程内会话状态管理器。线程安全(asyncio 单线程,锁保护 dict 操作)。"""

    def __init__(
        self,
        idle_timeout: float = 86400,
        db_path: str | None = None,
        max_pending_messages: int = 20,
    ):
        self._contexts: dict[str, SessionContext] = {}
        self.idle_timeout = idle_timeout
        self.db_path = db_path
        self.max_pending_messages = max(1, int(max_pending_messages))

    def _load_conversation_id(self, route_key: str) -> str:
        """从数据库恢复 route_key 当前会话;没有映射时保持旧行为。"""
        if not self.db_path:
            return route_key

        from hermes.db import get_gateway_conversation_id, init_db

        conn = init_db(self.db_path)
        try:
            return get_gateway_conversation_id(conn, route_key) or route_key
        finally:
            conn.close()

    def _save_conversation_id(
        self,
        route_key: str,
        conversation_id: str,
    ) -> None:
        """持久化 /new 产生的新会话映射。"""
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
        """取现有 context 或新建。"""
        ctx = self._contexts.get(route_key)
        if ctx is None:
            ctx = SessionContext(
                route_key=route_key,
                conversation_id=self._load_conversation_id(route_key),
                system_prompt=system_prompt,
            )
            self._contexts[route_key] = ctx
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
            ctx.generation += 1
            ctx.conversation_id = new_id
            ctx.system_prompt = system_prompt
            ctx.cancel_requested = False
            ctx.cancel_generation = None
            ctx.cancel_reason = None
            ctx.invalidation_event = asyncio.Event()
        ctx.last_activity = time.time()
        return ctx

    def get(self, route_key: str) -> SessionContext | None:
        """读取现有运行期上下文，不创建会话或刷新活跃时间。"""
        return self._contexts.get(route_key)

    def begin_task(self, ctx: SessionContext) -> tuple[int, asyncio.Event]:
        """开始一个串行任务并返回其不可变的世代与失效事件。"""
        ctx.generation += 1
        ctx.cancel_requested = False
        ctx.cancel_generation = None
        ctx.cancel_reason = None
        ctx.invalidation_event = asyncio.Event()
        ctx.busy = True
        return ctx.generation, ctx.invalidation_event

    def enqueue(self, ctx: SessionContext, event) -> bool:
        """在单会话上限内入队,队列已满时返回 False。"""
        if len(ctx.pending) >= self.max_pending_messages:
            return False
        ctx.pending.append(event)
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
            effective_reason = previous_reason or reason
        else:
            priority = {"superseded": 1, "user": 2, "new": 3}
            effective_reason = max(
                (previous_reason, reason),
                key=lambda item: priority.get(item, 0),
            )
        ctx.cancel_requested = True
        ctx.cancel_generation = target_generation
        ctx.cancel_reason = effective_reason
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
            "pending_count": len(ctx.pending),
            "pending_limit": self.max_pending_messages,
            "last_activity": ctx.last_activity,
            "idle_seconds": time.time() - ctx.last_activity,
        }

    def cleanup_idle(self) -> int:
        """清理超时会话,返回清理数量。"""
        now = time.time()
        expired = [
            k for k, ctx in self._contexts.items()
            if not ctx.busy and (now - ctx.last_activity) > self.idle_timeout
        ]
        for k in expired:
            del self._contexts[k]
        return len(expired)
