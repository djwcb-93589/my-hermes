"""Gateway runtime lease 的状态与续约边界。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable

from hermes.db import (
    acquire_gateway_runtime_lease,
    release_gateway_runtime_lease,
    renew_gateway_runtime_lease,
)
from hermes.gateway.persistence import GatewayPersistence


class GatewayRuntimeLease:
    """集中管理一个 Gateway 实例的 runtime lease 生命周期。"""

    def __init__(
        self,
        persistence: GatewayPersistence,
        *,
        lease_name: str,
        ttl_seconds: float,
        heartbeat_seconds: float,
        on_lost: Callable[[str | None], None],
    ):
        self._persistence = persistence
        self._lease_name = str(lease_name)
        self._ttl_seconds = float(ttl_seconds)
        self._heartbeat_seconds = float(heartbeat_seconds)
        self._on_lost = on_lost
        self._instance_id = str(uuid.uuid4())
        self._epoch: int | None = None
        self._acquired = False
        self._valid = False
        self._heartbeat_task: asyncio.Task | None = None

    @property
    def name(self) -> str:
        return self._lease_name

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def epoch(self) -> int | None:
        return self._epoch

    @property
    def acquired(self) -> bool:
        return self._acquired

    @property
    def valid(self) -> bool:
        return self._valid

    def fence(self) -> dict:
        """返回持有 lease 时可用于数据库 fencing 的稳定身份。"""
        if not self._acquired:
            return {}
        if self._epoch is None:
            raise RuntimeError("gateway runtime lease epoch is unavailable")
        return {
            "lease_name": self._lease_name,
            "instance_id": self._instance_id,
            "lease_epoch": self._epoch,
        }

    def valid_fence(self) -> dict | None:
        """仅在本地 lease 仍有效时允许领取新工作。"""
        if not self._acquired or not self._valid or self._epoch is None:
            return None
        return self.fence()

    def blocks_delivery(self) -> bool:
        """正式运行后失去 lease 时禁止开始新的投递。"""
        return self._acquired and not self._valid

    async def acquire(self) -> dict | None:
        """原子争用 lease，并只接受当前实例的领取结果。"""
        acquired = await self._persistence.call(
            acquire_gateway_runtime_lease,
            self._lease_name,
            self._instance_id,
            self._ttl_seconds,
        )
        if not acquired:
            return None
        if str(acquired["instance_id"]) != self._instance_id:
            raise RuntimeError("gateway runtime lease identity mismatch")
        self._epoch = int(acquired["lease_epoch"])
        self._acquired = True
        self._valid = True
        return acquired

    def revoke(self) -> None:
        """撤销本地资格，不触发失租回调。"""
        self._valid = False

    def lose(self, error_type: str | None = None) -> None:
        """只通知一次失租，由宿主负责停止外部工作。"""
        if not self._valid:
            return
        self._valid = False
        self._on_lost(error_type)

    def start_heartbeat(self) -> None:
        """在宿主确认启动成功前不自动续约。"""
        if not self._valid:
            return
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(),
                name="gateway-runtime-lease-heartbeat",
            )

    async def stop_heartbeat(self) -> None:
        """停止续约循环；不会释放数据库中的 lease。"""
        task = self._heartbeat_task
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def release(self) -> bool:
        """只释放当前实例持有的 fencing epoch，绝不删除其他实例 lease。"""
        if not self._acquired or self._epoch is None:
            return False
        try:
            return bool(await self._persistence.call(
                release_gateway_runtime_lease,
                self._lease_name,
                self._instance_id,
                self._epoch,
            ))
        finally:
            self._acquired = False
            self._valid = False
            self._epoch = None

    async def _heartbeat_loop(self) -> None:
        """续租异常或所有权变化时统一通知宿主进入安全停止流程。"""
        try:
            while self._valid:
                await asyncio.sleep(self._heartbeat_seconds)
                if not self._valid or self._epoch is None:
                    return
                try:
                    renewed = await self._persistence.call(
                        renew_gateway_runtime_lease,
                        self._lease_name,
                        self._instance_id,
                        self._epoch,
                        self._ttl_seconds,
                    )
                except Exception as exc:
                    self.lose(type(exc).__name__)
                    return
                if not renewed:
                    self.lose()
                    return
        except asyncio.CancelledError:
            raise
