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
    # 取消信号:AgentLoop.run 内通过 cancel_checker 检查
    cancel_requested: bool = False
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
            ctx.conversation_id = new_id
            ctx.system_prompt = system_prompt
            ctx.cancel_requested = False
        ctx.last_activity = time.time()
        return ctx

    def enqueue(self, ctx: SessionContext, event) -> bool:
        """在单会话上限内入队,队列已满时返回 False。"""
        if len(ctx.pending) >= self.max_pending_messages:
            return False
        ctx.pending.append(event)
        return True

    def request_cancel(self, route_key: str) -> bool:
        """/stop:标记取消当前任务。"""
        ctx = self._contexts.get(route_key)
        if ctx is None or not ctx.busy:
            return False
        ctx.cancel_requested = True
        return True

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
