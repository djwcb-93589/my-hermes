"""Cron 管理工具：在普通管理会话中维护任务完整生命周期。"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from hermes.tools import _metadata_registration_import_active


__hermes_metadata_only__ = _metadata_registration_import_active()


if not __hermes_metadata_only__:
    from hermes.approval import is_remote_approval
    from hermes.cron.approval import (
        approval_scope_for_job,
        approved_candidate_job_id,
        cron_approval_response,
        cron_grant_matches,
        register_cron_approval_handler,
    )
    from hermes.config import DB_PATH
    from hermes.cron.capability import (
        _normalise_path,
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
_PROMPT_QUOTED_PATH_PATTERNS = (
    re.compile(r'"((?:[A-Za-z]:[\\/]|/[A-Za-z](?:/|$)|\\\\)[^"\r\n]+)"'),
    re.compile(r"'((?:[A-Za-z]:[\\/]|/[A-Za-z](?:/|$)|\\\\)[^'\r\n]+)'"),
    re.compile(r"「((?:[A-Za-z]:[\\/]|/[A-Za-z](?:/|$)|\\\\)[^」\r\n]+)」"),
    re.compile(r"“((?:[A-Za-z]:[\\/]|/[A-Za-z](?:/|$)|\\\\)[^”\r\n]+)”"),
)
_PROMPT_BARE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"[A-Za-z]:[\\/][^\s\"'<>|]+"
    r"|\\\\[^\\/\s\"'<>|]+[\\/][^\s\"'<>|]+"
    r"|/[A-Za-z](?:/[^\s\"'<>|]+)+"
    r")"
)
_PROMPT_PATH_TRAILING_PUNCTUATION = ".,;:!?，。；：！？、)]}）】》」”"


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


def _prompt_absolute_paths(prompt: str) -> list[str]:
    """提取任务提示中的 Windows、UNC 和 Git Bash 绝对路径。"""
    candidates: list[tuple[int, str]] = []
    quoted_spans: list[tuple[int, int]] = []
    for pattern in _PROMPT_QUOTED_PATH_PATTERNS:
        for match in pattern.finditer(prompt):
            quoted_spans.append(match.span())
            candidates.append((match.start(), match.group(1).strip()))

    for match in _PROMPT_BARE_PATH_RE.finditer(prompt):
        if any(start <= match.start() < end for start, end in quoted_spans):
            continue
        value = match.group(0).rstrip(_PROMPT_PATH_TRAILING_PUNCTUATION)
        if value:
            candidates.append((match.start(), value))

    # 保留首次出现顺序，错误信息优先指向提示中最早的矛盾路径。
    return list(dict.fromkeys(value for _, value in sorted(candidates)))


def _validate_prompt_paths_within_workdir(
    prompt: str,
    workdir: object,
) -> None:
    """在任务入库前拒绝提示路径超出 workdir 的定义。"""
    root = _normalise_path(str(workdir or os.getcwd()))
    for raw_path in _prompt_absolute_paths(prompt):
        candidate = _normalise_path(raw_path)
        try:
            Path(candidate).relative_to(Path(root))
        except ValueError as exc:
            raise ValueError(
                f"prompt absolute path is outside workdir: {raw_path!r}; "
                f"set workdir to contain every absolute path in prompt"
            ) from exc


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


def _validate_schedule_recurrence(
    schedule: str,
    *,
    one_shot: bool,
    recurring: object,
) -> None:
    """防止把一次性相对延迟误写成会每年重复的日历 Cron。"""
    normalized = schedule.strip()
    if normalized.startswith("every "):
        if recurring not in (None, True):
            raise ValueError("recurring must be a boolean when supplied")
        return
    if one_shot:
        if recurring is not None:
            raise ValueError("one-time duration schedules must not set recurring")
        return
    if recurring is not True:
        raise ValueError(
            "five-field calendar schedules are recurring; pass recurring=true "
            "or use a one-time duration such as '5m'"
        )


def _new_job(args: dict, **kwargs) -> CronJob:
    """从公开参数和可信来源构造新的任务定义。"""
    schedule = args.get("schedule")
    prompt = args.get("prompt")
    if not isinstance(schedule, str) or not schedule.strip() or not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("'schedule' and 'prompt' are required")
    timezone = validate_timezone(str(args.get("timezone") or "UTC"))
    next_fire, one_shot = parse_schedule(schedule, timezone_name=timezone)
    _validate_schedule_recurrence(
        schedule,
        one_shot=one_shot,
        recurring=args.get("recurring"),
    )
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
    job = CronJob(
        job_id=approved_candidate_job_id(
            kwargs.get("approval_grant"),
            args,
            session_key=session_key,
        ) or uuid.uuid4().hex[:12],
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
    _validate_prompt_paths_within_workdir(job.prompt, job.workdir)
    return job


def _grant_for_job(
    job: CronJob,
    approval_id: str | None,
    *,
    canonical_scope: dict | None = None,
) -> dict:
    """冻结当前注册表中同时满足 Cron 策略和本任务授权的工具名。"""
    canonical_scope, scope = (
        (canonical_scope, build_capability_scope(job))
        if canonical_scope is not None
        else approval_scope_for_job(job)
    )
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
    return bool(
        not job.one_shot
        or "terminal" in scope["toolsets"]
        or scope["allow_external_communication"]
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
    supplied = set(args) - {"action", "job_id", "limit", "recurring"}
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
        _validate_schedule_recurrence(
            schedule,
            one_shot=one_shot,
            recurring=args.get("recurring"),
        )
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
            canonical_scope, _ = approval_scope_for_job(job)
            approved = cron_grant_matches(
                kwargs.get("approval_grant"),
                args,
                session_key=job.session_key,
                action="create",
                canonical_scope=canonical_scope,
                candidate_job_id=job.job_id,
            )
            if kwargs.get("approval_grant") is not None and not approved:
                return _json({
                    "ok": False,
                    "error_type": "approval_stale",
                    "error": "approved Cron capability scope changed; request approval again",
                })
            if (
                is_remote_approval(kwargs)
                and _requires_gateway_authorization(job)
                and not approved
            ):
                return cron_approval_response(
                    args, job, action="create", canonical_scope=canonical_scope
                )
            if not is_remote_approval(kwargs) and _requires_gateway_authorization(job):
                return _json({"ok": False, "error_type": "approval_required", "error": "Cron unattended execution requires Gateway remote authorization."})
            stored = store.add(job)
            conn = init_db(str(db_path))
            try:
                stored_scope, _ = approval_scope_for_job(stored)
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
            _validate_prompt_paths_within_workdir(
                candidate.prompt,
                candidate.workdir,
            )
            if "capability_spec" in args:
                _validate_system_capability_spec(candidate.capability_spec)
            _validate_terminal_capability(candidate.toolsets, candidate.capability_spec)
            sensitive = capability_change_requires_reauthorization(current, candidate)
            canonical_scope, _ = approval_scope_for_job(candidate)
            approved = cron_grant_matches(
                kwargs.get("approval_grant"),
                args,
                session_key=current.session_key,
                action="update",
                canonical_scope=canonical_scope,
                candidate_job_id=candidate.job_id,
            )
            if kwargs.get("approval_grant") is not None and not approved:
                return _json({
                    "ok": False,
                    "error_type": "approval_stale",
                    "error": "approved Cron capability scope changed; request approval again",
                })
            if (
                sensitive
                and _requires_gateway_authorization(candidate)
                and is_remote_approval(kwargs)
                and not approved
            ):
                return cron_approval_response(
                    args, candidate, action="update", canonical_scope=canonical_scope
                )
            if (
                sensitive
                and _requires_gateway_authorization(candidate)
                and not is_remote_approval(kwargs)
            ):
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
                    updated_scope, _ = approval_scope_for_job(updated)
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
    if not getattr(registry, "metadata_only", False):
        register_cron_approval_handler()
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
                "allowlist in capability_spec. To deliver files to the conversation, "
                "set delivery_policy to \"text_and_files\" and "
                "capability_spec.allow_file_write to true; the sub-agent writes "
                "files to the artifact directory (shown in its system prompt) and "
                "Gateway delivers them as attachments when the run finishes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "list", "get", "update", "pause", "resume", "run", "delete", "history"]},
                    "job_id": {"type": "string"}, "name": {"type": "string"},
                    "schedule": {
                        "type": "string",
                        "description": (
                            "One-time delays use a duration such as '5m' or '2h'. "
                            "Recurring durations use 'every 5m'. Five-field calendar "
                            "expressions are recurring and require recurring=true."
                        ),
                    },
                    "recurring": {
                        "type": "boolean",
                        "description": (
                            "Set true only for an intentional five-field recurring "
                            "calendar schedule; omit for one-time durations."
                        ),
                    },
                    "timezone": {"type": "string"},
                    "prompt": {"type": "string"},
                    "toolsets": {
                        "type": "array",
                        "description": "Required for create. Request only the minimum Cron toolsets required; never include cron.",
                        "items": {"type": "string"},
                    },
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "workdir": {
                        "type": "string",
                        "description": (
                            "Working directory for the Cron run. It also defines "
                            "the file access boundary: at run time the sub-agent "
                            "may only read or write paths inside this directory "
                            "(plus the system-managed artifact root). Any path "
                            "mentioned in the prompt must fall under this "
                            "directory; create and update reject a prompt that "
                            "references an absolute path outside workdir, even if "
                            "allow_file_write is true. On Windows, Git Bash "
                            "forms like /e/path and Windows forms like E:\\path "
                            "are both accepted and normalized to the same "
                            "absolute directory."
                        ),
                    },
                    "timeout": {"type": "number"}, "max_agent_iterations": {"type": "integer"},
                    "overlap_policy": {"type": "string", "enum": ["skip", "queue", "parallel"]},
                    "misfire_policy": {"type": "string", "enum": ["skip", "run_once", "catch_up"]},
                    "retry_policy": {"type": "object"},
                    "delivery_policy": {
                        "oneOf": [{"type": "string"}, {"type": "object"}],
                        "description": (
                            "Controls what Gateway sends to the conversation after "
                            "the run. String values: \"text\" (default, send only "
                            "the final text summary), \"text_and_files\" (send the "
                            "summary plus any files the sub-agent wrote to the "
                            "artifact directory as attachments), \"failure_only\" "
                            "(send only when the run fails), \"silent\" (send "
                            "nothing). Use \"text_and_files\" when the task must "
                            "deliver files."
                        ),
                    },
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
                            "time. Set this true together with "
                            "delivery_policy=\"text_and_files\" when the task must "
                            "deliver files to the conversation. "
                            "terminal_allowed_executables is required when "
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
