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
import hashlib
import json
import math
import os
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

from hermes.db import (
    acquire_gateway_runtime_lease,
    add_final_message_with_gateway_outbox,
    add_messages,
    cancel_gateway_delivery,
    cancel_pending_gateway_approvals,
    check_gateway_runtime_readiness,
    claim_gateway_approval,
    complete_gateway_delivery,
    complete_gateway_message,
    create_gateway_approval_with_outbox,
    delete_gateway_messages,
    deny_gateway_approval,
    enqueue_gateway_outbox,
    enqueue_gateway_message,
    fail_gateway_delivery,
    finish_gateway_approval_and_enqueue_resume,
    gateway_outbox_claim_is_valid,
    gateway_runtime_lease_is_valid,
    get_gateway_conversation_for_route,
    get_gateway_message_persistence_state,
    get_gateway_routes_with_pending_outbox,
    get_next_recoverable_gateway_outbox_for_route,
    get_gateway_outbox,
    get_gateway_approval_resume,
    get_pending_gateway_approval,
    get_gateway_queued_messages,
    get_recoverable_gateway_outbox,
    init_db,
    list_gateway_conversations,
    mark_gateway_message_delivery_failed,
    mark_gateway_message_processing,
    mark_gateway_outbox_chunk_sent,
    mark_gateway_outbox_retry,
    mark_gateway_outbox_sending,
    prune_gateway_terminal_outbox,
    prune_gateway_terminal_ownership,
    reconcile_gateway_terminal_deliveries,
    recover_gateway_approvals,
    release_gateway_runtime_lease,
    renew_gateway_runtime_lease,
    reset_gateway_processing_messages,
    reset_gateway_sending_outbox,
)
from hermes.gateway.adapters import BasePlatformAdapter
from hermes.gateway.observability import safe_message_digest, safe_route_digest
from hermes.gateway.persistence import GatewayPersistence
from hermes.gateway.session_store import SessionStore
from hermes.gateway.types import (
    MessageEvent,
    MessageType,
    SendResult,
    SessionSource,
    build_session_key,
)
from hermes.prompt import build_system_prompt
from hermes.redaction import redact_explicit_secrets


_GATEWAY_CONTEXT_FIELDS = (
    "include_soul",
    "include_memory",
    "include_user_profile",
    "include_project_context",
)
_GATEWAY_SUPPORTED_TOOLSETS = frozenset({
    "terminal",
    "file",
    "memory",
    "skill",
    "delegate",
})
_GATEWAY_CONTEXT_POLICY_DEFAULTS = {
    "default": {
        "include_soul": True,
        "include_memory": False,
        "include_user_profile": False,
        "include_project_context": False,
    },
    "dm": {
        "include_soul": True,
        "include_memory": True,
        "include_user_profile": True,
        "include_project_context": False,
    },
    "group": {
        "include_soul": True,
        "include_memory": False,
        "include_user_profile": False,
        "include_project_context": False,
    },
    "topic": {
        "include_soul": True,
        "include_memory": False,
        "include_user_profile": False,
        "include_project_context": False,
    },
}
_GATEWAY_RUNTIME_LEASE_NAME = "gateway-main"
_SAFE_MODEL_TIMEOUT_REPLY = "处理失败：模型响应超时，请稍后重试。"
_SAFE_MODEL_UNAVAILABLE_REPLY = "处理失败：模型服务暂时不可用，请稍后重试。"
_SAFE_PERSISTENCE_REPLY = "处理失败：系统暂时不可用，请稍后重试。"
_SAFE_INTERNAL_REPLY = "处理失败：任务未能完成，请稍后重试。"
_GATEWAY_APPROVAL_TTL_SECONDS = 600.0
_APPROVAL_RESUME_MESSAGE_PREFIX = "approval-resume:"
_APPROVAL_RESUME_TERMINAL_FAILURES = frozenset({
    "invalid_approval_resume_task",
    "invalid_approval_resume_history",
    "invalid_approval_resume_state",
    "approval_resume_fallback_unavailable",
})


def _short_approval_id(request_id: object) -> str:
    """生成便于在聊天中输入、仍可由 route 内唯一前缀解析的审批号。"""
    raw = str(request_id or "")
    if raw.startswith("approval_"):
        raw = raw[len("approval_"):]
    return raw[:12]


def _approval_value_preview(value: object, limit: int = 500) -> str:
    """审批问题只展示脱敏且限长的操作内容。"""
    text = redact_explicit_secrets(str(value or ""))
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…"


def _format_approval_question(request: dict) -> str:
    """把持久化请求格式化为明确的文本审批问题。"""
    request_id = _short_approval_id(request.get("id"))
    tool_name = str(request.get("tool_name", ""))
    arguments = request.get("arguments", {})
    details = request.get("details", {})
    lines = [
        "检测到受控操作，当前尚未执行。",
        "",
        f"审批编号：{request_id}",
        f"工具：{tool_name}",
        f"操作：{request.get('summary', '需要批准的工具操作')}",
    ]
    if tool_name == "terminal":
        cwd = _approval_value_preview(details.get("cwd", ""), 1000)
        if cwd:
            lines.append(f"工作目录：{cwd}")
        lines.append(
            f"命令：{_approval_value_preview(arguments.get('command', ''), 2000)}"
        )
    elif tool_name == "file":
        action = str(arguments.get("action", ""))
        path = _approval_value_preview(
            details.get("abs_path") or arguments.get("path", ""),
            1000,
        )
        lines.extend([f"File action：{action}", f"路径：{path}"])
        if action in {"write", "append"}:
            content = str(arguments.get("content", ""))
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
            lines.append(
                f"内容：{len(content.encode('utf-8'))} bytes，SHA-256 {digest}"
            )
            lines.append(f"预览：{_approval_value_preview(content, 300)}")
        elif action == "replace":
            lines.append(
                f"查找：{_approval_value_preview(arguments.get('find', ''), 300)}"
            )
            lines.append(
                f"替换：{_approval_value_preview(arguments.get('replace', ''), 300)}"
            )
    lines.extend([
        "",
        f"批准：/approve {request_id}",
        f"拒绝：/deny {request_id}",
        "该请求 10 分钟后失效，只能由原请求者批准一次。",
    ])
    return "\n".join(lines)


def _approval_command_reply(outcome: str, selector: str) -> str:
    """把审批状态映射为不泄漏其它 route 信息的用户文案。"""
    labels = {
        "invalid_id": "审批编号格式无效。",
        "not_found": "未找到该审批请求。",
        "ambiguous": "审批编号不唯一，请输入更长的编号。",
        "stale_conversation": "该审批请求不属于当前对话，不能执行。",
        "forbidden": "该审批请求只能由原请求者处理。",
        "expired": "该审批请求已经过期，操作未执行。",
        "denied": "该审批请求已经被拒绝，操作未执行。",
        "cancelled": "该审批请求已经取消，操作未执行。",
        "executed": "该审批请求已经执行，不能重复执行。",
        "failed": "该审批请求已经执行过，但工具返回失败。",
        "executing": "该审批请求正在执行，请勿重复提交。",
        "execution_unknown": "上次执行被中断，结果不确定；为避免重复副作用不会重试。",
        "conflict": "审批状态刚刚发生变化，请重新查看。",
    }
    return labels.get(outcome, f"审批请求 {selector} 当前不可处理。")


@dataclass(frozen=True)
class _GatewayAgentResult:
    """Runner 内部的 Agent 结果，只保留可安全发送的用户文案。"""

    response: str | None
    failed: bool = False
    failure_type: str | None = None


def _safe_audit_label(value: object) -> str:
    """限制审计分类字段，避免外部错误类型注入换行或正文。"""
    raw = str(value or "")[:64]
    return "".join(
        char for char in raw
        if char.isalnum() or char in {".", "_", "-"}
    )


def _load_gateway_context_values(
    context_cfg: dict,
    path: str,
) -> dict[str, bool]:
    """读取一层上下文开关，拒绝非布尔值。"""
    selected: dict[str, bool] = {}
    for name in _GATEWAY_CONTEXT_FIELDS:
        if name not in context_cfg:
            continue
        value = context_cfg[name]
        if not isinstance(value, bool):
            raise ValueError(f"{path}.{name} must be a boolean")
        selected[name] = value
    return selected


def _load_gateway_context_config(
    gateway_cfg: dict,
) -> dict[str, dict[str, bool]]:
    """读取 default / DM / group / topic 的只读上下文策略。"""
    context_cfg = gateway_cfg.get("context", {})
    if not isinstance(context_cfg, dict):
        raise ValueError("gateway.context must be a mapping")

    # 旧版扁平开关继续作为全局 default；新版的显式
    # default 随后覆盖它，最后再应用各会话类型的局部值。
    default_overrides = _load_gateway_context_values(
        context_cfg,
        "gateway.context",
    )
    nested_default = context_cfg.get("default", {})
    if not isinstance(nested_default, dict):
        raise ValueError("gateway.context.default must be a mapping")
    default_overrides.update(
        _load_gateway_context_values(
            nested_default,
            "gateway.context.default",
        )
    )

    policies: dict[str, dict[str, bool]] = {}
    for policy_name, builtin in _GATEWAY_CONTEXT_POLICY_DEFAULTS.items():
        policy = dict(builtin)
        policy.update(default_overrides)
        if policy_name != "default":
            policy_cfg = context_cfg.get(policy_name, {})
            if not isinstance(policy_cfg, dict):
                raise ValueError(
                    f"gateway.context.{policy_name} must be a mapping"
                )
            policy.update(
                _load_gateway_context_values(
                    policy_cfg,
                    f"gateway.context.{policy_name}",
                )
            )
        policies[policy_name] = policy
    return policies


def _load_gateway_platform_toolsets(
    gateway_cfg: dict,
) -> dict[str, tuple[str, ...]]:
    """读取各平台显式开放的工具集，未配置的平台保持无工具。"""
    platforms_cfg = gateway_cfg.get("platforms", {})
    if not isinstance(platforms_cfg, dict):
        raise ValueError("gateway.platforms must be a mapping")

    platform_toolsets: dict[str, tuple[str, ...]] = {}
    for raw_platform, platform_cfg in platforms_cfg.items():
        platform = str(raw_platform).strip().lower()
        if not platform:
            raise ValueError("gateway platform name must not be empty")
        if not isinstance(platform_cfg, dict):
            raise ValueError(
                f"gateway.platforms.{platform} must be a mapping"
            )

        configured = platform_cfg.get("toolsets", [])
        if not isinstance(configured, list) or not all(
            isinstance(toolset, str) for toolset in configured
        ):
            raise ValueError(
                f"gateway.platforms.{platform}.toolsets must be a list "
                "of strings"
            )

        normalized: list[str] = []
        for raw_toolset in configured:
            toolset = raw_toolset.strip().lower()
            if toolset not in _GATEWAY_SUPPORTED_TOOLSETS:
                raise ValueError(
                    f"gateway.platforms.{platform}.toolsets contains "
                    f"unsupported toolset: {raw_toolset!r}; allowed: "
                    f"{sorted(_GATEWAY_SUPPORTED_TOOLSETS)}"
                )
            if toolset not in normalized:
                normalized.append(toolset)
        platform_toolsets[platform] = tuple(normalized)

    return platform_toolsets


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


