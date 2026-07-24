"""Cron 能力审批的 Binding、决策展示与 Grant 复检。"""

from __future__ import annotations

import hashlib
import json
import re

from hermes.approval import build_approval_required
from hermes.approval_policy import (
    approval_binding_fingerprint,
    approval_grant_identity_matches,
    normalize_approval_session_key,
)
from hermes.cron.capability import build_capability_scope
from hermes.cron.job import CronJob
from hermes.redaction import redact_explicit_secrets
from hermes.tools import (
    ExecutionEnvironment,
    ToolPolicy,
    ToolRiskLevel,
    register_all,
    registry,
)


_TOOL_NAME = "cron"
_APPROVAL_ACTIONS = frozenset({"create", "update"})
_CANDIDATE_ID_RE = re.compile(r"[0-9a-f]{12}")


def _fingerprint(payload: dict) -> str:
    """生成 Cron 能力身份的稳定摘要。"""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def approval_scope_for_job(job: CronJob) -> tuple[dict, dict]:
    """从当前 ToolRegistry 解析任务可持有的完整能力范围。"""
    register_all()
    scope = build_capability_scope(job)
    resolution = registry.resolve(ToolPolicy(
        ExecutionEnvironment.CRON,
        enabled_toolsets=frozenset(scope["toolsets"]),
        unattended=True,
        trusted_context=frozenset({"cron_execution"}),
        max_risk_level=ToolRiskLevel.HIGH,
    ))
    if not resolution.allowed_tool_names:
        raise ValueError("Cron capability grant has no eligible tools")
    canonical_scope = {
        "capability_scope": scope,
        "allowed_tool_names": sorted(resolution.allowed_tool_names),
    }
    return canonical_scope, scope


def _approval_scope_identity(canonical_scope: dict) -> dict:
    """冻结任务版本、prompt 摘要和所有可执行工具能力。"""
    if not isinstance(canonical_scope, dict):
        raise ValueError("Cron canonical scope is invalid")
    scope = canonical_scope.get("capability_scope")
    allowed_tool_names = canonical_scope.get("allowed_tool_names")
    if (
        not isinstance(scope, dict)
        or not isinstance(allowed_tool_names, list)
        or not allowed_tool_names
        or not all(
            isinstance(name, str) and name for name in allowed_tool_names
        )
    ):
        raise ValueError("Cron canonical scope is invalid")
    if allowed_tool_names != sorted(set(allowed_tool_names)):
        raise ValueError("Cron canonical scope tools are not normalized")
    # JSON 往返用于深拷贝并拒绝不可持久化的能力状态。
    try:
        identity = json.loads(json.dumps(
            {
                "capability_scope": scope,
                "allowed_tool_names": allowed_tool_names,
            },
            ensure_ascii=False,
            sort_keys=True,
        ))
    except (TypeError, ValueError) as exc:
        raise ValueError("Cron canonical scope is invalid") from exc
    return identity


def cron_approval_binding(
    action: str,
    canonical_scope: dict,
    *,
    candidate_job_id: str,
) -> dict:
    """构造可由 Handler 完整重验的一次性 Cron 能力身份。"""
    normalized_action = str(action or "").lower()
    if normalized_action not in _APPROVAL_ACTIONS:
        raise ValueError("Cron approval action is invalid")
    if (
        not isinstance(candidate_job_id, str)
        or _CANDIDATE_ID_RE.fullmatch(candidate_job_id) is None
    ):
        raise ValueError("Cron candidate job id is invalid")
    scope_identity = _approval_scope_identity(canonical_scope)
    scope = scope_identity["capability_scope"]
    if (
        scope.get("job_id") != candidate_job_id
        or not isinstance(scope.get("job_version"), int)
        or isinstance(scope.get("job_version"), bool)
        or not isinstance(scope.get("prompt_digest"), str)
        or re.fullmatch(r"[0-9a-f]{64}", scope["prompt_digest"]) is None
    ):
        raise ValueError("Cron scope identity does not match candidate job")
    return {
        "backend_risk": {
            "backend_type": "gateway",
            "host_mounts": False,
            "docker_socket": False,
            "remote_host": False,
        },
        "cron_action": normalized_action,
        "scope_identity": scope_identity,
        "scope_digest": _fingerprint(scope_identity),
        "prompt_digest": scope["prompt_digest"],
        "cron_candidate_job_id": candidate_job_id,
        "job_version": scope["job_version"],
        "risk_level": "high",
    }


