"""Cron 管理工具：在普通管理会话中维护任务完整生命周期。"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from hermes.approval import build_approval_required, is_remote_approval
from hermes.approval_policy import approval_grant_identity_matches
from hermes.config import DB_PATH
from hermes.cron.capability import (
    build_capability_scope,
    build_cron_capability_grant,
    capability_change_requires_reauthorization,
)
from hermes.cron.job import CronJob
from hermes.cron.parser import parse_schedule, validate_timezone
from hermes.cron.store import JobStore
from hermes.db import (
    DBError,
    create_cron_capability_grant,
    create_manual_cron_run,
    get_cron_job,
    init_db,
    list_cron_runs,
    resume_cron_job,
    soft_delete_cron_job,
    update_cron_job_schedule_state,
)
from hermes.tools import (
    ExecutionEnvironment,
    ToolPolicy,
    ToolRiskLevel,
    register_all,
    registry,
)
from hermes.redaction import redact_explicit_secrets


_INTERNAL_FIELD_NAMES = frozenset({
    "route_key", "runtime_fence", "gateway_runtime_fence", "gateway_db_path",
    "db_path", "instance_id", "lease_name", "lease_epoch", "approval_grant",
    "session_grant", "platform_key", "api_key", "access_token", "secret",
    "creator_id", "created_source", "source",
})
_UPDATE_FIELDS = frozenset({
    "name", "schedule", "timezone", "prompt", "toolsets", "skills", "workdir",
    "timeout", "max_agent_iterations", "overlap_policy", "misfire_policy",
    "retry_policy", "delivery_policy", "artifact_policy", "capability_spec",
})


def _fingerprint(payload: dict) -> str:
    """生成与 Gateway 一次性审批绑定的稳定摘要。"""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _json(payload: dict) -> str:
    """统一输出管理动作可消费的结构化结果。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _contains_internal_field(value: Any) -> str | None:
    """拒绝模型伪造 Gateway 身份、授权或平台凭据。"""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in _INTERNAL_FIELD_NAMES or normalized.endswith("_token") or normalized.endswith("_secret"):
                return str(key)
            nested = _contains_internal_field(child)
            if nested is not None:
                return nested
    elif isinstance(value, list):
        for child in value:
            nested = _contains_internal_field(child)
            if nested is not None:
                return nested
    return None


def _approval_scope_for_job(job: CronJob) -> tuple[dict, dict]:
    """从同一份 registry 解析可展示且可写入 grant 的规范化能力范围。"""
    register_all()
    scope = build_capability_scope(job)
    resolution = registry.resolve(ToolPolicy(
        ExecutionEnvironment.CRON,
        enabled_toolsets=frozenset(scope["toolsets"]),
        unattended=True,
        trusted_context=frozenset({"cron_execution"}),
        # 工具声明风险不能替代命令风险上限；终端命令仍由 guard 逐次限制。
        max_risk_level=ToolRiskLevel.HIGH,
    ))
    if not resolution.allowed_tool_names:
        raise ValueError("Cron capability grant has no eligible tools")
    canonical_scope = {
        "capability_scope": scope,
        "allowed_tool_names": sorted(resolution.allowed_tool_names),
    }
    return canonical_scope, scope


def _approval_scope_identity(canonical_scope: dict, *, action: str) -> dict:
    """审批始终绑定候选任务身份、版本和完整能力范围。"""
    scope = dict(canonical_scope["capability_scope"])
    return {
        "capability_scope": scope,
        "allowed_tool_names": list(canonical_scope["allowed_tool_names"]),
    }


def _scope_display_path(value: object, limit: int = 180) -> str:
    """审批界面只显示限长、脱敏的授权路径。"""
    text = redact_explicit_secrets(str(value or ""))
    return text if len(text) <= limit else f"{text[:limit]}…"


