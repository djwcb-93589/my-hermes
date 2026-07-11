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

    def __init__(self, idle_timeout: float = 86400):
        self._contexts: dict[str, SessionContext] = {}
        self.idle_timeout = idle_timeout

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
                conversation_id=route_key,  # 默认 route_key 作 DB session_id
                system_prompt=system_prompt,
            )
            self._contexts[route_key] = ctx
        ctx.last_activity = time.time()
        return ctx

    def new_conversation(self, route_key: str, system_prompt: str) -> SessionContext:
        """/new:用新 UUID 重置 conversation_id,清空 pending。"""
        ctx = self._contexts.get(route_key)
        new_id = str(uuid.uuid4())
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
            ctx.pending.clear()
            ctx.cancel_requested = False
            ctx.busy = False
        ctx.last_activity = time.time()
        return ctx

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
