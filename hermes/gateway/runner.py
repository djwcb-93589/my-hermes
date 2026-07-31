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
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from hermes.config import (
    APPROVAL_REQUEST_TTL_SECONDS,
    HERMES_HOME,
    PATH_ACCESS_POLICY,
    load_gateway_busy_input_mode,
)
from hermes.hooks import (
    AsyncHookRegistry,
    SyncObservationBridge,
    build_sync_observation_bridge,
)
from hermes.db import (
    add_final_message_with_gateway_outbox,
    add_messages,
    cancel_gateway_delivery,
    cancel_pending_gateway_approvals,
    check_gateway_runtime_readiness,
    claim_cron_delivery_preparation,
    create_cron_run_artifact,
    claim_gateway_file_delivery,
    claim_gateway_approval,
    claim_gateway_approval_with_ack_outbox,
    complete_gateway_delivery,
    complete_gateway_message,
    complete_gateway_steer_messages_in_transaction,
    create_gateway_file_delivery_outbox,
    create_gateway_approval_with_outbox,
    delete_gateway_messages,
    deny_gateway_approval,
    enqueue_gateway_outbox,
    finish_cron_delivery_preparation,
    enqueue_gateway_message,
    fail_gateway_delivery,
    fail_gateway_file_delivery,
    fail_gateway_approval_identity_unavailable,
    finish_gateway_approval,
    begin_gateway_approval_execution,
    gateway_file_delivery_claim_is_valid,
    gateway_outbox_claim_is_valid,
    gateway_runtime_lease_is_valid,
    get_gateway_conversation_for_route,
    get_gateway_file_delivery,
    get_gateway_message_persistence_state,
    get_gateway_steer_recovery_states,
    get_gateway_routes_with_pending_outbox,
    get_next_recoverable_gateway_outbox_for_route,
    get_gateway_outbox,
    get_gateway_approval_resume,
    get_pending_gateway_approval,
    get_gateway_queued_messages,
    get_recoverable_gateway_outbox,
    get_recoverable_gateway_file_deliveries,
    list_gateway_conversations,
    list_cron_delivery_preparation_candidates,
    list_gateway_incomplete_tool_executions,
    mark_gateway_message_delivery_failed,
    mark_gateway_message_processing,
    mark_gateway_file_delivery_outbox_retry,
    mark_gateway_file_delivery_retry,
    mark_gateway_file_delivery_uploaded,
    mark_gateway_outbox_chunk_sent,
    mark_gateway_outbox_retry,
    mark_gateway_outbox_sending,
    prune_gateway_terminal_outbox,
    prune_gateway_terminal_ownership,
    prune_cron_terminal_history,
    reconcile_gateway_terminal_deliveries,
    claim_interrupted_cron_runs_for_tool_recovery,
    recover_interrupted_cron_runs,
    recover_gateway_approvals,
    refresh_cron_delivery_statuses,
    reset_gateway_processing_messages,
    reset_gateway_sending_outbox,
    reset_gateway_uploading_file_deliveries,
)
from hermes.approval_policy import (
    TrustedApprovalGrant,
    activate_session_grant,
    emit_approval_audit,
    issue_trusted_approval_grant,
)
from hermes.gateway.adapters import BasePlatformAdapter
from hermes.gateway.constants import GATEWAY_RUNTIME_LEASE_NAME
from hermes.gateway.file_transfer import load_file_transfer_config
from hermes.gateway.outbound_delivery import OutboundDeliveryService
from hermes.gateway.observability import (
    safe_identifier_digest,
    safe_message_digest,
    safe_route_digest,
)
from hermes.gateway.persistence import GatewayPersistence
from hermes.gateway.progressive_output import (
    ProgressiveReplyController,
    load_progressive_output_config,
)
from hermes.persistence.gateway import delete_gateway_conversation_for_route
from hermes.gateway.runtime_lease import GatewayRuntimeLease
from hermes.gateway.runtime_components import (
    build_gateway_runtime_components,
)
from hermes.cron.gateway_scheduler import GatewayCronScheduler
from hermes.cron.artifacts import cron_artifact_base_dir, cron_run_artifact_dir
from hermes.cron.executor import CronExecutor
from hermes.cron.job import CronJob, CronRun
from hermes.gateway.session_store import SessionStore
from hermes.steering import SteerEntry, SteerMailbox
from hermes.durable_tool_dispatcher import (
    DurableToolDispatcher,
    DurableToolExecutionContext,
    tool_output_failed,
)
from hermes.tool_execution_recovery import ToolExecutionRecoveryService
from hermes.gateway.types import (
    MessageEvent,
    MessageType,
    SendResult,
    SessionSource,
    build_session_key,
)
from hermes.prompt import build_system_prompt
from hermes.redaction import redact_explicit_secrets
from hermes.observability.runtime import (
    RuntimeProbeResult,
    RuntimeStatusPublisher,
)
from hermes.tools import (
    ApprovalMode,
    ExecutionEnvironment,
    ToolPolicy,
    register_all,
    registry,
)


_GATEWAY_CONTEXT_FIELDS = (
    "include_soul",
    "include_memory",
    "include_user_profile",
    "include_project_context",
)


def _probe_delegate_runtime() -> RuntimeProbeResult:
    """延迟探测 Delegate Manager，避免仅为监控创建进程级单例。"""
    from hermes.delegate_jobs import probe_delegate_job_manager

    return probe_delegate_job_manager()


