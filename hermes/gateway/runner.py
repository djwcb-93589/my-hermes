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
import math
import os
import random
import time
import uuid

from hermes.db import (
    acquire_gateway_runtime_lease,
    add_final_message_with_gateway_outbox,
    add_messages,
    cancel_gateway_delivery,
    complete_gateway_delivery,
    complete_gateway_message,
    delete_gateway_messages,
    enqueue_gateway_outbox,
    enqueue_gateway_message,
    fail_gateway_delivery,
    get_gateway_message_persistence_state,
    get_gateway_routes_with_pending_outbox,
    get_next_recoverable_gateway_outbox_for_route,
    get_gateway_outbox,
    get_gateway_queued_messages,
    get_recoverable_gateway_outbox,
    init_db,
    mark_gateway_message_delivery_failed,
    mark_gateway_message_processing,
    mark_gateway_outbox_chunk_sent,
    mark_gateway_outbox_retry,
    mark_gateway_outbox_sending,
    reconcile_gateway_terminal_deliveries,
    release_gateway_runtime_lease,
    renew_gateway_runtime_lease,
    reset_gateway_processing_messages,
    reset_gateway_sending_outbox,
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


_GATEWAY_CONTEXT_DEFAULTS = {
    "include_soul": True,
    "include_memory": True,
    "include_user_profile": True,
    "include_project_context": False,
}
_GATEWAY_RUNTIME_LEASE_NAME = "gateway-main"


def _load_gateway_context_config(gateway_cfg: dict) -> dict[str, bool]:
    """集中读取并严格校验 Gateway 的只读上下文暴露策略。"""
    context_cfg = gateway_cfg.get("context", {})
    if not isinstance(context_cfg, dict):
        raise ValueError("gateway.context must be a mapping")

    selected: dict[str, bool] = {}
    for name, default in _GATEWAY_CONTEXT_DEFAULTS.items():
        value = context_cfg.get(name, default)
        if not isinstance(value, bool):
            raise ValueError(f"gateway.context.{name} must be a boolean")
        selected[name] = value
    return selected


def _load_positive_seconds(
    gateway_cfg: dict,
    name: str,
    default: float,
) -> float:
    """读取正数秒配置；拒绝布尔值、非数字和非有限值。"""
    value = gateway_cfg.get(name, default)
    if isinstance(value, bool):
        raise ValueError(f"gateway.{name} must be a positive number")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"gateway.{name} must be a positive number"
        ) from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"gateway.{name} must be a positive number")
    return seconds