def _valid_cron_binding(
    arguments: dict,
    binding: dict,
    session_key: str,
) -> bool:
    """从公开参数和完整 scope identity 重验 Cron Binding。"""
    try:
        normalize_approval_session_key(session_key)
        if (
            not isinstance(arguments, dict)
            or set(binding) != {
                "backend_risk",
                "cron_action",
                "scope_identity",
                "scope_digest",
                "prompt_digest",
                "cron_candidate_job_id",
                "job_version",
                "risk_level",
            }
            or binding["cron_action"] not in _APPROVAL_ACTIONS
            or str(arguments.get("action") or "list").lower()
            != binding["cron_action"]
            or binding["risk_level"] != "high"
            or binding["backend_risk"] != {
                "backend_type": "gateway",
                "host_mounts": False,
                "docker_socket": False,
                "remote_host": False,
            }
        ):
            return False
        candidate_job_id = binding["cron_candidate_job_id"]
        if (
            not isinstance(candidate_job_id, str)
            or _CANDIDATE_ID_RE.fullmatch(candidate_job_id) is None
        ):
            return False
        identity = _approval_scope_identity(binding["scope_identity"])
        scope = identity["capability_scope"]
        if (
            binding["scope_digest"] != _fingerprint(identity)
            or scope.get("job_id") != candidate_job_id
            or binding["job_version"] != scope.get("job_version")
            or binding["prompt_digest"] != scope.get("prompt_digest")
            or not isinstance(binding["job_version"], int)
            or isinstance(binding["job_version"], bool)
            or not isinstance(binding["prompt_digest"], str)
            or re.fullmatch(r"[0-9a-f]{64}", binding["prompt_digest"]) is None
        ):
            return False
        if (
            binding["cron_action"] == "update"
            and arguments.get("job_id") != candidate_job_id
        ):
            return False
        prompt = arguments.get("prompt")
        if prompt is not None and (
            not isinstance(prompt, str)
            or hashlib.sha256(
                (
                    prompt.strip()
                    if binding["cron_action"] == "create"
                    else prompt
                ).encode("utf-8")
            ).hexdigest()
            != binding["prompt_digest"]
        ):
            return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


class CronApprovalHandler:
    """解释 Cron 一次性能力授权 Binding。"""

    def validate_request_binding(
        self, *, arguments: dict, binding: dict, session_key: str
    ) -> bool:
        return _valid_cron_binding(arguments, binding, session_key)

    def validate_grant_binding(
        self, *, arguments: dict, binding: dict, session_key: str
    ) -> bool:
        return _valid_cron_binding(arguments, binding, session_key)

    def build_session_rule(self, grant: object) -> None:
        return None

    def session_rule_matches(self, rule: object, runtime_context: object) -> bool:
        return False


_CRON_APPROVAL_HANDLER = CronApprovalHandler()


def register_cron_approval_handler() -> None:
    """随 Cron 工具注册唯一的审批 Handler。"""
    from hermes.approval_handlers import (
        get_approval_handler,
        register_approval_handler,
    )

    registered = get_approval_handler(_TOOL_NAME)
    if registered is None:
        register_approval_handler(_TOOL_NAME, _CRON_APPROVAL_HANDLER)
    elif registered is not _CRON_APPROVAL_HANDLER:
        raise ValueError(
            f"approval handler already registered: {_TOOL_NAME}"
        )


def cron_approval_fingerprint(
    arguments: dict,
    session_key: str,
    action: str,
    canonical_scope: dict,
    candidate_job_id: str,
) -> str:
    """将参数、会话、动作、任务版本和完整能力范围绑定。"""
    normalized_session = normalize_approval_session_key(session_key)
    binding = cron_approval_binding(
        action,
        canonical_scope,
        candidate_job_id=candidate_job_id,
    )
    return approval_binding_fingerprint(
        _TOOL_NAME,
        arguments,
        session_key=normalized_session,
        binding=binding,
    )