def _cron_scope_display(job: CronJob, canonical_scope: dict) -> dict:
    """为 Gateway 审批构造不含 route key、密钥和原始命令的展示摘要。"""
    scope = canonical_scope["capability_scope"]
    tools = list(canonical_scope["allowed_tool_names"])
    display_tools = tools[:12]
    if len(tools) > len(display_tools):
        display_tools.append(f"… and {len(tools) - len(display_tools)} more")
    roots = [_scope_display_path(root) for root in scope["allowed_roots"][:8]]
    if len(scope["allowed_roots"]) > len(roots):
        roots.append(f"… and {len(scope['allowed_roots']) - len(roots)} more")
    target = dict(scope.get("delivery_target") or {})
    target_kind = "thread" if target.get("thread_id") else ("chat" if target.get("chat_id") else "none")
    prompt_summary = redact_explicit_secrets(str(job.prompt).strip().replace("\n", " "))[:240]
    return {
        "name": redact_explicit_secrets(str(job.name or "(unnamed Cron task)"))[:160],
        "prompt_summary": prompt_summary,
        "prompt_digest": scope["prompt_digest"],
        "schedule": str(job.schedule),
        "timezone": str(job.timezone),
        "toolsets": list(scope["toolsets"]),
        "tool_names": display_tools,
        "workdir": _scope_display_path(scope["workdir"]),
        "allowed_roots": roots,
        "allow_file_write": bool(scope["allow_file_write"]),
        "terminal_risk_max": scope["terminal_risk_max"],
        "terminal_allowed_executables": list(scope["terminal_allowed_executables"][:12]),
        "terminal_allow_shell_operators": bool(scope["terminal_allow_shell_operators"]),
        "terminal_allow_redirection": bool(scope["terminal_allow_redirection"]),
        "terminal_allow_background": bool(scope["terminal_allow_background"]),
        "terminal_allow_network": bool(scope["terminal_allow_network"]),
        "terminal_allowed_workdirs": [
            _scope_display_path(value)
            for value in scope["terminal_allowed_workdirs"][:8]
        ],
        "delivery_platform": str(target.get("platform") or "none"),
        "delivery_target_kind": target_kind,
        "delivery_policy": str(job.delivery_config.get("policy") or "text"),
        "timeout_seconds": scope["timeout_seconds"],
        "max_artifact_file_bytes": scope["max_artifact_file_bytes"],
        "max_artifact_total_bytes": scope["max_artifact_total_bytes"],
    }


def _cron_approval_fingerprint(
    args: dict,
    session_key: str,
    action: str,
    canonical_scope: dict,
) -> str:
    """将一次性审批同时绑定调用参数、会话、动作和完整规范化能力范围。"""
    backend_risk = {"backend_type": "gateway", "host_mounts": False, "docker_socket": False, "remote_host": False}
    return _fingerprint({
        "version": 2, "tool_name": "cron", "arguments": args,
        "session_key": session_key, "backend_risk": backend_risk,
        "action": action,
        "capability_scope_digest": _fingerprint(
            _approval_scope_identity(canonical_scope, action=action)
        ),
        "prompt_digest": canonical_scope["capability_scope"]["prompt_digest"],
    })


def _cron_approval_response(
    args: dict,
    job: CronJob,
    *,
    action: str,
    canonical_scope: dict,
) -> str:
    """请求一次只用于创建或替换 Cron 持久授权的远程审批。"""
    session_key = job.session_key
    backend_risk = {"backend_type": "gateway", "host_mounts": False, "docker_socket": False, "remote_host": False}
    scope_identity = _approval_scope_identity(canonical_scope, action=action)
    fingerprint = _cron_approval_fingerprint(args, session_key, action, canonical_scope)
    return build_approval_required(
        "cron",
        "Authorize this Cron task's bounded unattended capabilities",
        details={
            "operation_type": "cron.capability_grant",
            "risk_level": "high",
            "allowed_grant_scopes": ["once"],
            "backend_risk": backend_risk,
            "decision_source": "cron_capability_policy",
            "session_key_fingerprint": "sha256:" + hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:16],
            "cron_action": action,
            "cron_candidate_job_id": job.job_id,
            "scope_digest": _fingerprint(scope_identity),
            "prompt_digest": canonical_scope["capability_scope"]["prompt_digest"],
            "cron_scope_display": _cron_scope_display(job, canonical_scope),
            "fingerprint": fingerprint,
        },
    )