def _load_retention_config(gateway_cfg: dict) -> dict[str, float | int]:
    """读取长期审计清理配置，并在启动前拒绝无效批次或保留期。"""
    retention_cfg = gateway_cfg.get("retention", {})
    if not isinstance(retention_cfg, dict):
        raise ValueError("gateway.retention must be a mapping")

    defaults = {
        "feishu_inbox_seconds": 72 * 60 * 60,
        "ownership_seconds": 30 * 24 * 60 * 60,
        "outbox_seconds": 30 * 24 * 60 * 60,
        "cleanup_interval_seconds": 60 * 60,
    }
    loaded: dict[str, float | int] = {}
    for name, default in defaults.items():
        value = retention_cfg.get(name, default)
        if isinstance(value, bool):
            raise ValueError(
                f"gateway.retention.{name} must be a positive number"
            )
        try:
            seconds = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"gateway.retention.{name} must be a positive number"
            ) from exc
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError(
                f"gateway.retention.{name} must be a positive number"
            )
        loaded[name] = seconds

    batch_size = retention_cfg.get("cleanup_batch_size", 200)
    if isinstance(batch_size, bool):
        raise ValueError(
            "gateway.retention.cleanup_batch_size must be a positive integer"
        )
    try:
        batch_size = int(batch_size)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "gateway.retention.cleanup_batch_size must be a positive integer"
        ) from exc
    if batch_size <= 0:
        raise ValueError(
            "gateway.retention.cleanup_batch_size must be a positive integer"
        )
    loaded["cleanup_batch_size"] = batch_size
    return loaded