def cron_grant_matches(
    approval_grant: object,
    arguments: dict,
    *,
    session_key: str,
    action: str,
    canonical_scope: dict,
    candidate_job_id: str,
) -> bool:
    """执行前从候选 Job 与当前 registry 重建完整审批身份。"""
    try:
        normalized_session = normalize_approval_session_key(session_key)
        binding = cron_approval_binding(
            action,
            canonical_scope,
            candidate_job_id=candidate_job_id,
        )
        fingerprint = approval_binding_fingerprint(
            _TOOL_NAME,
            arguments,
            session_key=normalized_session,
            binding=binding,
        )
        return bool(
            approval_grant_identity_matches(
                approval_grant, _TOOL_NAME, arguments
            )
            and getattr(approval_grant, "scope", None) == "once"
            and getattr(approval_grant, "session_key", None)
            == normalized_session
            and getattr(approval_grant, "fingerprint", None) == fingerprint
            and getattr(approval_grant, "binding", None) == binding
            and _valid_cron_binding(
                arguments, binding, normalized_session
            )
        )
    except (TypeError, ValueError):
        return False


def approved_candidate_job_id(
    approval_grant: object,
    arguments: dict,
    *,
    session_key: str,
) -> str | None:
    """在构造候选 Job 前读取可信 Grant 中的一次性任务身份。"""
    try:
        normalized_session = normalize_approval_session_key(session_key)
        binding = approval_grant.binding
        if (
            not approval_grant_identity_matches(
                approval_grant, _TOOL_NAME, arguments
            )
            or approval_grant.scope != "once"
            or approval_grant.session_key != normalized_session
            or not _valid_cron_binding(
                arguments, binding, normalized_session
            )
        ):
            return None
        return binding["cron_candidate_job_id"]
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _scope_display_path(value: object, limit: int = 180) -> str:
    """审批界面只显示限长、脱敏的授权路径。"""
    text = redact_explicit_secrets(str(value or ""))
    return text if len(text) <= limit else f"{text[:limit]}…"


def _cron_scope_display(job: CronJob, canonical_scope: dict) -> dict:
    """构造不含路由凭据和原始平台身份的 Cron 审批摘要。"""
    scope = canonical_scope["capability_scope"]
    tools = list(canonical_scope["allowed_tool_names"])
    display_tools = tools[:12]
    if len(tools) > len(display_tools):
        display_tools.append(f"… and {len(tools) - len(display_tools)} more")
    roots = [_scope_display_path(root) for root in scope["allowed_roots"][:8]]
    if len(scope["allowed_roots"]) > len(roots):
        roots.append(f"… and {len(scope['allowed_roots']) - len(roots)} more")
    target = dict(scope.get("delivery_target") or {})
    target_kind = (
        "thread"
        if target.get("thread_id")
        else ("chat" if target.get("chat_id") else "none")
    )
    prompt_summary = redact_explicit_secrets(
        str(job.prompt).strip().replace("\n", " ")
    )[:240]
    return {
        "name": redact_explicit_secrets(
            str(job.name or "(unnamed Cron task)")
        )[:160],
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
        "terminal_allowed_executables": list(
            scope["terminal_allowed_executables"][:12]
        ),
        "terminal_allow_shell_operators": bool(
            scope["terminal_allow_shell_operators"]
        ),
        "terminal_allow_redirection": bool(
            scope["terminal_allow_redirection"]
        ),
        "terminal_allow_background": bool(
            scope["terminal_allow_background"]
        ),
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


def cron_approval_response(
    arguments: dict,
    job: CronJob,
    *,
    action: str,
    canonical_scope: dict,
) -> str:
    """请求仅用于本次候选任务能力授权的一次性审批。"""
    binding = cron_approval_binding(
        action,
        canonical_scope,
        candidate_job_id=job.job_id,
    )
    fingerprint = approval_binding_fingerprint(
        _TOOL_NAME,
        arguments,
        session_key=job.session_key,
        binding=binding,
    )
    return build_approval_required(
        _TOOL_NAME,
        "Authorize this Cron task's bounded unattended capabilities",
        details={
            "binding_version": 1,
            "binding": binding,
            "operation_type": "cron.capability_grant",
            "risk_level": "high",
            "allowed_grant_scopes": ["once"],
            "decision_source": "cron_capability_policy",
            "cron_scope_display": _cron_scope_display(job, canonical_scope),
            "fingerprint": fingerprint,
        },
    )