def _trusted_origin(kwargs: dict) -> dict:
    """只从 Gateway 的可信上下文生成投递 origin，模型参数不能覆盖。"""
    platform = str(kwargs.get("gateway_platform") or "").strip()
    chat_id = str(kwargs.get("gateway_chat_id") or "").strip()
    if not platform or not chat_id:
        return {}
    origin = {"platform": platform, "chat_id": chat_id}
    thread_id = kwargs.get("gateway_thread_id")
    if isinstance(thread_id, str) and thread_id:
        origin["thread_id"] = thread_id
    return origin


def _delivery_config(args: dict, *, existing: dict | None, origin: dict) -> dict:
    """合并可编辑的投递策略，投递身份始终来自已有定义或可信 Gateway。"""
    config = dict(existing or {})
    policy = args.get("delivery_policy")
    if isinstance(policy, str) and policy.strip():
        config["policy"] = policy.strip()
    elif isinstance(policy, dict):
        forbidden = {"target", "origin", "route_key"} & set(policy)
        if forbidden:
            raise ValueError("delivery_policy cannot set delivery identity")
        config.update(policy)
    elif policy is not None:
        raise ValueError("delivery_policy must be a string or object")
    if not config.get("target") and not config.get("origin") and origin:
        config["origin"] = origin
    return config


def _cron_tool_policy(toolsets: frozenset[str] | None = None) -> ToolPolicy:
    """构造 Cron 无人值守工具解析条件，避免管理入口各自维护默认列表。"""
    return ToolPolicy(
        ExecutionEnvironment.CRON,
        enabled_toolsets=toolsets,
        unattended=True,
        trusted_context=frozenset({"cron_execution"}),
        max_risk_level=ToolRiskLevel.HIGH,
    )


def _validated_cron_toolsets(value: object) -> list[str]:
    """校验显式最小工具集，Cron 管理工具永不进入执行集。"""
    register_all()
    if value is None:
        raise ValueError(
            "toolsets is required; submit only the minimum toolsets needed "
            "for this Cron task"
        )
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError("toolsets must be a list of non-empty strings")
    normalized = frozenset(item.strip().lower() for item in value)
    if "cron" in normalized:
        raise ValueError("Cron management toolset cannot run inside Cron")
    resolution = registry.resolve(_cron_tool_policy(normalized))
    unsupported = normalized - resolution.toolsets
    if unsupported:
        raise ValueError(f"toolsets are not eligible for Cron: {sorted(unsupported)}")
    if not resolution.allowed_tool_names:
        raise ValueError("Cron toolsets must resolve to at least one tool")
    return sorted(normalized)