class GatewayRunner:
    """启动 adapter、路由消息、跑 agent、回发结果。

    每个实例只能启动一次；停止或启动失败后必须创建新的 Runner 实例。
    """

    def __init__(self, config: dict, db_path: str):
        self.config = config
        self.db_path = db_path
        self.adapters: dict[str, BasePlatformAdapter] = {}
        gateway_cfg = config.get("gateway", {})
        if not isinstance(gateway_cfg, dict):
            raise ValueError("gateway must be a mapping")
        self._gateway_context_policies = _load_gateway_context_config(
            gateway_cfg
        )
        self._gateway_platform_toolsets = _load_gateway_platform_toolsets(
            gateway_cfg
        )
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
        retention = _load_retention_config(gateway_cfg)
        self.feishu_inbox_retention_seconds = float(
            retention["feishu_inbox_seconds"]
        )
        self.ownership_retention_seconds = float(
            retention["ownership_seconds"]
        )
        self.outbox_retention_seconds = float(retention["outbox_seconds"])
        self.retention_cleanup_interval_seconds = float(
            retention["cleanup_interval_seconds"]
        )
        self.retention_cleanup_batch_size = int(
            retention["cleanup_batch_size"]
        )
        self.readiness_probe_cache_seconds = _load_positive_seconds(
            gateway_cfg,
            "readiness_probe_cache_seconds",
            1.0,
        )
        self.agent_name = gateway_cfg.get("agent_name", "main")
        idle_timeout = gateway_cfg.get("session_idle_timeout", 86400)
        max_pending = gateway_cfg.get("max_pending_messages", 20)
        max_concurrent = gateway_cfg.get("max_concurrent_llm_requests", 4)
        self.persistence = GatewayPersistence(db_path)
        self.sessions = SessionStore(
            idle_timeout=idle_timeout,
            db_path=db_path,
            max_pending_messages=max_pending,
            persistence=self.persistence,
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
        self._runtime_lease_epoch: int | None = None
        self._lease_heartbeat_task: asyncio.Task | None = None
        self._session_cleanup_task: asyncio.Task | None = None
        self._retention_cleanup_task: asyncio.Task | None = None
        self._lease_shutdown_task: asyncio.Task | None = None
        self._readiness_probe_lock = asyncio.Lock()
        self._readiness_probe_cached_at = 0.0
        self._readiness_probe_cached_result = False
        self._route_admission_locks: dict[str, asyncio.Lock] = {}
        self._route_admission_users: dict[str, int] = {}
        self._stop_lock = asyncio.Lock()
        # 异步模型客户端按需创建,Gateway 停止时统一关闭。
        self._async_client = None

    @staticmethod
    def _gateway_context_policy_name(source: SessionSource) -> str:
        """将平台会话信息收敛为稳定的上下文策略名。"""
        chat_type = str(source.chat_type or "").strip().lower()
        if chat_type == "topic" or source.thread_id:
            return "topic"
        if chat_type in {"dm", "p2p", "private"}:
            return "dm"
        if chat_type == "group":
            return "group"
        return "default"

    def _build_gateway_prompt(self, source: SessionSource) -> str:
        """按事件来源选择只读上下文与平台工具能力。"""
        policy_name = self._gateway_context_policy_name(source)
        context_policy = self._gateway_context_policies[policy_name]
        return build_system_prompt(
            os.getcwd(),
            enabled_toolsets=self._enabled_toolsets_for_source(source),
            **context_policy,
        )

    def _enabled_toolsets_for_source(
        self,
        source: SessionSource,
    ) -> list[str]:
        """返回来源平台的工具集副本，避免会话修改共享配置。"""
        platform = str(source.platform or "").strip().lower()
        return list(self._gateway_platform_toolsets.get(platform, ()))

    @staticmethod
    def _is_processing_event(event: MessageEvent) -> bool:
        """只有进入普通 Agent 流程的消息才展示平台处理状态。"""
        return (event.text or "").strip().lower() not in {
            "/new",
            "/stop",
            "/status",
        }

    @staticmethod
    def _outbox_tracks_processing(outbox: dict) -> bool:
        """控制回执和 queue-full 回执不拥有 Typing 生命周期。"""
        return str(outbox.get("delivery_kind", "")) in {
            "final",
            "internal_error",
        }

    async def _mark_processing_best_effort(
        self,
        event: MessageEvent,
    ) -> None:
        if not self._is_processing_event(event):
            return
        adapter = self.adapters.get(event.source.platform)
        if adapter is None:
            return
        try:
            await adapter.mark_processing(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                "  [gateway:audit] event=processing_status_failed "
                "operation=mark_processing "
                f"{safe_message_digest(event.message_id)} "
                f"platform={event.source.platform} "
                f"exception={type(exc).__name__}"
            )

    async def _finish_processing_best_effort(
        self,
        event: MessageEvent,
        outcome: str,
        *,
        ctx=None,
        generation: int | None = None,
    ) -> bool:
        """仅由仍持有 worker generation 的任务结束平台处理状态。"""
        if not self._is_processing_event(event):
            return False
        if (
            ctx is not None
            and generation is not None
            and getattr(ctx, "worker_generation", None) != generation
        ):
            return False
        if (
            outcome == "failed"
            and ctx is not None
            and generation is not None
            and self._task_cancel_reason(ctx, generation) is not None
        ):
            return False
        adapter = self.adapters.get(event.source.platform)
        if adapter is None:
            return False
        try:
            await adapter.finish_processing(event, outcome)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                "  [gateway:audit] event=processing_status_failed "
                f"operation=finish_{outcome} "
                f"{safe_message_digest(event.message_id)} "
                f"platform={event.source.platform} "
                f"exception={type(exc).__name__}"
            )
            return False

    @staticmethod
    def _safe_agent_result(result: dict) -> _GatewayAgentResult:
        """把 Agent 结构化错误映射为固定文案，绝不发送原始异常。"""
        final_response = result.get("final_response")
        if result.get("ok", False):
            response = str(final_response or "")
            if response:
                return _GatewayAgentResult(response)
            return _GatewayAgentResult(
                _SAFE_INTERNAL_REPLY,
                failed=True,
                failure_type="empty_response",
            )

        status = str(result.get("status", "") or "")
        error_type = str(result.get("error_type", "") or "")
        if status == "cancelled" or error_type == "cancelled":
            return _GatewayAgentResult(None)
        if error_type == "persistence_error":
            return _GatewayAgentResult(
                _SAFE_PERSISTENCE_REPLY,
                failed=True,
                failure_type="persistence_error",
            )
        if status == "model_error" or error_type == "model_error":
            detail = str(final_response or "").lower()
            if "timeout" in detail or "timed out" in detail:
                return _GatewayAgentResult(
                    _SAFE_MODEL_TIMEOUT_REPLY,
                    failed=True,
                    failure_type="model_timeout",
                )
            return _GatewayAgentResult(
                _SAFE_MODEL_UNAVAILABLE_REPLY,
                failed=True,
                failure_type="model_unavailable",
            )
        return _GatewayAgentResult(
            _SAFE_INTERNAL_REPLY,
            failed=True,
            failure_type=error_type or status or "internal_error",
        )

    @staticmethod
    def _safe_exception_result(exc: Exception) -> _GatewayAgentResult:
        """兜底异常只按类型分类，不把异常文本或本地路径发给用户。"""
        error_name = type(exc).__name__.lower()
        error_module = type(exc).__module__.lower()
        if "timeout" in error_name:
            return _GatewayAgentResult(
                _SAFE_MODEL_TIMEOUT_REPLY,
                failed=True,
                failure_type="model_timeout",
            )
        if "connection" in error_name or error_module.startswith("openai"):
            return _GatewayAgentResult(
                _SAFE_MODEL_UNAVAILABLE_REPLY,
                failed=True,
                failure_type="model_unavailable",
            )
        if "sqlite" in error_module or "persistence" in error_name:
            return _GatewayAgentResult(
                _SAFE_PERSISTENCE_REPLY,
                failed=True,
                failure_type="persistence_error",
            )
        return _GatewayAgentResult(
            _SAFE_INTERNAL_REPLY,
            failed=True,
            failure_type="internal_error",
        )

    @staticmethod
    def _conversation_preview(value: object, limit: int = 50) -> str:
        """把 user 消息压成单行安全预览，并限制展示长度。"""
        preview = " ".join(str(value or "").split())
        if not preview:
            return "暂无消息"
        if len(preview) <= limit:
            return preview
        return preview[:max(1, limit - 1)] + "…"

    @staticmethod
    def _short_conversation_id(conversation_id: object) -> str:
        value = str(conversation_id or "")
        return value[:8] if value else "<unknown>"

    @classmethod
    def _format_conversation_list(cls, conversations: list[dict]) -> str:
        if not conversations:
            return "当前路由暂无可用对话。"
        lines = ["对话列表：", ""]
        for index, conversation in enumerate(conversations, start=1):
            marker = "[当前] " if conversation.get("is_current") else ""
            active_at = (
                conversation.get("last_message_at")
                or conversation.get("last_selected_at")
            )
            try:
                active_timestamp = float(active_at)
                if not math.isfinite(active_timestamp):
                    raise ValueError("invalid conversation timestamp")
                active_text = datetime.fromtimestamp(
                    active_timestamp
                ).strftime("%Y-%m-%d %H:%M")
            except (OSError, OverflowError, TypeError, ValueError):
                active_text = "未知"
            lines.extend([
                f"{index}. {marker}{cls._short_conversation_id(conversation['conversation_id'])}",
                f"   消息：{int(conversation.get('message_count', 0))} 条",
                f"   最近：{cls._conversation_preview(conversation.get('preview'))}",
                f"   活跃时间：{active_text}",
                "",
            ])
        lines.extend([
            "使用 /resume 2 切换对话。",
            "序号以当前列表为准。",
        ])
        return "\n".join(lines)

    @classmethod
    def _format_resume_success(cls, conversation: dict) -> str:
        return "\n".join([
            "━━━━━━━━━━━━━━━━━━",
            "已切换对话",
            "",
            "当前对话："
            f"{cls._short_conversation_id(conversation['conversation_id'])}",
            f"历史消息：{int(conversation.get('message_count', 0))} 条",
            "最近内容："
            f"{cls._conversation_preview(conversation.get('preview'))}",
            "",
            "后续消息将进入该对话。",
            "━━━━━━━━━━━━━━━━━━",
        ])

    @staticmethod
    def _conversation_switch_is_busy(ctx) -> bool:
        worker = getattr(ctx, "worker_task", None)
        active_task = getattr(ctx, "active_task", None)
        return bool(
            getattr(ctx, "busy", False)
            or getattr(ctx, "dispatching", False)
            or getattr(ctx, "pending", ())
            or (worker is not None and not worker.done())
            or (active_task is not None and not active_task.done())
        )

    def add_adapter(self, adapter: BasePlatformAdapter):
        adapter._on_message = self._handle_message
        adapter._message_state_lookup = self._message_persistence_state_async
        adapter.bind_persistence(self.persistence)
        adapter.bind_readiness_lookup(self.readiness_status)
        adapter.bind_audit_context_lookup(
            lambda: {"lease_epoch": self._runtime_lease_epoch}
        )
        self.adapters[adapter.platform_name] = adapter

    async def _database_readiness_probe(self) -> bool:
        """合并并短暂缓存轻量 DB 读写与 lease fencing 检查。"""
        now = time.monotonic()
        if (
            now - self._readiness_probe_cached_at
            < self.readiness_probe_cache_seconds
        ):
            return self._readiness_probe_cached_result
        async with self._readiness_probe_lock:
            now = time.monotonic()
            if (
                now - self._readiness_probe_cached_at
                < self.readiness_probe_cache_seconds
            ):
                return self._readiness_probe_cached_result
            if self._runtime_lease_epoch is None:
                result = False
            else:
                try:
                    result = await self.persistence.call(
                        check_gateway_runtime_readiness,
                        self._runtime_lease_name,
                        self._runtime_instance_id,
                        self._runtime_lease_epoch,
                    )
                except Exception as exc:
                    result = False
                    print(
                        "  [gateway:audit] event=readiness_probe_failed "
                        f"exception={type(exc).__name__} "
                        f"lease_epoch={self._runtime_lease_epoch}"
                    )
            self._readiness_probe_cached_at = time.monotonic()
            self._readiness_probe_cached_result = bool(result)
            return bool(result)

    async def _database_runtime_lease_status(self) -> tuple[bool, bool]:
        """每次 readiness 都读取 lease；第二个返回值表示结果是否为确定判断。"""
        if self._runtime_lease_epoch is None:
            return False, True
        try:
            valid = await self.persistence.call(
                gateway_runtime_lease_is_valid,
                self._runtime_lease_name,
                self._runtime_instance_id,
                self._runtime_lease_epoch,
            )
        except Exception:
            return False, False
        return bool(valid), True

    async def readiness_status(self, platform_name: str) -> dict:
        """按平台聚合 lifecycle、lease、恢复、接收、dispatcher 与 DB 状态。"""
        adapter = self.adapters.get(platform_name)
        local = adapter.readiness_snapshot() if adapter is not None else {}
        checks = {
            "lifecycle_running": self._lifecycle_phase == "running",
            "runtime_lease_local": bool(
                self._runtime_lease_acquired
                and self._runtime_lease_valid
                and self._runtime_lease_epoch is not None
            ),
            "adapter_initialized": bool(
                adapter is not None
                and self._adapter_initialized.get(platform_name, False)
            ),
            "inbox_restored": platform_name in self._inbox_restored_adapters,
            "webhook_receiving": bool(
                self._accepting_external_messages
                and platform_name in self._receiving_adapters
                and local.get("adapter_receiving", False)
            ),
            "durable_inbox_dispatcher": bool(
                local.get("durable_dispatcher", False)
            ),
            "runtime_lease_database": False,
            "database_read_write": False,
        }
        local_ready = all(
            value
            for name, value in checks.items()
            if name not in {
                "runtime_lease_database",
                "database_read_write",
            }
        )
        if local_ready:
            lease_valid, definitive = (
                await self._database_runtime_lease_status()
            )
            checks["runtime_lease_database"] = lease_valid
            if not lease_valid and definitive:
                self._handle_runtime_lease_loss(None)
                checks["lifecycle_running"] = False
                checks["runtime_lease_local"] = False
                checks["webhook_receiving"] = False
            elif lease_valid:
                checks["database_read_write"] = (
                    await self._database_readiness_probe()
                )
        return {
            "ready": all(checks.values()),
            "platform": platform_name,
            "checks": checks,
            "lease_epoch": self._runtime_lease_epoch,
        }

    def _reconcile_terminal_deliveries(self) -> int:
        """在 Gateway 恢复前一次性收敛旧终态记录。"""
        conn = init_db(self.db_path)
        try:
            return reconcile_gateway_terminal_deliveries(
                conn,
                **self._runtime_fence_kwargs(),
            )
        finally:
            conn.close()

    async def _reconcile_terminal_deliveries_async(self) -> int:
        return await self.persistence.call(
            reconcile_gateway_terminal_deliveries,
            **self._runtime_fence_kwargs(),
        )

    def _acquire_runtime_lease(self) -> dict | None:
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
        if self._runtime_lease_epoch is None:
            return False
        conn = init_db(self.db_path)
        try:
            return renew_gateway_runtime_lease(
                conn,
                self._runtime_lease_name,
                self._runtime_instance_id,
                self._runtime_lease_epoch,
                self.runtime_lease_ttl_seconds,
            )
        finally:
            conn.close()

    def _release_runtime_lease(self) -> bool:
        """释放当前实例的租约；实例不匹配时不会删除其他持有者。"""
        if self._runtime_lease_epoch is None:
            return False
        conn = init_db(self.db_path)
        try:
            return release_gateway_runtime_lease(
                conn,
                self._runtime_lease_name,
                self._runtime_instance_id,
                self._runtime_lease_epoch,
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

    def _runtime_fence_kwargs(self) -> dict:
        """正式运行携带 fencing；未调用 start 的嵌入式路径保持兼容。"""
        if not self._runtime_lease_acquired:
            return {}
        if self._runtime_lease_epoch is None:
            raise RuntimeError("gateway runtime lease epoch is unavailable")
        return {
            "lease_name": self._runtime_lease_name,
            "instance_id": self._runtime_instance_id,
            "lease_epoch": self._runtime_lease_epoch,
        }

    def _database_delivery_fence_state(
        self,
        outbox_id: str,
    ) -> tuple[bool, bool]:
        """读取数据库 lease 与 Outbox claim 的当前匹配结果。"""
        if not self._runtime_lease_acquired:
            return True, True
        if self._runtime_lease_epoch is None:
            return False, False
        conn = init_db(self.db_path)
        try:
            claim_valid = gateway_outbox_claim_is_valid(
                conn,
                outbox_id,
                self._runtime_lease_name,
                self._runtime_instance_id,
                self._runtime_lease_epoch,
            )
            if claim_valid:
                return True, True
            lease_valid = gateway_runtime_lease_is_valid(
                conn,
                self._runtime_lease_name,
                self._runtime_instance_id,
                self._runtime_lease_epoch,
            )
            return lease_valid, False
        finally:
            conn.close()

    async def _database_delivery_fence_state_async(
        self,
        outbox_id: str,
    ) -> tuple[bool, bool]:
        """异步读取数据库 lease 与 Outbox claim 的匹配结果。"""
        if not self._runtime_lease_acquired:
            return True, True
        if self._runtime_lease_epoch is None:
            return False, False
        claim_valid = await self.persistence.call(
            gateway_outbox_claim_is_valid,
            outbox_id,
            self._runtime_lease_name,
            self._runtime_instance_id,
            self._runtime_lease_epoch,
        )
        if claim_valid:
            return True, True
        lease_valid = await self.persistence.call(
            gateway_runtime_lease_is_valid,
            self._runtime_lease_name,
            self._runtime_instance_id,
            self._runtime_lease_epoch,
        )
        return lease_valid, False

    def _outbox_send_fence_is_valid(self, outbox_id: str) -> bool:
        """发送前同时确认本地资格、数据库租约和 Outbox claim。"""
        if self._runtime_lease_blocks_delivery():
            return False
        lease_valid, claim_valid = self._database_delivery_fence_state(
            outbox_id
        )
        if not lease_valid:
            self._handle_runtime_lease_loss(None)
            return False
        return claim_valid

    async def _outbox_send_fence_is_valid_async(self, outbox_id: str) -> bool:
        """异步确认本地资格、数据库租约和 Outbox claim。"""
        if self._runtime_lease_blocks_delivery():
            return False
        lease_valid, claim_valid = (
            await self._database_delivery_fence_state_async(outbox_id)
        )
        if not lease_valid:
            self._handle_runtime_lease_loss(None)
            return False
        return claim_valid

    def _handle_runtime_lease_loss(self, error_type: str | None) -> None:
        """先撤销运行资格，再调度不会自等待的统一安全停止。"""
        if not self._runtime_lease_valid:
            return
        self._runtime_lease_valid = False
        self._accepting_external_messages = False
        self._lifecycle_phase = "lease_lost"
        if error_type:
            print(
                "  [gateway:audit] event=runtime_lease_lost "
                f"reason=renewal_failed exception={error_type} "
                f"lease_epoch={self._runtime_lease_epoch}"
            )
        else:
            print(
                "  [gateway:audit] event=runtime_lease_lost "
                "reason=ownership_lost "
                f"lease_epoch={self._runtime_lease_epoch}"
            )

        # 失租等同 shutdown：保留可恢复 Outbox，不把它误标为用户取消。
        for adapter in self.adapters.values():
            adapter.revoke_receiving()
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
                    if self._runtime_lease_epoch is None:
                        renewed = False
                    else:
                        renewed = await self.persistence.call(
                            renew_gateway_runtime_lease,
                            self._runtime_lease_name,
                            self._runtime_instance_id,
                            self._runtime_lease_epoch,
                            self.runtime_lease_ttl_seconds,
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
                    protected = await self.persistence.call(
                        get_gateway_routes_with_pending_outbox,
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

    async def _prune_retention_batches(
        self,
        operation,
        *,
        updated_before: float,
    ) -> int:
        """用多个独立小事务清理，单轮设置上限以免长期占用写锁。"""
        total = 0
        for _ in range(4):
            removed = await self.persistence.call(
                operation,
                updated_before=updated_before,
                limit=self.retention_cleanup_batch_size,
            )
            total += int(removed)
            if int(removed) < self.retention_cleanup_batch_size:
                break
            await asyncio.sleep(0)
        return total

    async def _run_retention_cleanup(self) -> None:
        """Outbox 先释放引用，再清理无引用 ownership；失败不影响运行。"""
        now = time.time()
        try:
            outbox_removed = await self._prune_retention_batches(
                prune_gateway_terminal_outbox,
                updated_before=now - self.outbox_retention_seconds,
            )
            if outbox_removed:
                print(
                    "  [gateway:audit] event=retention_cleanup "
                    f"kind=outbox removed={outbox_removed} "
                    f"lease_epoch={self._runtime_lease_epoch}"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                "  [gateway:audit] event=retention_cleanup_failed "
                f"kind=outbox exception={type(exc).__name__} "
                f"lease_epoch={self._runtime_lease_epoch}"
            )

        try:
            ownership_removed = await self._prune_retention_batches(
                prune_gateway_terminal_ownership,
                updated_before=now - self.ownership_retention_seconds,
            )
            if ownership_removed:
                print(
                    "  [gateway:audit] event=retention_cleanup "
                    f"kind=ownership removed={ownership_removed} "
                    f"lease_epoch={self._runtime_lease_epoch}"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                "  [gateway:audit] event=retention_cleanup_failed "
                f"kind=ownership exception={type(exc).__name__} "
                f"lease_epoch={self._runtime_lease_epoch}"
            )

    async def _retention_cleanup_loop(self) -> None:
        """Gateway running 期间周期执行有界审计清理。"""
        try:
            while self._lifecycle_phase == "running":
                await self._run_retention_cleanup()
                await asyncio.sleep(self.retention_cleanup_interval_seconds)
        except asyncio.CancelledError:
            raise

    def _start_runtime_lease_heartbeat(self) -> None:
        """获取 lease 后立即启动 heartbeat，覆盖整个初始化与恢复阶段。"""
        task = self._lease_heartbeat_task
        if task is not None and not task.done():
            return
        self._lease_heartbeat_task = asyncio.create_task(
            self._runtime_lease_heartbeat_loop(),
            name="gateway-runtime-lease-heartbeat",
        )

    def _start_session_cleanup(self) -> None:
        """只有进入 running 后才启动会话空闲清理。"""
        task = self._session_cleanup_task
        if task is not None and not task.done():
            return
        self._session_cleanup_task = asyncio.create_task(
            self._session_cleanup_loop(),
            name="gateway-session-cleanup",
        )

    def _start_retention_cleanup(self) -> None:
        """进入 running 后启动可失败、可取消的分批保留期清理。"""
        task = self._retention_cleanup_task
        if task is not None and not task.done():
            return
        self._retention_cleanup_task = asyncio.create_task(
            self._retention_cleanup_loop(),
            name="gateway-retention-cleanup",
        )

    async def _cancel_background_tasks(self) -> None:
        """取消并回收长期后台任务，避免事件循环退出时残留 Task。"""
        current = asyncio.current_task()
        tasks = [
            task
            for task in (
                self._lease_heartbeat_task,
                self._session_cleanup_task,
                self._retention_cleanup_task,
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
        if self._retention_cleanup_task is not current:
            self._retention_cleanup_task = None

    async def _require_startup_runtime_lease(self) -> None:
        """启动阶段一旦失租，等待统一安全停止完成后终止启动。"""
        if self._runtime_lease_valid:
            return
        shutdown_task = self._lease_shutdown_task
        if (
            shutdown_task is not None
            and shutdown_task is not asyncio.current_task()
        ):
            await asyncio.gather(shutdown_task, return_exceptions=True)
        self._startup_in_progress = False
        raise RuntimeError("gateway runtime lease lost during startup")

    async def _abort_startup_after_lease(self) -> None:
        """启动恢复失败时停止已创建资源并尽早交还租约。"""
        self._accepting_external_messages = False
        self._runtime_lease_valid = False
        await self._cancel_background_tasks()
        active_tasks = self.sessions.cancel_all(reason="shutdown")
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        for adapter in self.adapters.values():
            try:
                await adapter.disconnect()
            except Exception as exc:
                print(
                    "  [gateway] startup adapter disconnect failed: "
                    f"platform={adapter.platform_name} "
                    f"exception={type(exc).__name__}"
                )
        if self._runtime_lease_acquired:
            try:
                if self._runtime_lease_epoch is not None:
                    await self.persistence.call(
                        release_gateway_runtime_lease,
                        self._runtime_lease_name,
                        self._runtime_instance_id,
                        self._runtime_lease_epoch,
                    )
            except Exception as exc:
                print(
                    "  [gateway] runtime lease release failed: "
                    f"{type(exc).__name__}"
                )
            finally:
                self._runtime_lease_acquired = False
                self._runtime_lease_epoch = None

    async def start(self):
        """按初始化、终态收敛、持久恢复、接收阶段启动 Gateway。"""
        if self._lifecycle_phase != "created":
            raise RuntimeError(
                "GatewayRunner instances are single-use; create a new "
                "instance after stop or failed startup."
            )
        self._startup_in_progress = True
        self._accepting_external_messages = False
        self._inbox_restored_adapters.clear()
        self._receiving_adapters.clear()
        self._startup_message_states.clear()

        self._lifecycle_phase = "acquire_runtime_lease"
        try:
            acquired = await self.persistence.call(
                acquire_gateway_runtime_lease,
                self._runtime_lease_name,
                self._runtime_instance_id,
                self.runtime_lease_ttl_seconds,
            )
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
        if str(acquired["instance_id"]) != self._runtime_instance_id:
            raise RuntimeError("gateway runtime lease identity mismatch")
        self._runtime_lease_epoch = int(acquired["lease_epoch"])
        self._runtime_lease_acquired = True
        self._runtime_lease_valid = True
        self._start_runtime_lease_heartbeat()

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
            await self._require_startup_runtime_lease()

        self._lifecycle_phase = "gateway_terminal_reconcile"
        try:
            await self._reconcile_terminal_deliveries_async()
            self._lifecycle_phase = "gateway_approval_reconcile"
            await self.persistence.call(recover_gateway_approvals)
            await self._require_startup_runtime_lease()
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
            await self._require_startup_runtime_lease()
            self._lifecycle_phase = "gateway_queue_restore"
            await self._restore_queued_messages()
            await self._require_startup_runtime_lease()
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
            await self._require_startup_runtime_lease()

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
            await self._require_startup_runtime_lease()
        await self._require_startup_runtime_lease()
        self._startup_in_progress = False
        # 飞书 Inbox 已完成去重，此后实时事件重新以数据库和 Adapter 自身
        # completed 记录为准，不让启动快照变成长生命周期内存真相源。
        self._startup_message_states.clear()
        self._lifecycle_phase = "running"
        self._start_session_cleanup()
        self._start_retention_cleanup()

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

            # 先同步关闭所有外部入站资格，不能在等待 worker 时继续落库。
            for name, adapter in self.adapters.items():
                try:
                    adapter.revoke_receiving()
                except Exception as exc:
                    print(
                        f"  [gateway] {name} receive revoke failed: "
                        f"{type(exc).__name__}"
                    )
                finally:
                    self._receiving_adapters.discard(name)
            self._receiving_adapters.clear()

            # 入站关闭后再停止 heartbeat / housekeeping 和 route worker。
            await self._cancel_background_tasks()
            active_tasks = self.sessions.cancel_all(reason="shutdown")
            if active_tasks:
                await asyncio.gather(*active_tasks, return_exceptions=True)

            for adapter in self.adapters.values():
                try:
                    await adapter.disconnect()
                except Exception as exc:
                    print(
                        "  [gateway] adapter disconnect failed: "
                        f"platform={adapter.platform_name} "
                        f"exception={type(exc).__name__}"
                    )

            if self._runtime_lease_acquired:
                try:
                    if self._runtime_lease_epoch is not None:
                        await self.persistence.call(
                            release_gateway_runtime_lease,
                            self._runtime_lease_name,
                            self._runtime_instance_id,
                            self._runtime_lease_epoch,
                        )
                except Exception as exc:
                    print(
                        "  [gateway] runtime lease release failed: "
                        f"{type(exc).__name__}"
                    )
                finally:
                    self._runtime_lease_acquired = False
                    self._runtime_lease_epoch = None

            if self._async_client is not None:
                try:
                    await self._async_client.close()
                except Exception as exc:
                    print(
                        "  [gateway] model client close failed: "
                        f"{type(exc).__name__}"
                    )
                finally:
                    self._async_client = None
            from hermes.backends import cleanup_all_backends
            cleanup_all_backends()
            self._receiving_adapters.clear()
            self._inbox_restored_adapters.clear()
            self._startup_in_progress = False
            await self.persistence.close()
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

    async def _message_persistence_state_async(
        self,
        event: MessageEvent,
    ) -> dict | None:
        """异步查询平台消息是否已由 Gateway 持久层接管。"""
        route_key = build_session_key(event.source, self.agent_name)
        persisted = await self.persistence.call(
            get_gateway_message_persistence_state,
            route_key,
            event.message_id,
        )
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
        return bool(
            ctx.busy
            or ctx.dispatching
            or (worker is not None and not worker.done())
        )

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

    async def _persist_event_async(
        self,
        route_key: str,
        event: MessageEvent,
    ) -> bool:
        accepted = await self.persistence.call(
            enqueue_gateway_message,
            route_key,
            event.message_id,
            self._serialize_event(event),
        )
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

    async def _mark_event_processing_async(
        self,
        route_key: str,
        event: MessageEvent,
    ) -> None:
        await self.persistence.call(
            mark_gateway_message_processing,
            route_key,
            event.message_id,
        )

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

    async def _complete_event_async(
        self,
        route_key: str,
        event: MessageEvent,
    ) -> bool:
        """异步删除已完成的 Queue 恢复记录。"""
        try:
            await self.persistence.call(
                complete_gateway_message,
                route_key,
                event.message_id,
            )
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
        *,
        queue_message_id: str | None = None,
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
            "queue_message_id": queue_message_id or event.message_id,
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
            return enqueue_gateway_outbox(
                conn,
                outbox,
                **self._runtime_fence_kwargs(),
            )
        finally:
            conn.close()

    async def _enqueue_outbox_async(self, outbox: dict) -> str:
        return await self.persistence.call(
            enqueue_gateway_outbox,
            outbox,
            **self._runtime_fence_kwargs(),
        )

    def _load_outbox(self, outbox_id: str) -> dict | None:
        conn = init_db(self.db_path)
        try:
            return get_gateway_outbox(conn, outbox_id)
        finally:
            conn.close()

    async def _load_outbox_async(self, outbox_id: str) -> dict | None:
        return await self.persistence.call(get_gateway_outbox, outbox_id)

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
                **self._runtime_fence_kwargs(),
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

    async def _cancel_outbox_async(
        self,
        outbox_id: str,
        *,
        route_key: str | None = None,
        source_message_id: str | None = None,
    ) -> bool:
        outbox = await self._load_outbox_async(outbox_id)
        if outbox is None:
            return False
        expected_route = route_key or str(outbox["route_key"])
        expected_source = source_message_id or str(
            outbox["source_message_id"]
        )
        cancelled = await self.persistence.call(
            cancel_gateway_delivery,
            outbox_id,
            expected_route,
            expected_source,
            **self._runtime_fence_kwargs(),
        )
        if not cancelled:
            current = await self._load_outbox_async(outbox_id)
            cancelled = bool(
                current
                and current["status"] in {
                    "cancelled",
                    "partial_cancelled",
                    "delivered",
                }
            )
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
                **self._runtime_fence_kwargs(),
            )
            if not completed:
                current = get_gateway_outbox(conn, outbox_id)
                completed = bool(
                    current and current["status"] == "delivered"
                )
                if not completed and self._runtime_lease_acquired:
                    self._outbox_send_fence_is_valid(outbox_id)
        finally:
            conn.close()
        if completed:
            self._accepted_messages.discard((route_key, event.message_id))
        return completed

    async def _complete_outbox_async(
        self,
        outbox_id: str,
        route_key: str,
        event: MessageEvent,
    ) -> bool:
        completed = await self.persistence.call(
            complete_gateway_delivery,
            outbox_id,
            route_key,
            event.message_id,
            **self._runtime_fence_kwargs(),
        )
        if not completed:
            current = await self._load_outbox_async(outbox_id)
            completed = bool(current and current["status"] == "delivered")
            if not completed and self._runtime_lease_acquired:
                await self._outbox_send_fence_is_valid_async(outbox_id)
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
                **self._runtime_fence_kwargs(),
            )
        finally:
            conn.close()

    async def _fail_outbox_async(
        self,
        outbox_id: str,
        route_key: str,
        event: MessageEvent,
        error: str,
        error_code: str | None,
    ) -> bool:
        return await self.persistence.call(
            fail_gateway_delivery,
            outbox_id,
            route_key,
            event.message_id,
            error,
            error_code,
            **self._runtime_fence_kwargs(),
        )

    def _request_session_cancel(self, route_key: str, reason: str) -> bool:
        """失效内存任务，并立即持久化终止当前未完成 Outbox。"""
        ctx = self.sessions.get(route_key)
        delivery_id = ctx.delivery_id if ctx is not None else None
        cancelled = self.sessions.request_cancel(route_key, reason=reason)
        if cancelled and reason != "shutdown" and delivery_id:
            self._cancel_outbox(delivery_id, route_key=route_key)
        return cancelled

    async def _request_session_cancel_async(
        self,
        route_key: str,
        reason: str,
    ) -> bool:
        """先同步失效内存任务，再异步持久化显式 Outbox 取消。"""
        ctx = self.sessions.get(route_key)
        delivery_id = ctx.delivery_id if ctx is not None else None
        cancelled = self.sessions.request_cancel(route_key, reason=reason)
        if cancelled and reason != "shutdown" and delivery_id:
            await self._cancel_outbox_async(
                delivery_id,
                route_key=route_key,
            )
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

    async def _cancel_stale_outbox_async(
        self,
        ctx,
        generation: int | None,
        outbox_id: str,
    ) -> str | None:
        reason = self._task_cancel_reason(ctx, generation)
        if reason is not None and reason != "shutdown":
            await self._cancel_outbox_async(
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
        reason = await self._cancel_stale_outbox_async(
            ctx,
            generation,
            outbox_id,
        )
        if reason is not None or delay <= 0:
            return reason
        if invalidation_event is None:
            await asyncio.sleep(delay)
        else:
            try:
                await asyncio.wait_for(invalidation_event.wait(), timeout=delay)
            except TimeoutError:
                pass
        return await self._cancel_stale_outbox_async(
            ctx,
            generation,
            outbox_id,
        )

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

    async def _mark_delivery_failed_without_outbox_async(
        self,
        route_key: str,
        event: MessageEvent,
    ) -> None:
        await self.persistence.call(
            mark_gateway_message_delivery_failed,
            route_key,
            event.message_id,
        )

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
            outbox = await self._load_outbox_async(outbox_id)
            if outbox is None:
                raise RuntimeError("gateway outbox is missing")
            if self._runtime_lease_blocks_delivery():
                return None
            if await self._cancel_stale_outbox_async(
                ctx,
                generation,
                outbox_id,
            ) is not None:
                return None
            if outbox["status"] == "delivered":
                if self._outbox_tracks_processing(outbox):
                    await self._finish_processing_best_effort(
                        event,
                        (
                            "failed"
                            if outbox.get("delivery_kind") == "internal_error"
                            else "success"
                        ),
                        ctx=ctx,
                        generation=generation,
                    )
                return True
            if outbox["status"] in (
                "permanent_failed",
                "cancelled",
                "partial_cancelled",
            ):
                if self._outbox_tracks_processing(outbox):
                    outcome = (
                        "failed"
                        if outbox["status"] == "permanent_failed"
                        else "cancelled"
                    )
                    await self._finish_processing_best_effort(
                        event,
                        outcome,
                        ctx=ctx,
                        generation=generation,
                    )
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
            if await self._cancel_stale_outbox_async(
                ctx,
                generation,
                outbox_id,
            ) is not None:
                return None

            adapter = self.adapters.get(outbox["platform"])
            can_send = await self.persistence.call(
                mark_gateway_outbox_sending,
                outbox_id,
                **self._runtime_fence_kwargs(),
            )
            if not can_send:
                if self._runtime_lease_acquired:
                    await self._outbox_send_fence_is_valid_async(outbox_id)
                    return None
                continue

            payloads = outbox["payloads"]
            message_ids = list(outbox["message_ids"])
            failed_result = None
            failed_index = None
            for index in range(outbox["next_chunk_index"], len(payloads)):
                if self._runtime_lease_blocks_delivery():
                    return None
                if (
                    await self._cancel_stale_outbox_async(
                        ctx,
                        generation,
                        outbox_id,
                    )
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
                    if not await self._outbox_send_fence_is_valid_async(
                        outbox_id
                    ):
                        return None
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
                            "  [gateway:audit] event=outbox_send_error "
                            f"{safe_route_digest(route_key)} "
                            f"delivery_id={outbox_id} "
                            f"lease_epoch={self._runtime_lease_epoch} "
                            f"exception={type(exc).__name__}"
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
                progress_saved = await self.persistence.call(
                    mark_gateway_outbox_chunk_sent,
                    outbox_id,
                    index + 1,
                    message_ids,
                    len(payloads),
                    **self._runtime_fence_kwargs(),
                )
                if not progress_saved:
                    if self._runtime_lease_acquired:
                        await self._outbox_send_fence_is_valid_async(outbox_id)
                    return None

                all_chunks_sent = index + 1 >= len(payloads)
                if all_chunks_sent:
                    # 平台已经确认完整回答。即使取消与本次成功并发，delivered
                    # 也必须优先，取消不能反向抹除已发生的外部事实。
                    delivered = await self._complete_outbox_async(
                        outbox_id,
                        route_key,
                        event,
                    )
                    if delivered and self._outbox_tracks_processing(outbox):
                        await self._finish_processing_best_effort(
                            event,
                            (
                                "failed"
                                if outbox.get("delivery_kind") == "internal_error"
                                else "success"
                            ),
                            ctx=ctx,
                            generation=generation,
                        )
                    return True if delivered else None

                # 成功进度已经持久化；从这里开始，取消只终止尚未发送的分片。
                if self._runtime_lease_blocks_delivery():
                    return None
                if (
                    await self._cancel_stale_outbox_async(
                        ctx,
                        generation,
                        outbox_id,
                    )
                    is not None
                ):
                    return None

            if failed_result is None:
                # 恢复时可能读取到 next_chunk_index 已经等于总片数、但进程
                # 尚未来得及写 delivered 的记录；完整进度同样优先于取消。
                delivered = await self._complete_outbox_async(
                    outbox_id,
                    route_key,
                    event,
                )
                if delivered and self._outbox_tracks_processing(outbox):
                    await self._finish_processing_best_effort(
                        event,
                        (
                            "failed"
                            if outbox.get("delivery_kind") == "internal_error"
                            else "success"
                        ),
                        ctx=ctx,
                        generation=generation,
                    )
                return True if delivered else None

            attempt = int(outbox["attempt_count"]) + 1
            max_attempts = self._delivery_attempt_limit(outbox)
            error = (failed_result.error or "internal_send_error")[:120]
            if self._runtime_lease_blocks_delivery():
                return None
            if await self._cancel_stale_outbox_async(
                ctx,
                generation,
                outbox_id,
            ) is not None:
                return None
            if (
                not failed_result.retryable
                or attempt >= max_attempts
            ):
                permanently_failed = await self._fail_outbox_async(
                    outbox_id,
                    route_key,
                    event,
                    error,
                    failed_result.error_code,
                )
                if not permanently_failed:
                    current = await self._load_outbox_async(outbox_id)
                    if not current or current["status"] != "permanent_failed":
                        if self._runtime_lease_acquired:
                            await self._outbox_send_fence_is_valid_async(
                                outbox_id
                            )
                        return None
                print(
                    "  [gateway:audit] event=outbox_permanent_failure "
                    f"{safe_route_digest(route_key)} "
                    f"delivery_id={outbox_id} "
                    f"attempt_count={attempt} "
                    "retry_status=permanent_failed "
                    f"lease_epoch={self._runtime_lease_epoch} "
                    f"failure_type={error} chunk={failed_index}"
                )
                if self._outbox_tracks_processing(outbox):
                    await self._finish_processing_best_effort(
                        event,
                        "failed",
                        ctx=ctx,
                        generation=generation,
                    )
                return False

            delay = self._delivery_retry_delay(
                attempt,
                failed_result.retry_after_seconds,
            )
            if await self._cancel_stale_outbox_async(
                ctx,
                generation,
                outbox_id,
            ) is not None:
                return None
            retry_scheduled = await self.persistence.call(
                mark_gateway_outbox_retry,
                outbox_id,
                error,
                failed_result.error_code,
                time.time() + delay,
                **self._runtime_fence_kwargs(),
            )
            if not retry_scheduled:
                if self._runtime_lease_acquired:
                    await self._outbox_send_fence_is_valid_async(outbox_id)
                return None
            print(
                "  [gateway:audit] event=outbox_retry "
                f"{safe_route_digest(route_key)} "
                f"delivery_id={outbox_id} "
                f"attempt_count={attempt} "
                "retry_status=retry_wait "
                f"lease_epoch={self._runtime_lease_epoch} "
                f"failure_type={error} max_attempts={max_attempts} "
                f"delay_seconds={delay:.1f}"
            )
            if await self._cancel_stale_outbox_async(
                ctx,
                generation,
                outbox_id,
            ) is not None:
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

    async def _start_durable_reply_async(
        self,
        route_key: str,
        event: MessageEvent,
        content: str,
        delivery_kind: str,
        ctx,
    ) -> str:
        """异步持久化控制回执，route 空闲时再创建投递 worker。"""
        delivery_id = str(uuid.uuid4())
        outbox = self._build_outbox(
            route_key,
            event,
            content,
            delivery_id,
            delivery_kind,
        )
        delivery_id = await self._enqueue_outbox_async(outbox)
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
                owns_worker
                and cancel_reason is not None
                and cancel_reason != "shutdown"
            ):
                await self._finish_processing_best_effort(
                    event,
                    "cancelled",
                    ctx=ctx,
                    generation=generation,
                )
            if (
                ctx.delivery_id == delivery_id
                and ctx.delivery_generation == generation
            ):
                ctx.delivery_id = None
                ctx.delivery_generation = None
            if owns_worker:
                if cancel_reason != "shutdown":
                    ctx.dispatching = True
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

    async def _drop_events_async(
        self,
        route_key: str,
        events: list[MessageEvent],
    ) -> None:
        message_ids = [event.message_id for event in events]
        await self.persistence.call(
            delete_gateway_messages,
            route_key,
            message_ids,
        )
        for event in events:
            self._accepted_messages.discard((route_key, event.message_id))
            await self._finish_processing_best_effort(
                event,
                "cancelled",
            )

    async def _restore_outbound_messages(self) -> None:
        """按 route_key 恢复已生成但尚未完整送达的回复。"""
        await self.persistence.call(
            reset_gateway_sending_outbox,
            **self._runtime_fence_kwargs(),
        )
        rows = await self.persistence.call(get_recoverable_gateway_outbox)

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
                route_source: SessionSource | None = None
                for row in route_rows:
                    event = self._deserialize_event(row["event_json"])
                    expected = build_session_key(event.source, self.agent_name)
                    if expected != route_key:
                        raise ValueError("route key mismatch")
                    if event.source.platform != platform:
                        raise ValueError("outbox platform mismatch")
                    if route_source is None:
                        route_source = event.source
                    self._remember_startup_message(
                        route_key,
                        event,
                        layer="outbox",
                        status=str(row["status"]),
                    )
                if route_source is None:
                    raise ValueError("outbox route contains no events")
                ctx = await self.sessions.get_or_create_async(
                    route_key,
                    self._build_gateway_prompt(route_source),
                )
                if self._route_has_active_worker(ctx):
                    print(
                        "  [gateway] outbox recovery skipped duplicate worker "
                        f"(route={route_key})"
                    )
                    continue
                for row in route_rows:
                    if self._outbox_tracks_processing(row):
                        event = self._deserialize_event(row["event_json"])
                        await self._mark_processing_best_effort(event)
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
        if not rows:
            raise ValueError("outbox route contains no events")
        first_event = self._deserialize_event(rows[0]["event_json"])
        ctx = await self.sessions.get_or_create_async(
            route_key,
            self._build_gateway_prompt(first_event.source),
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
                            await self._cancel_outbox_async(
                                stale_row["id"],
                                route_key=route_key,
                                source_message_id=stale_row[
                                    "source_message_id"
                                ],
                            )
                            stale_event = self._deserialize_event(
                                stale_row["event_json"]
                            )
                            await self._finish_processing_best_effort(
                                stale_event,
                                "cancelled",
                                ctx=ctx,
                                generation=generation,
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
                if cancel_reason != "shutdown":
                    ctx.dispatching = True
                ctx.worker_task = None
                ctx.worker_generation = None
                ctx.busy = False
        if cancel_reason != "shutdown":
            await self._dispatch_next(ctx)

    async def _restore_queued_messages(self) -> None:
        """恢复 queued / processing；已有 Outbox worker 的 route 进入 pending。"""
        await self.persistence.call(reset_gateway_processing_messages)
        rows = await self.persistence.call(get_gateway_queued_messages)

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
        """按 route 串行完成数据库 admission 与内存任务注册。"""
        route_key = build_session_key(event.source, self.agent_name)
        lock = self._route_admission_locks.setdefault(
            route_key,
            asyncio.Lock(),
        )
        self._route_admission_users[route_key] = (
            self._route_admission_users.get(route_key, 0) + 1
        )
        try:
            async with lock:
                return await self._handle_message_serialized(
                    event,
                    from_queue=from_queue,
                )
        finally:
            users = self._route_admission_users.get(route_key, 1) - 1
            if users <= 0:
                self._route_admission_users.pop(route_key, None)
                if self._route_admission_locks.get(route_key) is lock:
                    self._route_admission_locks.pop(route_key, None)
            else:
                self._route_admission_users[route_key] = users

    async def _execute_claimed_approval(
        self,
        request: dict,
    ) -> tuple[str, bool, dict]:
        """执行数据库中已 claim 的原始工具参数，并立即固化结果。"""
        from hermes.tools import registry

        try:
            dispatch_context = {
                "session_key": request["conversation_id"],
                "interactive_approval": False,
                "approval_mode": "remote",
                "approval_grant": {
                    "id": request["id"],
                    "tool_name": request["tool_name"],
                    "arguments": dict(request["tool_args"]),
                },
            }
            # 只有已从 pending 原子 claim 为 executing 的 File 请求能获得本次内部许可。
            if request["tool_name"] == "file":
                dispatch_context["allow_sensitive"] = True

            output = await asyncio.to_thread(
                registry.dispatch,
                request["tool_name"],
                dict(request["tool_args"]),
                **dispatch_context,
            )
            try:
                payload = json.loads(output)
            except (TypeError, ValueError):
                succeeded = False
            else:
                succeeded = not (
                    isinstance(payload, dict) and payload.get("ok") is False
                )
        except Exception as exc:
            output = json.dumps(
                {
                    "ok": False,
                    "error_type": "approval_execution_failed",
                    "error": f"Approved tool execution failed: {type(exc).__name__}",
                },
                ensure_ascii=False,
            )
            succeeded = False

        terminal = await self.persistence.call(
            finish_gateway_approval_and_enqueue_resume,
            request["id"],
            output,
            succeeded=succeeded,
        )
        return output, succeeded, terminal

    async def _pending_approval_for_context(self, route_key: str, ctx):
        """读取当前 route/conversation 的未决请求。"""
        return await self.persistence.call(
            get_pending_gateway_approval,
            route_key,
            ctx.conversation_id,
        )

    async def _reject_approval_resume_task(
        self,
        route_key: str,
        event: MessageEvent,
    ) -> None:
        """把不满足可信恢复条件的内部 Queue 任务收敛为失败终态。"""
        await self.persistence.call(
            mark_gateway_message_delivery_failed,
            route_key,
            event.message_id,
        )
        self._accepted_messages.discard((route_key, event.message_id))
        print(
            "  [gateway:audit] event=approval_resume_rejected "
            f"{safe_route_digest(route_key)} "
            f"{safe_message_digest(event.message_id)} "
            "failure_type=invalid_approval_resume_task"
        )

    async def _handle_message_serialized(
        self,
        event: MessageEvent,
        *,
        from_queue: bool = False,
    ):
        """所有 adapter 的入站消息在此汇聚。"""
        if self._runtime_lease_blocks_delivery():
            raise RuntimeError(
                f"gateway runtime lease is invalid during {self._lifecycle_phase}"
            )
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
            persisted = await self._message_persistence_state_async(event)
            if persisted is not None:
                self._accepted_messages.add(queue_key)
                return
            if queue_key in self._accepted_messages:
                return

        approval_resume_id = None
        delivery_event = event
        if from_queue and event.message_id.startswith(
            _APPROVAL_RESUME_MESSAGE_PREFIX
        ):
            approval_resume_id = event.message_id[
                len(_APPROVAL_RESUME_MESSAGE_PREFIX):
            ]
            ctx = await self.sessions.get_or_create_async(
                route_key,
                self._build_gateway_prompt(event.source),
            )
            resume_record = await self.persistence.call(
                get_gateway_approval_resume,
                route_key,
                ctx.conversation_id,
                event.message_id,
                approval_resume_id,
            )
            if resume_record is None:
                await self._reject_approval_resume_task(route_key, event)
                return
            try:
                approval = resume_record["approval"]
                delivery_event = self._deserialize_event(
                    approval["source_event_json"]
                )
                if (
                    build_session_key(delivery_event.source, self.agent_name)
                    != route_key
                    or delivery_event.message_id
                    != approval["source_message_id"]
                ):
                    raise ValueError("approval source event identity mismatch")
            except Exception:
                await self._reject_approval_resume_task(route_key, event)
                return

        # slash 命令只规范化命令名，conversation_id 参数保持原样精确匹配。
        command_text = (event.text or "").strip()
        command_parts = command_text.split(maxsplit=1)
        cmd = command_parts[0].lower() if command_parts else ""
        command_argument = (
            command_parts[1].strip() if len(command_parts) > 1 else ""
        )
        if cmd in {"/approve", "/deny"}:
            ctx = await self.sessions.get_or_create_async(
                route_key, self._build_gateway_prompt(event.source),
            )
            content = None
            if self._route_has_active_worker(ctx):
                content = "当前任务仍在处理中，请稍后再处理审批请求。"
            elif not command_argument:
                content = f"用法：{cmd} <审批编号>"
            elif cmd == "/deny":
                decision = await self.persistence.call(
                    deny_gateway_approval,
                    route_key,
                    ctx.conversation_id,
                    event.source.user_id or event.source.user_id_alt,
                    command_argument,
                    event.message_id,
                )
                outcome = str(decision.get("outcome", ""))
                if outcome == "denied":
                    content = "已拒绝该审批请求，操作未执行。"
                else:
                    content = _approval_command_reply(outcome, command_argument)
            else:
                decision = await self.persistence.call(
                    claim_gateway_approval,
                    route_key,
                    ctx.conversation_id,
                    event.source.user_id or event.source.user_id_alt,
                    command_argument,
                    event.message_id,
                )
                outcome = str(decision.get("outcome", ""))
                if outcome == "claimed":
                    request = decision["request"]
                    _output, _succeeded, terminal = (
                        await self._execute_claimed_approval(request)
                    )
                    resume_task = terminal.get("resume_task")
                    if not isinstance(resume_task, dict):
                        raise RuntimeError(
                            "approval terminal transaction did not create resume task"
                        )
                    resume_event = self._deserialize_event(
                        str(resume_task["event_json"])
                    )
                    self._accepted_messages.add(
                        (route_key, resume_event.message_id)
                    )
                    await self._handle_message_serialized(
                        resume_event,
                        from_queue=True,
                    )
                    return
                else:
                    content = _approval_command_reply(outcome, command_argument)

            if content is not None:
                if event.source.platform not in self.adapters:
                    await self._reply(event, content)
                    return
                await self._start_durable_reply_async(
                    route_key,
                    event,
                    content,
                    "approval_command",
                    ctx,
                )
                return

        if cmd not in {"/sessions", "/status", "/new", "/stop"}:
            ctx = await self.sessions.get_or_create_async(
                route_key, self._build_gateway_prompt(event.source),
            )
            pending_approval = await self._pending_approval_for_context(
                route_key,
                ctx,
            )
            if pending_approval is not None:
                request_id = _short_approval_id(pending_approval["id"])
                content = (
                    f"当前有待审批操作 {request_id}，原任务已暂停。\n"
                    f"批准：/approve {request_id}\n"
                    f"拒绝：/deny {request_id}"
                )
                if event.source.platform not in self.adapters:
                    await self._reply(event, content)
                    return
                await self._start_durable_reply_async(
                    route_key,
                    event,
                    content,
                    "approval_pending",
                    ctx,
                )
                return
        if cmd == "/sessions" and not command_argument:
            ctx = await self.sessions.get_or_create_async(
                route_key, self._build_gateway_prompt(event.source),
            )
            conversations = await self.persistence.call(
                list_gateway_conversations,
                route_key,
                10,
            )
            content = self._format_conversation_list(conversations)
            if event.source.platform not in self.adapters:
                await self._reply(event, content)
                return
            await self._start_durable_reply_async(
                route_key,
                event,
                content,
                "sessions_command",
                ctx,
            )
            return
        if cmd == "/resume":
            ctx = await self.sessions.get_or_create_async(
                route_key, self._build_gateway_prompt(event.source),
            )
            target = None
            if not command_argument:
                content = (
                    "用法：/resume <序号或完整 conversation_id>\n"
                    "请先使用 /sessions 查看当前对话列表。"
                )
            elif self._conversation_switch_is_busy(ctx):
                content = (
                    "当前任务仍在处理中，暂不能切换对话。\n"
                    "请等待任务完成后重试，或先使用 /stop。"
                )
            else:
                if command_argument.isdecimal():
                    conversations = await self.persistence.call(
                        list_gateway_conversations,
                        route_key,
                        10,
                    )
                    try:
                        selected_index = int(command_argument) - 1
                    except ValueError:
                        selected_index = -1
                    if 0 <= selected_index < len(conversations):
                        target = conversations[selected_index]
                else:
                    target = await self.persistence.call(
                        get_gateway_conversation_for_route,
                        route_key,
                        command_argument,
                    )

                if target is None:
                    content = (
                        "未找到可切换的对话。\n"
                        "请使用 /sessions 查看当前会话的对话列表。"
                    )
                elif target["conversation_id"] == ctx.conversation_id:
                    content = (
                        "已经位于该对话："
                        f"{self._short_conversation_id(ctx.conversation_id)}"
                    )
                else:
                    try:
                        await self.sessions.switch_conversation_async(
                            route_key,
                            target["conversation_id"],
                            self._build_gateway_prompt(event.source),
                        )
                    except RuntimeError:
                        content = (
                            "当前任务仍在处理中，暂不能切换对话。\n"
                            "请等待任务完成后重试，或先使用 /stop。"
                        )
                    except ValueError:
                        content = (
                            "未找到可切换的对话。\n"
                            "请使用 /sessions 查看当前会话的对话列表。"
                        )
                    else:
                        content = self._format_resume_success(target)

            if event.source.platform not in self.adapters:
                await self._reply(event, content)
                return
            await self._start_durable_reply_async(
                route_key,
                event,
                content,
                "resume_command",
                ctx,
            )
            return
        if cmd == "/new" and not command_argument:
            ctx = await self.sessions.get_or_create_async(
                route_key, self._build_gateway_prompt(event.source),
            )
            await self.persistence.call(
                cancel_pending_gateway_approvals,
                route_key,
                ctx.conversation_id,
            )
            if self._route_has_active_worker(ctx):
                # /new 作为串行屏障:丢弃命令前尚未执行的旧消息,
                # 等当前 worker 完全退出后再切换 conversation_id。
                dropped_events = list(ctx.pending)
                await self._drop_events_async(route_key, dropped_events)
                ctx.pending.clear()
                if (
                    not from_queue
                    and not await self._persist_event_async(route_key, event)
                ):
                    return
                self.sessions.enqueue(ctx, event)
                await self._request_session_cancel_async(
                    route_key,
                    reason="new",
                )
                print(
                    f"  [gateway] {route_key}: /new queued "
                    f"({len(dropped_events)} old pending dropped)"
                )
                return
            if (
                not from_queue
                and not await self._persist_event_async(route_key, event)
            ):
                return
            ctx = await self.sessions.new_conversation_async(
                route_key, self._build_gateway_prompt(event.source),
            )
            if event.source.platform not in self.adapters:
                # 保留无 Adapter 的测试 / 嵌入式调用兼容路径。真实平台事件
                # 一定有对应 Adapter,仍走下面的持久 outbox。
                result = await self._reply(event, "(new conversation started)")
                if result is None or result.success:
                    await self._complete_event_async(route_key, event)
                else:
                    await self._mark_delivery_failed_without_outbox_async(
                        route_key,
                        event,
                    )
                await self._dispatch_next(ctx, admission_locked=True)
                return
            await self._start_durable_reply_async(
                route_key,
                event,
                "(new conversation started)",
                "new_conversation",
                ctx,
            )
            return
        if cmd == "/stop" and not command_argument:
            ctx = await self.sessions.get_or_create_async(
                route_key, self._build_gateway_prompt(event.source),
            )
            cancelled_approvals = await self.persistence.call(
                cancel_pending_gateway_approvals,
                route_key,
                ctx.conversation_id,
            )
            ok = await self._request_session_cancel_async(
                route_key,
                reason="user",
            )
            if ok:
                content = "(cancel requested)"
            elif cancelled_approvals:
                content = "(pending approval cancelled)"
            else:
                content = "(no active task)"
            if event.source.platform not in self.adapters:
                # 保留无 Adapter 的测试 / 嵌入式调用兼容路径。
                await self._reply(event, content)
                return
            await self._start_durable_reply_async(
                route_key,
                event,
                content,
                "stop_command",
                ctx,
            )
            return
        if cmd == "/status" and not command_argument:
            ctx = await self.sessions.get_or_create_async(
                route_key, self._build_gateway_prompt(event.source),
            )
            status = self.sessions.get_status(route_key)
            content = f"({status})" if status else "(no session)"
            if event.source.platform not in self.adapters:
                # 保留无 Adapter 的测试 / 嵌入式调用兼容路径。
                await self._reply(event, content)
                return
            await self._start_durable_reply_async(
                route_key,
                event,
                content,
                "status_command",
                ctx,
            )
            return

        ctx = await self.sessions.get_or_create_async(
            route_key, self._build_gateway_prompt(event.source),
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
                await self._start_durable_reply_async(
                    route_key,
                    event,
                    content,
                    "queue_full",
                    ctx,
                )
                return
            if (
                not from_queue
                and not await self._persist_event_async(route_key, event)
            ):
                return
            if from_queue:
                # 已持久化消息必须全部恢复,不能因重启后的新上限丢失。
                ctx.pending.append(event)
            else:
                self.sessions.enqueue(ctx, event)
            await self._mark_processing_best_effort(event)
            # 重启恢复的历史队列按原顺序完整执行,不能让后一条恢复消息
            # 取消前一条;只有新到达的实时消息才覆盖当前请求。
            if not from_queue:
                await self._request_session_cancel_async(
                    route_key,
                    reason="superseded",
                )
            print(f"  [gateway] {route_key}: queued ({len(ctx.pending)} pending)")
            return

        # 原子设置 busy,避免竞态:create_task 不会立即执行,_rocess 也没
        # 机会在 _handle_message 返回前跑。所以在 _handle_message 里设 busy
        # 就能保证同一 route_key 只有一个 worker。
        if (
            not from_queue
            and not await self._persist_event_async(route_key, event)
        ):
            return
        await self._mark_processing_best_effort(event)
        await self._mark_event_processing_async(route_key, event)
        generation, invalidation_event = self.sessions.begin_task(ctx)
        delivery_id = str(uuid.uuid4())
        ctx.delivery_id = delivery_id
        ctx.delivery_generation = generation
        # 模型 Task 与串行收尾 worker 分开管理。即使模型 Task 在首次运行前
        # 就被取消,worker 仍会启动并清理 busy / 持久队列。
        agent_task = asyncio.create_task(
            self._run_agent(
                event,
                ctx,
                resume_from_history=approval_resume_id is not None,
                approval_resume_id=approval_resume_id,
            ),
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
                delivery_event=delivery_event,
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
        *,
        delivery_event: MessageEvent | None = None,
    ):
        """串行处理一条消息,然后检查队列。"""
        delivery_event = delivery_event or event
        ctx = await self.sessions.get_or_create_async(
            route_key, self._build_gateway_prompt(event.source),
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
        agent_result = _GatewayAgentResult(None)
        try:
            if agent_task is None:
                raw_agent_result = await self._run_agent(event, ctx)
            else:
                raw_agent_result = await agent_task
            if isinstance(raw_agent_result, _GatewayAgentResult):
                agent_result = raw_agent_result
            else:
                # 保留嵌入式调用和既有 monkeypatch 返回纯文本的兼容性。
                agent_result = _GatewayAgentResult(raw_agent_result)
            response = agent_result.response
            if (
                ctx.delivery_generation == generation
                and ctx.delivery_id is not None
            ):
                delivery_id = ctx.delivery_id
            # worker 返回到事件循环后做最后一道取消检查。即使取消恰好
            # 发生在模型响应检查之后,也不能把旧回复发送给用户。
            persisted_outbox = await self._load_outbox_async(delivery_id)
            cancel_reason = self._task_cancel_reason(ctx, generation)
            approval_resume_failed = (
                event.message_id.startswith(_APPROVAL_RESUME_MESSAGE_PREFIX)
                and agent_result.failed
                and agent_result.failure_type
                in _APPROVAL_RESUME_TERMINAL_FAILURES
            )
            if approval_resume_failed:
                await self.persistence.call(
                    mark_gateway_message_delivery_failed,
                    route_key,
                    event.message_id,
                )
                self._accepted_messages.discard(
                    (route_key, event.message_id)
                )
                event_completed = True
                await self._finish_processing_best_effort(
                    delivery_event,
                    "failed",
                    ctx=ctx,
                    generation=generation,
                )
                print(
                    "  [gateway:audit] event=approval_resume_failed "
                    f"{safe_route_digest(route_key)} "
                    f"{safe_message_digest(event.message_id)} "
                    "failure_type="
                    f"{_safe_audit_label(agent_result.failure_type)}"
                )
            elif cancel_reason is not None:
                abandoned = True
                if (
                    cancel_reason != "shutdown"
                    and persisted_outbox
                ):
                    event_completed = await self._cancel_outbox_async(
                        delivery_id,
                        route_key=route_key,
                        source_message_id=delivery_event.message_id,
                    )
                print(f"  [gateway] {route_key}: stale response discarded")
            elif not response and not persisted_outbox:
                event_completed = await self._complete_event_async(
                    route_key,
                    event,
                )
            elif event.source.platform not in self.adapters:
                # 无 Adapter 只用于测试或嵌入式调用;保留原 _reply 注入点。
                result = await self._reply(delivery_event, str(response or ""))
                if result is None or result.success:
                    if persisted_outbox:
                        event_completed = await self._cancel_outbox_async(
                            delivery_id,
                            route_key=route_key,
                            source_message_id=delivery_event.message_id,
                        )
                    else:
                        event_completed = await self._complete_event_async(
                            route_key,
                            event,
                        )
                else:
                    if persisted_outbox:
                        event_completed = await self._fail_outbox_async(
                            delivery_id,
                            route_key,
                            delivery_event,
                            result.error or "internal_send_error",
                            result.error_code,
                        )
                    else:
                        await self._mark_delivery_failed_without_outbox_async(
                            route_key,
                            event,
                        )
            elif persisted_outbox:
                delivered = await self._deliver_outbox(
                    route_key,
                    delivery_event,
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
                    delivery_event,
                    response,
                    delivery_id,
                    "internal_error" if agent_result.failed else "final",
                    queue_message_id=event.message_id,
                )
                delivery_id = await self._enqueue_outbox_async(outbox)
                if (
                    ctx.delivery_generation == generation
                    and self._task_cancel_reason(ctx, generation) is None
                ):
                    ctx.delivery_id = delivery_id
                if agent_result.failed:
                    await self._finish_processing_best_effort(
                        delivery_event,
                        "failed",
                        ctx=ctx,
                        generation=generation,
                    )
                    print(
                        "  [gateway:audit] event=agent_final_failure "
                        f"{safe_route_digest(route_key)} "
                        f"{safe_message_digest(event.message_id)} "
                        "failure_type="
                        f"{_safe_audit_label(agent_result.failure_type) or 'internal_error'}"
                    )
                delivered = await self._deliver_outbox(
                    route_key,
                    delivery_event,
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
            else:
                try:
                    existing_error_outbox = await self._load_outbox_async(
                        delivery_id
                    )
                except Exception as lookup_exc:
                    print(
                        f"  [gateway] {route_key}: error outbox lookup failed "
                        f"({type(lookup_exc).__name__})"
                    )
                    await self._finish_processing_best_effort(
                        event,
                        "failed",
                        ctx=ctx,
                        generation=generation,
                    )
                else:
                    if existing_error_outbox is None:
                        failure = self._safe_exception_result(exc)
                        try:
                            outbox = self._build_outbox(
                                route_key,
                                delivery_event,
                                failure.response or _SAFE_INTERNAL_REPLY,
                                delivery_id,
                                "internal_error",
                                queue_message_id=event.message_id,
                            )
                            delivery_id = await self._enqueue_outbox_async(
                                outbox
                            )
                            if (
                                ctx.delivery_generation == generation
                                and self._task_cancel_reason(
                                    ctx,
                                    generation,
                                ) is None
                            ):
                                ctx.delivery_id = delivery_id
                            await self._finish_processing_best_effort(
                                delivery_event,
                                "failed",
                                ctx=ctx,
                                generation=generation,
                            )
                            delivered = await self._deliver_outbox(
                                route_key,
                                delivery_event,
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
                            await self._finish_processing_best_effort(
                                event,
                                "failed",
                                ctx=ctx,
                                generation=generation,
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
            if (
                ctx.delivery_id == delivery_id
                and ctx.delivery_generation in (None, generation)
            ):
                ctx.delivery_id = None
                ctx.delivery_generation = None
            # 用户取消 /new / 后续消息覆盖表示旧回答被明确放弃;shutdown
            # 则保留 processing / outbox,下次启动从持久状态恢复。
            try:
                if (
                    abandoned
                    and cancel_reason != "shutdown"
                    and not event_completed
                ):
                    # 先让触发取消的入站批次完成 admission，再
                    # 删除旧 processing 记录。这使内存队列与持久
                    # 队列在同一边界上对外可见。
                    await self._wait_for_route_admissions(route_key)
                    if await self._load_outbox_async(delivery_id):
                        event_completed = await self._cancel_outbox_async(
                            delivery_id,
                            route_key=route_key,
                            source_message_id=delivery_event.message_id,
                        )
                    else:
                        event_completed = await self._complete_event_async(
                            route_key,
                            event,
                        )
            finally:
                # 关键状态已落库后才对外暴露 route 空闲。普通
                # 收尾先进入 dispatching，由 admission 锁保证 pending
                # 队头不会被同时到达的新消息越过。
                if (
                    owns_worker
                    and abandoned
                    and cancel_reason != "shutdown"
                ):
                    await self._finish_processing_best_effort(
                        delivery_event,
                        "cancelled",
                        ctx=ctx,
                        generation=generation,
                    )
                if owns_worker:
                    if cancel_reason != "shutdown":
                        ctx.dispatching = True
                    ctx.worker_task = None
                    ctx.worker_generation = None
                    ctx.busy = False

        if cancel_reason != "shutdown":
            await self._dispatch_next(ctx)

    async def _dispatch_next(
        self,
        ctx,
        *,
        admission_locked: bool = False,
    ) -> None:
        """在 route admission 临界区内接力持久工作。"""
        if admission_locked:
            await self._dispatch_next_locked(ctx)
            return

        route_key = ctx.route_key
        # 当前 admission 调用可能在同一个事件循环回调中紧接着
        # 提交多条消息。后台收尾者等到已注册的 admission
        # 全部退出，并再让出一次调度；这样同一回调紧接着
        # 发起的下一条消息可以先注册，不会被接力者插队。
        await self._wait_for_route_admissions(route_key)
        lock = self._route_admission_locks.setdefault(
            route_key,
            asyncio.Lock(),
        )
        self._route_admission_users[route_key] = (
            self._route_admission_users.get(route_key, 0) + 1
        )
        try:
            async with lock:
                await self._dispatch_next_locked(ctx)
        finally:
            users = self._route_admission_users.get(route_key, 1) - 1
            if users <= 0:
                self._route_admission_users.pop(route_key, None)
                if self._route_admission_locks.get(route_key) is lock:
                    self._route_admission_locks.pop(route_key, None)
            else:
                self._route_admission_users[route_key] = users

    async def _wait_for_route_admissions(self, route_key: str) -> None:
        """等待当前入站批次稳定退出 route 临界区。"""
        while True:
            while self._route_admission_users.get(route_key, 0) > 0:
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            if self._route_admission_users.get(route_key, 0) == 0:
                return

    async def _dispatch_next_locked(self, ctx) -> None:
        """优先接力已生成回复，再分发 pending 模型任务。"""
        ctx.dispatching = False
        if self._route_has_active_worker(ctx):
            return
        try:
            outbox = await self.persistence.call(
                get_next_recoverable_gateway_outbox_for_route,
                ctx.route_key,
            )
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
                if self._outbox_tracks_processing(outbox):
                    await self._mark_processing_best_effort(event)
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
        await self._handle_message_serialized(next_event, from_queue=True)

    async def _run_agent(
        self,
        event: MessageEvent,
        ctx,
        *,
        resume_from_history: bool = False,
        approval_resume_id: str | None = None,
    ) -> _GatewayAgentResult | str | None:
        """在全局并发限制内运行异步主会话。"""
        # 所有 route_key 共用同一信号量,避免不同会话同时打满模型服务。
        async with self._llm_semaphore:
            return await self._run_agent_async(
                event,
                ctx,
                resume_from_history=resume_from_history,
                approval_resume_id=approval_resume_id,
            )

    async def _run_agent_async(
        self,
        event: MessageEvent,
        ctx,
        *,
        resume_from_history: bool = False,
        approval_resume_id: str | None = None,
    ) -> _GatewayAgentResult:
        """使用 AsyncOpenAI 跑主会话，数据库 hook 统一在线程执行。"""
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
        task_event = event
        approval_resume = None
        if resume_from_history:
            if not approval_resume_id:
                return _GatewayAgentResult(
                    _SAFE_INTERNAL_REPLY,
                    failed=True,
                    failure_type="invalid_approval_resume_task",
                )
            approval_resume = await self.persistence.call(
                get_gateway_approval_resume,
                route_key,
                conversation_id,
                event.message_id,
                approval_resume_id,
            )
            if approval_resume is None:
                return _GatewayAgentResult(
                    _SAFE_INTERNAL_REPLY,
                    failed=True,
                    failure_type="invalid_approval_resume_task",
                )
            try:
                task_event = self._deserialize_event(
                    approval_resume["approval"]["source_event_json"]
                )
                if (
                    build_session_key(task_event.source, self.agent_name)
                    != route_key
                    or task_event.message_id
                    != approval_resume["approval"]["source_message_id"]
                ):
                    raise ValueError("approval source event identity mismatch")
            except Exception:
                return _GatewayAgentResult(
                    _SAFE_INTERNAL_REPLY,
                    failed=True,
                    failure_type="invalid_approval_resume_task",
                )
        elif approval_resume_id is not None:
            return _GatewayAgentResult(
                _SAFE_INTERNAL_REPLY,
                failed=True,
                failure_type="invalid_approval_resume_task",
            )

        def persist_final_message(conn, session_id, msg) -> str | None:
            """最终回答和 outbox 必须在同一个 SQLite 事务中落盘。"""
            nonlocal delivery_id
            if self._task_cancel_reason(ctx, generation) is not None:
                raise asyncio.CancelledError
            content = str(msg.get("content", "") or "")
            if not content:
                add_messages(conn, session_id, [msg])
                return
            outbox = self._build_outbox(
                route_key,
                task_event,
                content,
                delivery_id,
                "final",
                queue_message_id=event.message_id,
            )
            actual_delivery_id = add_final_message_with_gateway_outbox(
                conn,
                session_id,
                msg,
                outbox,
                **self._runtime_fence_kwargs(),
            )
            delivery_id = actual_delivery_id
            return actual_delivery_id

        await self.persistence.call(
            ensure_session,
            conversation_id,
            source=task_event.source.platform,
        )
        approval = (
            approval_resume["approval"]
            if approval_resume is not None
            else None
        )
        result = await run_conversation_async(
            event.text,
            None,
            conversation_id,
            system_prompt,
            conversation_id,
            cancel_checker,
            async_client=self._get_async_client(),
            final_message_callback=persist_final_message,
            persistence_call=self.persistence.call,
            resume_from_history=resume_from_history,
            approval_resume_id=(approval["id"] if approval is not None else None),
            approval_tool_call_id=(
                approval["tool_call_id"] if approval is not None else None
            ),
            resume_state=(approval["agent_state"] if approval is not None else None),
            enabled_toolsets=self._enabled_toolsets_for_source(task_event.source),
            tool_context={
                "interactive_approval": False,
                "approval_mode": "remote",
            },
        )
        if result.get("status") == "awaiting_approval":
            request = result.get("approval_request")
            if not isinstance(request, dict):
                return _GatewayAgentResult(
                    _SAFE_INTERNAL_REPLY,
                    failed=True,
                    failure_type="invalid_approval_request",
                )
            if self._task_cancel_reason(ctx, generation) is not None:
                return _GatewayAgentResult(None)
            question = _format_approval_question(request)
            msg = {"role": "assistant", "content": question}
            outbox = self._build_outbox(
                route_key,
                task_event,
                question,
                delivery_id,
                "approval_request",
                queue_message_id=event.message_id,
            )
            delivery_id = await self.persistence.call(
                create_gateway_approval_with_outbox,
                conversation_id,
                request,
                task_event.source.user_id or task_event.source.user_id_alt,
                msg,
                outbox,
                _GATEWAY_APPROVAL_TTL_SECONDS,
                agent_state=result.get("agent_state"),
                **self._runtime_fence_kwargs(),
            )
            if (
                getattr(ctx, "delivery_generation", generation) == generation
                and self._task_cancel_reason(ctx, generation) is None
            ):
                ctx.delivery_id = delivery_id
            return _GatewayAgentResult(question)
        if (
            getattr(ctx, "delivery_generation", generation) == generation
            and self._task_cancel_reason(ctx, generation) is None
        ):
            ctx.delivery_id = delivery_id
        return self._safe_agent_result(result)

    def _get_async_client(self):
        """按需创建 Runner 独占的异步模型客户端。"""
        if self._async_client is None:
            from hermes.config import create_async_client
            self._async_client = create_async_client()
        return self._async_client

    async def _reply(self, event: MessageEvent, content: str) -> SendResult:
        """只为未正式启动的嵌入式路径保留直接回复兼容。"""
        if self._runtime_lease_acquired:
            raise RuntimeError(
                "gateway platform sends must use a fenced Outbox claim"
            )
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