class GatewayRunner:
    """启动 adapter、路由消息、跑 agent、回发结果。"""

    def __init__(self, config: dict, db_path: str):
        self.config = config
        self.db_path = db_path
        self.adapters: dict[str, BasePlatformAdapter] = {}
        gateway_cfg = config.get("gateway", {})
        if not isinstance(gateway_cfg, dict):
            raise ValueError("gateway must be a mapping")
        self._gateway_context = _load_gateway_context_config(gateway_cfg)
        self.runtime_lease_ttl_seconds = _load_positive_seconds(
            gateway_cfg,
            "runtime_lease_ttl_seconds",
            30.0,
        )
        self.runtime_lease_heartbeat_seconds = _load_positive_seconds(
            gateway_cfg,
            "runtime_lease_heartbeat_seconds",
            10.0,
        )
        if (
            self.runtime_lease_ttl_seconds
            <= self.runtime_lease_heartbeat_seconds * 2
        ):
            raise ValueError(
                "gateway.runtime_lease_ttl_seconds must be greater than "
                "twice gateway.runtime_lease_heartbeat_seconds"
            )
        self.session_cleanup_interval_seconds = _load_positive_seconds(
            gateway_cfg,
            "session_cleanup_interval_seconds",
            600.0,
        )
        self.agent_name = gateway_cfg.get("agent_name", "main")
        idle_timeout = gateway_cfg.get("session_idle_timeout", 86400)
        max_pending = gateway_cfg.get("max_pending_messages", 20)
        max_concurrent = gateway_cfg.get("max_concurrent_llm_requests", 4)
        self.sessions = SessionStore(
            idle_timeout=idle_timeout,
            db_path=db_path,
            max_pending_messages=max_pending,
        )
        self.max_concurrent_llm_requests = max(1, int(max_concurrent))
        self.delivery_max_attempts = max(
            1,
            int(gateway_cfg.get("delivery_max_attempts", 20)),
        )
        self.delivery_retry_base_delay = max(
            0.1,
            float(gateway_cfg.get("delivery_retry_base_delay", 2.0)),
        )
        self.delivery_retry_max_delay = max(
            self.delivery_retry_base_delay,
            float(gateway_cfg.get("delivery_retry_max_delay", 60.0)),
        )
        self.delivery_retry_jitter_ratio = min(
            1.0,
            max(
                0.0,
                float(gateway_cfg.get("delivery_retry_jitter_ratio", 0.2)),
            ),
        )
        self.queue_full_reply_max_attempts = max(
            1,
            int(gateway_cfg.get("queue_full_reply_max_attempts", 3)),
        )
        self._llm_semaphore = asyncio.Semaphore(
            self.max_concurrent_llm_requests
        )
        self._accepted_messages: set[tuple[str, str]] = set()
        self._startup_message_states: dict[tuple[str, str], dict] = {}
        self._adapter_initialized: dict[str, bool] = {}
        self._inbox_restored_adapters: set[str] = set()
        self._receiving_adapters: set[str] = set()
        self._startup_in_progress = False
        self._accepting_external_messages = True
        self._lifecycle_phase = "created"
        self._runtime_lease_name = _GATEWAY_RUNTIME_LEASE_NAME
        self._runtime_instance_id = str(uuid.uuid4())
        self._runtime_lease_acquired = False
        self._runtime_lease_valid = False
        self._lease_heartbeat_task: asyncio.Task | None = None
        self._session_cleanup_task: asyncio.Task | None = None
        self._lease_shutdown_task: asyncio.Task | None = None
        self._stop_lock = asyncio.Lock()
        # 异步模型客户端按需创建,Gateway 停止时统一关闭。
        self._async_client = None

    def _build_gateway_prompt(self) -> str:
        """按统一暴露策略构造无本地工具能力的 Gateway prompt。"""
        return build_system_prompt(
            os.getcwd(),
            enabled_toolsets=[],
            **self._gateway_context,
        )

    def add_adapter(self, adapter: BasePlatformAdapter):
        adapter._on_message = self._handle_message
        adapter._message_state_lookup = self._message_persistence_state
        self.adapters[adapter.platform_name] = adapter

    def _reconcile_terminal_deliveries(self) -> int:
        """在 Gateway 恢复前一次性收敛旧终态记录。"""
        conn = init_db(self.db_path)
        try:
            return reconcile_gateway_terminal_deliveries(conn)
        finally:
            conn.close()

    def _acquire_runtime_lease(self) -> bool:
        """在独立连接中争用当前数据库的 Gateway 单实例租约。"""
        conn = init_db(self.db_path)
        try:
            return acquire_gateway_runtime_lease(
                conn,
                self._runtime_lease_name,
                self._runtime_instance_id,
                self.runtime_lease_ttl_seconds,
            )
        finally:
            conn.close()

    def _renew_runtime_lease(self) -> bool:
        """仅为当前实例持有的运行租约续期。"""
        conn = init_db(self.db_path)
        try:
            return renew_gateway_runtime_lease(
                conn,
                self._runtime_lease_name,
                self._runtime_instance_id,
                self.runtime_lease_ttl_seconds,
            )
        finally:
            conn.close()

    def _release_runtime_lease(self) -> bool:
        """释放当前实例的租约；实例不匹配时不会删除其他持有者。"""
        conn = init_db(self.db_path)
        try:
            return release_gateway_runtime_lease(
                conn,
                self._runtime_lease_name,
                self._runtime_instance_id,
            )
        finally:
            conn.close()

    def _pending_outbox_route_keys(self) -> set[str]:
        """读取仍由持久 Outbox 管理、不能清理内存会话的 route。"""
        conn = init_db(self.db_path)
        try:
            return get_gateway_routes_with_pending_outbox(conn)
        finally:
            conn.close()

    def _runtime_lease_blocks_delivery(self) -> bool:
        """嵌入式私有调用保持兼容；正式启动后失租必须阻止投递。"""
        return self._runtime_lease_acquired and not self._runtime_lease_valid

    def _handle_runtime_lease_loss(self, error_type: str | None) -> None:
        """先撤销运行资格，再调度不会自等待的统一安全停止。"""
        if not self._runtime_lease_valid:
            return
        self._runtime_lease_valid = False
        self._accepting_external_messages = False
        self._lifecycle_phase = "lease_lost"
        if error_type:
            print(
                "  [gateway] runtime lease renewal failed: "
                f"{error_type}"
            )
        else:
            print("  [gateway] runtime lease ownership lost")

        # 失租等同 shutdown：保留可恢复 Outbox，不把它误标为用户取消。
        self.sessions.cancel_all(reason="shutdown")
        if (
            self._lease_shutdown_task is None
            or self._lease_shutdown_task.done()
        ):
            self._lease_shutdown_task = asyncio.create_task(
                self.stop(),
                name="gateway-lease-loss-shutdown",
            )

    async def _runtime_lease_heartbeat_loop(self) -> None:
        """周期续租；任何续租异常或所有权丢失都进入安全停止。"""
        try:
            while self._runtime_lease_valid:
                await asyncio.sleep(self.runtime_lease_heartbeat_seconds)
                if not self._runtime_lease_valid:
                    return
                try:
                    renewed = await asyncio.to_thread(
                        self._renew_runtime_lease
                    )
                except Exception as exc:
                    self._handle_runtime_lease_loss(type(exc).__name__)
                    return
                if not renewed:
                    self._handle_runtime_lease_loss(None)
                    return
        except asyncio.CancelledError:
            raise

    async def _session_cleanup_loop(self) -> None:
        """周期清理没有运行、排队或持久投递负担的空闲会话。"""
        try:
            while self._lifecycle_phase == "running":
                await asyncio.sleep(self.session_cleanup_interval_seconds)
                if self._lifecycle_phase != "running":
                    return
                try:
                    protected = await asyncio.to_thread(
                        self._pending_outbox_route_keys
                    )
                    removed = self.sessions.cleanup_idle(protected)
                except Exception as exc:
                    print(
                        "  [gateway] session cleanup failed: "
                        f"{type(exc).__name__}"
                    )
                    continue
                if removed:
                    print(
                        "  [gateway] idle sessions cleaned: "
                        f"{removed}"
                    )
        except asyncio.CancelledError:
            raise

    def _start_background_tasks(self) -> None:
        """Runner 成功运行后统一创建长期后台任务。"""
        self._lease_heartbeat_task = asyncio.create_task(
            self._runtime_lease_heartbeat_loop(),
            name="gateway-runtime-lease-heartbeat",
        )
        self._session_cleanup_task = asyncio.create_task(
            self._session_cleanup_loop(),
            name="gateway-session-cleanup",
        )

    async def _cancel_background_tasks(self) -> None:
        """取消并回收长期后台任务，避免事件循环退出时残留 Task。"""
        current = asyncio.current_task()
        tasks = [
            task
            for task in (
                self._lease_heartbeat_task,
                self._session_cleanup_task,
            )
            if task is not None and task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._lease_heartbeat_task is not current:
            self._lease_heartbeat_task = None
        if self._session_cleanup_task is not current:
            self._session_cleanup_task = None

    async def _abort_startup_after_lease(self) -> None:
        """启动恢复失败时停止已创建资源并尽早交还租约。"""
        self._accepting_external_messages = False
        self._runtime_lease_valid = False
        active_tasks = self.sessions.cancel_all(reason="shutdown")
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        for adapter in self.adapters.values():
            try:
                await adapter.disconnect()
            except Exception:
                pass
        if self._runtime_lease_acquired:
            try:
                await asyncio.to_thread(self._release_runtime_lease)
            except Exception as exc:
                print(
                    "  [gateway] runtime lease release failed: "
                    f"{type(exc).__name__}"
                )
            finally:
                self._runtime_lease_acquired = False

    async def start(self):
        """按初始化、终态收敛、持久恢复、接收阶段启动 Gateway。"""
        if self._startup_in_progress or self._lifecycle_phase == "running":
            raise RuntimeError("gateway runner is already starting or running")
        self._startup_in_progress = True
        self._accepting_external_messages = False
        self._inbox_restored_adapters.clear()
        self._receiving_adapters.clear()
        self._startup_message_states.clear()

        self._lifecycle_phase = "acquire_runtime_lease"
        try:
            acquired = await asyncio.to_thread(self._acquire_runtime_lease)
        except Exception as exc:
            self._lifecycle_phase = "startup_failed"
            self._startup_in_progress = False
            print(
                "  [gateway] runtime lease acquisition failed: "
                f"{type(exc).__name__}"
            )
            raise
        if not acquired:
            self._lifecycle_phase = "startup_failed"
            self._startup_in_progress = False
            print(
                "  [gateway] startup blocked: another active Gateway "
                "instance holds the runtime lease"
            )
            raise RuntimeError(
                "another active Gateway instance holds the runtime lease"
            )
        self._runtime_lease_acquired = True
        self._runtime_lease_valid = True

        self._lifecycle_phase = "adapter_initialize"
        for name, adapter in self.adapters.items():
            self._adapter_initialized[name] = False
            try:
                ok = await adapter.initialize()
                self._adapter_initialized[name] = bool(ok)
                if ok:
                    print(f"  [gateway] {name} initialized")
                else:
                    print(f"  [gateway] {name} FAILED to initialize")
            except Exception as exc:
                print(
                    f"  [gateway] {name} initialization failed: "
                    f"{type(exc).__name__}"
                )

        self._lifecycle_phase = "gateway_terminal_reconcile"
        try:
            self._reconcile_terminal_deliveries()
        except Exception as exc:
            # 终态无法收敛时不能继续恢复，也不能开放外部入口。
            print(
                "  [gateway] terminal delivery reconciliation failed: "
                f"{type(exc).__name__}"
            )
            await self._abort_startup_after_lease()
            self._lifecycle_phase = "startup_failed"
            self._startup_in_progress = False
            raise

        try:
            self._lifecycle_phase = "gateway_outbox_restore"
            await self._restore_outbound_messages()
            self._lifecycle_phase = "gateway_queue_restore"
            await self._restore_queued_messages()
        except Exception:
            # Gateway 自身持久状态无法完成恢复时不能开放外部入口。
            await self._abort_startup_after_lease()
            self._lifecycle_phase = "startup_failed"
            self._startup_in_progress = False
            raise

        # 此时 Gateway 两层状态已经恢复。Adapter Inbox 可以通过同一个
        # Runner 回调提交，但外部监听器仍未启动，不会混入实时事件。
        self._lifecycle_phase = "adapter_inbox_restore"
        self._accepting_external_messages = True
        for name, adapter in self.adapters.items():
            if not self._adapter_initialized.get(name, False):
                continue
            try:
                await adapter.restore_pending()
                self._inbox_restored_adapters.add(name)
            except Exception as exc:
                print(
                    f"  [gateway] {name} inbox recovery failed: "
                    f"{type(exc).__name__}"
                )

        self._lifecycle_phase = "start_receiving"
        for name, adapter in self.adapters.items():
            if name not in self._inbox_restored_adapters:
                continue
            try:
                ok = await adapter.start_receiving()
                if ok:
                    self._receiving_adapters.add(name)
                    print(f"  [gateway] {name} receiving")
                else:
                    print(f"  [gateway] {name} FAILED to start receiving")
            except Exception as exc:
                print(
                    f"  [gateway] {name} receive start failed: "
                    f"{type(exc).__name__}"
                )
        self._startup_in_progress = False
        # 飞书 Inbox 已完成去重，此后实时事件重新以数据库和 Adapter 自身
        # completed 记录为准，不让启动快照变成长生命周期内存真相源。
        self._startup_message_states.clear()
        self._lifecycle_phase = "running"
        self._start_background_tasks()

    async def stop(self):
        """取消运行中任务,断开 adapter,关闭模型客户端并清理 backend。"""
        async with self._stop_lock:
            if (
                self._lifecycle_phase == "stopped"
                and not self._runtime_lease_acquired
            ):
                return
            self._lifecycle_phase = "stopping"
            self._accepting_external_messages = False
            self._runtime_lease_valid = False

            # 先停止 heartbeat / housekeeping，再等待 route worker 收尾。
            await self._cancel_background_tasks()
            active_tasks = self.sessions.cancel_all(reason="shutdown")
            if active_tasks:
                await asyncio.gather(*active_tasks, return_exceptions=True)

            for adapter in self.adapters.values():
                try:
                    await adapter.disconnect()
                except Exception:
                    pass

            if self._runtime_lease_acquired:
                try:
                    await asyncio.to_thread(self._release_runtime_lease)
                except Exception as exc:
                    print(
                        "  [gateway] runtime lease release failed: "
                        f"{type(exc).__name__}"
                    )
                finally:
                    self._runtime_lease_acquired = False

            if self._async_client is not None:
                try:
                    await self._async_client.close()
                except Exception:
                    pass
                finally:
                    self._async_client = None
            from hermes.backends import cleanup_all_backends
            cleanup_all_backends()
            self._receiving_adapters.clear()
            self._inbox_restored_adapters.clear()
            self._startup_in_progress = False
            self._lifecycle_phase = "stopped"

    # ----- 消息路由 -----

    def _message_persistence_state(self, event: MessageEvent) -> dict | None:
        """以数据库为准查询平台消息是否已被 Gateway 接受。"""
        route_key = build_session_key(event.source, self.agent_name)
        conn = init_db(self.db_path)
        try:
            persisted = get_gateway_message_persistence_state(
                conn,
                route_key,
                event.message_id,
            )
        finally:
            conn.close()
        if persisted is not None:
            return persisted
        return self._startup_message_states.get((route_key, event.message_id))

    def _remember_startup_message(
        self,
        route_key: str,
        event: MessageEvent,
        *,
        layer: str,
        status: str,
    ) -> None:
        """短暂缓存本次启动从数据库实际读到的消息归属。"""
        state = {"layer": layer, "status": status}
        self._startup_message_states[(route_key, event.message_id)] = state
        source_ids = event.metadata.get("source_message_ids", [])
        if isinstance(source_ids, list):
            for message_id in source_ids:
                self._startup_message_states[
                    (route_key, str(message_id))
                ] = state

    def _adapter_ready_for_recovery(self, platform: str) -> bool:
        """直接调用恢复方法时兼容旧用法；正式 start 则服从初始化结果。"""
        if self._lifecycle_phase == "created":
            # 既有嵌入式调用会直接执行单个恢复方法并注入 _reply；正式启动
            # 由下面的 Adapter 初始化结果约束。
            return True
        if platform not in self.adapters:
            return False
        return self._adapter_initialized.get(platform, True)

    @staticmethod
    def _route_has_active_worker(ctx) -> bool:
        worker = ctx.worker_task
        return bool(ctx.busy or (worker is not None and not worker.done()))

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

    def _persist_event(self, route_key: str, event: MessageEvent) -> bool:
        """消息进入内存 busy / pending 前先持久化。"""
        conn = init_db(self.db_path)
        try:
            accepted = enqueue_gateway_message(
                conn,
                route_key,
                event.message_id,
                self._serialize_event(event),
            )
        finally:
            conn.close()
        if not accepted:
            return False
        self._accepted_messages.add((route_key, event.message_id))
        return True

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

    def _build_outbox(
        self,
        route_key: str,
        event: MessageEvent,
        content: str,
        delivery_id: str,
        delivery_kind: str,
    ) -> dict:
        """构造包含确定分片的 outbox,这里不执行网络请求。"""
        adapter = self.adapters.get(event.source.platform)
        if adapter:
            payloads = adapter.prepare_outbound(
                content,
                delivery_id=delivery_id,
            )
        else:
            payloads = [{"content": content}]
        if not payloads:
            raise ValueError("adapter produced no outbound payload")
        return {
            "id": delivery_id,
            "route_key": route_key,
            "source_message_id": event.message_id,
            "event_json": self._serialize_event(event),
            "platform": event.source.platform,
            "chat_id": event.source.chat_id,
            # 回复当前触发消息;thread_id 决定飞书是否在话题内回复。
            "reply_to_message_id": event.message_id,
            "thread_id": event.source.thread_id,
            "delivery_kind": delivery_kind,
            "payloads": payloads,
        }

    def _enqueue_outbox(self, outbox: dict) -> str:
        conn = init_db(self.db_path)
        try:
            return enqueue_gateway_outbox(conn, outbox)
        finally:
            conn.close()

    def _load_outbox(self, outbox_id: str) -> dict | None:
        conn = init_db(self.db_path)
        try:
            return get_gateway_outbox(conn, outbox_id)
        finally:
            conn.close()

    def _cancel_outbox(
        self,
        outbox_id: str,
        *,
        route_key: str | None = None,
        source_message_id: str | None = None,
    ) -> bool:
        conn = init_db(self.db_path)
        try:
            outbox = get_gateway_outbox(conn, outbox_id)
            if outbox is None:
                return False
            expected_route = route_key or str(outbox["route_key"])
            expected_source = source_message_id or str(
                outbox["source_message_id"]
            )
            cancelled = cancel_gateway_delivery(
                conn,
                outbox_id,
                expected_route,
                expected_source,
            )
            if not cancelled:
                current = get_gateway_outbox(conn, outbox_id)
                cancelled = bool(
                    current
                    and current["status"] in {
                        "cancelled",
                        "partial_cancelled",
                        "delivered",
                    }
                )
        finally:
            conn.close()
        if cancelled:
            self._accepted_messages.discard((expected_route, expected_source))
        return cancelled

    def _complete_outbox(
        self,
        outbox_id: str,
        route_key: str,
        event: MessageEvent,
    ) -> bool:
        conn = init_db(self.db_path)
        try:
            completed = complete_gateway_delivery(
                conn,
                outbox_id,
                route_key,
                event.message_id,
            )
            if not completed:
                current = get_gateway_outbox(conn, outbox_id)
                completed = bool(
                    current and current["status"] == "delivered"
                )
        finally:
            conn.close()
        if completed:
            self._accepted_messages.discard((route_key, event.message_id))
        return completed

    def _fail_outbox(
        self,
        outbox_id: str,
        route_key: str,
        event: MessageEvent,
        error: str,
        error_code: str | None,
    ) -> bool:
        conn = init_db(self.db_path)
        try:
            return fail_gateway_delivery(
                conn,
                outbox_id,
                route_key,
                event.message_id,
                error,
                error_code,
            )
        finally:
            conn.close()

    def _request_session_cancel(self, route_key: str, reason: str) -> bool:
        """失效内存任务，并立即持久化终止当前未完成 Outbox。"""
        ctx = self.sessions.get(route_key)
        delivery_id = ctx.delivery_id if ctx is not None else None
        cancelled = self.sessions.request_cancel(route_key, reason=reason)
        if cancelled and reason != "shutdown" and delivery_id:
            self._cancel_outbox(delivery_id, route_key=route_key)
        return cancelled

    @staticmethod
    def _task_cancel_reason(ctx, generation: int | None) -> str | None:
        """返回某一世代的取消原因；``None`` 表示任务仍有效。"""
        if ctx is None:
            return None
        if generation is None:
            if getattr(ctx, "cancel_requested", False):
                return getattr(ctx, "cancel_reason", None) or "user"
            return None

        current_generation = getattr(ctx, "generation", generation)
        cancel_generation = getattr(ctx, "cancel_generation", None)
        if current_generation != generation:
            if cancel_generation == generation:
                return getattr(ctx, "cancel_reason", None) or "superseded"
            return "superseded"
        if (
            getattr(ctx, "cancel_requested", False)
            and cancel_generation in (None, generation)
        ):
            return getattr(ctx, "cancel_reason", None) or "user"
        return None

    def _cancel_stale_outbox(
        self,
        ctx,
        generation: int | None,
        outbox_id: str,
    ) -> str | None:
        """显式放弃旧任务时原子地终止未完成 Outbox。"""
        reason = self._task_cancel_reason(ctx, generation)
        if reason is not None and reason != "shutdown":
            self._cancel_outbox(
                outbox_id,
                route_key=getattr(ctx, "route_key", None),
            )
        return reason

    async def _wait_for_delivery_attempt(
        self,
        delay: float,
        ctx,
        generation: int | None,
        invalidation_event: asyncio.Event | None,
        outbox_id: str,
    ) -> str | None:
        """可被 generation 失效事件唤醒的重试等待。"""
        reason = self._cancel_stale_outbox(ctx, generation, outbox_id)
        if reason is not None or delay <= 0:
            return reason
        if invalidation_event is None:
            await asyncio.sleep(delay)
        else:
            try:
                await asyncio.wait_for(invalidation_event.wait(), timeout=delay)
            except TimeoutError:
                pass
        return self._cancel_stale_outbox(ctx, generation, outbox_id)

    def _mark_delivery_failed_without_outbox(
        self,
        route_key: str,
        event: MessageEvent,
    ) -> None:
        """兼容无 Outbox 的嵌入式回复，只保留入站失败审计。"""
        conn = init_db(self.db_path)
        try:
            mark_gateway_message_delivery_failed(
                conn,
                route_key,
                event.message_id,
            )
        finally:
            conn.close()

    def _delivery_attempt_limit(self, outbox: dict) -> int:
        """queue-full 回执只做短期 durable 投递，其余使用完整恢复预算。"""
        if outbox.get("delivery_kind") == "queue_full":
            return self.queue_full_reply_max_attempts
        return self.delivery_max_attempts

    def _delivery_retry_delay(
        self,
        attempt: int,
        suggested_delay: float | None = None,
    ) -> float:
        """计算带小幅 jitter 的持久退避，返回值永不超过配置上限。"""
        exponential = min(
            self.delivery_retry_max_delay,
            self.delivery_retry_base_delay * (2 ** max(0, attempt - 1)),
        )
        if suggested_delay is not None:
            try:
                suggested = max(0.0, float(suggested_delay))
            except (TypeError, ValueError):
                suggested = 0.0
            if not math.isfinite(suggested):
                suggested = self.delivery_retry_max_delay
            exponential = min(
                self.delivery_retry_max_delay,
                max(exponential, suggested),
            )
        if self.delivery_retry_jitter_ratio <= 0:
            return exponential
        jitter_span = exponential * self.delivery_retry_jitter_ratio
        jittered = exponential + random.uniform(-jitter_span, jitter_span)
        return min(
            self.delivery_retry_max_delay,
            max(0.1, jittered),
        )

    async def _deliver_outbox(
        self,
        route_key: str,
        event: MessageEvent,
        outbox_id: str,
        ctx=None,
        generation: int | None = None,
        invalidation_event: asyncio.Event | None = None,
    ) -> bool | None:
        """投递并逐片保存进度；``None`` 表示任务已取消或过期。"""
        while True:
            if self._runtime_lease_blocks_delivery():
                return None
            outbox = self._load_outbox(outbox_id)
            if outbox is None:
                raise RuntimeError("gateway outbox is missing")
            if self._runtime_lease_blocks_delivery():
                return None
            if self._cancel_stale_outbox(ctx, generation, outbox_id) is not None:
                return None
            if outbox["status"] == "delivered":
                return True
            if outbox["status"] in (
                "permanent_failed",
                "cancelled",
                "partial_cancelled",
            ):
                return False

            next_attempt_at = outbox.get("next_attempt_at")
            if next_attempt_at:
                delay = max(0.0, float(next_attempt_at) - time.time())
                reason = await self._wait_for_delivery_attempt(
                    delay,
                    ctx,
                    generation,
                    invalidation_event,
                    outbox_id,
                )
                if reason is not None:
                    return None

            if self._runtime_lease_blocks_delivery():
                return None
            if self._cancel_stale_outbox(ctx, generation, outbox_id) is not None:
                return None

            adapter = self.adapters.get(outbox["platform"])
            conn = init_db(self.db_path)
            try:
                can_send = mark_gateway_outbox_sending(conn, outbox_id)
            finally:
                conn.close()
            if not can_send:
                continue

            payloads = outbox["payloads"]
            message_ids = list(outbox["message_ids"])
            failed_result = None
            failed_index = None
            for index in range(outbox["next_chunk_index"], len(payloads)):
                if self._runtime_lease_blocks_delivery():
                    return None
                if (
                    self._cancel_stale_outbox(ctx, generation, outbox_id)
                    is not None
                ):
                    return None
                payload = payloads[index]
                if not adapter:
                    result = SendResult(
                        success=False,
                        error="adapter_unavailable",
                        retryable=True,
                    )
                elif not isinstance(payload, dict):
                    result = SendResult(
                        success=False,
                        error="invalid_outbox_payload",
                        retryable=False,
                    )
                else:
                    try:
                        result = await adapter.send_prepared(
                            outbox["chat_id"],
                            payload,
                            reply_to_message_id=outbox["reply_to_message_id"],
                            thread_id=outbox["thread_id"],
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        print(
                            f"  [gateway] {route_key}: adapter send error "
                            f"({type(exc).__name__})"
                        )
                        result = SendResult(
                            success=False,
                            error="internal_send_error",
                            retryable=False,
                        )

                if not result.success:
                    failed_result = result
                    failed_index = index
                    break

                if result.message_id:
                    message_ids.append(result.message_id)
                conn = init_db(self.db_path)
                try:
                    progress_saved = mark_gateway_outbox_chunk_sent(
                        conn,
                        outbox_id,
                        index + 1,
                        message_ids,
                        len(payloads),
                    )
                finally:
                    conn.close()
                if not progress_saved:
                    return None

                all_chunks_sent = index + 1 >= len(payloads)
                if all_chunks_sent:
                    # 平台已经确认完整回答。即使取消与本次成功并发，delivered
                    # 也必须优先，取消不能反向抹除已发生的外部事实。
                    delivered = self._complete_outbox(
                        outbox_id,
                        route_key,
                        event,
                    )
                    return True if delivered else None

                # 成功进度已经持久化；从这里开始，取消只终止尚未发送的分片。
                if self._runtime_lease_blocks_delivery():
                    return None
                if (
                    self._cancel_stale_outbox(ctx, generation, outbox_id)
                    is not None
                ):
                    return None

            if failed_result is None:
                # 恢复时可能读取到 next_chunk_index 已经等于总片数、但进程
                # 尚未来得及写 delivered 的记录；完整进度同样优先于取消。
                delivered = self._complete_outbox(
                    outbox_id,
                    route_key,
                    event,
                )
                return True if delivered else None

            attempt = int(outbox["attempt_count"]) + 1
            max_attempts = self._delivery_attempt_limit(outbox)
            error = (failed_result.error or "internal_send_error")[:120]
            if self._runtime_lease_blocks_delivery():
                return None
            if self._cancel_stale_outbox(ctx, generation, outbox_id) is not None:
                return None
            if (
                not failed_result.retryable
                or attempt >= max_attempts
            ):
                permanently_failed = self._fail_outbox(
                    outbox_id,
                    route_key,
                    event,
                    error,
                    failed_result.error_code,
                )
                if not permanently_failed:
                    current = self._load_outbox(outbox_id)
                    if not current or current["status"] != "permanent_failed":
                        return None
                print(
                    f"  [gateway] {route_key}: delivery failed permanently "
                    f"(chunk={failed_index}, error={error})"
                )
                return False

            delay = self._delivery_retry_delay(
                attempt,
                failed_result.retry_after_seconds,
            )
            if self._cancel_stale_outbox(ctx, generation, outbox_id) is not None:
                return None
            conn = init_db(self.db_path)
            try:
                retry_scheduled = mark_gateway_outbox_retry(
                    conn,
                    outbox_id,
                    error,
                    failed_result.error_code,
                    time.time() + delay,
                )
            finally:
                conn.close()
            if not retry_scheduled:
                return None
            print(
                f"  [gateway] {route_key}: delivery retry "
                f"{attempt}/{max_attempts} in {delay:.1f}s"
            )
            if self._cancel_stale_outbox(ctx, generation, outbox_id) is not None:
                return None

    def _start_durable_reply(
        self,
        route_key: str,
        event: MessageEvent,
        content: str,
        delivery_kind: str,
        ctx,
    ) -> str:
        """先持久化控制回执；route 空闲时才创建唯一投递 worker。"""
        delivery_id = str(uuid.uuid4())
        outbox = self._build_outbox(
            route_key,
            event,
            content,
            delivery_id,
            delivery_kind,
        )
        delivery_id = self._enqueue_outbox(outbox)
        self._accepted_messages.add((route_key, event.message_id))
        self._launch_durable_reply_worker(
            route_key,
            event,
            delivery_id,
            ctx,
        )
        return delivery_id

    def _launch_durable_reply_worker(
        self,
        route_key: str,
        event: MessageEvent,
        delivery_id: str,
        ctx,
    ) -> bool:
        """只在 route 真正空闲时接管一条已落库 Outbox。"""
        if self._route_has_active_worker(ctx):
            return False
        generation, invalidation_event = self.sessions.begin_task(ctx)
        ctx.delivery_id = delivery_id
        ctx.delivery_generation = generation
        worker_task = asyncio.create_task(
            self._process_durable_reply(
                route_key,
                event,
                delivery_id,
                ctx,
                generation,
                invalidation_event,
            ),
        )
        ctx.worker_task = worker_task
        ctx.worker_generation = generation
        return True

    async def _process_durable_reply(
        self,
        route_key: str,
        event: MessageEvent,
        delivery_id: str,
        ctx,
        generation: int,
        invalidation_event: asyncio.Event,
    ) -> None:
        """投递不需要再次调用模型的持久化回复。"""
        cancel_reason = None
        try:
            delivered = await self._deliver_outbox(
                route_key,
                event,
                delivery_id,
                ctx,
                generation,
                invalidation_event,
            )
            if delivered is None:
                cancel_reason = self._task_cancel_reason(ctx, generation)
        except asyncio.CancelledError:
            cancel_reason = self._task_cancel_reason(ctx, generation)
            print(f"  [gateway] {route_key}: durable reply cancelled")
            raise
        except Exception as exc:
            print(
                f"  [gateway] {route_key}: durable reply error "
                f"({type(exc).__name__})"
            )
        finally:
            current_task = asyncio.current_task()
            owns_worker = (
                ctx.worker_task is current_task
                and ctx.worker_generation == generation
            )
            if (
                ctx.delivery_id == delivery_id
                and ctx.delivery_generation == generation
            ):
                ctx.delivery_id = None
                ctx.delivery_generation = None
            if owns_worker:
                ctx.worker_task = None
                ctx.worker_generation = None
                ctx.busy = False
        if cancel_reason != "shutdown":
            await self._dispatch_next(ctx)

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

    async def _restore_outbound_messages(self) -> None:
        """按 route_key 恢复已生成但尚未完整送达的回复。"""
        conn = init_db(self.db_path)
        try:
            reset_gateway_sending_outbox(conn)
            rows = get_recoverable_gateway_outbox(conn)
        finally:
            conn.close()

        grouped: dict[str, list[dict]] = {}
        for row in rows:
            grouped.setdefault(row["route_key"], []).append(row)

        restored = 0
        for route_key, route_rows in grouped.items():
            try:
                damaged = [
                    row for row in route_rows if row.get("recovery_error")
                ]
                if damaged:
                    for row in damaged:
                        print(
                            "  [gateway] outbox recovery deferred "
                            f"(route={route_key}, id={row['id']}, "
                            f"error={row['recovery_error']})"
                        )
                    continue

                platforms = {str(row["platform"]) for row in route_rows}
                if len(platforms) != 1:
                    raise ValueError("outbox route contains multiple platforms")
                platform = next(iter(platforms))
                if not self._adapter_ready_for_recovery(platform):
                    print(
                        "  [gateway] outbox recovery deferred "
                        f"(route={route_key}, platform={platform}, "
                        "adapter unavailable)"
                    )
                    continue

                # 创建 worker 前验证该 route 的全部事件。任一记录损坏时保留
                # 整个 route 的原顺序，不跳过坏记录发送后面的回复。
                for row in route_rows:
                    event = self._deserialize_event(row["event_json"])
                    expected = build_session_key(event.source, self.agent_name)
                    if expected != route_key:
                        raise ValueError("route key mismatch")
                    if event.source.platform != platform:
                        raise ValueError("outbox platform mismatch")
                    self._remember_startup_message(
                        route_key,
                        event,
                        layer="outbox",
                        status=str(row["status"]),
                    )
                ctx = self.sessions.get_or_create(
                    route_key,
                    self._build_gateway_prompt(),
                )
                if self._route_has_active_worker(ctx):
                    print(
                        "  [gateway] outbox recovery skipped duplicate worker "
                        f"(route={route_key})"
                    )
                    continue
                generation, invalidation_event = self.sessions.begin_task(ctx)
                for row in route_rows:
                    self._accepted_messages.add((
                        route_key,
                        row["source_message_id"],
                    ))
                worker_task = asyncio.create_task(
                    self._resume_outbox_route(
                        route_key,
                        route_rows,
                        generation,
                        invalidation_event,
                    ),
                )
                ctx.worker_task = worker_task
                ctx.worker_generation = generation
                restored += len(route_rows)
            except Exception as exc:
                print(
                    "  [gateway] outbox recovery deferred "
                    f"(route={route_key}, error={type(exc).__name__})"
                )
        if restored:
            print(f"  [gateway] restored outbound messages: {restored}")

    async def _resume_outbox_route(
        self,
        route_key: str,
        rows: list[dict],
        generation: int,
        invalidation_event: asyncio.Event,
    ) -> None:
        """同一路由按原创建顺序恢复回复,避免后回复先到。"""
        ctx = self.sessions.get_or_create(
            route_key,
            self._build_gateway_prompt(),
        )
        cancel_reason = None
        try:
            for position, row in enumerate(rows):
                event = self._deserialize_event(row["event_json"])
                ctx.delivery_id = row["id"]
                ctx.delivery_generation = generation
                delivered = await self._deliver_outbox(
                    route_key,
                    event,
                    row["id"],
                    ctx,
                    generation,
                    invalidation_event,
                )
                if delivered is None:
                    cancel_reason = self._task_cancel_reason(ctx, generation)
                    if cancel_reason != "shutdown":
                        # 该恢复 worker 中其余回复同属已经被明确放弃的旧工作；
                        # 全部终止，避免下一次重启又把它们发送出来。
                        for stale_row in rows[position:]:
                            self._cancel_outbox(
                                stale_row["id"],
                                route_key=route_key,
                                source_message_id=stale_row[
                                    "source_message_id"
                                ],
                            )
                    break
                if (
                    ctx.delivery_id == row["id"]
                    and ctx.delivery_generation == generation
                ):
                    ctx.delivery_id = None
                    ctx.delivery_generation = None
        except asyncio.CancelledError:
            cancel_reason = self._task_cancel_reason(ctx, generation)
            print(f"  [gateway] {route_key}: delivery recovery cancelled")
            raise
        except Exception as exc:
            print(
                f"  [gateway] {route_key}: delivery recovery error "
                f"({type(exc).__name__})"
            )
        finally:
            current_task = asyncio.current_task()
            owns_worker = (
                ctx.worker_task is current_task
                and ctx.worker_generation == generation
            )
            if ctx.delivery_generation == generation:
                ctx.delivery_id = None
                ctx.delivery_generation = None
            if owns_worker:
                ctx.worker_task = None
                ctx.worker_generation = None
                ctx.busy = False
        if cancel_reason != "shutdown":
            await self._dispatch_next(ctx)

    async def _restore_queued_messages(self) -> None:
        """恢复 queued / processing；已有 Outbox worker 的 route 进入 pending。"""
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
                self._remember_startup_message(
                    route_key,
                    event,
                    layer="queue",
                    status=str(row["status"]),
                )
                if not self._adapter_ready_for_recovery(event.source.platform):
                    print(
                        "  [gateway] queued message recovery deferred "
                        f"(route={route_key}, platform={event.source.platform}, "
                        "adapter unavailable)"
                    )
                    continue
                key = (route_key, event.message_id)
                if key in self._accepted_messages:
                    continue
                self._accepted_messages.add(key)
                await self._handle_message(event, from_queue=True)
                restored += 1
            except Exception as exc:
                print(
                    "  [gateway] queued message recovery deferred "
                    f"(id={row.get('message_id', '<unknown>')}, "
                    f"error={type(exc).__name__})"
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
        if (
            not from_queue
            and not self._accepting_external_messages
        ):
            raise RuntimeError(
                f"gateway is not accepting messages during {self._lifecycle_phase}"
            )
        route_key = build_session_key(event.source, self.agent_name)
        queue_key = (route_key, event.message_id)
        if not from_queue:
            persisted = self._message_persistence_state(event)
            if persisted is not None:
                self._accepted_messages.add(queue_key)
                return
            if queue_key in self._accepted_messages:
                return

        # slash 命令(所有平台通用)
        cmd = (event.text or "").strip().lower()
        if cmd == "/new":
            ctx = self.sessions.get_or_create(
                route_key, self._build_gateway_prompt(),
            )
            if self._route_has_active_worker(ctx):
                # /new 作为串行屏障:丢弃命令前尚未执行的旧消息,
                # 等当前 worker 完全退出后再切换 conversation_id。
                dropped_events = list(ctx.pending)
                self._drop_events(route_key, dropped_events)
                ctx.pending.clear()
                if not from_queue and not self._persist_event(route_key, event):
                    return
                self.sessions.enqueue(ctx, event)
                self._request_session_cancel(route_key, reason="new")
                print(
                    f"  [gateway] {route_key}: /new queued "
                    f"({len(dropped_events)} old pending dropped)"
                )
                return
            if not from_queue and not self._persist_event(route_key, event):
                return
            ctx = self.sessions.new_conversation(
                route_key, self._build_gateway_prompt(),
            )
            if event.source.platform not in self.adapters:
                # 保留无 Adapter 的测试 / 嵌入式调用兼容路径。真实平台事件
                # 一定有对应 Adapter,仍走下面的持久 outbox。
                result = await self._reply(event, "(new conversation started)")
                if result is None or result.success:
                    self._complete_event(route_key, event)
                else:
                    self._mark_delivery_failed_without_outbox(route_key, event)
                await self._dispatch_next(ctx)
                return
            self._start_durable_reply(
                route_key,
                event,
                "(new conversation started)",
                "new_conversation",
                ctx,
            )
            return
        if cmd == "/stop":
            ctx = self.sessions.get_or_create(
                route_key, self._build_gateway_prompt(),
            )
            ok = self._request_session_cancel(route_key, reason="user")
            content = "(cancel requested)" if ok else "(no active task)"
            if event.source.platform not in self.adapters:
                # 保留无 Adapter 的测试 / 嵌入式调用兼容路径。
                await self._reply(event, content)
                return
            self._start_durable_reply(
                route_key,
                event,
                content,
                "stop_command",
                ctx,
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
            route_key, self._build_gateway_prompt(),
        )

        if self._route_has_active_worker(ctx):
            # 正在处理 → 在单会话上限内排队。
            if (
                not from_queue
                and len(ctx.pending) >= self.sessions.max_pending_messages
            ):
                print(f"  [gateway] {route_key}: queue full")
                content = "(queue full: please wait for pending messages)"
                if event.source.platform not in self.adapters:
                    await self._reply(event, content)
                    return
                self._start_durable_reply(
                    route_key,
                    event,
                    content,
                    "queue_full",
                    ctx,
                )
                return
            if not from_queue and not self._persist_event(route_key, event):
                return
            if from_queue:
                # 已持久化消息必须全部恢复,不能因重启后的新上限丢失。
                ctx.pending.append(event)
            else:
                self.sessions.enqueue(ctx, event)
            # 重启恢复的历史队列按原顺序完整执行,不能让后一条恢复消息
            # 取消前一条;只有新到达的实时消息才覆盖当前请求。
            if not from_queue:
                self._request_session_cancel(
                    route_key,
                    reason="superseded",
                )
            print(f"  [gateway] {route_key}: queued ({len(ctx.pending)} pending)")
            return

        # 原子设置 busy,避免竞态:create_task 不会立即执行,_rocess 也没
        # 机会在 _handle_message 返回前跑。所以在 _handle_message 里设 busy
        # 就能保证同一 route_key 只有一个 worker。
        if not from_queue and not self._persist_event(route_key, event):
            return
        self._mark_event_processing(route_key, event)
        generation, invalidation_event = self.sessions.begin_task(ctx)
        delivery_id = str(uuid.uuid4())
        ctx.delivery_id = delivery_id
        ctx.delivery_generation = generation
        # 模型 Task 与串行收尾 worker 分开管理。即使模型 Task 在首次运行前
        # 就被取消,worker 仍会启动并清理 busy / 持久队列。
        agent_task = asyncio.create_task(
            self._run_agent(event, ctx),
        )
        ctx.active_task = agent_task
        ctx.active_generation = generation
        worker_task = asyncio.create_task(
            self._process(
                route_key,
                event,
                delivery_id,
                agent_task,
                generation,
                invalidation_event,
            ),
        )
        ctx.worker_task = worker_task
        ctx.worker_generation = generation

    async def _process(
        self,
        route_key: str,
        event: MessageEvent,
        delivery_id: str | None = None,
        agent_task: asyncio.Task | None = None,
        generation: int | None = None,
        invalidation_event: asyncio.Event | None = None,
    ):
        """串行处理一条消息,然后检查队列。"""
        ctx = self.sessions.get_or_create(
            route_key, self._build_gateway_prompt(),
        )
        if generation is None:
            generation = (
                ctx.worker_generation
                if ctx.worker_generation is not None
                else ctx.generation
            )
        if invalidation_event is None:
            invalidation_event = ctx.invalidation_event
        delivery_id = delivery_id or ctx.delivery_id or str(uuid.uuid4())
        if ctx.delivery_generation in (None, generation):
            ctx.delivery_id = delivery_id
            ctx.delivery_generation = generation

        cancel_reason = None
        event_completed = False
        abandoned = False
        try:
            if agent_task is None:
                response = await self._run_agent(event, ctx)
            else:
                response = await agent_task
            if (
                ctx.delivery_generation == generation
                and ctx.delivery_id is not None
            ):
                delivery_id = ctx.delivery_id
            # worker 返回到事件循环后做最后一道取消检查。即使取消恰好
            # 发生在模型响应检查之后,也不能把旧回复发送给用户。
            persisted_outbox = self._load_outbox(delivery_id)
            cancel_reason = self._task_cancel_reason(ctx, generation)
            if cancel_reason is not None:
                abandoned = True
                if (
                    cancel_reason != "shutdown"
                    and persisted_outbox
                ):
                    event_completed = self._cancel_outbox(
                        delivery_id,
                        route_key=route_key,
                        source_message_id=event.message_id,
                    )
                print(f"  [gateway] {route_key}: stale response discarded")
            elif not response and not persisted_outbox:
                event_completed = self._complete_event(route_key, event)
            elif event.source.platform not in self.adapters:
                # 无 Adapter 只用于测试或嵌入式调用;保留原 _reply 注入点。
                result = await self._reply(event, str(response or ""))
                if result is None or result.success:
                    if persisted_outbox:
                        event_completed = self._cancel_outbox(
                            delivery_id,
                            route_key=route_key,
                            source_message_id=event.message_id,
                        )
                    else:
                        event_completed = self._complete_event(route_key, event)
                else:
                    if persisted_outbox:
                        event_completed = self._fail_outbox(
                            delivery_id,
                            route_key,
                            event,
                            result.error or "internal_send_error",
                            result.error_code,
                        )
                    else:
                        self._mark_delivery_failed_without_outbox(
                            route_key,
                            event,
                        )
            elif persisted_outbox:
                delivered = await self._deliver_outbox(
                    route_key,
                    event,
                    delivery_id,
                    ctx,
                    generation,
                    invalidation_event,
                )
                if delivered:
                    event_completed = True
                elif delivered is None:
                    cancel_reason = self._task_cancel_reason(ctx, generation)
                    abandoned = True
            else:
                # 模型错误等没有 assistant 最终消息的返回在这里补建 outbox。
                outbox = self._build_outbox(
                    route_key,
                    event,
                    response,
                    delivery_id,
                    "final",
                )
                delivery_id = self._enqueue_outbox(outbox)
                if (
                    ctx.delivery_generation == generation
                    and self._task_cancel_reason(ctx, generation) is None
                ):
                    ctx.delivery_id = delivery_id
                delivered = await self._deliver_outbox(
                    route_key,
                    event,
                    delivery_id,
                    ctx,
                    generation,
                    invalidation_event,
                )
                if delivered:
                    event_completed = True
                elif delivered is None:
                    cancel_reason = self._task_cancel_reason(ctx, generation)
                    abandoned = True
        except asyncio.CancelledError:
            cancel_reason = (
                self._task_cancel_reason(ctx, generation) or "cancelled"
            )
            abandoned = True
            print(f"  [gateway] {route_key}: task cancelled ({cancel_reason})")
        except Exception as exc:
            print(f"  [gateway] {route_key} error: {type(exc).__name__}")
            # 已有 outbox 时不能再发送第二条内部错误,否则可能与部分成功
            # 的正式回复重复。只有模型阶段尚未生成 outbox 才补错误回复。
            cancel_reason = self._task_cancel_reason(ctx, generation)
            if cancel_reason is not None:
                abandoned = True
            elif self._load_outbox(delivery_id) is None:
                try:
                    outbox = self._build_outbox(
                        route_key,
                        event,
                        f"(internal error: {type(exc).__name__})",
                        delivery_id,
                        "internal_error",
                    )
                    delivery_id = self._enqueue_outbox(outbox)
                    if (
                        ctx.delivery_generation == generation
                        and self._task_cancel_reason(ctx, generation) is None
                    ):
                        ctx.delivery_id = delivery_id
                    delivered = await self._deliver_outbox(
                        route_key,
                        event,
                        delivery_id,
                        ctx,
                        generation,
                        invalidation_event,
                    )
                    if delivered:
                        event_completed = True
                    elif delivered is None:
                        cancel_reason = self._task_cancel_reason(
                            ctx,
                            generation,
                        )
                        abandoned = True
                except asyncio.CancelledError:
                    cancel_reason = (
                        self._task_cancel_reason(ctx, generation)
                        or "cancelled"
                    )
                except Exception as send_exc:
                    print(
                        f"  [gateway] {route_key}: error reply failed "
                        f"({type(send_exc).__name__})"
                    )
        finally:
            cancel_reason = cancel_reason or self._task_cancel_reason(
                ctx,
                generation,
            )
            if (
                agent_task is not None
                and ctx.active_task is agent_task
                and ctx.active_generation in (None, generation)
            ):
                ctx.active_task = None
                ctx.active_generation = None
            current_task = asyncio.current_task()
            owns_worker = (
                (
                    ctx.worker_task is current_task
                    and ctx.worker_generation == generation
                )
                or (
                    ctx.worker_task is None
                    and ctx.worker_generation is None
                )
            )
            if owns_worker:
                ctx.worker_task = None
                ctx.worker_generation = None
                ctx.busy = False
            if (
                ctx.delivery_id == delivery_id
                and ctx.delivery_generation in (None, generation)
            ):
                ctx.delivery_id = None
                ctx.delivery_generation = None
            # 用户取消 /new / 后续消息覆盖表示旧回答被明确放弃;shutdown
            # 则保留 processing / outbox,下次启动从持久状态恢复。
            if (
                abandoned
                and cancel_reason != "shutdown"
                and not event_completed
            ):
                if self._load_outbox(delivery_id):
                    event_completed = self._cancel_outbox(
                        delivery_id,
                        route_key=route_key,
                        source_message_id=event.message_id,
                    )
                else:
                    event_completed = self._complete_event(route_key, event)

        if cancel_reason != "shutdown":
            await self._dispatch_next(ctx)

    async def _dispatch_next(self, ctx) -> None:
        """优先接力已生成回复，再分发 pending 模型任务。"""
        if self._route_has_active_worker(ctx):
            return
        try:
            conn = init_db(self.db_path)
            try:
                outbox = get_next_recoverable_gateway_outbox_for_route(
                    conn,
                    ctx.route_key,
                )
            finally:
                conn.close()
            if outbox is not None:
                event = self._deserialize_event(outbox["event_json"])
                expected_route = build_session_key(event.source, self.agent_name)
                if expected_route != ctx.route_key:
                    raise ValueError("route key mismatch")
                if event.source.platform != outbox["platform"]:
                    raise ValueError("outbox platform mismatch")
                self._accepted_messages.add((
                    ctx.route_key,
                    outbox["source_message_id"],
                ))
                self._launch_durable_reply_worker(
                    ctx.route_key,
                    event,
                    outbox["id"],
                    ctx,
                )
                return
        except Exception as exc:
            # 损坏记录保留审计，并阻止同 route 后续任务越过它。
            print(
                f"  [gateway] {ctx.route_key}: outbox dispatch deferred "
                f"({type(exc).__name__})"
            )
            return
        if not ctx.pending:
            return
        next_event = ctx.pending.popleft()
        await self._handle_message(next_event, from_queue=True)

    async def _run_agent(
        self,
        event: MessageEvent,
        ctx,
    ) -> str | None:
        """在全局并发限制内运行异步主会话。"""
        # 所有 route_key 共用同一信号量,避免不同会话同时打满模型服务。
        async with self._llm_semaphore:
            return await self._run_agent_async(event, ctx)

    async def _run_agent_async(
        self,
        event: MessageEvent,
        ctx,
    ) -> str | None:
        """使用 AsyncOpenAI 跑主会话,SQLite 仍复用现有同步接口。"""
        from hermes.db import ensure_session
        from hermes.conversation import run_conversation_async

        generation = getattr(ctx, "active_generation", None)
        if generation is None:
            generation = getattr(ctx, "generation", None)
        cancel_checker = lambda: (  # noqa: E731
            self._task_cancel_reason(ctx, generation) is not None
        )
        if (
            getattr(ctx, "delivery_generation", generation) == generation
            and getattr(ctx, "delivery_id", None)
        ):
            delivery_id = ctx.delivery_id
        else:
            delivery_id = str(uuid.uuid4())
            if self._task_cancel_reason(ctx, generation) is None:
                ctx.delivery_id = delivery_id
                if hasattr(ctx, "delivery_generation"):
                    ctx.delivery_generation = generation
        route_key = ctx.route_key
        conversation_id = ctx.conversation_id
        system_prompt = ctx.system_prompt

        def persist_final_message(conn, session_id, msg) -> None:
            """最终回答和 outbox 必须在同一个 SQLite 事务中落盘。"""
            if self._task_cancel_reason(ctx, generation) is not None:
                raise asyncio.CancelledError
            content = str(msg.get("content", "") or "")
            if not content:
                add_messages(conn, session_id, [msg])
                return
            outbox = self._build_outbox(
                route_key,
                event,
                content,
                delivery_id,
                "final",
            )
            actual_delivery_id = add_final_message_with_gateway_outbox(
                conn,
                session_id,
                msg,
                outbox,
            )
            if (
                self._task_cancel_reason(ctx, generation) is None
                and getattr(ctx, "delivery_generation", generation) == generation
            ):
                ctx.delivery_id = actual_delivery_id

        conn = init_db(self.db_path)
        try:
            ensure_session(
                conn,
                conversation_id,
                source=event.source.platform,
            )
            result = await run_conversation_async(
                event.text,
                conn,
                conversation_id,
                system_prompt,
                conversation_id,
                cancel_checker,
                async_client=self._get_async_client(),
                final_message_callback=persist_final_message,
                enabled_toolsets=[],
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

    async def _reply(self, event: MessageEvent, content: str) -> SendResult:
        """发送时效性回复并把结果返回调用方,不再吞掉失败。"""
        adapter = self.adapters.get(event.source.platform)
        if not adapter:
            return SendResult(
                success=False,
                error="adapter_unavailable",
                retryable=True,
            )
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
            return result
        except Exception as exc:
            print(f"  [gateway] send exception on {event.source.platform}: {type(exc).__name__}")
            return SendResult(
                success=False,
                error="internal_send_error",
                retryable=False,
            )
