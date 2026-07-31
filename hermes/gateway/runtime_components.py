"""Gateway 内部长期组件的 Runtime 状态装配与生命周期编排。"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import cast

from hermes.observability.runtime import (
    AsyncRuntimeHeartbeat,
    RuntimeComponentProbe,
    RuntimeComponentReporter,
    RuntimeHeartbeatPolicy,
    RuntimeStatusPublisher,
)


logger = logging.getLogger(__name__)

_COMPONENT_ID = "gateway"
_HEARTBEAT_POLICY = RuntimeHeartbeatPolicy(
    heartbeat_interval_seconds=10.0,
    stale_after_seconds=30.0,
)
_SHUTDOWN_INCOMPLETE_ERROR = "RuntimeShutdownIncomplete"


class GatewayRuntimeComponents:
    """在 Gateway 生命周期内统一持有 Reporter 与异步 Heartbeat。"""

    def __init__(
        self,
        *,
        publisher: RuntimeStatusPublisher,
        probes: Mapping[str, RuntimeComponentProbe],
    ) -> None:
        self._reporters: dict[str, RuntimeComponentReporter] = {}
        self._heartbeats: dict[str, AsyncRuntimeHeartbeat] = {}
        for component_type, probe in probes.items():
            reporter = RuntimeComponentReporter(
                component_type=component_type,
                component_id=_COMPONENT_ID,
                instance_id=uuid.uuid4().hex,
                publisher=publisher,
                metadata={"environment": "gateway"},
                heartbeat_policy=_HEARTBEAT_POLICY,
            )
            self._reporters[component_type] = reporter
            self._heartbeats[component_type] = AsyncRuntimeHeartbeat(
                reporter=reporter,
                policy=_HEARTBEAT_POLICY,
                probe=probe,
            )
        self._started = False
        self._heartbeats_started = False
        self._terminal = False

    def starting(self) -> None:
        """取得 Gateway lease 后，为全部内部组件发布 starting。"""
        if self._started or self._terminal:
            return
        self._started = True
        for component_type, reporter in self._reporters.items():
            try:
                reporter.starting()
            except Exception as exc:
                self._log_failure(component_type, "starting", exc)

    def start_heartbeats(self) -> None:
        """实际组件可服务后启动同事件循环内的 Heartbeat。"""
        if not self._started or self._heartbeats_started or self._terminal:
            return
        started: list[tuple[str, AsyncRuntimeHeartbeat]] = []
        for component_type, heartbeat in self._heartbeats.items():
            try:
                heartbeat.start()
            except Exception as exc:
                self._log_failure(component_type, "heartbeat_start", exc)
                continue
            started.append((component_type, heartbeat))
        self._heartbeats_started = bool(started)

    async def stop_heartbeats(self) -> None:
        """可靠取消所有已创建的 Heartbeat Task。"""
        if not self._heartbeats_started:
            return
        for component_type, heartbeat in reversed(
            tuple(self._heartbeats.items())
        ):
            try:
                await heartbeat.stop()
            except Exception as exc:
                self._log_failure(component_type, "heartbeat_stop", exc)
        self._heartbeats_started = False

    def stopping(self) -> None:
        """Heartbeat 停止后发布 stopping。"""
        if not self._started or self._terminal:
            return
        for component_type, reporter in self._reporters.items():
            try:
                reporter.stopping()
            except Exception as exc:
                self._log_failure(component_type, "stopping", exc)

    def complete(self, shutdown_results: Mapping[str, bool]) -> None:
        """按实际资源清理结果分别发布 stopped 或 failed。"""
        if not self._started or self._terminal:
            return
        for component_type, reporter in self._reporters.items():
            try:
                if shutdown_results.get(component_type) is True:
                    reporter.stopped()
                else:
                    reporter.failed(_SHUTDOWN_INCOMPLETE_ERROR)
            except Exception as exc:
                self._log_failure(component_type, "shutdown_complete", exc)
        self._terminal = True

    async def fail_startup(self, error_type: str) -> None:
        """启动失败时停止 Heartbeat，并为已发布组件进入 failed 终态。"""
        if not self._started or self._terminal:
            return
        await self.stop_heartbeats()
        for component_type, reporter in self._reporters.items():
            try:
                reporter.failed(error_type)
            except Exception as exc:
                self._log_failure(component_type, "startup_failed", exc)
        self._terminal = True

    @staticmethod
    def _log_failure(
        component_type: str,
        publish_stage: str,
        error: BaseException,
    ) -> None:
        """只记录稳定阶段和异常类型，不泄露组件身份或异常正文。"""
        logger.warning(
            "Runtime component lifecycle failed: component_type=%s "
            "publish_stage=%s exception_type=%s",
            component_type,
            publish_stage,
            type(error).__name__,
        )


class NullGatewayRuntimeComponents:
    """未配置 Publisher 时不创建 Reporter、线程或 asyncio Task。"""

    __slots__ = ()

    def starting(self) -> None:
        return None

    def start_heartbeats(self) -> None:
        return None

    async def stop_heartbeats(self) -> None:
        return None

    def stopping(self) -> None:
        return None

    def complete(self, shutdown_results: Mapping[str, bool]) -> None:
        del shutdown_results

    async def fail_startup(self, error_type: str) -> None:
        del error_type


def build_gateway_runtime_components(
    *,
    publisher: RuntimeStatusPublisher | None,
    cron_probe: RuntimeComponentProbe | None = None,
    process_probe: RuntimeComponentProbe | None = None,
    delegate_probe: RuntimeComponentProbe | None = None,
    background_review_probe: RuntimeComponentProbe | None = None,
) -> GatewayRuntimeComponents | NullGatewayRuntimeComponents:
    """只在 composition root 注入 Publisher 时装配 Gateway 内部组件。"""
    if publisher is None:
        return NullGatewayRuntimeComponents()
    if not all(
        callable(probe)
        for probe in (
            cron_probe,
            process_probe,
            delegate_probe,
            background_review_probe,
        )
    ):
        raise TypeError("Gateway runtime component probes must be callable")
    return GatewayRuntimeComponents(
        publisher=publisher,
        probes={
            "cron_scheduler": cast(RuntimeComponentProbe, cron_probe),
            "process_manager": cast(RuntimeComponentProbe, process_probe),
            "delegate_manager": cast(RuntimeComponentProbe, delegate_probe),
            "background_review": cast(
                RuntimeComponentProbe,
                background_review_probe,
            ),
        },
    )