def _validated_retry_policy(value: object) -> dict:
    """校验跨 CronRun 重试的有限退避配置。"""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("retry_policy must be an object")
    allowed = {"max_attempts", "base_delay_seconds", "max_delay_seconds", "jitter_ratio", "retryable_error_types"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unsupported retry_policy fields: {sorted(unknown)}")
    try:
        max_attempts = int(value.get("max_attempts", 1))
        base_delay = float(value.get("base_delay_seconds", 5.0))
        max_delay = float(value.get("max_delay_seconds", 300.0))
        jitter = float(value.get("jitter_ratio", 0.2))
    except (TypeError, ValueError) as exc:
        raise ValueError("retry_policy fields must be numeric") from exc
    retryable = value.get("retryable_error_types", [
        "model_error",
    ])
    allowed_retryable = {
        "model_service_unavailable", "model_timeout", "infrastructure_error",
        "model_error", "network_or_timeout", "rate_limit", "server_error",
    }
    if (max_attempts < 1 or base_delay < 0 or max_delay < base_delay
            or not 0 <= jitter <= 1 or not isinstance(retryable, list)
            or not all(isinstance(item, str) and item in allowed_retryable for item in retryable)):
        raise ValueError("retry_policy values are invalid")
    return {
        "max_attempts": max_attempts,
        "base_delay_seconds": base_delay,
        "max_delay_seconds": max_delay,
        "jitter_ratio": jitter,
        "retryable_error_types": sorted(set(retryable)),
    }


def _validate_terminal_capability(toolsets: list[str], capability_spec: dict) -> None:
    """Cron 选择 terminal 时必须同时给出可审计的可执行文件白名单。"""
    if "terminal" not in toolsets:
        return
    allowed = capability_spec.get("terminal_allowed_executables")
    if not isinstance(allowed, list) or not allowed:
        raise ValueError(
            "Cron terminal requires a non-empty "
            "terminal_allowed_executables allowlist"
        )


def _validate_system_capability_spec(capability_spec: dict) -> None:
    """拒绝模型指定产物根目录，避免授权范围与运行目录分离。"""
    if "artifact_root" in capability_spec:
        raise ValueError("capability_spec.artifact_root is system-managed")


def _approved_candidate_job_id(args: dict, kwargs: dict) -> str | None:
    """仅复用 Gateway 已签发的一次性审批中保存的候选任务身份。"""
    grant = kwargs.get("approval_grant")
    candidate_job_id = getattr(grant, "cron_candidate_job_id", None)
    if (
        approval_grant_identity_matches(grant, "cron", args)
        and getattr(grant, "scope", None) == "once"
        and getattr(grant, "session_key", None)
        == str(kwargs.get("session_key") or "cli")
        and isinstance(candidate_job_id, str)
        and len(candidate_job_id) == 12
        and all(char in "0123456789abcdef" for char in candidate_job_id)
    ):
        return candidate_job_id
    return None


def _new_job(args: dict, **kwargs) -> CronJob:
    """从公开参数和可信来源构造新的任务定义。"""
    schedule = args.get("schedule")
    prompt = args.get("prompt")
    if not isinstance(schedule, str) or not schedule.strip() or not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("'schedule' and 'prompt' are required")
    timezone = validate_timezone(str(args.get("timezone") or "UTC"))
    next_fire, one_shot = parse_schedule(schedule, timezone_name=timezone)
    toolsets = _validated_cron_toolsets(args.get("toolsets"))
    skills = args.get("skills", [])
    if not isinstance(skills, list) or not all(isinstance(item, str) for item in skills):
        raise ValueError("skills must be a list of strings")
    try:
        timeout = float(args.get("timeout", 300.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a number") from exc
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    capability_spec = args.get("capability_spec", {})
    retry_policy = _validated_retry_policy(args.get("retry_policy", {}))
    artifact_policy = args.get("artifact_policy", {})
    if not all(isinstance(item, dict) for item in (capability_spec, artifact_policy)):
        raise ValueError("capability_spec and artifact_policy must be objects")
    _validate_system_capability_spec(capability_spec)
    _validate_terminal_capability(toolsets, capability_spec)
    session_key = str(kwargs.get("session_key") or "cli")
    gateway_origin = _trusted_origin(kwargs)
    source = "gateway" if gateway_origin else "cli"
    creator_id = str(kwargs.get("creator_id") or (f"cli:{session_key}" if source == "cli" else session_key))
    return CronJob(
        job_id=_approved_candidate_job_id(args, kwargs) or uuid.uuid4().hex[:12],
        schedule=schedule.strip(), prompt=prompt.strip(),
        session_key=session_key, created_at=datetime.now().isoformat(), next_fire=next_fire,
        one_shot=one_shot, name=str(args.get("name") or ""), created_source=source,
        creator_id=creator_id, timezone=timezone,
        toolsets=list(toolsets), skills=list(skills), workdir=args.get("workdir"),
        execution_timeout_seconds=timeout,
        max_agent_iterations=int(args.get("max_agent_iterations", 20)),
        overlap_policy=str(args.get("overlap_policy", "skip")),
        misfire_policy=str(args.get("misfire_policy", "run_once")),
        retry_policy=retry_policy, artifact_policy=dict(artifact_policy),
        delivery_config=_delivery_config(args, existing=None, origin=gateway_origin),
        capability_spec=dict(capability_spec), approval_status="pending",
    )


def _grant_for_job(
    job: CronJob,
    approval_id: str | None,
    *,
    canonical_scope: dict | None = None,
) -> dict:
    """冻结当前注册表中同时满足 Cron 策略和本任务授权的工具名。"""
    canonical_scope, scope = (canonical_scope, build_capability_scope(job)) if canonical_scope is not None else _approval_scope_for_job(job)
    if canonical_scope["capability_scope"] != scope:
        raise ValueError("Cron approval scope does not match the task definition")
    return build_cron_capability_grant(
        job, creator_id=job.creator_id,
        allowed_tool_names=set(canonical_scope["allowed_tool_names"]),
        approval_id=approval_id,
        scope=scope,
    )


def _requires_gateway_authorization(job: CronJob) -> bool:
    """判断创建或敏感更新是否携带无人值守高风险能力。"""
    scope = build_capability_scope(job)
    return bool("terminal" in scope["toolsets"] or scope["allow_file_write"] or scope["allow_external_communication"])


def _approved(args: dict, kwargs: dict, expected_fingerprint: str) -> bool:
    """确认本次调用确实由 Gateway 已领取的一次性 grant 恢复。"""
    grant = kwargs.get("approval_grant")
    return bool(
        approval_grant_identity_matches(grant, "cron", args)
        and getattr(grant, "scope", None) == "once"
        and getattr(grant, "session_key", None) == str(kwargs.get("session_key") or "cli")
        and getattr(grant, "fingerprint", None) == expected_fingerprint
    )


def _job_payload(job: CronJob) -> dict:
    """返回不含授权原文和内部路由的任务管理视图。"""
    return {
        "job_id": job.job_id, "name": job.name, "version": job.version,
        "schedule": job.schedule, "timezone": job.timezone, "prompt": job.prompt[:4000], "toolsets": job.toolsets,
        "skills": job.skills, "workdir": job.workdir,
        "timeout": job.execution_timeout_seconds, "overlap_policy": job.overlap_policy,
        "misfire_policy": job.misfire_policy, "retry_policy": job.retry_policy,
        "delivery_policy": job.delivery_config.get("policy", "text"),
        "artifact_policy": job.artifact_policy, "paused": job.paused,
        "next_run_at": job.next_fire, "last_run_at": job.last_run_at,
        "consecutive_failures": job.consecutive_failures,
        "approval_status": job.approval_status, "deleted": job.deleted_at is not None,
    }


def _history_payload(records: list[dict], limit: int) -> list[dict]:
    """限制历史返回量，并只暴露恢复判断所需的脱敏摘要。"""
    result = []
    for record in records[:limit]:
        result.append({
            "run_id": record["run_id"], "scheduled_for": record["scheduled_for"],
            "claimed_at": record["claimed_at"], "started_at": record["started_at"],
            "finished_at": record["finished_at"], "status": record["status"],
            "error_type": record["error_type"],
            "result_summary": redact_explicit_secrets(
                str(record.get("result_summary") or "")[:500]
            ),
            "delivery_status": record["delivery_status"],
            "attempt_number": record.get("attempt_number", 1),
            "root_run_id": record.get("root_run_id"),
            "delivery_summary": {
                key: int(value)
                for key, value in dict(record.get("delivery_ref") or {}).items()
                if key in {"prepared_count", "skipped_count", "failed_count", "delivered_count", "delivery_failed_count"}
                and isinstance(value, int)
            },
        })
    return result


def _update_changes(args: dict, current: CronJob, kwargs: dict) -> tuple[dict, float | None]:
    """把公开管理参数映射为定义更新，并在改计划时计算新的未来窗口。"""
    supplied = set(args) - {"action", "job_id", "limit"}
    unknown = supplied - _UPDATE_FIELDS
    if unknown:
        raise ValueError(f"unsupported update fields: {sorted(unknown)}")
    changes: dict = {}
    next_run_at = None
    for name in ("name", "prompt", "skills", "workdir", "max_agent_iterations", "overlap_policy", "misfire_policy", "artifact_policy", "capability_spec"):
        if name in args:
            changes[name] = args[name]
    if "timezone" in args:
        changes["timezone"] = validate_timezone(str(args["timezone"]))
    if "toolsets" in args:
        changes["toolsets"] = _validated_cron_toolsets(args["toolsets"])
    if "retry_policy" in args:
        changes["retry_policy"] = _validated_retry_policy(args["retry_policy"])
    if "timeout" in args:
        changes["execution_timeout_seconds"] = args["timeout"]
    if "delivery_policy" in args:
        changes["delivery_config"] = _delivery_config(
            args, existing=current.delivery_config, origin=_trusted_origin(kwargs)
        )
    if "schedule" in args:
        schedule = args["schedule"]
        if not isinstance(schedule, str) or not schedule.strip():
            raise ValueError("schedule must be a non-empty string")
        timezone = str(changes.get("timezone", current.timezone))
        next_run_at, one_shot = parse_schedule(schedule, timezone_name=timezone)
        changes["schedule_expr"] = schedule.strip()
        changes["schedule_type"] = "one_shot" if one_shot else ("interval" if schedule.strip().startswith("every ") else "cron")
    elif "timezone" in changes and current.schedule_type == "cron":
        next_run_at, _ = parse_schedule(
            current.schedule,
            timezone_name=str(changes["timezone"]),
        )
    return changes, next_run_at


def handle_cron_tool(args, **kwargs):
    """处理普通 CLI/Gateway 管理会话中的 Cron 生命周期动作。"""
    if not isinstance(args, dict):
        return _json({"ok": False, "error_type": "invalid_args", "error": "arguments must be an object"})
    internal = _contains_internal_field(args)
    if internal is not None:
        return _json({"ok": False, "error_type": "invalid_args", "error": "unexpected internal-only argument"})
    action = str(args.get("action") or "list").lower()
    if action not in {"create", "list", "get", "update", "pause", "resume", "run", "delete", "history"}:
        return _json({"ok": False, "error_type": "invalid_args", "error": "unknown action"})
    db_path = kwargs.get("gateway_db_path") or DB_PATH
    store = JobStore(db_path=db_path)
    try:
        if action == "create":
            job = _new_job(args, **kwargs)
            canonical_scope, _ = _approval_scope_for_job(job)
            approval_fingerprint = _cron_approval_fingerprint(
                args, job.session_key, "create", canonical_scope
            )
            approved = _approved(args, kwargs, approval_fingerprint)
            if is_remote_approval(kwargs) and not approved:
                return _cron_approval_response(
                    args, job, action="create", canonical_scope=canonical_scope
                )
            if not is_remote_approval(kwargs) and _requires_gateway_authorization(job):
                return _json({"ok": False, "error_type": "approval_required", "error": "Cron unattended execution requires Gateway remote authorization."})
            stored = store.add(job)
            conn = init_db(str(db_path))
            try:
                stored_scope, _ = _approval_scope_for_job(stored)
                create_cron_capability_grant(
                    conn,
                    _grant_for_job(
                        stored,
                        getattr(kwargs.get("approval_grant"), "request_id", None)
                        if approved else None,
                        canonical_scope=stored_scope,
                    ),
                )
            finally:
                conn.close()
            return _json({"ok": True, "action": action, "job": _job_payload(store.get(stored.job_id) or stored)})

        job_id = args.get("job_id")
        if action not in {"list"} and (not isinstance(job_id, str) or not job_id):
            return _json({"ok": False, "error_type": "invalid_args", "error": "job_id is required"})
        if action == "list":
            return _json({"ok": True, "action": action, "jobs": [_job_payload(job) for job in store.list_all()]})
        current = store.get(job_id)
        if current is None:
            return _json({"ok": False, "error_type": "not_found", "error": "Cron job not found"})
        if action == "get":
            return _json({"ok": True, "action": action, "job": _job_payload(current)})
        if action == "update":
            if current.deleted_at is not None:
                return _json({"ok": False, "error_type": "deleted", "error": "Cron job is deleted"})
            changes, next_run_at = _update_changes(args, current, kwargs)
            if not changes:
                return _json({"ok": False, "error_type": "invalid_args", "error": "no update fields supplied"})
            candidate_record = current.to_record()
            candidate_record.update(changes)
            candidate_record["version"] = current.version + 1
            candidate = CronJob.from_record(candidate_record)
            if "capability_spec" in args:
                _validate_system_capability_spec(candidate.capability_spec)
            _validate_terminal_capability(candidate.toolsets, candidate.capability_spec)
            sensitive = capability_change_requires_reauthorization(current, candidate)
            canonical_scope, _ = _approval_scope_for_job(candidate)
            approval_fingerprint = _cron_approval_fingerprint(
                args, current.session_key, "update", canonical_scope
            )
            approved = _approved(args, kwargs, approval_fingerprint)
            if sensitive and is_remote_approval(kwargs) and not approved:
                return _cron_approval_response(
                    args, candidate, action="update", canonical_scope=canonical_scope
                )
            if sensitive and not is_remote_approval(kwargs):
                return _json({"ok": False, "error_type": "approval_required", "error": "Capability-expanding Cron updates require Gateway remote authorization."})
            updated = store.update(job_id, changes)
            if next_run_at is not None:
                conn = init_db(str(db_path))
                try:
                    record = update_cron_job_schedule_state(conn, job_id, next_run_at=next_run_at)
                    updated = CronJob.from_record(record)
                finally:
                    conn.close()
            if sensitive:
                conn = init_db(str(db_path))
                try:
                    updated_scope, _ = _approval_scope_for_job(updated)
                    create_cron_capability_grant(
                        conn,
                        _grant_for_job(
                            updated,
                            getattr(kwargs.get("approval_grant"), "request_id", None),
                            canonical_scope=updated_scope,
                        ),
                    )
                finally:
                    conn.close()
                updated = store.get(job_id) or updated
            return _json({"ok": True, "action": action, "grant_reauthorized": sensitive, "job": _job_payload(updated)})
        if action == "pause":
            return _json({"ok": True, "action": action, "job": _job_payload(store.set_paused(job_id, True))})
        if action == "resume":
            if current.deleted_at is not None:
                return _json({"ok": False, "error_type": "deleted", "error": "Cron job is deleted"})
            if (
                current.misfire_policy == "catch_up"
                and current.next_fire is not None
                and current.next_fire <= datetime.now().timestamp()
            ):
                next_run_at = current.next_fire
            else:
                next_run_at, _ = parse_schedule(
                    current.schedule,
                    timezone_name=current.timezone,
                )
            conn = init_db(str(db_path))
            try:
                resumed = CronJob.from_record(resume_cron_job(conn, job_id, next_run_at))
            finally:
                conn.close()
            return _json({"ok": True, "action": action, "job": _job_payload(resumed)})
        if action == "run":
            if current.deleted_at is not None:
                return _json({"ok": False, "error_type": "deleted", "error": "Cron job is deleted"})
            conn = init_db(str(db_path))
            try:
                run = create_manual_cron_run(conn, job_id, uuid.uuid4().hex)
            finally:
                conn.close()
            return _json({"ok": True, "action": action, "run_id": run["run_id"], "status": run["status"]})
        if action == "delete":
            conn = init_db(str(db_path))
            try:
                deleted = CronJob.from_record(soft_delete_cron_job(conn, job_id))
            finally:
                conn.close()
            return _json({"ok": True, "action": action, "job": _job_payload(deleted)})
        limit = args.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            return _json({"ok": False, "error_type": "invalid_args", "error": "limit must be an integer from 1 to 100"})
        conn = init_db(str(db_path))
        try:
            records = list_cron_runs(conn, job_id)
        finally:
            conn.close()
        return _json({"ok": True, "action": action, "runs": _history_payload(records, limit)})
    except ValueError as exc:
        return _json({"ok": False, "error_type": "invalid_args", "error": str(exc)[:240]})
    except DBError as exc:
        return _json({"ok": False, "error_type": "cron_management_error", "error": str(exc)[:240]})


def register(registry):
    registry.register(
        name="cron", toolset="cron",
        schema={
            "name": "cron",
            "description": (
                "Manage Cron task lifecycle: create, list, get, update, pause, "
                "resume, run, delete, and history. Cron management is unavailable "
                "inside Cron execution. A create request must explicitly provide the "
                "minimum toolsets needed by the task: read-only file work normally "
                "uses only file; do not request terminal unless command execution is "
                "needed, and do not request delegate unless a child agent is needed. "
                "Cron terminal also requires a narrow terminal_allowed_executables "
                "allowlist in capability_spec."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "list", "get", "update", "pause", "resume", "run", "delete", "history"]},
                    "job_id": {"type": "string"}, "name": {"type": "string"},
                    "schedule": {"type": "string"}, "timezone": {"type": "string"},
                    "prompt": {"type": "string"},
                    "toolsets": {
                        "type": "array",
                        "description": "Required for create. Request only the minimum Cron toolsets required; never include cron.",
                        "items": {"type": "string"},
                    },
                    "skills": {"type": "array", "items": {"type": "string"}}, "workdir": {"type": "string"},
                    "timeout": {"type": "number"}, "max_agent_iterations": {"type": "integer"},
                    "overlap_policy": {"type": "string", "enum": ["skip", "queue", "parallel"]},
                    "misfire_policy": {"type": "string", "enum": ["skip", "run_once", "catch_up"]},
                    "retry_policy": {"type": "object"}, "delivery_policy": {"oneOf": [{"type": "string"}, {"type": "object"}]},
                    "artifact_policy": {"type": "object"},
                    "capability_spec": {
                        "type": "object",
                        "description": (
                            "Capability constraints for unattended execution. "
                            "Defaults below are conservative reminders of the safe "
                            "baseline; they are not recommendations to copy. Judge "
                            "each field against what the task actually needs to do "
                            "and override only the ones the task requires. "
                            "allow_file_write (default false): set to true when the "
                            "task must create, modify, or delete files via the file "
                            "tool; a write action without this stays denied at run "
                            "time. terminal_allowed_executables is required when "
                            "toolsets includes terminal. terminal_allow_shell_operators, "
                            "terminal_allow_redirection, terminal_allow_background, and "
                            "terminal_allow_network default to false. "
                            "max_artifact_file_bytes and max_artifact_total_bytes "
                            "default to 20MB and 50MB. artifact_root is system-managed "
                            "and cannot be set here."
                        ),
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["action"],
            },
        },
        handler=handle_cron_tool, execution_environments=("cli", "gateway"),
        unattended_allowed=False, approval_mode="remote_once", risk_level="high",
        default_enabled_environments=("cli",),
    )