def _probe_background_review_runtime() -> RuntimeProbeResult:
    """延迟探测 Background Review，不为监控初始化 Coordinator。"""
    from hermes.review.runtime import probe_background_review_runtime

    return probe_background_review_runtime()


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
_FILE_UPLOAD_CONCURRENCY = 2
_FILE_DELIVERY_POLL_SECONDS = 0.5
_SAFE_MODEL_TIMEOUT_REPLY = "处理失败：模型响应超时，请稍后重试。"
_SAFE_MODEL_UNAVAILABLE_REPLY = "处理失败：模型服务暂时不可用，请稍后重试。"
_SAFE_PERSISTENCE_REPLY = "处理失败：系统暂时不可用，请稍后重试。"
_SAFE_INTERNAL_REPLY = "处理失败：任务未能完成，请稍后重试。"
_GATEWAY_APPROVAL_TTL_SECONDS = APPROVAL_REQUEST_TTL_SECONDS
_APPROVAL_RESUME_MESSAGE_PREFIX = "approval-resume:"
_APPROVAL_RESUME_TERMINAL_FAILURES = frozenset({
    "invalid_approval_resume_task",
    "invalid_approval_resume_history",
    "invalid_approval_resume_state",
    "approval_execution_persistence_failed",
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


def _bind_approval_request_metadata(
    request: dict,
    *,
    session_key: str,
    ttl_seconds: float,
) -> dict:
    """在持久化前绑定审批生命周期、会话、tool call 和指纹。"""
    normalized = dict(request)
    details = normalized.get("details")
    if not isinstance(details, dict):
        raise ValueError("approval request details are invalid")
    fingerprint = details.get("fingerprint")
    tool_call_id = str(normalized.get("tool_call_id", "")).strip()
    normalized_session_key = str(session_key or "").strip()
    if (
        not isinstance(fingerprint, str)
        or not fingerprint.startswith("sha256:")
        or not tool_call_id
        or not normalized_session_key
    ):
        raise ValueError("approval request identity is incomplete")
    created_at = time.time()
    normalized.update({
        "created_at": created_at,
        "expires_at": created_at + float(ttl_seconds),
        "session_key": normalized_session_key,
        "tool_call_id": tool_call_id,
        "fingerprint": fingerprint,
    })
    return normalized


def _gateway_approval_request_is_allowed(
    request: dict,
    result_messages: object,
    tool_policy: ToolPolicy,
) -> bool:
    """确认待审批结果来自当前 Gateway 允许的已注册工具调用。"""
    tool_name = request.get("tool_name")
    tool_call_id = request.get("tool_call_id")
    arguments = request.get("arguments")
    request_id = request.get("id")
    if (
        not isinstance(tool_name, str)
        or not isinstance(tool_call_id, str)
        or not isinstance(arguments, dict)
        or not isinstance(request_id, str)
        or not isinstance(result_messages, list)
    ):
        return False

    entry = registry.get_entry(tool_name)
    if (
        entry is None
        or tool_name not in registry.resolve(tool_policy).allowed_tool_names
        or entry.approval_mode == ApprovalMode.NONE
    ):
        return False

    matching_calls = 0
    matching_results = 0
    for message in result_messages:
        if not isinstance(message, dict):
            return False
        if message.get("role") == "assistant":
            tool_calls = message.get("tool_calls")
            if tool_calls is None:
                continue
            if not isinstance(tool_calls, list):
                return False
            for call in tool_calls:
                if not isinstance(call, dict) or call.get("id") != tool_call_id:
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    return False
                try:
                    call_arguments = json.loads(function.get("arguments"))
                except (TypeError, ValueError):
                    return False
                if (
                    function.get("name") != tool_name
                    or call_arguments != arguments
                ):
                    return False
                matching_calls += 1
        elif (
            message.get("role") == "tool"
            and message.get("tool_call_id") == tool_call_id
        ):
            try:
                payload = json.loads(message.get("content"))
                result_request = payload["approval_request"]
            except (KeyError, TypeError, ValueError):
                return False
            if (
                not isinstance(payload, dict)
                or not isinstance(result_request, dict)
                or result_request.get("id") != request_id
                or result_request.get("tool_name") != tool_name
            ):
                return False
            matching_results += 1
    return matching_calls == 1 and matching_results == 1


def _format_approval_question(request: dict) -> str:
    """只用脱敏后的操作元数据格式化审批问题。"""
    request_id = _short_approval_id(request.get("id"))
    tool_name = str(request.get("tool_name", ""))
    arguments = request.get("arguments", {})
    details = request.get("details", {})
    if not isinstance(arguments, dict):
        arguments = {}
    if not isinstance(details, dict):
        details = {}
    operation_type = str(details.get("operation_type", "") or "")
    if not operation_type:
        if tool_name == "file":
            operation_type = f"file.{arguments.get('action', 'unknown')}"
        else:
            operation_type = f"{tool_name}.operation"
    risk_level = str(details.get("risk_level", "") or "unknown")
    reason = str(
        details.get("reason")
        or request.get("summary")
        or "该操作需要显式审批"
    )
    lines = [
        "检测到受控操作，当前尚未执行。",
        "",
        f"审批编号：{request_id}",
        f"工具：{tool_name}",
        f"操作类型：{_approval_value_preview(operation_type, 200)}",
        f"风险等级：{_approval_value_preview(risk_level, 100)}",
        f"原因：{_approval_value_preview(reason, 500)}",
    ]
    backend_risk = details.get("backend_risk")
    if isinstance(backend_risk, dict):
        backend_type = _approval_value_preview(
            backend_risk.get("backend_type", "unknown"),
            80,
        )
        if backend_type:
            lines.append(f"执行环境：{backend_type}")
    if tool_name == "terminal":
        cwd = _approval_value_preview(details.get("cwd", ""), 1000)
        if cwd:
            lines.append(f"工作目录：{cwd}")
        displayed_command = (
            details.get("normalized_command")
            or arguments.get("command", "")
        )
        lines.append(
            "命令："
            f"{_approval_value_preview(displayed_command, 2000)}"
        )
    target_paths = details.get("target_paths")
    if not isinstance(target_paths, list):
        target_paths = []
    if not target_paths:
        target_path = (
            details.get("target_path")
            or details.get("abs_path")
            or (arguments.get("path", "") if tool_name == "file" else "")
        )
        if target_path:
            target_paths = [target_path]
    for target_path in target_paths[:8]:
        lines.append(
            f"目标路径：{_approval_value_preview(target_path, 1000)}"
        )
    if tool_name == "gateway_send_file":
        display_name = _approval_value_preview(
            details.get("display_name", ""),
            300,
        )
        if display_name:
            lines.append(f"显示文件名：{display_name}")
        lines.extend([
            "文件大小："
            f"{_approval_value_preview(details.get('size_bytes', ''), 80)} bytes",
            "SHA-256："
            f"{_approval_value_preview(details.get('sha256', ''), 100)}",
            "目标平台："
            f"{_approval_value_preview(details.get('target_platform', ''), 80)}",
            "目标会话摘要："
            f"{_approval_value_preview(details.get('target_chat_fingerprint', ''), 100)}",
        ])
    if tool_name == "cron":
        cron_scope = details.get("cron_scope_display")
        if isinstance(cron_scope, dict):
            name = _approval_value_preview(
                cron_scope.get("name", "(unnamed Cron task)"), 180
            )
            schedule = _approval_value_preview(
                cron_scope.get("schedule", ""), 180
            )
            timezone = _approval_value_preview(
                cron_scope.get("timezone", "UTC"), 80
            )
            prompt_summary = _approval_value_preview(
                cron_scope.get("prompt_summary", ""), 260
            )
            lines.extend([
                "",
                f"定时任务：{name}",
                f"执行时间：{schedule}，{timezone}",
            ])
            if prompt_summary:
                lines.append(f"任务摘要：{prompt_summary}")
            lines.extend(["", "允许工具："])
            toolsets = cron_scope.get("toolsets", [])
            tool_names = cron_scope.get("tool_names", [])
            if isinstance(toolsets, list):
                for value in toolsets[:12]:
                    lines.append(f"- toolset: {_approval_value_preview(value, 120)}")
            if isinstance(tool_names, list):
                for value in tool_names[:12]:
                    lines.append(f"- tool: {_approval_value_preview(value, 120)}")
            lines.extend([
                "",
                "文件范围：",
                f"工作目录：{_approval_value_preview(cron_scope.get('workdir', ''), 220)}",
            ])
            roots = cron_scope.get("allowed_roots", [])
            if isinstance(roots, list):
                for value in roots[:8]:
                    lines.append(f"- {_approval_value_preview(value, 220)}")
            lines.append(
                "文件写入：" + ("允许" if cron_scope.get("allow_file_write") else "禁止")
            )
            if "terminal" not in set(cron_scope.get("toolsets", [])):
                lines.append("Terminal：未授权")
            else:
                executables = cron_scope.get("terminal_allowed_executables", [])
                executable_text = ", ".join(
                    _approval_value_preview(value, 80)
                    for value in executables[:12]
                ) if isinstance(executables, list) else ""
                lines.extend([
                    "Terminal：",
                    f"- 最大风险：{_approval_value_preview(cron_scope.get('terminal_risk_max', ''), 80)}",
                    f"- 可执行文件：{executable_text or '未授权'}",
                    "- Shell 操作符：" + ("允许" if cron_scope.get("terminal_allow_shell_operators") else "禁止"),
                    "- 重定向：" + ("允许" if cron_scope.get("terminal_allow_redirection") else "禁止"),
                    "- 后台执行：" + ("允许" if cron_scope.get("terminal_allow_background") else "禁止"),
                    "- 网络访问：" + ("允许" if cron_scope.get("terminal_allow_network") else "禁止"),
                ])
                terminal_workdirs = cron_scope.get("terminal_allowed_workdirs", [])
                if isinstance(terminal_workdirs, list):
                    for value in terminal_workdirs[:8]:
                        lines.append(
                            "- 终端工作目录："
                            f"{_approval_value_preview(value, 220)}"
                        )
            lines.extend([
                "",
                "投递：",
                f"- 平台：{_approval_value_preview(cron_scope.get('delivery_platform', 'none'), 80)}",
                f"- 目标类型：{_approval_value_preview(cron_scope.get('delivery_target_kind', 'none'), 80)}",
                f"- 策略：{_approval_value_preview(cron_scope.get('delivery_policy', 'text'), 80)}",
                f"最长执行：{_approval_value_preview(cron_scope.get('timeout_seconds', ''), 80)} 秒",
                "产物限制："
                f"单文件 {_approval_value_preview(cron_scope.get('max_artifact_file_bytes', ''), 80)} bytes，"
                f"总计 {_approval_value_preview(cron_scope.get('max_artifact_total_bytes', ''), 80)} bytes",
            ])
    raw_scopes = details.get("allowed_grant_scopes")
    scopes = (
        [scope for scope in raw_scopes if scope in {"once", "session"}]
        if isinstance(raw_scopes, list)
        else []
    )
    lines.append("")
    if "once" in scopes:
        lines.append("单次批准：/approve")
    if "session" in scopes:
        lines.append("本会话授权：/approve session")
        lines.append(
            "会话授权仅匹配结构化命令/路径规则、当前 session 和风险上限。"
        )
    if not scopes:
        lines.append("该风险级别不能批准，只能拒绝。")
    try:
        remaining_seconds = max(
            0,
            math.ceil(float(request.get("expires_at")) - time.time()),
        )
    except (TypeError, ValueError):
        remaining_seconds = int(_GATEWAY_APPROVAL_TTL_SECONDS)
    lines.extend([
        "拒绝：/deny",
        f"该请求约 {remaining_seconds} 秒后失效，只能由原请求者处理。",
    ])
    return "\n".join(lines)


def _parse_approval_selector_and_scope(
    value: object,
) -> tuple[str, str] | None:
    """默认选择当前对话唯一 pending，同时兼容旧 selector 语法。"""
    parts = str(value or "").strip().split()
    if not parts:
        return "", "once"
    if len(parts) == 1:
        if parts[0].lower() in {"once", "session"}:
            return "", parts[0].lower()
        return parts[0], "once"
    if len(parts) != 2:
        return None
    first, second = parts
    if second.lower() in {"once", "session"}:
        return first, second.lower()
    if first.lower() in {"once", "session"}:
        return second, first.lower()
    return None


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
        "invalid_scope": "授权范围格式无效，请选择 once 或 session。",
        "scope_forbidden": "当前风险级别不允许该授权范围。",
        "conflict": "审批状态刚刚发生变化，请重新查看。",
    }
    if not selector:
        if outcome == "not_found":
            return "当前对话没有待审批请求。"
        if outcome == "ambiguous":
            return "当前对话存在多个待审批请求，无法安全地自动选择。"
        return labels.get(outcome, "当前待审批请求不可处理。")
    return labels.get(outcome, f"审批请求 {selector} 当前不可处理。")


@dataclass(frozen=True)
class _GatewayAgentResult:
    """Runner 内部的 Agent 结果，只保留可安全发送的用户文案。"""

    response: str | None
    failed: bool = False
    failure_type: str | None = None
    pending_steer: tuple[SteerEntry, ...] = ()
    progressive_controller: ProgressiveReplyController | None = None


class _GatewayAgentRunError(RuntimeError):
    """携带展示控制器，同时保留未预期 Agent 异常的原始类型。"""

    def __init__(
        self,
        original: Exception,
        progressive_controller: ProgressiveReplyController,
    ):
        self.original = original
        self.progressive_controller = progressive_controller
        super().__init__(type(original).__name__)


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
    *,
    browser_enabled: bool,
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

        supported_toolsets = registry.toolsets_for_environment(
            ExecutionEnvironment.GATEWAY
        )
        normalized: list[str] = []
        for raw_toolset in configured:
            toolset = raw_toolset.strip().lower()
            # browser 的全局开关是额外门槛；平台列表不能绕过它。
            if toolset == "browser" and not browser_enabled:
                continue
            if toolset not in supported_toolsets:
                raise ValueError(
                    f"gateway.platforms.{platform}.toolsets contains "
                    f"unsupported toolset: {raw_toolset!r}; allowed: "
                    f"{sorted(supported_toolsets)}"
                )
            if toolset not in normalized:
                normalized.append(toolset)
        if browser_enabled and "browser" not in normalized:
            normalized.append("browser")
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
        "cron_run_seconds": 30 * 24 * 60 * 60,
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

    def __init__(
        self,
        config: dict,
        db_path: str,
        *,
        hook_registry: AsyncHookRegistry | None = None,
        process_manager=None,
        runtime_status_publisher: RuntimeStatusPublisher | None = None,
    ):
        # Gateway 的配置校验依赖全局元数据，先完成幂等注册。
        if process_manager is None:
            from hermes.processes import (
                process_manager as default_process_manager,
            )

            process_manager = default_process_manager
        self._process_manager = process_manager
        register_all(process_manager=self._process_manager)
        if hook_registry is not None and not isinstance(
            hook_registry,
            AsyncHookRegistry,
        ):
            raise TypeError("hook_registry must be an AsyncHookRegistry or None")
        self.config = config
        self.db_path = db_path
        self._hook_registry = hook_registry
        self.adapters: dict[str, BasePlatformAdapter] = {}
        gateway_cfg = config.get("gateway", {})
        if not isinstance(gateway_cfg, dict):
            raise ValueError("gateway must be a mapping")
        self.busy_input_mode = load_gateway_busy_input_mode(gateway_cfg)
        self.progressive_output_config = load_progressive_output_config(
            gateway_cfg
        )
        self.file_transfer_config = load_file_transfer_config(
            gateway_cfg,
            hermes_home=HERMES_HOME,
            path_policy=PATH_ACCESS_POLICY,
        )
        self._gateway_context_policies = _load_gateway_context_config(
            gateway_cfg
        )
        browser_cfg = config.get("browser", {})
        self._browser_enabled = bool(
            browser_cfg.get("enabled", False)
            if isinstance(browser_cfg, dict)
            else False
        )
        self._gateway_platform_toolsets = _load_gateway_platform_toolsets(
            gateway_cfg,
            browser_enabled=self._browser_enabled,
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
        self.cron_run_retention_seconds = float(retention["cron_run_seconds"])
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
        self._cron_observation_bridge: SyncObservationBridge | None = None
        if hook_registry is not None:
            from hermes.persistence.observation import (
                configure_sqlite_observation_sink,
            )

            configure_sqlite_observation_sink(hook_registry, db_path)
        self.persistence = GatewayPersistence(db_path)
        self._runtime_lease = GatewayRuntimeLease(
            self.persistence,
            lease_name=GATEWAY_RUNTIME_LEASE_NAME,
            ttl_seconds=self.runtime_lease_ttl_seconds,
            heartbeat_seconds=self.runtime_lease_heartbeat_seconds,
            on_lost=self._on_runtime_lease_lost,
        )
        self.sessions = SessionStore(
            idle_timeout=idle_timeout,
            db_path=db_path,
            max_pending_messages=max_pending,
            persistence=self.persistence,
        )
        self.max_concurrent_llm_requests = max(1, int(max_concurrent))
        cron_cfg = gateway_cfg.get("cron", {})
        if not isinstance(cron_cfg, dict):
            raise ValueError("gateway.cron must be a mapping")
        self.cron_poll_seconds = _load_positive_seconds(
            cron_cfg,
            "poll_seconds",
            5.0,
        )
        self.cron_max_concurrent = max(
            1,
            int(cron_cfg.get("max_concurrent", 1)),
        )
        self.cron_misfire_grace_seconds = max(
            0.0,
            float(cron_cfg.get("misfire_grace_seconds", 60.0)),
        )
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
        self._cron_scheduler = GatewayCronScheduler(
            self.persistence,
            self.db_path,
            llm_semaphore=self._llm_semaphore,
            lease_fence_provider=self._cron_runtime_fence,
            lease_is_valid=lambda: self._runtime_lease_valid,
            process_manager=self._process_manager,
            poll_seconds=self.cron_poll_seconds,
            max_concurrent=self.cron_max_concurrent,
            misfire_grace_seconds=self.cron_misfire_grace_seconds,
            execution_finished=self._prepare_cron_delivery,
        )
        if runtime_status_publisher is None:
            self._runtime_components = build_gateway_runtime_components(
                publisher=None,
            )
        else:
            self._runtime_components = build_gateway_runtime_components(
                publisher=runtime_status_publisher,
                cron_probe=self._cron_scheduler.runtime_probe,
                process_probe=self._process_manager.runtime_probe,
                delegate_probe=_probe_delegate_runtime,
                background_review_probe=_probe_background_review_runtime,
            )
        self._owns_runtime_lifecycle = False
        self.outbound_delivery = OutboundDeliveryService(
            self.db_path,
            self.file_transfer_config,
        )
        self._accepted_messages: set[tuple[str, str]] = set()
        self._mailbox_registration_fallback_events: set[
            tuple[str, str]
        ] = set()
        self._startup_message_states: dict[tuple[str, str], dict] = {}
        self._adapter_initialized: dict[str, bool] = {}
        self._inbox_restored_adapters: set[str] = set()
        self._receiving_adapters: set[str] = set()
        self._startup_in_progress = False
        self._accepting_external_messages = True
        self._lifecycle_phase = "created"
        self._session_cleanup_task: asyncio.Task | None = None
        self._retention_cleanup_task: asyncio.Task | None = None
        self._file_delivery_dispatcher_task: asyncio.Task | None = None
        self._cron_delivery_preparation_task: asyncio.Task | None = None
        self._cron_delivery_preparation_wakeup = asyncio.Event()
        self._file_delivery_tasks: dict[str, asyncio.Task] = {}
        self._file_delivery_task_routes: dict[str, str] = {}
        self._file_delivery_wakeup = asyncio.Event()
        self._system_outbox_tasks: dict[str, asyncio.Task] = {}
        self._lease_shutdown_task: asyncio.Task | None = None
        self._readiness_probe_lock = asyncio.Lock()
        self._readiness_probe_cached_at = 0.0
        self._readiness_probe_cached_result = False
        self._route_admission_locks: dict[str, asyncio.Lock] = {}
        self._route_admission_users: dict[str, int] = {}
        self._route_admission_closed = False
        self._stop_lock = asyncio.Lock()
        # 异步模型客户端按需创建,Gateway 停止时统一关闭。
        self._async_client = None

    @property
    def _runtime_lease_name(self) -> str:
        return self._runtime_lease.name

    @property
    def _runtime_instance_id(self) -> str:
        return self._runtime_lease.instance_id

    @property
    def _runtime_lease_epoch(self) -> int | None:
        return self._runtime_lease.epoch

    @property
    def _runtime_lease_acquired(self) -> bool:
        return self._runtime_lease.acquired

    @property
    def _runtime_lease_valid(self) -> bool:
        return self._runtime_lease.valid

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

    @staticmethod
    def _stable_actor_id(event: MessageEvent) -> str | None:
        """只接受平台提供的稳定用户标识，不以 route 或展示信息代替。"""
        actor_id = event.source.user_id or event.source.user_id_alt
        actor_id = str(actor_id or "").strip()
        return actor_id or None

    def _build_gateway_prompt(self, source: SessionSource) -> str:
        """按事件来源选择只读上下文与平台工具能力。"""
        policy_name = self._gateway_context_policy_name(source)
        context_policy = self._gateway_context_policies[policy_name]
        return build_system_prompt(
            os.getcwd(),
            enabled_toolsets=self._enabled_toolsets_for_source(source),
            **context_policy,
        )

    def _tool_policy_for_source(self, source: SessionSource) -> ToolPolicy:
        """为平台消息构造唯一工具解析策略；Cron 一律无人值守。"""
        platform = str(source.platform or "").strip().lower()
        if platform == ExecutionEnvironment.CRON.value:
            return ToolPolicy(
                ExecutionEnvironment.CRON,
                unattended=True,
            )
        enabled_toolsets = frozenset(
            self._gateway_platform_toolsets.get(platform, ())
        )
        trusted_context = frozenset(
            {"gateway_file_delivery"}
            if "messaging" in enabled_toolsets
            else ()
        )
        return ToolPolicy(
            ExecutionEnvironment.GATEWAY,
            enabled_toolsets=enabled_toolsets,
            trusted_context=trusted_context,
        )

    def _enabled_toolsets_for_source(
        self,
        source: SessionSource,
    ) -> list[str]:
        """返回解析后确实会暴露给当前会话的工具集。"""
        resolution = registry.resolve(self._tool_policy_for_source(source))
        return sorted(resolution.toolsets)

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
        event: MessageEvent | None,
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
        pending_steer = result.get("pending_steer")
        if isinstance(pending_steer, (list, tuple)) and all(
            isinstance(entry, SteerEntry) for entry in pending_steer
        ):
            pending_steer = tuple(pending_steer)
        else:
            pending_steer = ()
        if result.get("ok", False):
            response = str(final_response or "")
            if response:
                return _GatewayAgentResult(
                    response,
                    pending_steer=pending_steer,
                )
            return _GatewayAgentResult(
                _SAFE_INTERNAL_REPLY,
                failed=True,
                failure_type="empty_response",
                pending_steer=pending_steer,
            )

        status = str(result.get("status", "") or "")
        error_type = str(result.get("error_type", "") or "")
        if status == "cancelled" or error_type == "cancelled":
            return _GatewayAgentResult(
                None,
                pending_steer=pending_steer,
            )
        if error_type == "persistence_error":
            return _GatewayAgentResult(
                _SAFE_PERSISTENCE_REPLY,
                failed=True,
                failure_type="persistence_error",
                pending_steer=pending_steer,
            )
        if status == "model_error" or error_type == "model_error":
            detail = str(final_response or "").lower()
            if "timeout" in detail or "timed out" in detail:
                return _GatewayAgentResult(
                    _SAFE_MODEL_TIMEOUT_REPLY,
                    failed=True,
                    failure_type="model_timeout",
                    pending_steer=pending_steer,
                )
            return _GatewayAgentResult(
                _SAFE_MODEL_UNAVAILABLE_REPLY,
                failed=True,
                failure_type="model_unavailable",
                pending_steer=pending_steer,
            )
        return _GatewayAgentResult(
            _SAFE_INTERNAL_REPLY,
            failed=True,
            failure_type=error_type or status or "internal_error",
            pending_steer=pending_steer,
        )

    @staticmethod
    def _safe_exception_result(
        exc: Exception,
        *,
        pending_steer: tuple[SteerEntry, ...] = (),
    ) -> _GatewayAgentResult:
        """兜底异常只按类型分类，不把异常文本或本地路径发给用户。"""
        error_name = type(exc).__name__.lower()
        error_module = type(exc).__module__.lower()
        if "timeout" in error_name:
            return _GatewayAgentResult(
                _SAFE_MODEL_TIMEOUT_REPLY,
                failed=True,
                failure_type="model_timeout",
                pending_steer=pending_steer,
            )
        if "connection" in error_name or error_module.startswith("openai"):
            return _GatewayAgentResult(
                _SAFE_MODEL_UNAVAILABLE_REPLY,
                failed=True,
                failure_type="model_unavailable",
                pending_steer=pending_steer,
            )
        if "sqlite" in error_module or "persistence" in error_name:
            return _GatewayAgentResult(
                _SAFE_PERSISTENCE_REPLY,
                failed=True,
                failure_type="persistence_error",
                pending_steer=pending_steer,
            )
        return _GatewayAgentResult(
            _SAFE_INTERNAL_REPLY,
            failed=True,
            failure_type="internal_error",
            pending_steer=pending_steer,
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
    def _format_conversation_list(
        cls,
        conversations: list[dict],
        page: int,
    ) -> str:
        if not conversations:
            return "当前路由暂无可用对话。"
        lines = ["对话列表：", ""]
        for position, conversation in enumerate(conversations, start=1):
            display_index = (page - 1) * 10 + position
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
                f"{display_index}. {marker}{cls._short_conversation_id(conversation['conversation_id'])}",
                f"   消息：{int(conversation.get('message_count', 0))} 条",
                f"   最近：{cls._conversation_preview(conversation.get('preview'))}",
                f"   活跃时间：{active_text}",
                "",
            ])
        lines.extend([
            f"当前第 {page} 页",
            "使用 /sessions <页码> 查看其他页面。",
            "使用 /resume <序号> 切换对话。",
            "使用 /delete <序号> 删除对话。",
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
        return self.persistence.call_sync(
            reconcile_gateway_terminal_deliveries,
            **self._runtime_fence_kwargs(),
        )

    async def _reconcile_terminal_deliveries_async(self) -> int:
        return await self.persistence.call(
            reconcile_gateway_terminal_deliveries,
            **self._runtime_fence_kwargs(),
        )

    def _pending_outbox_route_keys(self) -> set[str]:
        """读取仍由持久 Outbox 管理、不能清理内存会话的 route。"""
        return self.persistence.call_sync(
            get_gateway_routes_with_pending_outbox,
        )

    def _runtime_lease_blocks_delivery(self) -> bool:
        """嵌入式私有调用保持兼容；正式启动后失租必须阻止投递。"""
        return self._runtime_lease.blocks_delivery()

    def _runtime_fence_kwargs(self) -> dict:
        """正式运行携带 fencing；未调用 start 的嵌入式路径保持兼容。"""
        return self._runtime_lease.fence()

    def _require_sync_recovery_runtime_lease(self) -> None:
        """在同步工具恢复前再次确认当前实例仍持有有效 runtime lease。"""
        if not self._runtime_lease_valid or self._runtime_lease_epoch is None:
            raise RuntimeError("gateway runtime lease is no longer valid")
        if not self.persistence.call_sync(
            gateway_runtime_lease_is_valid,
            self._runtime_lease_name,
            self._runtime_instance_id,
            self._runtime_lease_epoch,
        ):
            raise RuntimeError("gateway runtime lease is no longer valid")

    def _recover_gateway_tool_executions(
        self,
        connection,
        records: list[dict],
    ) -> None:
        """在持久化边界提供的连接内恢复未完成的 Gateway 工具调用。"""
        def require_runtime_lease() -> None:
            if (
                not self._runtime_lease_valid
                or self._runtime_lease_epoch is None
                or not gateway_runtime_lease_is_valid(
                    connection,
                    self._runtime_lease_name,
                    self._runtime_instance_id,
                    self._runtime_lease_epoch,
                )
            ):
                raise RuntimeError("gateway runtime lease is no longer valid")

        context = DurableToolExecutionContext(
            environment="gateway",
            connection=connection,
            gateway_lease_name=self._runtime_lease_name,
            gateway_instance_id=self._runtime_instance_id,
            gateway_lease_epoch=self._runtime_lease_epoch,
        )
        ToolExecutionRecoveryService(
            registry,
            DurableToolDispatcher(registry, context),
            before_recover=require_runtime_lease,
            interactive_approval=False,
            approval_mode="remote",
        ).recover(records)

    def _cron_runtime_fence(self) -> dict | None:
        """只有持有且本地仍有效的 Gateway lease 才能领取 Cron。"""
        return self._runtime_lease.valid_fence()

    def _gateway_tool_context(
        self,
        event: MessageEvent,
        route_key: str,
        conversation_id: str,
    ) -> dict:
        """构造模型参数不可覆盖的 Gateway 工具身份与 fencing 上下文。"""
        fence = self._runtime_fence_kwargs()
        return {
            "gateway_context": True,
            "gateway_route_key": route_key,
            "gateway_conversation_id": conversation_id,
            "gateway_source_message_id": event.message_id,
            "gateway_platform": event.source.platform,
            "gateway_chat_id": event.source.chat_id,
            # 文件任务回复当前触发消息，和文本 Outbox 保持一致。
            "gateway_reply_to_message_id": event.message_id,
            "gateway_thread_id": event.source.thread_id,
            "creator_id": event.source.user_id or event.source.user_id_alt,
            "source": "gateway",
            "gateway_db_path": self.db_path,
            "gateway_file_transfer_config": self.file_transfer_config,
            "gateway_runtime_fence": fence if fence else None,
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
        claim_valid = self.persistence.call_sync(
            gateway_outbox_claim_is_valid,
            outbox_id,
            self._runtime_lease_name,
            self._runtime_instance_id,
            self._runtime_lease_epoch,
        )
        if claim_valid:
            return True, True
        lease_valid = self.persistence.call_sync(
            gateway_runtime_lease_is_valid,
            self._runtime_lease_name,
            self._runtime_instance_id,
            self._runtime_lease_epoch,
        )
        return lease_valid, False

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
        """将外部发现的失租事件交给 lease 所有者做一次性状态转换。"""
        self._runtime_lease.lose(error_type)

    def _on_runtime_lease_lost(self, error_type: str | None) -> None:
        """先撤销运行资格，再调度不会自等待的统一安全停止。"""
        self._accepting_external_messages = False
        self._route_admission_closed = True
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

        # 先保证统一关闭一定被调度；后续同步撤销即使部分失败，也不能让
        # Runtime Heartbeat 在已经失租后继续存活。
        if (
            self._lease_shutdown_task is None
            or self._lease_shutdown_task.done()
        ):
            self._lease_shutdown_task = asyncio.create_task(
                self.stop(),
                name="gateway-lease-loss-shutdown",
            )

        # 失租等同 shutdown：保留可恢复 Outbox，不把它误标为用户取消。
        for name, adapter in self.adapters.items():
            try:
                adapter.revoke_receiving()
            except Exception as exc:
                print(
                    f"  [gateway] {name} receive revoke failed: "
                    f"{type(exc).__name__}"
                )
        try:
            self._cron_scheduler.revoke()
        except Exception as exc:
            print(
                "  [gateway] Cron revoke failed: "
                f"{type(exc).__name__}"
            )
        try:
            self.sessions.cancel_all(reason="shutdown")
        except Exception as exc:
            print(
                "  [gateway] session cancellation failed: "
                f"{type(exc).__name__}"
            )

    async def _await_operation_completion(
        self,
        operation,
        *,
        task_name: str,
    ):
        """取消调用协程时仍等待已启动的真实操作完成。"""

        operation_task = asyncio.create_task(
            operation,
            name=task_name,
        )
        cancelled = False
        while not operation_task.done():
            try:
                await asyncio.shield(operation_task)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            try:
                operation_task.result()
            except BaseException:
                pass
            raise asyncio.CancelledError
        return operation_task.result()

    async def _await_blocking_operation(
        self,
        operation,
        *args,
        **kwargs,
    ):
        """在线程中完成同步操作，取消时也等待真实 worker 收口。"""

        return await self._await_operation_completion(
            asyncio.to_thread(
                operation,
                *args,
                **kwargs,
            ),
            task_name="gateway-blocking-operation",
        )

    async def _cleanup_session_resources(
        self,
        session_key: str,
    ):
        """使用 Runner 注册工具时绑定的同一 Manager 清理单会话资源。"""

        from hermes.session_resources import cleanup_session_resources

        return await self._await_blocking_operation(
            cleanup_session_resources,
            session_key,
            process_manager=self._process_manager,
        )

    async def _cleanup_all_session_resources(
        self,
        *,
        lifecycle_barrier_complete: bool = True,
    ):
        """使用 Runner 绑定的同一 Manager 清理全部运行期资源。"""

        from hermes.session_resources import cleanup_all_session_resources

        return await self._await_blocking_operation(
            cleanup_all_session_resources,
            process_manager=self._process_manager,
            lifecycle_barrier_complete=lifecycle_barrier_complete,
        )

    async def _shutdown_background_review_runtime(self) -> bool:
        """停止接收并有限等待已初始化的 Background Review worker。"""
        from hermes.review.runtime import shutdown_background_review_runtime

        try:
            unfinished_workers = await self._await_blocking_operation(
                shutdown_background_review_runtime,
            )
        except Exception as exc:
            print(
                "  [gateway] background review shutdown incomplete: "
                f"exception={type(exc).__name__}"
            )
            return False
        if unfinished_workers:
            print(
                "  [gateway] background review shutdown incomplete: "
                f"active_workers={unfinished_workers}"
            )
            return False
        return True

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
                    candidates = (
                        self.sessions.idle_conversation_candidates(protected)
                    )
                    removed = 0
                    incomplete = 0
                    for route_key, conversation_id in candidates:
                        async with self._route_admission(route_key):
                            current_protected = await self.persistence.call(
                                get_gateway_routes_with_pending_outbox,
                            )
                            if not self.sessions.idle_conversation_is_current(
                                route_key,
                                conversation_id,
                                current_protected,
                            ):
                                continue
                            report = await self._cleanup_session_resources(
                                conversation_id,
                            )
                            if not report.complete:
                                incomplete += 1
                                continue
                            if self.sessions.remove_idle_conversation(
                                route_key,
                                conversation_id,
                                current_protected,
                            ):
                                removed += 1
                except Exception as exc:
                    print(
                        "  [gateway] session cleanup failed: "
                        f"{type(exc).__name__}"
                    )
                    continue
                if incomplete:
                    print(
                        "  [gateway] idle session resource cleanup "
                        f"incomplete: count={incomplete}"
                    )
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
        """清理数据库审计和 Adapter 文件缓存；单项失败不影响运行。"""
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

        try:
            artifact_paths = await self.persistence.call(
                prune_cron_terminal_history,
                updated_before=now - self.cron_run_retention_seconds,
                limit=self.retention_cleanup_batch_size,
            )
            artifact_root = cron_artifact_base_dir().resolve()
            removed_files = 0
            for raw_path in artifact_paths:
                try:
                    candidate = Path(str(raw_path)).resolve()
                    candidate.relative_to(artifact_root)
                    if candidate.is_file():
                        candidate.unlink()
                        removed_files += 1
                except (OSError, ValueError):
                    continue
            if artifact_paths:
                print(
                    "  [gateway:audit] event=retention_cleanup "
                    f"kind=cron_runs removed={len(artifact_paths)} "
                    f"artifact_files={removed_files} "
                    f"lease_epoch={self._runtime_lease_epoch}"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                "  [gateway:audit] event=retention_cleanup_failed "
                f"kind=cron_runs exception={type(exc).__name__} "
                f"lease_epoch={self._runtime_lease_epoch}"
            )

        for platform, adapter in self.adapters.items():
            cleanup_file_cache = getattr(adapter, "cleanup_file_cache", None)
            if not callable(cleanup_file_cache):
                continue
            try:
                result = await cleanup_file_cache()
                scanned = int(getattr(result, "scanned_files", 0))
                removed = int(getattr(result, "removed_files", 0))
                failed = int(getattr(result, "failed_files", 0))
                error_code = str(getattr(result, "error_code", None) or "")
                print(
                    "  [gateway:audit] event=retention_cleanup "
                    f"kind=file_cache platform={platform} "
                    f"scanned={scanned} removed={removed} failed={failed} "
                    f"error_code={error_code or 'none'} "
                    f"lease_epoch={self._runtime_lease_epoch}"
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    "  [gateway:audit] event=retention_cleanup_failed "
                    f"kind=file_cache platform={platform} "
                    f"exception={type(exc).__name__} "
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
        self._runtime_lease.start_heartbeat()

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

    async def _durable_file_transition(self, operation, *args, **kwargs):
        """平台成功后的本地事务即使遇到 shutdown 取消也必须完成提交。"""
        task = asyncio.create_task(
            self.persistence.call(operation, *args, **kwargs)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # persistence.call 自身会保护线程内事务；这里继续等待，确保 stop
            # 不会在 file_key 或 Outbox 提交尚未结束时执行 uploading 恢复。
            try:
                await task
            except Exception:
                pass
            raise

    @staticmethod
    def _is_cron_delivery(delivery: dict) -> bool:
        return str(delivery.get("origin_kind", "")) == "cron"

    async def _launch_system_outbox(self, outbox_id: str, route_key: str) -> None:
        """发送无入站消息归属的 Cron Outbox，不创建会话或伪造用户事件。"""
        task = self._system_outbox_tasks.get(outbox_id)
        if task is not None and not task.done():
            return

        async def deliver() -> None:
            await self._deliver_outbox(route_key, None, outbox_id)

        task = asyncio.create_task(deliver(), name=f"gateway-system-outbox-{outbox_id}")
        self._system_outbox_tasks[outbox_id] = task
        task.add_done_callback(lambda completed, item_id=outbox_id: self._system_outbox_tasks.pop(item_id, None))

    async def _prepare_cron_delivery(self, job, run, result, fence: dict) -> None:
        """执行结束后的即时唤醒；真正的投递准备始终由持久扫描器领取。"""
        if self._runtime_lease_valid:
            self._cron_delivery_preparation_wakeup.set()

    async def _cron_delivery_preparation_loop(self) -> None:
        """用 runtime lease 领取可恢复的投递准备，进程重启后也会继续处理。"""
        try:
            while self._runtime_lease_valid:
                try:
                    fence = self._cron_runtime_fence()
                    if fence is not None:
                        await self.persistence.call(
                            refresh_cron_delivery_statuses,
                            **fence,
                        )
                        candidates = await self.persistence.call(
                            list_cron_delivery_preparation_candidates,
                            stale_after_seconds=max(30.0, self.cron_poll_seconds * 3),
                            limit=max(1, self.cron_max_concurrent),
                        )
                        for candidate in candidates:
                            if not self._runtime_lease_valid:
                                return
                            claimed = await self.persistence.call(
                                claim_cron_delivery_preparation,
                                candidate["run_id"],
                                stale_after_seconds=max(30.0, self.cron_poll_seconds * 3),
                                **fence,
                            )
                            if claimed.get("outcome") != "claimed":
                                continue
                            try:
                                await self._prepare_claimed_cron_delivery(
                                    CronJob.from_record(claimed["job"]),
                                    claimed["run"],
                                    fence,
                                )
                            except Exception as exc:
                                await self.persistence.call(
                                    finish_cron_delivery_preparation,
                                    candidate["run_id"],
                                    "permanent_failed",
                                    {"error_type": "delivery_preparation_failed"},
                                    **fence,
                                )
                                print("  [gateway:cron] delivery preparation failed: " f"{type(exc).__name__}")
                except Exception as exc:
                    print("  [gateway:cron] delivery preparation scan failed: " f"{type(exc).__name__}")
                self._cron_delivery_preparation_wakeup.clear()
                try:
                    await asyncio.wait_for(
                        self._cron_delivery_preparation_wakeup.wait(),
                        timeout=self.cron_poll_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise

    def _start_cron_delivery_preparation(self) -> None:
        """只在 lease 有效的 Gateway 生命周期内启动唯一的投递准备扫描器。"""
        task = self._cron_delivery_preparation_task
        if task is None or task.done():
            self._cron_delivery_preparation_task = asyncio.create_task(
                self._cron_delivery_preparation_loop(),
                name="gateway-cron-delivery-preparation",
            )
        self._cron_delivery_preparation_wakeup.set()

    async def _prepare_claimed_cron_delivery(self, job: CronJob, run: dict, fence: dict) -> None:
        """为已领取的投递准备幂等创建文本 Outbox 与文件投递，绝不重跑 Agent。"""
        config = dict(job.delivery_config or {})
        policy = str(config.get("policy", "text")).strip().lower()
        aliases = {
            "text": "text", "text_only": "text",
            "text_and_files": "text_and_files", "text_with_files": "text_and_files",
            "failure_only": "failure_only", "silent": "silent",
        }
        policy = aliases.get(policy, "text")
        target = config.get("target", config.get("origin", {}))
        if not isinstance(target, dict) or policy == "silent":
            await self.persistence.call(
                finish_cron_delivery_preparation, run["run_id"], "not_requested", {}, **fence
            )
            return
        platform = str(target.get("platform", "")).strip()
        chat_id = str(target.get("chat_id", "")).strip()
        if not platform or not chat_id:
            await self.persistence.call(
                finish_cron_delivery_preparation, run["run_id"], "invalid_target",
                {"error_type": "invalid_delivery_target"}, **fence
            )
            return
        failed = str(run["status"]) != "completed"
        if policy == "failure_only" and not failed:
            await self.persistence.call(
                finish_cron_delivery_preparation, run["run_id"], "not_requested", {}, **fence
            )
            return
        adapter = self.adapters.get(platform)
        if adapter is None:
            await self.persistence.call(
                finish_cron_delivery_preparation, run["run_id"], "permanent_failed",
                {"error_type": "delivery_adapter_unavailable"}, **fence
            )
            return
        text = str(run.get("result_summary") or "").strip()
        if not text and failed:
            text = "Cron task failed without a final response."
        if not text and policy != "text_and_files":
            await self.persistence.call(
                finish_cron_delivery_preparation, run["run_id"], "not_requested", {}, **fence
            )
            return

        route_key = str(target.get("route_key") or f"cron:{platform}:{chat_id}")
        outbox_ids: list[str] = []
        if text:
            outbox_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"hermes:cron:text:{run['run_id']}"))
            system_identity = f"cron-outbox:{run['run_id']}:text"
            outbox = {
                "id": outbox_id,
                "route_key": route_key,
                # 这是 Outbox 的内部幂等键，不是平台 source message ID。
                "source_message_id": system_identity,
                "queue_message_id": system_identity,
                "event_json": json.dumps({"origin_kind": "cron", "run_id": run["run_id"]}),
                "platform": platform,
                "chat_id": chat_id,
                "reply_to_message_id": None,
                "thread_id": target.get("thread_id"),
                "delivery_kind": "cron_text",
                "payloads": adapter.prepare_outbound(text, delivery_id=outbox_id),
            }
            created = await self.persistence.call(enqueue_gateway_outbox, outbox, **fence)
            outbox_ids.append(created)
            await self._launch_system_outbox(created, route_key)

        file_delivery_ids: list[str] = []
        failed_artifact_ids: list[str] = []
        skipped_artifact_ids: list[str] = []
        if policy == "text_and_files" and self.file_transfer_config.get("enabled") is True:
            artifact_dir = cron_run_artifact_dir(job.job_id, run["run_id"])
            candidates: list[tuple[object, bool]] = [
                (item.get("path") if isinstance(item, dict) else None, False)
                for item in run.get("artifacts", [])
            ]
            fixed_files = config.get("fixed_files", [])
            if isinstance(fixed_files, list):
                candidates.extend((item, True) for item in fixed_files)
            for index, (path, is_fixed_path) in enumerate(candidates):
                if not isinstance(path, str):
                    artifact_id = hashlib.sha256(
                        f"{run['run_id']}:candidate:{index}".encode("utf-8")
                    ).hexdigest()[:32]
                    await self.persistence.call(
                        create_cron_run_artifact,
                        {
                            "artifact_id": f"artifact_{artifact_id}", "run_id": run["run_id"],
                            "display_name": f"artifact-{index + 1}", "local_path": "unavailable",
                            "size_bytes": 1, "sha256": artifact_id,
                            "delivery_status": "failed",
                            "preparation_error_type": "artifact_path_invalid",
                            "preparation_retryable": False,
                        },
                    )
                    failed_artifact_ids.append(f"artifact_{artifact_id}")
                    continue
                try:
                    if not is_fixed_path:
                        self.outbound_delivery.require_artifact_path(path, str(artifact_dir))
                    snapshot = self.outbound_delivery.capture_file(path)
                    digest = hashlib.sha256(
                        f"{run['run_id']}:{index}:{snapshot['abs_path']}".encode("utf-8")
                    ).hexdigest()
                    delivery_id = f"delivery_{digest[:32]}"
                    artifact_id = f"artifact_{digest[:32]}"
                    delivery = {
                        "id": delivery_id, "cron_run_id": run["run_id"],
                        "route_key": route_key, "conversation_id": f"cron:{job.job_id}:{run['run_id']}",
                        "source_message_id": f"cron-file:{run['run_id']}:{index}",
                        "platform": platform, "chat_id": chat_id,
                        "reply_to_message_id": None, "thread_id": target.get("thread_id"),
                        "local_path": snapshot["abs_path"], "display_name": snapshot["display_name"],
                        "size_bytes": snapshot["size_bytes"], "sha256": snapshot["sha256"],
                    }
                    self.outbound_delivery.create_cron_artifact_delivery(
                        artifact={"artifact_id": artifact_id, "run_id": run["run_id"],
                                  "display_name": snapshot["display_name"], "local_path": snapshot["abs_path"],
                                  "size_bytes": snapshot["size_bytes"], "sha256": snapshot["sha256"]},
                        delivery=delivery, runtime_fence=fence,
                    )
                    file_delivery_ids.append(delivery_id)
                except Exception as exc:
                    digest = hashlib.sha256(
                        f"{run['run_id']}:candidate:{index}".encode("utf-8")
                    ).hexdigest()
                    artifact_id = f"artifact_{digest[:32]}"
                    await self.persistence.call(
                        create_cron_run_artifact,
                        {
                            "artifact_id": artifact_id, "run_id": run["run_id"],
                            "display_name": f"artifact-{index + 1}",
                            "local_path": "unavailable", "size_bytes": 1, "sha256": digest,
                            "delivery_status": "failed",
                            "preparation_error_type": "artifact_validation_failed",
                            "preparation_retryable": isinstance(exc, OSError),
                        },
                    )
                    failed_artifact_ids.append(artifact_id)
                    print("  [gateway:cron] artifact preparation skipped: " f"{type(exc).__name__}")
        elif policy == "text_and_files":
            candidates = [
                item.get("path") if isinstance(item, dict) else None
                for item in run.get("artifacts", [])
            ]
            fixed_files = config.get("fixed_files", [])
            if isinstance(fixed_files, list):
                candidates.extend(fixed_files)
            for index, _path in enumerate(candidates):
                digest = hashlib.sha256(
                    f"{run['run_id']}:candidate:{index}".encode("utf-8")
                ).hexdigest()
                artifact_id = f"artifact_{digest[:32]}"
                await self.persistence.call(
                    create_cron_run_artifact,
                    {
                        "artifact_id": artifact_id, "run_id": run["run_id"],
                        "display_name": f"artifact-{index + 1}", "local_path": "unavailable",
                        "size_bytes": 1, "sha256": digest, "delivery_status": "skipped",
                        "preparation_error_type": "file_delivery_disabled",
                        "preparation_retryable": True,
                    },
                )
                skipped_artifact_ids.append(artifact_id)
        if file_delivery_ids:
            self._file_delivery_wakeup.set()
        prepared_count = len(outbox_ids) + len(file_delivery_ids)
        final_delivery_status = (
            "partial_failed" if failed_artifact_ids and prepared_count
            else ("permanent_failed" if failed_artifact_ids else (
                "pending" if prepared_count else "not_requested"
            ))
        )
        await self.persistence.call(
            finish_cron_delivery_preparation,
            run["run_id"],
            final_delivery_status,
            {
                "outbox_ids": outbox_ids, "file_delivery_ids": file_delivery_ids,
                "prepared_count": prepared_count, "skipped_count": len(skipped_artifact_ids),
                "failed_count": len(failed_artifact_ids), "failed_artifact_ids": failed_artifact_ids,
                "skipped_artifact_ids": skipped_artifact_ids,
            },
            **fence,
        )

    def _build_file_delivery_outbox(self, delivery: dict) -> tuple[dict, MessageEvent | None]:
        """用已持久化 file_key 构造单片 file Outbox，不暴露本地路径。"""
        if self._is_cron_delivery(delivery):
            adapter = self.adapters.get(delivery["platform"])
            prepare_file = getattr(adapter, "prepare_file_outbound", None)
            if not callable(prepare_file):
                raise ValueError("file delivery adapter is unsupported")
            platform_file_key = str(delivery.get("platform_file_key") or "").strip()
            if not platform_file_key:
                raise ValueError("file delivery platform key is missing")
            outbox_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"hermes:cron:file-outbox:{delivery['id']}",
            ))
            system_identity = f"cron-file-outbox:{delivery['id']}"
            return ({
                "id": outbox_id,
                "route_key": delivery["route_key"],
                "source_message_id": system_identity,
                "queue_message_id": system_identity,
                "event_json": json.dumps({
                    "origin_kind": "cron",
                    "run_id": delivery.get("cron_run_id"),
                    "delivery_id": delivery["id"],
                }),
                "platform": delivery["platform"],
                "chat_id": delivery["chat_id"],
                "reply_to_message_id": None,
                "thread_id": delivery.get("thread_id"),
                "delivery_kind": f"cron_file:{delivery['id']}",
                "payloads": prepare_file(platform_file_key, delivery_id=delivery["id"]),
            }, None)
        source_event = self._deserialize_event(
            str(delivery.get("source_event_json", ""))
        )
        if (
            source_event.message_id != delivery["source_message_id"]
            or source_event.source.platform != delivery["platform"]
            or source_event.source.chat_id != delivery["chat_id"]
            or build_session_key(source_event.source, self.agent_name)
            != delivery["route_key"]
        ):
            raise ValueError("file delivery source identity mismatch")

        adapter = self.adapters.get(delivery["platform"])
        prepare_file = getattr(adapter, "prepare_file_outbound", None)
        if not callable(prepare_file):
            raise ValueError("file delivery adapter is unsupported")
        platform_file_key = str(
            delivery.get("platform_file_key") or ""
        ).strip()
        if not platform_file_key:
            raise ValueError("file delivery platform key is missing")
        payloads = prepare_file(
            platform_file_key,
            delivery_id=delivery["id"],
        )

        event = MessageEvent(
            message_id=delivery["id"],
            text="Gateway file delivery.",
            source=source_event.source,
            message_type=MessageType.DOCUMENT,
            metadata={"file_delivery_id": delivery["id"]},
        )
        outbox_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"hermes:gateway:file-outbox:{delivery['id']}",
        ))
        return ({
            "id": outbox_id,
            "route_key": delivery["route_key"],
            # 文件任务使用自己的稳定 ID，避免占用原入站消息的 Outbox 唯一键。
            "source_message_id": delivery["id"],
            "queue_message_id": delivery["id"],
            "event_json": self._serialize_event(event),
            "platform": delivery["platform"],
            "chat_id": delivery["chat_id"],
            "reply_to_message_id": delivery["reply_to_message_id"],
            "thread_id": delivery["thread_id"],
            "delivery_kind": f"file_delivery:{delivery['id']}",
            "payloads": payloads,
        }, event)

    def _build_file_failure_outbox(
        self,
        delivery: dict,
    ) -> tuple[dict, MessageEvent]:
        """构造不暴露本地路径和平台错误细节的永久失败通知。"""
        source_event = self._deserialize_event(
            str(delivery.get("source_event_json", ""))
        )
        if (
            source_event.message_id != delivery["source_message_id"]
            or source_event.source.platform != delivery["platform"]
            or source_event.source.chat_id != delivery["chat_id"]
            or build_session_key(source_event.source, self.agent_name)
            != delivery["route_key"]
        ):
            raise ValueError("file delivery source identity mismatch")

        delivery_id = str(delivery["id"])
        notification_source_id = f"file-delivery-failure:{delivery_id}"
        outbox_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"hermes:gateway:file-failure-outbox:{delivery_id}",
        ))
        display_name = str(delivery.get("display_name") or "文件")
        content = (
            f"文件“{display_name}”发送失败，系统已停止重试。"
            "请重新发起发送。"
        )
        event = MessageEvent(
            message_id=notification_source_id,
            text=content,
            source=source_event.source,
            message_type=MessageType.TEXT,
            metadata={
                "file_delivery_id": delivery_id,
                "notification": "permanent_failure",
            },
        )
        adapter = self.adapters.get(delivery["platform"])
        if adapter:
            payloads = adapter.prepare_outbound(
                content,
                delivery_id=outbox_id,
            )
        else:
            payloads = [{"content": content}]
        if not payloads:
            raise ValueError("adapter produced no failure notification payload")
        return ({
            "id": outbox_id,
            "route_key": delivery["route_key"],
            "source_message_id": notification_source_id,
            "queue_message_id": notification_source_id,
            "event_json": self._serialize_event(event),
            "platform": delivery["platform"],
            "chat_id": delivery["chat_id"],
            "reply_to_message_id": (
                delivery.get("reply_to_message_id")
                or delivery["source_message_id"]
            ),
            "thread_id": delivery.get("thread_id"),
            "delivery_kind": "file_delivery_failure",
            "payloads": payloads,
        }, event)

    async def _wake_file_delivery_route(
        self,
        delivery: dict,
        event: MessageEvent | None,
        *,
        admission_locked: bool = False,
    ) -> None:
        """持久 Outbox 已提交后唤醒原 route；停机时交给下次启动恢复。"""
        if self._lifecycle_phase != "running":
            return
        if event is None:
            await self._launch_system_outbox(
                str(delivery.get("outbox_id") or ""),
                str(delivery["route_key"]),
            )
            return
        route_key = str(delivery["route_key"])
        if admission_locked:
            await self._wake_file_delivery_route_locked(
                route_key,
                event,
            )
            return
        async with self._route_admission(route_key):
            await self._wake_file_delivery_route_locked(route_key, event)

    async def _wake_file_delivery_route_locked(
        self,
        route_key: str,
        event: MessageEvent,
    ) -> None:
        """在 route admission 内取得当前 Context 并唤醒持久投递。"""

        if self._lifecycle_phase != "running":
            return
        ctx = await self.sessions.get_or_create_async(
            route_key,
            self._build_gateway_prompt(event.source),
        )
        await self._dispatch_next_locked(ctx)

    async def _fail_file_delivery_with_notification(
        self,
        delivery: dict,
        error_code: str,
    ) -> bool:
        """原子持久化永久失败和确定性通知，再唤醒现有 Outbox 调度。"""
        if self._is_cron_delivery(delivery):
            failed = await self.persistence.call(
                fail_gateway_file_delivery,
                delivery["id"],
                error_code,
                None,
                **self._runtime_fence_kwargs(),
            )
            return bool(failed)
        failure_outbox, event = self._build_file_failure_outbox(delivery)
        if self._route_admission_closed:
            return False
        async with self._route_admission(str(delivery["route_key"])):
            if self._lifecycle_phase != "running":
                return False
            failed = await self.persistence.call(
                fail_gateway_file_delivery,
                delivery["id"],
                error_code,
                failure_outbox,
                **self._runtime_fence_kwargs(),
            )
            if failed:
                await self._wake_file_delivery_route(
                    delivery,
                    event,
                    admission_locked=True,
                )
            return bool(failed)

    async def _schedule_file_outbox(self, delivery: dict) -> None:
        """在 file_key 边界之后原子创建 Outbox，并唤醒既有 route 调度器。"""
        outbox, event = self._build_file_delivery_outbox(delivery)
        if event is not None:
            if self._route_admission_closed:
                return
            async with self._route_admission(str(delivery["route_key"])):
                if self._lifecycle_phase != "running":
                    return
                await self._persist_file_outbox_and_wake(
                    delivery,
                    outbox,
                    event,
                    admission_locked=True,
                )
            return
        await self._persist_file_outbox_and_wake(
            delivery,
            outbox,
            event,
            admission_locked=False,
        )

    async def _persist_file_outbox_and_wake(
        self,
        delivery: dict,
        outbox: dict,
        event: MessageEvent | None,
        *,
        admission_locked: bool,
    ) -> None:
        """持久化文件 Outbox，并在同一 route 屏障内唤醒对应会话。"""

        try:
            outbox_id = await self._durable_file_transition(
                create_gateway_file_delivery_outbox,
                delivery["id"],
                outbox,
                **self._runtime_fence_kwargs(),
            )
            delivery["outbox_id"] = outbox_id
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            delay = self._delivery_retry_delay(
                max(1, int(delivery.get("attempt_count", 1)))
            )
            try:
                await self.persistence.call(
                    mark_gateway_file_delivery_outbox_retry,
                    delivery["id"],
                    "outbox_create_failed",
                    time.time() + delay,
                    **self._runtime_fence_kwargs(),
                )
            except Exception:
                pass
            print(
                "  [gateway:audit] event=file_outbox_create_failed "
                f"{safe_route_digest(delivery['route_key'])} "
                f"{safe_identifier_digest(delivery['id'], label='delivery')} "
                f"exception={type(exc).__name__}"
            )
            return

        await self._wake_file_delivery_route(
            delivery,
            event,
            admission_locked=admission_locked,
        )

    async def _run_claimed_file_upload(
        self,
        delivery_id: str,
        upload_file,
        **kwargs,
    ):
        """上传期间持续确认 claim；失租后立即取消尚未完成的 HTTP 流。"""
        valid = await self.persistence.call(
            gateway_file_delivery_claim_is_valid,
            delivery_id,
            **self._runtime_fence_kwargs(),
        )
        if not valid:
            raise asyncio.CancelledError

        upload_task = asyncio.create_task(upload_file(**kwargs))
        check_interval = min(
            1.0,
            max(0.1, self.runtime_lease_heartbeat_seconds / 2),
        )
        try:
            while True:
                done, _ = await asyncio.wait(
                    (upload_task,),
                    timeout=check_interval,
                )
                if done:
                    result = await upload_task
                    if not await self.persistence.call(
                        gateway_file_delivery_claim_is_valid,
                        delivery_id,
                        **self._runtime_fence_kwargs(),
                    ):
                        raise asyncio.CancelledError
                    return result
                if not await self.persistence.call(
                    gateway_file_delivery_claim_is_valid,
                    delivery_id,
                    **self._runtime_fence_kwargs(),
                ):
                    raise asyncio.CancelledError
        finally:
            if not upload_task.done():
                upload_task.cancel()
            await asyncio.gather(upload_task, return_exceptions=True)

    async def _process_file_delivery(self, delivery: dict) -> None:
        """claim、上传、保存 file_key，再交给持久 Outbox 发送。"""
        current = dict(delivery)
        if current["status"] in {"pending", "retry_wait"}:
            claimed = await self.persistence.call(
                claim_gateway_file_delivery,
                current["id"],
                **self._runtime_fence_kwargs(),
            )
            if claimed is None:
                return
            claimed["source_event_json"] = current["source_event_json"]
            current = claimed

            adapter = self.adapters.get(current["platform"])
            upload_file = getattr(adapter, "upload_file_delivery", None)
            if not callable(upload_file):
                await self._fail_file_delivery_with_notification(
                    current,
                    "unsupported_platform",
                )
                return

            try:
                result = await self._run_claimed_file_upload(
                    current["id"],
                    upload_file,
                    local_path=current["local_path"],
                    display_name=current["display_name"],
                    size_bytes=current["size_bytes"],
                    sha256=current["sha256"],
                    database_path=self.db_path,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    "  [gateway:audit] event=file_upload_error "
                    f"{safe_route_digest(current['route_key'])} "
                    f"{safe_identifier_digest(current['id'], label='delivery')} "
                    f"exception={type(exc).__name__}"
                )
                result = None

            platform_file_key = str(
                getattr(result, "platform_file_key", "") or ""
            ).strip()
            if platform_file_key:
                saved = await self._durable_file_transition(
                    mark_gateway_file_delivery_uploaded,
                    current["id"],
                    platform_file_key,
                    **self._runtime_fence_kwargs(),
                )
                if not saved:
                    persisted = await self.persistence.call(
                        get_gateway_file_delivery,
                        current["id"],
                    )
                    if not persisted or (
                        persisted.get("platform_file_key")
                        != platform_file_key
                    ):
                        return
                    current.update(persisted)
                else:
                    current["status"] = "uploaded"
                    current["platform_file_key"] = platform_file_key
                    current["next_attempt_at"] = None
            else:
                retryable = bool(getattr(result, "retryable", True))
                error_code = str(
                    getattr(result, "error_code", None)
                    or "internal_upload_error"
                )[:120]
                attempt = int(current.get("attempt_count", 1))
                if retryable and attempt < self.delivery_max_attempts:
                    delay = self._delivery_retry_delay(
                        attempt,
                        getattr(result, "retry_after_seconds", None),
                    )
                    await self.persistence.call(
                        mark_gateway_file_delivery_retry,
                        current["id"],
                        error_code,
                        time.time() + delay,
                        **self._runtime_fence_kwargs(),
                    )
                else:
                    await self._fail_file_delivery_with_notification(
                        current,
                        error_code,
                    )
                return

        if current["status"] == "uploaded":
            await self._schedule_file_outbox(current)

    def _on_file_delivery_done(
        self,
        delivery_id: str,
        task: asyncio.Task,
    ) -> None:
        """回收上传任务并唤醒 dispatcher，日志只使用不可逆摘要。"""
        route_key = self._file_delivery_task_routes.pop(delivery_id, "")
        self._file_delivery_tasks.pop(delivery_id, None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(
                "  [gateway:audit] event=file_delivery_worker_failed "
                f"{safe_route_digest(route_key)} "
                f"{safe_identifier_digest(delivery_id, label='delivery')} "
                f"exception={type(exc).__name__}"
            )
        self._file_delivery_wakeup.set()

    async def _file_delivery_dispatcher_loop(self) -> None:
        """以保守全局并发和 route 串行约束恢复持久文件任务。"""
        try:
            while self._lifecycle_phase == "running":
                capacity = _FILE_UPLOAD_CONCURRENCY - len(
                    self._file_delivery_tasks
                )
                if capacity > 0 and not self._runtime_lease_blocks_delivery():
                    try:
                        rows = await self.persistence.call(
                            get_recoverable_gateway_file_deliveries,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        print(
                            "  [gateway:audit] event=file_delivery_poll_failed "
                            f"exception={type(exc).__name__}"
                        )
                        rows = []
                    active_routes = set(
                        self._file_delivery_task_routes.values()
                    )
                    for row in rows:
                        delivery_id = str(row["id"])
                        route_key = str(row["route_key"])
                        if (
                            delivery_id in self._file_delivery_tasks
                            or route_key in active_routes
                        ):
                            continue
                        task = asyncio.create_task(
                            self._process_file_delivery(row),
                            name=(
                                "gateway-file-delivery-"
                                f"{hashlib.sha256(delivery_id.encode('utf-8')).hexdigest()[:12]}"
                            ),
                        )
                        self._file_delivery_tasks[delivery_id] = task
                        self._file_delivery_task_routes[delivery_id] = route_key
                        active_routes.add(route_key)
                        task.add_done_callback(
                            lambda completed, item_id=delivery_id: (
                                self._on_file_delivery_done(
                                    item_id,
                                    completed,
                                )
                            )
                        )
                        capacity -= 1
                        if capacity <= 0:
                            break

                self._file_delivery_wakeup.clear()
                try:
                    await asyncio.wait_for(
                        self._file_delivery_wakeup.wait(),
                        timeout=_FILE_DELIVERY_POLL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                "  [gateway:audit] event=file_delivery_dispatcher_failed "
                f"exception={type(exc).__name__}"
            )

    def _start_file_delivery_dispatcher(self) -> None:
        """在完整恢复结束后启动唯一文件任务 dispatcher。"""
        task = self._file_delivery_dispatcher_task
        if task is not None and not task.done():
            return
        self._file_delivery_dispatcher_task = asyncio.create_task(
            self._file_delivery_dispatcher_loop(),
            name="gateway-file-delivery-dispatcher",
        )

    async def _cancel_background_tasks(self) -> None:
        """取消并回收长期后台任务，避免事件循环退出时残留 Task。"""
        current = asyncio.current_task()
        await self._runtime_lease.stop_heartbeat()
        tasks = [
            task
            for task in (
                self._session_cleanup_task,
                self._retention_cleanup_task,
                self._file_delivery_dispatcher_task,
                self._cron_delivery_preparation_task,
                *tuple(self._file_delivery_tasks.values()),
                *tuple(self._system_outbox_tasks.values()),
            )
            if task is not None and task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._session_cleanup_task is not current:
            self._session_cleanup_task = None
        if self._retention_cleanup_task is not current:
            self._retention_cleanup_task = None
        if self._file_delivery_dispatcher_task is not current:
            self._file_delivery_dispatcher_task = None
        if self._cron_delivery_preparation_task is not current:
            self._cron_delivery_preparation_task = None
        self._file_delivery_tasks.clear()
        self._file_delivery_task_routes.clear()
        self._system_outbox_tasks.clear()

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

    async def _abort_startup_after_lease(
        self,
        error_type: str = "GatewayStartupFailed",
    ) -> None:
        """启动恢复失败时停止已创建资源并尽早交还租约。"""
        try:
            await self._runtime_components.fail_startup(error_type)
        except (Exception, asyncio.CancelledError) as exc:
            print(
                "  [gateway] startup Runtime cleanup failed: "
                f"exception={type(exc).__name__}"
            )
        self._accepting_external_messages = False
        self._route_admission_closed = True
        try:
            self._runtime_lease.revoke()
        except Exception as exc:
            print(
                "  [gateway] startup lease revoke failed: "
                f"exception={type(exc).__name__}"
            )
        try:
            await self._cron_scheduler.stop()
        except (Exception, asyncio.CancelledError) as exc:
            print(
                "  [gateway] startup Cron cleanup failed: "
                f"exception={type(exc).__name__}"
            )
        try:
            await self._cancel_background_tasks()
        except (Exception, asyncio.CancelledError) as exc:
            print(
                "  [gateway] startup background cleanup failed: "
                f"exception={type(exc).__name__}"
            )
        try:
            active_tasks = self.sessions.cancel_all(reason="shutdown")
            if active_tasks:
                await asyncio.gather(*active_tasks, return_exceptions=True)
        except (Exception, asyncio.CancelledError) as exc:
            print(
                "  [gateway] startup session cleanup failed: "
                f"exception={type(exc).__name__}"
            )
        if self._owns_runtime_lifecycle:
            try:
                await self._shutdown_background_review_runtime()
            except (Exception, asyncio.CancelledError) as exc:
                print(
                    "  [gateway] startup review cleanup failed: "
                    f"exception={type(exc).__name__}"
                )
        for adapter in self.adapters.values():
            try:
                await adapter.disconnect()
            except (Exception, asyncio.CancelledError) as exc:
                print(
                    "  [gateway] startup adapter disconnect failed: "
                    f"platform={adapter.platform_name} "
                    f"exception={type(exc).__name__}"
                )
        if self._runtime_lease_acquired:
            try:
                await self._runtime_lease.release()
            except (Exception, asyncio.CancelledError) as exc:
                print(
                    "  [gateway] runtime lease release failed: "
                    f"{type(exc).__name__}"
                )
        try:
            self._close_cron_observation_bridge()
        except Exception as exc:
            print(
                "  [gateway] startup observation bridge cleanup failed: "
                f"exception={type(exc).__name__}"
            )

    async def tick_cron(self) -> None:
        """供受控手工入口复用同一 lease、claim 与无重入边界。"""
        await self._cron_scheduler.tick()

    def _bind_cron_observation_bridge(self) -> None:
        """把 Cron 同步 Observation 一次性接入 Gateway 公共 Hook 链。"""
        if self._hook_registry is None:
            return
        if self._cron_observation_bridge is not None:
            raise RuntimeError("Cron observation bridge is already bound")
        bridge = build_sync_observation_bridge(self._hook_registry)
        try:
            self._cron_scheduler.bind_hook_registry(bridge)
        except Exception:
            bridge.close()
            raise
        self._cron_observation_bridge = bridge

    def _close_cron_observation_bridge(self) -> None:
        """关闭 Gateway 生命周期持有的 Cron Observation 桥。"""
        bridge = self._cron_observation_bridge
        if bridge is not None:
            bridge.close()

    async def start(self):
        """按初始化、终态收敛、持久恢复、接收阶段启动 Gateway。"""
        if self._lifecycle_phase != "created":
            raise RuntimeError(
                "GatewayRunner instances are single-use; create a new "
                "instance after stop or failed startup."
            )
        self._bind_cron_observation_bridge()
        self._startup_in_progress = True
        self._accepting_external_messages = False
        self._route_admission_closed = False
        self._inbox_restored_adapters.clear()
        self._receiving_adapters.clear()
        self._startup_message_states.clear()

        self._lifecycle_phase = "acquire_runtime_lease"
        try:
            acquired = await self._runtime_lease.acquire()
        except Exception as exc:
            self._lifecycle_phase = "startup_failed"
            self._startup_in_progress = False
            self._close_cron_observation_bridge()
            print(
                "  [gateway] runtime lease acquisition failed: "
                f"{type(exc).__name__}"
            )
            raise
        if not acquired:
            self._lifecycle_phase = "startup_failed"
            self._startup_in_progress = False
            self._close_cron_observation_bridge()
            print(
                "  [gateway] startup blocked: another active Gateway "
                "instance holds the runtime lease"
            )
            raise RuntimeError(
                "another active Gateway instance holds the runtime lease"
            )
        self._owns_runtime_lifecycle = True
        try:
            self._runtime_components.starting()
            self._start_runtime_lease_heartbeat()
        except Exception as exc:
            await self._abort_startup_after_lease(type(exc).__name__)
            self._lifecycle_phase = "startup_failed"
            self._startup_in_progress = False
            raise

        self._lifecycle_phase = "gateway_tool_execution_recovery"
        try:
            fence = self._runtime_fence_kwargs()
            records = await self.persistence.call(
                list_gateway_incomplete_tool_executions,
                **fence,
            )
            if records:
                await self._await_operation_completion(
                    self.persistence.call(
                        self._recover_gateway_tool_executions,
                        records,
                    ),
                    task_name="gateway-tool-execution-recovery",
                )
            await self._require_startup_runtime_lease()
        except Exception as exc:
            await self._abort_startup_after_lease(type(exc).__name__)
            self._lifecycle_phase = "startup_failed"
            self._startup_in_progress = False
            raise

        self._lifecycle_phase = "cron_run_recovery"
        try:
            self._lifecycle_phase = "cron_tool_execution_recovery"
            interrupted = await self.persistence.call(
                claim_interrupted_cron_runs_for_tool_recovery,
                **self._cron_runtime_fence(),
            )
            for item in interrupted:
                await self._require_startup_runtime_lease()
                await self._await_blocking_operation(
                    CronExecutor(
                        self.db_path,
                        hook_registry=self._cron_observation_bridge,
                        process_manager=self._process_manager,
                        **self._cron_runtime_fence(),
                    ).execute,
                    CronJob.from_record(item["job"]),
                    CronRun.from_record(item["run"]),
                    recovery_only=True,
                )
                await self._require_startup_runtime_lease()

            self._lifecycle_phase = "cron_run_recovery"
            recovered = await self.persistence.call(
                recover_interrupted_cron_runs,
                **self._cron_runtime_fence(),
            )
            await self._require_startup_runtime_lease()
            if recovered:
                print(f"  [gateway:cron] recovered interrupted runs={recovered}")
        except Exception as exc:
            await self._abort_startup_after_lease(type(exc).__name__)
            self._lifecycle_phase = "startup_failed"
            self._startup_in_progress = False
            raise

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
            self._lifecycle_phase = "gateway_file_delivery_reconcile"
            await self.persistence.call(
                reset_gateway_uploading_file_deliveries,
                **self._runtime_fence_kwargs(),
            )
            await self._require_startup_runtime_lease()
        except Exception as exc:
            # 终态无法收敛时不能继续恢复，也不能开放外部入口。
            print(
                "  [gateway] terminal delivery reconciliation failed: "
                f"{type(exc).__name__}"
            )
            await self._abort_startup_after_lease(type(exc).__name__)
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
        except Exception as exc:
            # Gateway 自身持久状态无法完成恢复时不能开放外部入口。
            await self._abort_startup_after_lease(type(exc).__name__)
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
        try:
            self._start_session_cleanup()
            self._start_retention_cleanup()
            self._start_file_delivery_dispatcher()
            self._start_cron_delivery_preparation()
            self._cron_scheduler.start()
            self._runtime_components.start_heartbeats()
        except Exception as exc:
            await self._abort_startup_after_lease(type(exc).__name__)
            self._lifecycle_phase = "startup_failed"
            raise

    async def stop(self):
        """取消运行中任务,断开 adapter,关闭模型客户端并清理 backend。"""
        async with self._stop_lock:
            if (
                self._lifecycle_phase == "stopped"
                and not self._runtime_lease_acquired
            ):
                return

            cleanup_error: BaseException | None = None

            def record_cleanup_error(
                stage: str,
                error: BaseException,
            ) -> None:
                """记录首个清理异常并继续收口，最终恢复原有失败可见性。"""
                nonlocal cleanup_error
                if cleanup_error is None:
                    cleanup_error = error
                print(
                    "  [gateway] shutdown cleanup failed: "
                    f"stage={stage} exception={type(error).__name__}"
                )

            self._lifecycle_phase = "stopping"
            self._accepting_external_messages = False
            self._route_admission_closed = True
            try:
                self._runtime_lease.revoke()
            except Exception as exc:
                record_cleanup_error("runtime_lease_revoke", exc)
            try:
                self._cron_scheduler.revoke()
            except Exception as exc:
                record_cleanup_error("cron_revoke", exc)

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

            try:
                await self._runtime_components.stop_heartbeats()
            except asyncio.CancelledError as exc:
                record_cleanup_error("runtime_heartbeat_stop", exc)
            except Exception as exc:
                print(
                    "  [gateway] Runtime heartbeat cleanup failed: "
                    f"exception={type(exc).__name__}"
                )
            try:
                self._runtime_components.stopping()
            except Exception as exc:
                print(
                    "  [gateway] Runtime stopping publish failed: "
                    f"exception={type(exc).__name__}"
                )

            # 入站关闭后先等待已经进入 admission 的请求完成 worker 登记，
            # 再统一取消，避免 global cleanup 漏过迟到的工具调用。
            try:
                await self._drain_route_admissions()
            except (Exception, asyncio.CancelledError) as exc:
                record_cleanup_error("route_admission_drain", exc)

            # admission 收口后再停止 Gateway lease heartbeat、housekeeping
            # 和 route worker。
            cron_shutdown_complete = False
            try:
                await self._cron_scheduler.stop()
            except (Exception, asyncio.CancelledError) as exc:
                record_cleanup_error("cron_stop", exc)
            else:
                cron_shutdown_complete = True
            try:
                await self._cancel_background_tasks()
            except (Exception, asyncio.CancelledError) as exc:
                record_cleanup_error("background_task_cancel", exc)
            try:
                await self.persistence.call(
                    reset_gateway_uploading_file_deliveries,
                    **self._runtime_fence_kwargs(),
                )
            except asyncio.CancelledError as exc:
                record_cleanup_error("file_delivery_reset", exc)
            except Exception as exc:
                # 当前 lease 已失效时由下一实例的启动恢复再次执行，不阻塞关闭。
                print(
                    "  [gateway:audit] event=file_delivery_shutdown_reset_failed "
                    f"exception={type(exc).__name__}"
                )
            try:
                active_tasks = self.sessions.cancel_all(reason="shutdown")
                if active_tasks:
                    await asyncio.gather(
                        *active_tasks,
                        return_exceptions=True,
                    )
            except (Exception, asyncio.CancelledError) as exc:
                record_cleanup_error("session_cancel", exc)

            background_review_lifecycle_complete = False
            if self._owns_runtime_lifecycle:
                try:
                    background_review_lifecycle_complete = (
                        await self._shutdown_background_review_runtime()
                    )
                except asyncio.CancelledError as exc:
                    record_cleanup_error("background_review_stop", exc)
                except Exception as exc:
                    print(
                        "  [gateway] background review shutdown incomplete: "
                        f"exception={type(exc).__name__}"
                    )

            delegate_lifecycle_complete = False
            try:
                from hermes.delegate_jobs import shutdown_delegate_jobs

                unfinished_delegate_jobs = (
                    await self._await_blocking_operation(
                        shutdown_delegate_jobs,
                    )
                )
            except asyncio.CancelledError as error:
                record_cleanup_error("delegate_stop", error)
            except Exception as error:
                print(
                    "  [gateway] delegate shutdown incomplete: "
                    f"exception={type(error).__name__}"
                )
            else:
                delegate_lifecycle_complete = (
                    not unfinished_delegate_jobs
                )
                if unfinished_delegate_jobs:
                    print(
                        "  [gateway] delegate shutdown incomplete: "
                        f"active_jobs={len(unfinished_delegate_jobs)}"
                    )

            process_lifecycle_complete = False
            try:
                resource_cleanup = await self._cleanup_all_session_resources(
                    lifecycle_barrier_complete=delegate_lifecycle_complete,
                )
            except (Exception, asyncio.CancelledError) as exc:
                record_cleanup_error("session_resource_cleanup", exc)
            else:
                process_lifecycle_complete = (
                    resource_cleanup.process_cleanup is not None
                    and resource_cleanup.process_cleanup.complete
                )
                if not resource_cleanup.complete:
                    process_cleanup = resource_cleanup.process_cleanup
                    unresolved_count = (
                        0
                        if process_cleanup is None
                        else len(
                            process_cleanup.unresolved_process_ids
                        )
                    )
                    print(
                        "  [gateway] global resource cleanup incomplete: "
                        f"unresolved_processes={unresolved_count}"
                    )

            for adapter in self.adapters.values():
                try:
                    await adapter.disconnect()
                except asyncio.CancelledError as exc:
                    record_cleanup_error("adapter_disconnect", exc)
                except Exception as exc:
                    print(
                        "  [gateway] adapter disconnect failed: "
                        f"platform={adapter.platform_name} "
                        f"exception={type(exc).__name__}"
                    )

            if self._runtime_lease_acquired:
                try:
                    await self._runtime_lease.release()
                except asyncio.CancelledError as exc:
                    record_cleanup_error("runtime_lease_release", exc)
                except Exception as exc:
                    print(
                        "  [gateway] runtime lease release failed: "
                        f"{type(exc).__name__}"
                    )

            if self._async_client is not None:
                try:
                    await self._async_client.close()
                except asyncio.CancelledError as exc:
                    record_cleanup_error("model_client_close", exc)
                except Exception as exc:
                    print(
                        "  [gateway] model client close failed: "
                        f"{type(exc).__name__}"
                    )
                finally:
                    self._async_client = None
            self._receiving_adapters.clear()
            self._inbox_restored_adapters.clear()
            self._startup_in_progress = False
            try:
                await self.persistence.close()
            except (Exception, asyncio.CancelledError) as exc:
                record_cleanup_error("persistence_close", exc)
            try:
                self._close_cron_observation_bridge()
            except Exception as exc:
                record_cleanup_error("observation_bridge_close", exc)
            finally:
                try:
                    self._runtime_components.complete(
                        {
                            "cron_scheduler": cron_shutdown_complete,
                            "process_manager": process_lifecycle_complete,
                            "delegate_manager": delegate_lifecycle_complete,
                            "background_review": (
                                background_review_lifecycle_complete
                            ),
                        }
                    )
                except Exception as exc:
                    print(
                        "  [gateway] Runtime terminal publish failed: "
                        f"exception={type(exc).__name__}"
                    )
                self._owns_runtime_lifecycle = False
            self._lifecycle_phase = "stopped"
            if cleanup_error is not None:
                raise cleanup_error

    # ----- 消息路由 -----

    def _message_persistence_state(self, event: MessageEvent) -> dict | None:
        """以数据库为准查询平台消息是否已被 Gateway 接受。"""
        route_key = build_session_key(event.source, self.agent_name)
        persisted = self.persistence.call_sync(
            get_gateway_message_persistence_state,
            route_key,
            event.message_id,
        )
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
        accepted = self.persistence.call_sync(
            enqueue_gateway_message,
            route_key,
            event.message_id,
            self._serialize_event(event),
        )
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
        self.persistence.call_sync(
            mark_gateway_message_processing,
            route_key,
            event.message_id,
        )

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
            self.persistence.call_sync(
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
        reply_to_message_id = event.message_id
        return {
            "id": delivery_id,
            "route_key": route_key,
            "source_message_id": event.message_id,
            "queue_message_id": queue_message_id or event.message_id,
            "event_json": self._serialize_event(event),
            "platform": event.source.platform,
            "chat_id": event.source.chat_id,
            # 回复当前触发消息;thread_id 决定飞书是否在话题内回复。
            "reply_to_message_id": reply_to_message_id,
            "thread_id": event.source.thread_id,
            "delivery_kind": delivery_kind,
            "payloads": payloads,
        }

    def _enqueue_outbox(self, outbox: dict) -> str:
        return self.persistence.call_sync(
            enqueue_gateway_outbox,
            outbox,
            **self._runtime_fence_kwargs(),
        )

    async def _enqueue_outbox_async(self, outbox: dict) -> str:
        return await self.persistence.call(
            enqueue_gateway_outbox,
            outbox,
            **self._runtime_fence_kwargs(),
        )

    def _load_outbox(self, outbox_id: str) -> dict | None:
        return self.persistence.call_sync(get_gateway_outbox, outbox_id)

    async def _load_outbox_async(self, outbox_id: str) -> dict | None:
        return await self.persistence.call(get_gateway_outbox, outbox_id)

    def _cancel_outbox(
        self,
        outbox_id: str,
        *,
        route_key: str | None = None,
        source_message_id: str | None = None,
    ) -> bool:
        def operation(conn) -> tuple[bool, str | None, str | None]:
            outbox = get_gateway_outbox(conn, outbox_id)
            if outbox is None:
                return False, None, None
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
            return cancelled, expected_route, expected_source

        cancelled, expected_route, expected_source = self.persistence.call_sync(
            operation,
        )
        if cancelled:
            assert expected_route is not None and expected_source is not None
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
        def operation(conn) -> bool:
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
            return completed

        completed = self.persistence.call_sync(operation)
        if completed:
            self._accepted_messages.discard((route_key, event.message_id))
        return completed

    async def _complete_outbox_async(
        self,
        outbox_id: str,
        route_key: str,
        source_message_id: str,
    ) -> bool:
        completed = await self.persistence.call(
            complete_gateway_delivery,
            outbox_id,
            route_key,
            source_message_id,
            **self._runtime_fence_kwargs(),
        )
        if not completed:
            current = await self._load_outbox_async(outbox_id)
            completed = bool(current and current["status"] == "delivered")
            if not completed and self._runtime_lease_acquired:
                await self._outbox_send_fence_is_valid_async(outbox_id)
        if completed:
            self._accepted_messages.discard((route_key, source_message_id))
        return completed

    def _fail_outbox(
        self,
        outbox_id: str,
        route_key: str,
        event: MessageEvent,
        error: str,
        error_code: str | None,
    ) -> bool:
        return self.persistence.call_sync(
            fail_gateway_delivery,
            outbox_id,
            route_key,
            event.message_id,
            error,
            error_code,
            **self._runtime_fence_kwargs(),
        )

    async def _fail_outbox_async(
        self,
        outbox_id: str,
        route_key: str,
        source_message_id: str,
        error: str,
        error_code: str | None,
    ) -> bool:
        return await self.persistence.call(
            fail_gateway_delivery,
            outbox_id,
            route_key,
            source_message_id,
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

    @classmethod
    def _refresh_task_cancel_reason(
        cls,
        ctx,
        generation: int | None,
        observed_reason: str | None,
    ) -> str | None:
        """以 SessionStore 中该 generation 的最新原因刷新 worker 观察值。"""
        latest_reason = cls._task_cancel_reason(ctx, generation)
        return (
            latest_reason
            if latest_reason is not None
            else observed_reason
        )

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
        event: MessageEvent | None,
    ) -> None:
        """兼容无 Outbox 的嵌入式回复，只保留入站失败审计。"""
        self.persistence.call_sync(
            mark_gateway_message_delivery_failed,
            route_key,
            event.message_id,
        )

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
        event: MessageEvent | None,
        outbox_id: str,
        ctx=None,
        generation: int | None = None,
        invalidation_event: asyncio.Event | None = None,
        progressive_controller: ProgressiveReplyController | None = None,
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
                    result = None
                    if (
                        index == 0
                        and progressive_controller is not None
                        and progressive_controller.has_draft
                        and progressive_controller.adapter is adapter
                    ):
                        try:
                            result = await progressive_controller.finalize(
                                payload
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            print(
                                "  [gateway:audit] "
                                "event=progressive_finalize_error "
                                f"{safe_route_digest(route_key)} "
                                f"delivery_id={outbox_id} "
                                f"lease_epoch={self._runtime_lease_epoch} "
                                f"exception={type(exc).__name__}"
                            )
                            result = SendResult(
                                success=False,
                                error="progressive_reply_finalize_failed",
                                retryable=False,
                            )
                        progressive_controller = None
                        if not result.success:
                            print(
                                "  [gateway:audit] "
                                "event=progressive_finalize_fallback "
                                f"{safe_route_digest(route_key)} "
                                f"delivery_id={outbox_id} "
                                f"lease_epoch={self._runtime_lease_epoch}"
                            )
                            result = None
                    if result is None:
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
                        outbox["source_message_id"],
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
                    outbox["source_message_id"],
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
                    outbox["source_message_id"],
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
            cancel_reason = self._refresh_task_cancel_reason(
                ctx,
                generation,
                cancel_reason,
            )
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

    async def _requeue_pending_steer(
        self,
        ctx,
        generation: int,
        entries: tuple[SteerEntry, ...],
    ) -> None:
        """按原始消息 ID 将未消费 steer 放回普通 pending。"""
        if (
            self._task_cancel_reason(ctx, generation) is not None
            or ctx.generation != generation
        ):
            return
        for entry in entries:
            event = ctx.inflight_steer_events.get(entry.steer_id)
            if (
                not isinstance(event, MessageEvent)
                or event.message_id != entry.steer_id
            ):
                print(
                    "  [gateway:audit] steer requeue mapping missing "
                    f"route={safe_route_digest(ctx.route_key)} "
                    f"steer={safe_identifier_digest(entry.steer_id)}"
                )
                continue
            already_pending = any(
                queued.message_id == event.message_id
                for queued in ctx.pending
            )
            if not already_pending:
                try:
                    requeued = self.sessions.enqueue_ordered(
                        ctx,
                        event,
                        force=True,
                    )
                except Exception as exc:
                    print(
                        "  [gateway:audit] steer requeue failed "
                        f"route={safe_route_digest(ctx.route_key)} "
                        f"steer={safe_identifier_digest(entry.steer_id)} "
                        f"exception={type(exc).__name__}"
                    )
                    continue
                if not requeued:
                    print(
                        "  [gateway:audit] steer requeue rejected "
                        f"route={safe_route_digest(ctx.route_key)} "
                        f"steer={safe_identifier_digest(entry.steer_id)}"
                    )
                    continue
            self.sessions.forget_steer_event(
                ctx,
                generation,
                entry.steer_id,
            )

    def _close_and_drain_generation_steer(
        self,
        ctx,
        generation: int,
    ) -> tuple[SteerEntry, ...]:
        """关闭并收取只属于指定 generation 的未消费 steer。"""
        if getattr(ctx, "steer_generation", None) != generation:
            return ()
        mailbox = getattr(ctx, "active_steer_mailbox", None)
        if mailbox is None:
            return ()
        try:
            entries = tuple(mailbox.close_and_drain())
            if any(not isinstance(entry, SteerEntry) for entry in entries):
                raise TypeError(
                    "generation steer mailbox returned invalid entries"
                )
            return entries
        except Exception as exc:
            print(
                "  [gateway:audit] "
                "event=steer_exception_drain_failed "
                f"route={safe_route_digest(ctx.route_key)} "
                f"generation={generation} "
                f"exception={type(exc).__name__}"
            )
            return ()

    async def _restore_user_cancelled_steer(
        self,
        ctx,
        generation: int,
    ) -> None:
        """在 /stop 收尾中把该 generation 未确认 steer 恢复为普通 pending。"""
        self.sessions.defer_steer_events(ctx, generation)
        events = self.sessions.get_deferred_steer_events(
            ctx,
            generation,
        )
        if not events:
            return

        try:
            ordered_events = tuple(sorted(
                events,
                key=lambda item: self.sessions.event_sequence(ctx, item),
            ))
        except Exception as exc:
            print(
                "  [gateway:audit] event=steer_stop_order_failed "
                f"route={safe_route_digest(ctx.route_key)} "
                f"exception={type(exc).__name__}"
            )
            return

        message_ids = tuple(event.message_id for event in ordered_events)
        try:
            states = await self.persistence.call(
                get_gateway_steer_recovery_states,
                ctx.route_key,
                message_ids,
            )
        except Exception as exc:
            print(
                "  [gateway:audit] event=steer_stop_state_failed "
                f"route={safe_route_digest(ctx.route_key)} "
                f"exception={type(exc).__name__}"
            )
            return

        for event in ordered_events:
            message_id = event.message_id
            state = states.get(message_id)
            owns_queue = (
                isinstance(state, dict)
                and state.get("layer") == "queue"
                and state.get("owner_id") == message_id
            )
            status = state.get("status") if owns_queue else None
            queue_status = (
                state.get("queue_status")
                if owns_queue
                else None
            )

            if (
                owns_queue
                and status in {"completed", "cancelled"}
                and queue_status is None
            ):
                self.sessions.resolve_steer_event(
                    ctx,
                    generation,
                    message_id,
                )
                self._accepted_messages.discard(
                    (ctx.route_key, message_id)
                )
                continue

            if (
                not owns_queue
                or status not in {"queued", "processing"}
                or queue_status != status
            ):
                print(
                    "  [gateway:audit] event=steer_stop_state_unresolved "
                    f"route={safe_route_digest(ctx.route_key)} "
                    f"steer={safe_identifier_digest(message_id)}"
                )
                continue

            already_pending = any(
                getattr(pending_event, "message_id", None) == message_id
                for pending_event in ctx.pending
            )
            if not already_pending:
                try:
                    requeued = self.sessions.enqueue_ordered(
                        ctx,
                        event,
                        force=True,
                    )
                except Exception as exc:
                    print(
                        "  [gateway:audit] event=steer_stop_requeue_failed "
                        f"route={safe_route_digest(ctx.route_key)} "
                        f"steer={safe_identifier_digest(message_id)} "
                        f"exception={type(exc).__name__}"
                    )
                    continue
                if not requeued:
                    print(
                        "  [gateway:audit] event=steer_stop_requeue_rejected "
                        f"route={safe_route_digest(ctx.route_key)} "
                        f"steer={safe_identifier_digest(message_id)}"
                    )
                    continue

            self.sessions.resolve_steer_event(
                ctx,
                generation,
                message_id,
            )

    def _drop_events(self, route_key: str, events: list[MessageEvent]) -> None:
        """持久化删除被 /new 明确取消的旧 pending。"""
        message_ids = [event.message_id for event in events]
        self.persistence.call_sync(
            delete_gateway_messages,
            route_key,
            message_ids,
        )
        for message_id in message_ids:
            self._accepted_messages.discard((route_key, message_id))
            self._mailbox_registration_fallback_events.discard(
                (route_key, message_id)
            )

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
            self._mailbox_registration_fallback_events.discard(
                (route_key, event.message_id)
            )
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

        system_rows = [
            row for row in rows
            if str(row.get("delivery_kind", "")).startswith("cron_")
        ]
        for row in system_rows:
            await self._launch_system_outbox(row["id"], row["route_key"])
        rows = [row for row in rows if row not in system_rows]

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
            cancel_reason = self._refresh_task_cancel_reason(
                ctx,
                generation,
                cancel_reason,
            )
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
        async with self._route_admission(route_key):
            return await self._handle_message_serialized(
                event,
                from_queue=from_queue,
            )

    @asynccontextmanager
    async def _route_admission(self, route_key: str):
        """登记并持有 route 的短临界区，退出后回收空闲锁。"""

        if self._route_admission_closed:
            raise RuntimeError(
                f"gateway route admission is closed during "
                f"{self._lifecycle_phase}"
            )
        lock = self._route_admission_locks.setdefault(
            route_key,
            asyncio.Lock(),
        )
        self._route_admission_users[route_key] = (
            self._route_admission_users.get(route_key, 0) + 1
        )
        try:
            async with lock:
                yield
        finally:
            users = self._route_admission_users.get(route_key, 1) - 1
            if users <= 0:
                self._route_admission_users.pop(route_key, None)
                if self._route_admission_locks.get(route_key) is lock:
                    self._route_admission_locks.pop(route_key, None)
            else:
                self._route_admission_users[route_key] = users

    async def _drain_route_admissions(self) -> None:
        """关闭新 admission，并等待已登记临界区全部退出。"""

        self._route_admission_closed = True
        locks = tuple(self._route_admission_locks.values())
        for lock in locks:
            async with lock:
                pass
        # 让刚退出临界区的 finally 完成 users/lock 回收。
        await asyncio.sleep(0)

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
        self._mailbox_registration_fallback_events.discard(
            (route_key, event.message_id)
        )
        print(
            "  [gateway:audit] event=approval_resume_rejected "
            f"{safe_route_digest(route_key)} "
            f"{safe_message_digest(event.message_id)} "
            "failure_type=invalid_approval_resume_task"
        )

    async def _start_agent_worker(
        self,
        route_key: str,
        event: MessageEvent,
        ctx,
        generation: int,
        invalidation_event: asyncio.Event,
        *,
        approval_resume_id: str | None,
        delivery_event: MessageEvent,
        steer_mailbox: SteerMailbox | None,
    ) -> None:
        """为正常或无 mailbox fallback run 启动同一套唯一 worker。"""
        if steer_mailbox is None:
            ctx.active_steer_mailbox = None
            ctx.steer_generation = None
        elif (
            ctx.active_steer_mailbox is not steer_mailbox
            or ctx.steer_generation != generation
        ):
            raise RuntimeError(
                "registered steer mailbox does not own task generation"
            )
        ctx.active_generation = generation

        await self._mark_processing_best_effort(event)
        try:
            await self._mark_event_processing_async(route_key, event)
        except Exception as exc:
            # 原始 Queue 记录已经可靠存在；状态标记失败不能让 route
            # 停在尚未创建 worker 的 busy 状态。
            print(
                "  [gateway:audit] "
                "event=queue_processing_mark_failed "
                f"route={safe_route_digest(route_key)} "
                f"message={safe_message_digest(event.message_id)} "
                f"exception={type(exc).__name__}"
            )

        delivery_id = str(uuid.uuid4())
        ctx.delivery_id = delivery_id
        ctx.delivery_generation = generation
        # 模型 Task 与串行收尾 worker 分开管理。即使模型 Task 在首次运行前
        # 就被取消，worker 仍会启动并清理 busy / 持久队列。
        agent_task = asyncio.create_task(
            self._run_agent(
                event,
                ctx,
                resume_from_history=approval_resume_id is not None,
                approval_resume_id=approval_resume_id,
            ),
        )
        ctx.active_task = agent_task
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
                if persisted.get("layer") == "queue" and persisted.get(
                    "status"
                ) in {"queued", "processing"}:
                    existing_ctx = self.sessions.get(route_key)
                    if (
                        existing_ctx is not None
                        and self._route_has_active_worker(existing_ctx)
                    ):
                        return
                    # 数据库中仍为 queued/processing 的原始事件可直接复用，
                    # 不再次持久化，也不创建新的 ownership。
                    from_queue = True
                else:
                    return
            elif queue_key in self._accepted_messages:
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
            actor_id = self._stable_actor_id(event)
            if actor_id is None:
                content = (
                    "当前平台事件缺少可验证的用户身份，无法处理审批请求。"
                )
            elif cmd == "/deny":
                if len(command_argument.split()) > 1:
                    content = "用法：/deny"
                else:
                    approval_selector = command_argument
                    decision = await self.persistence.call(
                        deny_gateway_approval,
                        route_key,
                        ctx.conversation_id,
                        actor_id,
                        approval_selector,
                        event.message_id,
                    )
                    outcome = str(decision.get("outcome", ""))
                    if outcome == "denied":
                        content = "已拒绝该审批请求，操作未执行。"
                    else:
                        content = _approval_command_reply(
                            outcome,
                            approval_selector,
                        )
            else:
                parsed_approval = _parse_approval_selector_and_scope(
                    command_argument
                )
                if parsed_approval is None:
                    content = "用法：/approve [once|session]"
                else:
                    approval_selector, grant_scope = parsed_approval
                    content = (
                        "审批已通过，操作已进入原会话的恢复队列；成功后将启用"
                        "受限会话授权。"
                        if grant_scope == "session"
                        else "审批已通过，操作已进入原会话的恢复队列。"
                    )
                    has_adapter = event.source.platform in self.adapters
                    if has_adapter:
                        ack_outbox = self._build_outbox(
                            route_key,
                            event,
                            content,
                            str(uuid.uuid4()),
                            "approval_command",
                        )
                        decision = await self.persistence.call(
                            claim_gateway_approval_with_ack_outbox,
                            route_key,
                            ctx.conversation_id,
                            actor_id,
                            approval_selector,
                            event.message_id,
                            grant_scope,
                            ack_outbox,
                            **self._runtime_fence_kwargs(),
                        )
                    else:
                        # 嵌入式调用没有可持久投递的 Adapter，保留直接回复，
                        # 但仍由同一领取入口先持久化恢复任务。
                        decision = await self.persistence.call(
                            claim_gateway_approval,
                            route_key,
                            ctx.conversation_id,
                            actor_id,
                            approval_selector,
                            event.message_id,
                            grant_scope,
                        )
                    outcome = str(decision.get("outcome", ""))
                    if outcome == "claimed":
                        resume_task = decision.get("resume_task")
                        if not isinstance(resume_task, dict):
                            raise RuntimeError(
                                "approved gateway operation is missing its "
                                "recovery task"
                            )
                        resume_event = self._deserialize_event(
                            str(resume_task["event_json"])
                        )
                        if not has_adapter:
                            sent = await self._reply(event, content)
                            if sent is not None and sent.success:
                                self._accepted_messages.add(
                                    (route_key, resume_event.message_id)
                                )
                                await self._handle_message_serialized(
                                    resume_event,
                                    from_queue=True,
                                )
                            return
                        ack_outbox_id = decision.get("ack_outbox_id")
                        if not isinstance(ack_outbox_id, str) or not ack_outbox_id:
                            raise RuntimeError(
                                "approved gateway operation is missing its "
                                "acknowledgement outbox"
                            )
                        self._accepted_messages.add((
                            route_key,
                            event.message_id,
                        ))
                        self._accepted_messages.add((
                            route_key,
                            resume_event.message_id,
                        ))
                        self.sessions.enqueue(ctx, resume_event)
                        self._launch_durable_reply_worker(
                            route_key,
                            event,
                            ack_outbox_id,
                            ctx,
                        )
                        return
                    else:
                        content = _approval_command_reply(
                            outcome,
                            approval_selector,
                        )

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

        if (
            approval_resume_id is None
            and cmd not in {
                "/delete", "/sessions", "/status", "/new", "/stop",
            }
        ):
            ctx = await self.sessions.get_or_create_async(
                route_key, self._build_gateway_prompt(event.source),
            )
            pending_approval = await self._pending_approval_for_context(
                route_key,
                ctx,
            )
            if pending_approval is not None:
                request_id = _short_approval_id(pending_approval["id"])
                details = pending_approval.get("details", {})
                scopes = (
                    details.get("allowed_grant_scopes", [])
                    if isinstance(details, dict)
                    else []
                )
                lines = [
                    f"当前有待审批操作 {request_id}，原任务已暂停。"
                ]
                if "once" in scopes:
                    lines.append("单次批准：/approve")
                if "session" in scopes:
                    lines.append("本会话授权：/approve session")
                lines.append("拒绝：/deny")
                content = "\n".join(lines)
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
        if cmd == "/sessions":
            if (
                command_argument
                and (
                    not command_argument.isdecimal()
                    or int(command_argument) <= 0
                )
            ):
                content = "用法：/sessions <页码>，页码必须是正整数。"
                ctx = None
            else:
                page = int(command_argument) if command_argument else 1
                ctx = await self.sessions.get_or_create_async(
                    route_key, self._build_gateway_prompt(event.source),
                )
                conversations = await self.persistence.call(
                    list_gateway_conversations,
                    route_key,
                    10,
                    (page - 1) * 10,
                )
                if not conversations and page > 1:
                    content = (
                        "页码超出实际范围，请使用 /sessions <页码> "
                        "查看有效页面。"
                    )
                else:
                    mapping = {
                        (page - 1) * 10 + position: conversation[
                            "conversation_id"
                        ]
                        for position, conversation in enumerate(
                            conversations,
                            start=1,
                        )
                    }
                    self.sessions.save_conversation_list_mapping(ctx, mapping)
                    content = self._format_conversation_list(
                        conversations,
                        page,
                    )
            if event.source.platform not in self.adapters:
                await self._reply(event, content)
                return
            if ctx is None:
                ctx = await self.sessions.get_or_create_async(
                    route_key, self._build_gateway_prompt(event.source),
                )
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
            content = None
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
                    mapping = self.sessions.get_conversation_list_mapping(ctx)
                    if mapping is None:
                        content = "请先使用 /sessions 查看当前会话列表。"
                    else:
                        conversation_id = mapping.get(int(command_argument))
                        if conversation_id is None:
                            content = (
                                "该序号不在当前会话列表中，请先使用 "
                                "/sessions <页码> 查看对应页面。"
                            )
                        else:
                            target = await self.persistence.call(
                                get_gateway_conversation_for_route,
                                route_key,
                                conversation_id,
                            )
                else:
                    target = await self.persistence.call(
                        get_gateway_conversation_for_route,
                        route_key,
                        command_argument,
                    )

                if content is not None:
                    pass
                elif target is None:
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
        if cmd == "/delete":
            ctx = await self.sessions.get_or_create_async(
                route_key, self._build_gateway_prompt(event.source),
            )
            if (
                not command_argument
                or not command_argument.isdecimal()
                or int(command_argument) <= 0
            ):
                content = "用法：/delete <序号>，序号必须是正整数。"
            else:
                mapping = self.sessions.get_conversation_list_mapping(ctx)
                if mapping is None:
                    content = "请先使用 /sessions 查看当前会话列表。"
                else:
                    conversation_id = mapping.get(int(command_argument))
                    if conversation_id is None:
                        content = (
                            "该序号不在当前会话列表中，请先使用 "
                            "/sessions <页码> 查看对应页面。"
                        )
                    else:
                        target = await self.persistence.call(
                            get_gateway_conversation_for_route,
                            route_key,
                            conversation_id,
                        )
                        if target is None:
                            content = (
                                "删除失败：该会话不存在或无权访问。"
                            )
                        elif conversation_id == ctx.conversation_id:
                            content = (
                                "不能删除当前正在使用的会话，请先使用 /new 或 "
                                "/resume <序号> 切换到其他会话。"
                            )
                        else:
                            try:
                                cleanup_report = (
                                    await self._cleanup_session_resources(
                                        conversation_id,
                                    )
                                )
                            except Exception:
                                cleanup_report = None
                            if (
                                cleanup_report is None
                                or not cleanup_report.complete
                            ):
                                content = (
                                    "删除失败：该会话资源未能完成清理，"
                                    "请稍后重试。"
                                )
                            else:
                                try:
                                    result = await self.persistence.call(
                                        delete_gateway_conversation_for_route,
                                        route_key,
                                        conversation_id,
                                    )
                                except Exception:
                                    content = (
                                        "删除失败：暂时无法删除该会话，"
                                        "请稍后重试。"
                                    )
                                else:
                                    outcome = result.get("outcome")
                                    if outcome == "deleted":
                                        self.sessions.clear_conversation_list_mapping(
                                            ctx
                                        )
                                        content = "\n".join([
                                            "会话 "
                                            f"{self._short_conversation_id(conversation_id)} "
                                            "已删除。",
                                            "请重新使用 /sessions <页码> 刷新会话列表。",
                                        ])
                                    elif outcome == "current":
                                        content = (
                                            "不能删除当前正在使用的会话，请先使用 /new 或 "
                                            "/resume <序号> 切换到其他会话。"
                                        )
                                    else:
                                        content = (
                                            "删除失败：该会话不存在或无权访问。"
                                        )

            if event.source.platform not in self.adapters:
                await self._reply(event, content)
                return
            await self._start_durable_reply_async(
                route_key,
                event,
                content,
                "delete_command",
                ctx,
            )
            return
        if cmd == "/new" and not command_argument:
            ctx = await self.sessions.get_or_create_async(
                route_key, self._build_gateway_prompt(event.source),
            )
            previous_conversation_id = ctx.conversation_id
            await self.persistence.call(
                cancel_pending_gateway_approvals,
                route_key,
                ctx.conversation_id,
                decision_source="new_conversation",
            )
            unconfirmed_steer = (
                self.sessions.get_unconfirmed_steer_event_records(ctx)
            )
            if self._route_has_active_worker(ctx):
                # /new 作为串行屏障:丢弃命令前尚未执行的旧消息,
                # 等当前 worker 完全退出后再切换 conversation_id。
                dropped_by_id = {
                    pending_event.message_id: pending_event
                    for pending_event in ctx.pending
                }
                for _steer_generation, steer_event in unconfirmed_steer:
                    dropped_by_id.setdefault(
                        steer_event.message_id,
                        steer_event,
                    )
                dropped_events = list(dropped_by_id.values())
                await self._drop_events_async(route_key, dropped_events)
                ctx.pending.clear()
                for steer_generation, steer_event in unconfirmed_steer:
                    self.sessions.resolve_steer_event(
                        ctx,
                        steer_generation,
                        steer_event.message_id,
                    )
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
            # /stop 恢复曾失败时，旧 generation 的 steer 仍可能只保留在
            # durable Queue 与 deferred 映射中。空闲 /new 也必须完成屏障，
            # 不能让这些旧输入在重启后进入新会话。
            dropped_by_id = (
                {
                    pending_event.message_id: pending_event
                    for pending_event in ctx.pending
                }
                if not from_queue
                else {}
            )
            for _steer_generation, steer_event in unconfirmed_steer:
                dropped_by_id.setdefault(
                    steer_event.message_id,
                    steer_event,
                )
            if dropped_by_id:
                await self._drop_events_async(
                    route_key,
                    list(dropped_by_id.values()),
                )
                if not from_queue:
                    ctx.pending.clear()
                for steer_generation, steer_event in unconfirmed_steer:
                    self.sessions.resolve_steer_event(
                        ctx,
                        steer_generation,
                        steer_event.message_id,
                    )
            if (
                not from_queue
                and not await self._persist_event_async(route_key, event)
            ):
                return

            try:
                cleanup_report = await self._cleanup_session_resources(
                    previous_conversation_id,
                )
            except Exception:
                cleanup_report = None
            if cleanup_report is None or not cleanup_report.complete:
                cleanup_error = (
                    "无法创建新会话：旧会话资源未能完成清理，请稍后重试。"
                )
                if event.source.platform not in self.adapters:
                    result = await self._reply(event, cleanup_error)
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
                    cleanup_error,
                    "new_conversation_cleanup_failed",
                    ctx,
                )
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
                decision_source="user_stop",
            )
            ok = await self._request_session_cancel_async(
                route_key,
                reason="user",
            )
            if ok or cancelled_approvals:
                content = "(cancel requested)"
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
            if status is not None:
                status["busy_input_mode"] = self.busy_input_mode
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
            if not from_queue:
                self.sessions.event_sequence(ctx, event)
                event_persisted = False
                if self.busy_input_mode == "steer":
                    if not await self._persist_event_async(route_key, event):
                        return
                    event_persisted = True
                    steer_entry = SteerEntry(
                        steer_id=event.message_id,
                        text=event.text,
                        sequence=self.sessions.event_sequence(ctx, event),
                    )
                    if self.sessions.submit_steer(
                        route_key,
                        steer_entry,
                        event,
                    ):
                        print(
                            f"  [gateway] {route_key}: steer accepted"
                        )
                        return
                    # mailbox 接收失败时复用已经持久化的原始 Queue 记录。
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
                # 先持久化入站事件再创建 Outbox,保证重启恢复时
                # Outbox 的 source_message_id 能在 gateway_messages
                # 表中找到对应记录,去重窗口与审计关联不会断裂。
                if (
                    not event_persisted
                    and not await self._persist_event_async(route_key, event)
                ):
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
                and not event_persisted
                and not await self._persist_event_async(route_key, event)
            ):
                return
            if from_queue:
                # 已持久化消息必须全部恢复,不能因重启后的新上限丢失。
                self.sessions.enqueue(ctx, event, force=True)
            else:
                self.sessions.enqueue(ctx, event)
            await self._mark_processing_best_effort(event)
            # 重启恢复的历史队列按原顺序完整执行,不能让后一条恢复消息
            # 取消前一条;只有新到达的实时消息才覆盖当前请求。
            if not from_queue and self.busy_input_mode == "interrupt":
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
        if approval_resume_id is not None:
            if (
                self._lifecycle_phase
                in {"stopping", "stopped", "lease_lost"}
                or self._runtime_lease_blocks_delivery()
            ):
                await self._reject_approval_resume_task(route_key, event)
                return
        fallback_key = (route_key, event.message_id)
        fallback_without_mailbox = (
            from_queue
            and fallback_key
            in self._mailbox_registration_fallback_events
        )
        if fallback_without_mailbox:
            self._mailbox_registration_fallback_events.discard(
                fallback_key
            )
        generation, invalidation_event = self.sessions.begin_task(ctx)
        steer_mailbox = None
        if fallback_without_mailbox:
            print(
                "  [gateway:audit] "
                "event=steer_mailbox_fallback_run "
                f"route={safe_route_digest(route_key)} "
                f"message={safe_message_digest(event.message_id)}"
            )
        else:
            steer_mailbox = SteerMailbox()
            if not self.sessions.register_steer_mailbox(
                ctx,
                generation,
                steer_mailbox,
            ):
                steer_mailbox.close()
                self.sessions.rollback_task_begin(ctx, generation)
                print(
                    "  [gateway:audit] steer mailbox registration failed "
                    f"route={safe_route_digest(route_key)} "
                    f"message={safe_message_digest(event.message_id)}"
                )
                requeued = False
                try:
                    already_pending = any(
                        pending_event.message_id == event.message_id
                        for pending_event in ctx.pending
                    )
                    requeued = (
                        True
                        if already_pending
                        else self.sessions.enqueue_ordered(
                            ctx,
                            event,
                            force=True,
                        )
                    )
                except Exception as exc:
                    print(
                        "  [gateway:audit] queued event recovery after mailbox "
                        "registration failure failed "
                        f"route={safe_route_digest(route_key)} "
                        f"exception={type(exc).__name__}"
                    )
                    return
                if not requeued:
                    print(
                        "  [gateway:audit] queued event recovery after mailbox "
                        "registration failure rejected "
                        f"route={safe_route_digest(route_key)} "
                        f"message={safe_message_digest(event.message_id)}"
                    )
                    return
                self._mailbox_registration_fallback_events.add(
                    fallback_key
                )
                await self._dispatch_next(
                    ctx,
                    admission_locked=True,
                )
                return
        await self._start_agent_worker(
            route_key,
            event,
            ctx,
            generation,
            invalidation_event,
            approval_resume_id=approval_resume_id,
            delivery_event=delivery_event,
            steer_mailbox=steer_mailbox,
        )

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
        # 防止 try 块中已成功调用 finish_processing 后,finally 块
        # 再次以 "cancelled" 调用,导致 adapter 收到重复状态通知。
        processing_finished = False
        agent_result = _GatewayAgentResult(None)
        progressive_controller: ProgressiveReplyController | None = None
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
            progressive_controller = agent_result.progressive_controller
            await self._close_progressive_controller(
                progressive_controller,
                abort=False,
            )
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
                # 用户已经收到“审批已通过”回执，但恢复执行最终失败。若没有
                # 任何回复到达用户，补一条失败提示，避免审批通过后无下文。
                if persisted_outbox is None:
                    fallback_outbox = self._build_outbox(
                        route_key,
                        delivery_event,
                        _SAFE_INTERNAL_REPLY,
                        delivery_id,
                        "internal_error",
                        queue_message_id=event.message_id,
                    )
                    fallback_delivery_id = await self._enqueue_outbox_async(
                        fallback_outbox
                    )
                    if (
                        ctx.delivery_generation == generation
                        and self._task_cancel_reason(ctx, generation) is None
                    ):
                        ctx.delivery_id = fallback_delivery_id
                    await self._deliver_outbox(
                        route_key,
                        delivery_event,
                        fallback_delivery_id,
                        ctx,
                        generation,
                        invalidation_event,
                        progressive_controller=progressive_controller,
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
                            delivery_event.message_id,
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
                    progressive_controller=progressive_controller,
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
                    if not processing_finished:
                        processing_finished = (
                            await self._finish_processing_best_effort(
                                delivery_event,
                                "failed",
                                ctx=ctx,
                                generation=generation,
                            )
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
                    progressive_controller=progressive_controller,
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
            original_exc = exc
            if isinstance(exc, _GatewayAgentRunError):
                original_exc = exc.original
                progressive_controller = exc.progressive_controller
                await self._close_progressive_controller(
                    progressive_controller,
                    abort=False,
                )
            print(
                f"  [gateway] {route_key} error: "
                f"{type(original_exc).__name__}"
            )
            drained_steer = self._close_and_drain_generation_steer(
                ctx,
                generation,
            )
            if drained_steer:
                pending_by_id = {
                    entry.steer_id: entry
                    for entry in agent_result.pending_steer
                }
                for entry in drained_steer:
                    pending_by_id.setdefault(entry.steer_id, entry)
                agent_result = _GatewayAgentResult(
                    agent_result.response,
                    failed=agent_result.failed,
                    failure_type=agent_result.failure_type,
                    pending_steer=tuple(pending_by_id.values()),
                    progressive_controller=progressive_controller,
                )
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
                    if not processing_finished:
                        processing_finished = (
                            await self._finish_processing_best_effort(
                                event,
                                "failed",
                                ctx=ctx,
                                generation=generation,
                            )
                        )
                else:
                    if existing_error_outbox is None:
                        failure = self._safe_exception_result(original_exc)
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
                            if not processing_finished:
                                processing_finished = (
                                    await self._finish_processing_best_effort(
                                        delivery_event,
                                        "failed",
                                        ctx=ctx,
                                        generation=generation,
                                    )
                                )
                            delivered = await self._deliver_outbox(
                                route_key,
                                delivery_event,
                                delivery_id,
                                ctx,
                                generation,
                                invalidation_event,
                                progressive_controller=progressive_controller,
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
                            if not processing_finished:
                                processing_finished = (
                                    await self._finish_processing_best_effort(
                                        event,
                                        "failed",
                                        ctx=ctx,
                                        generation=generation,
                                    )
                                )
                    else:
                        try:
                            delivered = await self._deliver_outbox(
                                route_key,
                                delivery_event,
                                delivery_id,
                                ctx,
                                generation,
                                invalidation_event,
                                progressive_controller=progressive_controller,
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
                                f"  [gateway] {route_key}: persisted error "
                                "outbox delivery failed "
                                f"({type(send_exc).__name__})"
                            )
        finally:
            await self._close_progressive_controller(
                progressive_controller,
                abort=True,
            )
            cancel_reason = self._refresh_task_cancel_reason(
                ctx,
                generation,
                cancel_reason,
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
                cancel_reason = self._refresh_task_cancel_reason(
                    ctx,
                    generation,
                    cancel_reason,
                )
                # 关键状态已落库后才对外暴露 route 空闲。普通
                # 收尾先进入 dispatching，由 admission 锁保证 pending
                # 队头不会被同时到达的新消息越过。
                if (
                    owns_worker
                    and abandoned
                    and cancel_reason != "shutdown"
                    and not processing_finished
                ):
                    await self._finish_processing_best_effort(
                        delivery_event,
                        "cancelled",
                        ctx=ctx,
                        generation=generation,
                    )
                if (
                    owns_worker
                    and cancel_reason == "user"
                ):
                    try:
                        await self._restore_user_cancelled_steer(
                            ctx,
                            generation,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        print(
                            "  [gateway:audit] "
                            "event=steer_stop_recovery_failed "
                            f"route={safe_route_digest(route_key)} "
                            f"exception={type(exc).__name__}"
                        )
                if (
                    owns_worker
                    and cancel_reason is None
                    and agent_result.pending_steer
                ):
                    await self._requeue_pending_steer(
                        ctx,
                        generation,
                        agent_result.pending_steer,
                    )
                if owns_worker:
                    self.sessions.clear_steer_mailbox(ctx, generation)
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
        if self._route_admission_closed:
            return
        # 当前 admission 调用可能在同一个事件循环回调中紧接着
        # 提交多条消息。后台收尾者等到已注册的 admission
        # 全部退出，并再让出一次调度；这样同一回调紧接着
        # 发起的下一条消息可以先注册，不会被接力者插队。
        await self._wait_for_route_admissions(route_key)
        if self._route_admission_closed:
            return
        async with self._route_admission(route_key):
            await self._dispatch_next_locked(ctx)

    async def _wait_for_route_admissions(self, route_key: str) -> None:
        """等待当前入站批次稳定退出 route 临界区。"""
        while True:
            while self._route_admission_users.get(route_key, 0) > 0:
                if self._route_admission_closed:
                    return
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            if (
                self._route_admission_closed
                or self._route_admission_users.get(route_key, 0) == 0
            ):
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

    async def _execute_claimed_approval(
        self,
        execution: dict,
        approval_grant: TrustedApprovalGrant,
        *,
        cancel_checker,
    ) -> tuple[str, bool]:
        """在已领取的 Gateway 审批边界内直接执行原始工具调用。"""
        if cancel_checker():
            return json.dumps({
                "ok": False,
                "error_type": "cancelled",
                "error": "approval execution was cancelled",
            }, ensure_ascii=False), False
        try:
            task_event = self._deserialize_event(
                str(execution["source_event_json"])
            )
            if (
                task_event.message_id != execution["source_message_id"]
                or build_session_key(task_event.source, self.agent_name)
                != execution["route_key"]
                or approval_grant.session_key != execution["conversation_id"]
            ):
                raise ValueError("approval execution identity is invalid")
            tool_policy = self._tool_policy_for_source(task_event.source)
            resolution = registry.resolve(tool_policy)
            entry = registry.get_entry(approval_grant.tool_name)
            if (
                entry is None
                or entry.approval_mode == ApprovalMode.NONE
                or approval_grant.tool_name
                not in resolution.allowed_tool_names
            ):
                raise ValueError("approval execution tool is unavailable")
            if self._runtime_lease_acquired:
                self._require_sync_recovery_runtime_lease()

            tool_context = {
                "session_key": approval_grant.session_key,
                "interactive_approval": False,
                "approval_mode": "remote",
                "approval_grant": approval_grant,
                "allowed_tool_names": resolution.allowed_tool_names,
                "cancel_checker": cancel_checker,
            }
            if "messaging" in resolution.toolsets:
                tool_context.update(self._gateway_tool_context(
                    task_event,
                    str(execution["route_key"]),
                    approval_grant.session_key,
                ))
            durable_context = DurableToolExecutionContext(
                environment="gateway",
                session_id=approval_grant.session_key,
                source_message_id=task_event.message_id,
                database_path=self.db_path,
                gateway_lease_name=self._runtime_lease_name,
                gateway_instance_id=self._runtime_instance_id,
                gateway_lease_epoch=self._runtime_lease_epoch,
            )
            dispatcher = DurableToolDispatcher(
                registry,
                durable_context,
            )
            # 审批恢复会执行同步工具；放到工作线程中等待，不能占住 Gateway
            # 事件循环，否则 runtime lease 的心跳无法按时续约。
            output = await self._await_blocking_operation(
                dispatcher.dispatch,
                approval_grant.tool_name,
                approval_grant.arguments,
                tool_call_id=str(execution["tool_call_id"]),
                **tool_context,
            )
            return output, not tool_output_failed(output)
        except Exception as exc:
            return json.dumps({
                "ok": False,
                "error_type": "approval_execution_failed",
                "error": (
                    "approved tool execution failed: "
                    f"{type(exc).__name__}"
                ),
            }, ensure_ascii=False), False

    def _create_progressive_controller(
        self,
        event: MessageEvent,
        ctx,
        generation: int | None,
    ) -> ProgressiveReplyController | None:
        """为当前 generation 创建至多一个平台无关的渐进式控制器。"""
        if (
            not self.progressive_output_config.enabled
            or generation is None
        ):
            return None
        adapter = self.adapters.get(event.source.platform)
        if (
            adapter is None
            or not adapter.supports_progressive_reply
        ):
            return None

        def generation_is_valid() -> bool:
            return (
                ctx.generation == generation
                and ctx.active_generation == generation
                and self._task_cancel_reason(ctx, generation) is None
                and not self._runtime_lease_blocks_delivery()
            )

        if not generation_is_valid():
            return None
        return ProgressiveReplyController(
            route_key=ctx.route_key,
            generation=generation,
            event=event,
            adapter=adapter,
            config=self.progressive_output_config,
            generation_is_valid=generation_is_valid,
        )

    @staticmethod
    async def _close_progressive_controller(
        controller: ProgressiveReplyController | None,
        *,
        abort: bool,
    ) -> None:
        """尽力回收展示任务，不让展示层异常覆盖 Agent 或 Outbox 结果。"""
        if controller is None:
            return
        try:
            if abort:
                await controller.abort()
            else:
                await controller.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

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
        """绑定单次运行的渐进式控制器，并保证异常路径回收后台任务。"""
        progressive_holder: list[ProgressiveReplyController] = []
        try:
            result = await self._run_agent_async_impl(
                event,
                ctx,
                resume_from_history=resume_from_history,
                approval_resume_id=approval_resume_id,
                progressive_holder=progressive_holder,
            )
        except asyncio.CancelledError:
            if progressive_holder:
                try:
                    await progressive_holder[0].abort()
                except BaseException:
                    pass
            raise
        except Exception as exc:
            if not progressive_holder:
                raise
            controller = progressive_holder[0]
            try:
                await controller.close()
            except BaseException:
                pass
            raise _GatewayAgentRunError(exc, controller) from exc
        except BaseException:
            if progressive_holder:
                try:
                    await progressive_holder[0].abort()
                except BaseException:
                    pass
            raise
        if not progressive_holder:
            return result
        return replace(
            result,
            progressive_controller=progressive_holder[0],
        )

    async def _run_agent_async_impl(
        self,
        event: MessageEvent,
        ctx,
        *,
        resume_from_history: bool = False,
        approval_resume_id: str | None = None,
        progressive_holder: list[ProgressiveReplyController],
    ) -> _GatewayAgentResult:
        """使用 AsyncOpenAI 跑主会话，数据库 hook 统一在线程执行。"""
        from hermes.db import ensure_session
        from hermes.conversation import run_conversation_async

        generation = getattr(ctx, "active_generation", None)
        if generation is None:
            generation = getattr(ctx, "generation", None)
        steer_mailbox = getattr(ctx, "active_steer_mailbox", None)
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
        callback_steer_ids: set[str] = set()

        def persist_steer_callback(conn, steer_ids) -> None:
            """在工具消息事务内确认对应的原始 Gateway Queue 记录。"""
            ids = tuple(steer_ids)
            if not ids:
                return
            if (
                generation is None
                or ctx.generation != generation
                or self._task_cancel_reason(ctx, generation) is not None
            ):
                raise RuntimeError("stale steer generation")
            if any(
                steer_id not in ctx.inflight_steer_events
                for steer_id in ids
            ):
                raise RuntimeError("steer event mapping is missing")
            complete_gateway_steer_messages_in_transaction(
                conn,
                route_key,
                ids,
            )
            callback_steer_ids.update(ids)

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

        if (
            approval_resume is not None
            and approval_resume["approval"]["status"] == "executing"
        ):
            approval_id = approval_resume["approval"]["id"]
            execution = await self.persistence.call(
                begin_gateway_approval_execution,
                route_key,
                conversation_id,
                event.message_id,
                approval_id,
            )
            if execution is None:
                return _GatewayAgentResult(
                    _SAFE_INTERNAL_REPLY,
                    failed=True,
                    failure_type="invalid_approval_resume_task",
                )
            try:
                approval_grant = issue_trusted_approval_grant(
                    {
                        "id": execution["id"],
                        "tool_name": execution["tool_name"],
                        "tool_args": execution["tool_args"],
                        "details": execution["details"],
                        "conversation_id": execution["conversation_id"],
                        "session_key": execution["session_key"],
                        "tool_call_id": execution["tool_call_id"],
                        "status": "executing",
                        "grant_scope": execution["grant_scope"],
                        "created_at": execution["created_at"],
                        "expires_at": execution["expires_at"],
                        "updated_at": execution["updated_at"],
                        "fingerprint": execution["fingerprint"],
                    },
                    scope=str(execution["grant_scope"] or ""),
                )
            except (TypeError, ValueError):
                return _GatewayAgentResult(
                    _SAFE_INTERNAL_REPLY,
                    failed=True,
                    failure_type="invalid_approval_resume_task",
                )
            output, succeeded = await self._execute_claimed_approval(
                execution,
                approval_grant,
                cancel_checker=cancel_checker,
            )
            try:
                completed = await self.persistence.call(
                    finish_gateway_approval,
                    approval_id,
                    output,
                    succeeded=succeeded,
                )
            except Exception:
                return _GatewayAgentResult(
                    _SAFE_INTERNAL_REPLY,
                    failed=True,
                    failure_type="approval_execution_persistence_failed",
                )
            if succeeded and approval_grant.scope == "session":
                activate_session_grant(approval_grant)
            approval_resume["approval"] = completed

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
        tool_policy = self._tool_policy_for_source(task_event.source)
        enabled_toolsets = registry.resolve(tool_policy).toolsets
        tool_context = {
            "interactive_approval": False,
            "approval_mode": "remote",
            "durable_tool_execution": {
                "environment": "gateway",
                "session_id": conversation_id,
                "source_message_id": task_event.message_id,
                "database_path": self.db_path,
                "gateway_lease_name": self._runtime_lease_name,
                "gateway_instance_id": self._runtime_instance_id,
                "gateway_lease_epoch": self._runtime_lease_epoch,
            },
        }
        if "messaging" in enabled_toolsets:
            tool_context.update(self._gateway_tool_context(
                task_event,
                route_key,
                conversation_id,
            ))
        progressive_controller = self._create_progressive_controller(
            task_event,
            ctx,
            generation,
        )
        if progressive_controller is not None:
            progressive_holder.append(progressive_controller)
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
            steer_persist_callback=persist_steer_callback,
            hook_registry=self._hook_registry,
            resume_from_history=resume_from_history,
            approval_resume_id=(approval["id"] if approval is not None else None),
            approval_tool_call_id=(
                approval["tool_call_id"] if approval is not None else None
            ),
            resume_state=(approval["agent_state"] if approval is not None else None),
            tool_context=tool_context,
            tool_policy=tool_policy,
            steer_mailbox=steer_mailbox,
            stream_sink=(
                progressive_controller.feed
                if progressive_controller is not None
                else None
            ),
        )
        acknowledged_steer_ids: set[str] = set()
        for steer_id in callback_steer_ids:
            try:
                state = await self.persistence.call(
                    get_gateway_message_persistence_state,
                    route_key,
                    steer_id,
                )
            except Exception:
                continue
            if (
                isinstance(state, dict)
                and state.get("layer") == "queue"
                and state.get("owner_id") == steer_id
                and state.get("status") == "completed"
            ):
                acknowledged_steer_ids.add(steer_id)
        if acknowledged_steer_ids:
            self.sessions.acknowledge_steer_events(
                ctx,
                generation,
                acknowledged_steer_ids,
            )
            for steer_id in acknowledged_steer_ids:
                self._accepted_messages.discard((route_key, steer_id))
        pending_steer = result.get("pending_steer")
        if isinstance(pending_steer, (list, tuple)) and all(
            isinstance(entry, SteerEntry) for entry in pending_steer
        ):
            pending_steer = tuple(pending_steer)
        else:
            pending_steer = ()
        if result.get("status") == "awaiting_approval":
            request = result.get("approval_request")
            if not isinstance(request, dict):
                return _GatewayAgentResult(
                    _SAFE_INTERNAL_REPLY,
                    failed=True,
                    failure_type="invalid_approval_request",
                    pending_steer=pending_steer,
                )
            if not _gateway_approval_request_is_allowed(
                request,
                result.get("messages"),
                tool_policy,
            ):
                return _GatewayAgentResult(
                    _SAFE_INTERNAL_REPLY,
                    failed=True,
                    failure_type="invalid_approval_request",
                    pending_steer=pending_steer,
                )
            try:
                request = _bind_approval_request_metadata(
                    request,
                    session_key=conversation_id,
                    ttl_seconds=_GATEWAY_APPROVAL_TTL_SECONDS,
                )
            except (TypeError, ValueError):
                return _GatewayAgentResult(
                    _SAFE_INTERNAL_REPLY,
                    failed=True,
                    failure_type="invalid_approval_request",
                    pending_steer=pending_steer,
                )
            if self._task_cancel_reason(ctx, generation) is not None:
                return _GatewayAgentResult(
                    None,
                    pending_steer=pending_steer,
                )
            actor_id = self._stable_actor_id(task_event)
            if actor_id is None:
                try:
                    await self.persistence.call(
                        fail_gateway_approval_identity_unavailable,
                        conversation_id,
                        str(request.get("id", "")),
                        str(request.get("tool_call_id", "")),
                    )
                except Exception as exc:
                    print(
                        "  [gateway:audit] "
                        "event=approval_identity_failure_persist_failed "
                        f"route={safe_route_digest(route_key)} "
                        f"exception={type(exc).__name__}"
                    )
                    return self._safe_exception_result(
                        exc,
                        pending_steer=pending_steer,
                    )
                return _GatewayAgentResult(
                    "当前平台事件缺少可验证的用户身份，受控操作未执行。",
                    failed=True,
                    failure_type="approval_identity_unavailable",
                    pending_steer=pending_steer,
                )
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
            try:
                delivery_id = await self.persistence.call(
                    create_gateway_approval_with_outbox,
                    conversation_id,
                    request,
                    actor_id,
                    msg,
                    outbox,
                    _GATEWAY_APPROVAL_TTL_SECONDS,
                    agent_state=result.get("agent_state"),
                    **self._runtime_fence_kwargs(),
                )
            except Exception as exc:
                print(
                    "  [gateway:audit] "
                    "event=approval_outbox_persist_failed "
                    f"route={safe_route_digest(route_key)} "
                    f"exception={type(exc).__name__}"
                )
                return self._safe_exception_result(
                    exc,
                    pending_steer=pending_steer,
                )
            if (
                getattr(ctx, "delivery_generation", generation) == generation
                and self._task_cancel_reason(ctx, generation) is None
            ):
                ctx.delivery_id = delivery_id
            return _GatewayAgentResult(
                question,
                pending_steer=pending_steer,
            )
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
