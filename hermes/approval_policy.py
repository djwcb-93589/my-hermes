"""通用审批信封、指纹、签发和会话授权基础。"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Protocol

from hermes.approval_handlers import get_approval_handler


class ApprovalDecision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ApprovalRiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


ALLOW = ApprovalDecision.ALLOW
ASK = ApprovalDecision.ASK
DENY = ApprovalDecision.DENY
LOW = ApprovalRiskLevel.LOW
MEDIUM = ApprovalRiskLevel.MEDIUM
HIGH = ApprovalRiskLevel.HIGH
CRITICAL = ApprovalRiskLevel.CRITICAL


@dataclass(frozen=True, slots=True)
class ApprovalAssessment:
    tool_name: str
    decision: ApprovalDecision
    risk_level: ApprovalRiskLevel
    fingerprint: str
    reason: str
    normalized_arguments: dict
    details: dict
    normalized_command: str | None = None
    normalized_cwd: str | None = None
    normalized_path: str | None = None
    session_key: str | None = None
    error_type: str | None = None
    error: str | None = None
    fatal: bool = False


class IntelligentApprovalAdvisor(Protocol):
    def assess(self, assessment: ApprovalAssessment) -> ApprovalDecision | None:
        """返回通用审批建议。"""


class IntelligentApprovalSettings(Protocol):
    intelligent_approval_enabled: bool


@dataclass(frozen=True, slots=True)
class TrustedApprovalGrant:
    """只能由审批处理链创建的内部授权对象。"""

    scope: str
    request_id: str
    tool_name: str
    arguments: dict
    fingerprint: str
    session_key: str
    binding: dict
    _issuer: object = field(repr=False, compare=False, default=None)


_TRUSTED_GRANT_ISSUER = object()
_APPROVAL_BINDING_VERSION = 1
_RISK_ORDER = {LOW: 0, MEDIUM: 1, HIGH: 2, CRITICAL: 3}
_SESSION_GRANTS: dict[str, list[tuple[str, str, object]]] = {}
_SESSION_GRANTS_LOCK = threading.Lock()


def _canonical_fingerprint(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _normalize_approval_binding(value: object) -> dict:
    """复制并校验可持久化的工具 Binding。"""
    if not isinstance(value, Mapping):
        raise ValueError("approval binding must be an object")
    try:
        normalized = json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("approval binding is not JSON serializable") from exc
    if not isinstance(normalized, dict):
        raise ValueError("approval binding must be an object")
    return normalized


def normalize_approval_session_key(session_key: object) -> str:
    normalized = str(session_key or "").strip()
    if not normalized:
        raise ValueError("session_key must be a non-empty string")
    return normalized


def normalize_risk_level(value: object) -> ApprovalRiskLevel:
    if isinstance(value, ApprovalRiskLevel):
        return value
    try:
        return ApprovalRiskLevel(str(value))
    except ValueError as exc:
        raise ValueError("invalid approval risk level") from exc


def allowed_grant_scopes(
    risk_level: ApprovalRiskLevel | str,
) -> tuple[str, ...]:
    risk = normalize_risk_level(risk_level)
    if risk in {LOW, MEDIUM}:
        return ("once", "session")
    if risk == HIGH:
        return ("once",)
    return ()


def is_grant_scope_allowed(
    risk_level: ApprovalRiskLevel | str, scope: object
) -> bool:
    try:
        return (
            str(scope or "").strip().lower()
            in allowed_grant_scopes(risk_level)
        )
    except ValueError:
        return False


def _risk_at_most(
    current: ApprovalRiskLevel, maximum: ApprovalRiskLevel
) -> bool:
    return _RISK_ORDER[current] <= _RISK_ORDER[maximum]


def _max_risk(
    first: ApprovalRiskLevel, second: ApprovalRiskLevel
) -> ApprovalRiskLevel:
    return first if _RISK_ORDER[first] >= _RISK_ORDER[second] else second


def approval_binding_fingerprint(
    tool_name: str,
    arguments: dict,
    *,
    session_key: str,
    binding: object,
) -> str:
    return _canonical_fingerprint(
        {
            "binding_version": _APPROVAL_BINDING_VERSION,
            "tool_name": str(tool_name),
            "arguments": dict(arguments),
            "session_key": normalize_approval_session_key(session_key),
            "binding": _normalize_approval_binding(binding),
        }
    )


def _approval_details(
    tool_name: str,
    arguments: dict,
    *,
    session_key: str,
    binding: object,
    operation_type: str,
    risk_level: ApprovalRiskLevel,
    reason: str,
    decision_source: str,
    allowed_scopes: Sequence[str] | None = None,
) -> tuple[dict, str]:
    normalized_binding = _normalize_approval_binding(binding)
    fingerprint = approval_binding_fingerprint(
        tool_name,
        arguments,
        session_key=session_key,
        binding=normalized_binding,
    )
    return {
        "binding_version": _APPROVAL_BINDING_VERSION,
        "binding": normalized_binding,
        "fingerprint": fingerprint,
        "operation_type": str(operation_type),
        "risk_level": risk_level.value,
        "allowed_grant_scopes": list(
            allowed_scopes
            if allowed_scopes is not None
            else allowed_grant_scopes(risk_level)
        ),
        "reason": reason,
        "decision_source": decision_source,
    }, fingerprint


def _backend_fingerprint_payload(
    backend_context: Mapping | None,
) -> dict:
    """只保存所有执行环境共有的非敏感风险标志。"""
    context = backend_context if isinstance(backend_context, Mapping) else {}
    return {
        "backend_type": str(context.get("backend_type", "unknown")),
        "host_mounts": bool(context.get("host_mounts", False)),
        "docker_socket": bool(context.get("docker_socket", False)),
        "remote_host": bool(context.get("remote_host", False)),
    }


def _identifier_fingerprint(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"sha256:{digest}"


def _safe_audit_text(value: object, *, limit: int = 240) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    return text[:limit]


def emit_approval_audit(
    *,
    request_id: object,
    session_key: object,
    tool_name: object,
    risk_level: object,
    reason: object,
    decision: object,
    grant_scope: object = None,
    decision_source: object,
    timestamp: float | None = None,
) -> None:
    try:
        normalized_session = str(session_key or "").strip()
        record = {
            "event": "approval_decision",
            "request_id": _safe_audit_text(request_id, limit=96) or None,
            "session_security_id": (
                _identifier_fingerprint(normalized_session)
                if normalized_session
                else None
            ),
            "tool": _safe_audit_text(tool_name, limit=32),
            "risk": _safe_audit_text(risk_level, limit=16),
            "reason": _safe_audit_text(reason),
            "decision": _safe_audit_text(decision, limit=32),
            "grant_scope": (
                _safe_audit_text(grant_scope, limit=16)
                if grant_scope
                else None
            ),
            "decision_source": _safe_audit_text(
                decision_source, limit=32
            ),
            "timestamp": float(
                time.time() if timestamp is None else timestamp
            ),
        }
        print(
            "  [approval:audit] "
            + json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except Exception:
        # 审计失败不能改变审批结果。
        return


def apply_intelligent_approval(
    assessment: ApprovalAssessment,
    *,
    security_policy: IntelligentApprovalSettings,
    advisor: IntelligentApprovalAdvisor | None,
) -> ApprovalAssessment:
    if (
        not security_policy.intelligent_approval_enabled
        or advisor is None
        or assessment.decision != ASK
        or assessment.risk_level in {HIGH, CRITICAL}
    ):
        return assessment
    try:
        advised = advisor.assess(assessment)
    except Exception:
        return assessment
    if advised == ALLOW:
        details = dict(assessment.details)
        details["decision_source"] = "intelligent_approval"
        return replace(
            assessment,
            decision=ALLOW,
            reason="智能审批接口批准 low/medium 操作",
            details=details,
        )
    if advised == DENY:
        details = dict(assessment.details)
        details["decision_source"] = "intelligent_approval"
        return replace(
            assessment,
            decision=DENY,
            reason="智能审批接口拒绝操作",
            error_type="intelligent_approval_denied",
            error="operation was denied by the intelligent approval advisor",
            fatal=True,
            details=details,
        )
    return assessment


def _is_trusted_approval_grant(value: object) -> bool:
    return (
        isinstance(value, TrustedApprovalGrant)
        and value._issuer is _TRUSTED_GRANT_ISSUER
    )


def approval_grant_identity_matches(
    approval_grant: object, tool_name: str, arguments: dict
) -> bool:
    return (
        _is_trusted_approval_grant(approval_grant)
        and approval_grant.request_id.startswith("approval_")
        and approval_grant.tool_name == tool_name
        and approval_grant.arguments == arguments
    )


def approval_request_binding_matches(
    tool_name: str,
    arguments: dict,
    details: object,
    *,
    session_key: object,
) -> bool:
    if not isinstance(details, dict):
        return False
    if details.get("binding_version") != _APPROVAL_BINDING_VERSION:
        return False
    try:
        normalized_session = normalize_approval_session_key(session_key)
        binding = _normalize_approval_binding(details.get("binding"))
    except ValueError:
        return False

    from hermes.tools import ApprovalMode, registry

    entry = registry.get_entry(tool_name)
    handler = get_approval_handler(tool_name)
    if (
        entry is None
        or entry.approval_mode == ApprovalMode.NONE
        or handler is None
        or not handler.validate_request_binding(
            arguments=arguments,
            binding=binding,
            session_key=normalized_session,
        )
    ):
        return False
    risk_level = details.get("risk_level")
    if risk_level not in {level.value for level in ApprovalRiskLevel}:
        return False
    operation_type = details.get("operation_type")
    if not isinstance(operation_type, str) or not operation_type:
        return False
    detail_scopes = details.get("allowed_grant_scopes")
    permitted_scopes = allowed_grant_scopes(risk_level)
    if (
        not isinstance(detail_scopes, list)
        or not detail_scopes
        or len(detail_scopes) != len(set(detail_scopes))
        or any(scope not in permitted_scopes for scope in detail_scopes)
    ):
        return False
    if not isinstance(details.get("decision_source"), str):
        return False
    fingerprint = details.get("fingerprint")
    return (
        isinstance(fingerprint, str)
        and fingerprint.startswith("sha256:")
        and fingerprint
        == approval_binding_fingerprint(
            tool_name,
            arguments,
            session_key=normalized_session,
            binding=binding,
        )
    )


def issue_trusted_approval_grant(
    request: dict, *, scope: str
) -> TrustedApprovalGrant:
    normalized_scope = str(scope or "").strip().lower()
    tool_name = str(request.get("tool_name", ""))
    arguments = request.get("tool_args")
    details = request.get("details")
    session_key = normalize_approval_session_key(
        request.get("conversation_id")
    )
    if not isinstance(arguments, dict) or not isinstance(details, dict):
        raise ValueError("approval grant request is invalid")
    risk_level = normalize_risk_level(details.get("risk_level"))
    if (
        normalize_approval_session_key(request.get("session_key"))
        != session_key
    ):
        raise ValueError("approval grant session binding is invalid")
    if not str(request.get("tool_call_id", "")).strip():
        raise ValueError("approval grant tool call binding is invalid")
    if request.get("status") != "executing":
        raise ValueError("approval grant request has not been claimed")
    if str(request.get("grant_scope", "")).strip().lower() != normalized_scope:
        raise ValueError("approval grant scope binding is invalid")
    try:
        created_at = float(request.get("created_at"))
        expires_at = float(request.get("expires_at"))
        claimed_at = float(request.get("updated_at"))
    except (TypeError, ValueError) as exc:
        raise ValueError("approval grant lifetime binding is invalid") from exc
    if not created_at <= claimed_at < expires_at:
        raise ValueError("approval grant request has expired")
    if not is_grant_scope_allowed(risk_level, normalized_scope):
        raise ValueError("approval grant scope is not allowed for this risk")
    fingerprint = details.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.startswith(
        "sha256:"
    ):
        raise ValueError("approval grant fingerprint is invalid")
    if request.get("fingerprint") != fingerprint:
        raise ValueError("approval grant fingerprint binding is invalid")
    request_id = str(request.get("id", ""))
    if not request_id.startswith("approval_"):
        raise ValueError("approval grant request id is invalid")
    if details.get("binding_version") != _APPROVAL_BINDING_VERSION:
        raise ValueError("approval grant binding version is invalid")
    binding = _normalize_approval_binding(details.get("binding"))
    handler = get_approval_handler(tool_name)
    if handler is None or not handler.validate_grant_binding(
        arguments=arguments,
        binding=binding,
        session_key=session_key,
    ):
        raise ValueError("approval grant binding is invalid")
    if fingerprint != approval_binding_fingerprint(
        tool_name,
        arguments,
        session_key=session_key,
        binding=binding,
    ):
        raise ValueError("approval grant fingerprint is invalid")
    if not approval_request_binding_matches(
        tool_name,
        arguments,
        details,
        session_key=session_key,
    ):
        raise ValueError("approval grant operation binding is invalid")
    return TrustedApprovalGrant(
        scope=normalized_scope,
        request_id=request_id,
        tool_name=tool_name,
        arguments=json.loads(json.dumps(arguments, ensure_ascii=False)),
        fingerprint=fingerprint,
        session_key=session_key,
        binding=binding,
        _issuer=_TRUSTED_GRANT_ISSUER,
    )


def issue_interactive_approval_grant(
    request: dict,
    *,
    session_key: str,
    scope: str,
) -> TrustedApprovalGrant:
    """从当前进程已生成的交互式待审批结果签发一次内部 Grant。"""
    normalized_session = normalize_approval_session_key(session_key)
    normalized_scope = str(scope or "").strip().lower()
    tool_name = str(request.get("tool_name", ""))
    arguments = request.get("arguments")
    details = request.get("details")
    request_id = str(request.get("id", ""))
    if (
        not request_id.startswith("approval_")
        or not isinstance(arguments, dict)
        or not isinstance(details, dict)
    ):
        raise ValueError("interactive approval request is invalid")
    risk_level = normalize_risk_level(details.get("risk_level"))
    configured_scopes = details.get("allowed_grant_scopes")
    if (
        not isinstance(configured_scopes, list)
        or normalized_scope not in configured_scopes
        or not is_grant_scope_allowed(risk_level, normalized_scope)
    ):
        raise ValueError("interactive approval scope is invalid")
    if not approval_request_binding_matches(
        tool_name,
        arguments,
        details,
        session_key=normalized_session,
    ):
        raise ValueError("interactive approval operation binding is invalid")
    binding = _normalize_approval_binding(details.get("binding"))
    handler = get_approval_handler(tool_name)
    if handler is None or not handler.validate_grant_binding(
        arguments=arguments,
        binding=binding,
        session_key=normalized_session,
    ):
        raise ValueError("interactive approval binding is invalid")
    return TrustedApprovalGrant(
        scope=normalized_scope,
        request_id=request_id,
        tool_name=tool_name,
        arguments=json.loads(json.dumps(arguments, ensure_ascii=False)),
        fingerprint=str(details["fingerprint"]),
        session_key=normalized_session,
        binding=binding,
        _issuer=_TRUSTED_GRANT_ISSUER,
    )


def activate_session_grant(grant: TrustedApprovalGrant) -> bool:
    """让工具 Handler 构造不透明的会话授权规则。"""
    if not _is_trusted_approval_grant(grant) or grant.scope != "session":
        return False
    handler = get_approval_handler(grant.tool_name)
    if handler is None:
        return False
    try:
        rule = handler.build_session_rule(grant)
    except (TypeError, ValueError):
        return False
    if rule is None:
        return False
    with _SESSION_GRANTS_LOCK:
        entries = _SESSION_GRANTS.setdefault(grant.session_key, [])
        item = (grant.request_id, grant.tool_name, rule)
        if item not in entries:
            entries.append(item)
    return True


def clear_session_grants(session_key: str) -> None:
    normalized = str(session_key or "").strip()
    if not normalized:
        return
    with _SESSION_GRANTS_LOCK:
        _SESSION_GRANTS.pop(normalized, None)


def session_grant_matches(
    tool_name: str, runtime_context: dict
) -> bool:
    """把不透明规则交回对应工具 Handler 解释。"""
    if not isinstance(runtime_context, dict):
        return False
    try:
        session_key = normalize_approval_session_key(
            runtime_context.get("session_key")
        )
    except ValueError:
        return False
    handler = get_approval_handler(tool_name)
    if handler is None:
        return False
    with _SESSION_GRANTS_LOCK:
        rules = tuple(
            rule
            for _, registered_tool, rule in _SESSION_GRANTS.get(
                session_key, ()
            )
            if registered_tool == tool_name
        )
    for rule in rules:
        try:
            if handler.session_rule_matches(rule, runtime_context):
                return True
        except (TypeError, ValueError):
            continue
    return False
