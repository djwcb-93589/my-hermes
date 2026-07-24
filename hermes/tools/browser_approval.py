"""Browser 工具的审批决策、Binding 与执行前状态复检。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence

from hermes.approval_policy import (
    ALLOW,
    ASK,
    DENY,
    HIGH,
    MEDIUM,
    ApprovalAssessment,
    ApprovalRiskLevel,
    IntelligentApprovalAdvisor,
    _approval_details,
    _backend_fingerprint_payload,
    approval_binding_fingerprint,
    approval_grant_identity_matches,
    apply_intelligent_approval,
    normalize_approval_session_key,
)
from hermes.approval_security import (
    ApprovalSecurityPolicy,
    DEFAULT_APPROVAL_SECURITY_POLICY,
)


_ANALYZE_TOOL = "browser_analyze_page"
_BROWSER_OPERATION_RISKS = {
    "browser_upload_files": HIGH,
    "browser_console": HIGH,
    "browser_delete_artifact": MEDIUM,
    "browser_cleanup_artifacts": HIGH,
}
_BROWSER_APPROVAL_TOOLS = frozenset({_ANALYZE_TOOL, *_BROWSER_OPERATION_RISKS})
_FORBIDDEN_CONTEXT_FIELDS = frozenset({
    "path",
    "abs_path",
    "artifact_dir",
    "workspace_root",
    "parent_abs_path",
})


def browser_operation_risk_level(tool_name: str) -> ApprovalRiskLevel:
    """返回 Browser 审批工具固定的最低风险等级。"""
    try:
        return (
            HIGH if tool_name == _ANALYZE_TOOL
            else _BROWSER_OPERATION_RISKS[tool_name]
        )
    except KeyError as exc:
        raise ValueError("browser approval tool is invalid") from exc


def normalize_browser_media_snapshots(value: object) -> tuple[dict, ...]:
    """规范化浏览器产物状态，不把本地绝对路径写入审批记录。"""
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("browser media snapshots must be a non-empty sequence")
    normalized: list[dict] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("browser media snapshot must be an object")
        artifact_id = item.get("artifact_id")
        filename = item.get("filename")
        sha256 = item.get("sha256")
        if (
            not isinstance(artifact_id, str)
            or not artifact_id
            or not isinstance(filename, str)
            or not filename
            or not isinstance(sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
        ):
            raise ValueError("browser media snapshot identity is invalid")
        try:
            size_bytes = int(item.get("size_bytes"))
            mtime_ns = int(item.get("mtime_ns"))
        except (TypeError, ValueError) as exc:
            raise ValueError("browser media snapshot state is invalid") from exc
        if (
            isinstance(item.get("size_bytes"), bool)
            or isinstance(item.get("mtime_ns"), bool)
            or size_bytes <= 0
            or mtime_ns < 0
        ):
            raise ValueError("browser media snapshot state is invalid")
        snapshot = {
            "artifact_id": artifact_id,
            "filename": filename,
            "size_bytes": size_bytes,
            "mtime_ns": mtime_ns,
            "sha256": sha256,
        }
        for field_name in ("page_id", "snapshot_id", "source_url"):
            field_value = item.get(field_name)
            if field_value is not None:
                if not isinstance(field_value, str):
                    raise ValueError("browser media snapshot metadata is invalid")
                snapshot[field_name] = field_value
        normalized.append(snapshot)
    return tuple(normalized)


def normalize_browser_context(value: object) -> dict:
    """只允许审批记录持久化 JSON 安全且不含本地路径的上下文。"""
    if not isinstance(value, Mapping):
        raise ValueError("browser approval context must be an object")
    if any(key in value for key in _FORBIDDEN_CONTEXT_FIELDS):
        raise ValueError("browser approval context must not contain local paths")
    try:
        normalized = json.loads(
            json.dumps(value, ensure_ascii=False, sort_keys=True)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("browser approval context is invalid") from exc
    if not isinstance(normalized, dict):
        raise ValueError("browser approval context must be an object")
    return normalized


def _valid_artifact_state(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return (
        isinstance(value.get("artifact_id"), str)
        and bool(value["artifact_id"])
        and isinstance(value.get("filename"), str)
        and bool(value["filename"])
        and isinstance(value.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None
        and isinstance(value.get("size_bytes"), int)
        and not isinstance(value["size_bytes"], bool)
        and value["size_bytes"] > 0
        and isinstance(value.get("mtime_ns"), int)
        and not isinstance(value["mtime_ns"], bool)
        and value["mtime_ns"] >= 0
    )


def _context_matches_operation(
    tool_name: str,
    arguments: dict,
    context: dict,
) -> bool:
    """把每个 Browser 工具的参数与待复检页面或 artifact 状态绑定。"""
    if tool_name == "browser_upload_files":
        return (
            context.get("ref") == arguments.get("ref")
            and context.get("snapshot_id") == arguments.get("snapshot_id")
            and isinstance(context.get("uploaded_files"), list)
            and bool(context["uploaded_files"])
            and all(
                isinstance(item, Mapping)
                and isinstance(item.get("workspace_path"), str)
                and bool(item["workspace_path"])
                and isinstance(item.get("sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is not None
                for item in context["uploaded_files"]
            )
        )
    if tool_name == "browser_console":
        return (
            context.get("snapshot_id") == arguments.get("snapshot_id")
            and context.get("expression") == arguments.get("expression")
        )
    artifacts = context.get("artifacts")
    if tool_name == "browser_delete_artifact":
        return (
            isinstance(artifacts, list)
            and len(artifacts) == 1
            and _valid_artifact_state(artifacts[0])
            and artifacts[0].get("artifact_id") == arguments.get("artifact_id")
        )
    if tool_name == "browser_cleanup_artifacts":
        return (
            isinstance(artifacts, list)
            and all(_valid_artifact_state(item) for item in artifacts)
        )
    if tool_name == _ANALYZE_TOOL:
        return (
            context.get("snapshot_id") == arguments.get("snapshot_id")
            and isinstance(context.get("artifact_id"), str)
            and bool(context["artifact_id"])
            and isinstance(context.get("filename"), str)
            and bool(context["filename"])
            and isinstance(context.get("size_bytes"), int)
            and context["size_bytes"] > 0
            and context.get("full_page")
            == bool(arguments.get("full_page", False))
        )
    return False


def _valid_browser_binding(
    tool_name: str,
    arguments: dict,
    binding: dict,
    session_key: str,
) -> bool:
    """严格重建 Browser 工具参数、风险与页面状态之间的关系。"""
    try:
        normalize_approval_session_key(session_key)
        if (
            tool_name not in _BROWSER_APPROVAL_TOOLS
            or not isinstance(arguments, dict)
            or binding.get("operation") != tool_name
            or _backend_fingerprint_payload(binding.get("backend_risk"))
            != binding.get("backend_risk")
        ):
            return False
        context = normalize_browser_context(binding.get("browser_context"))
        if not _context_matches_operation(tool_name, arguments, context):
            return False
        expected_risk = browser_operation_risk_level(tool_name)
        if binding.get("risk_level") != expected_risk.value:
            return False
        if tool_name == _ANALYZE_TOOL:
            if set(binding) != {
                "operation",
                "browser_context",
                "media_snapshots",
                "provider",
                "model",
                "backend_risk",
                "risk_level",
            }:
                return False
            snapshots = normalize_browser_media_snapshots(
                binding["media_snapshots"]
            )
            if (
                len(snapshots) != 1
                or snapshots[0]["artifact_id"] != context["artifact_id"]
                or snapshots[0]["filename"] != context["filename"]
                or snapshots[0]["size_bytes"] != context["size_bytes"]
                or snapshots[0].get("snapshot_id")
                != context.get("snapshot_id")
                or binding.get("provider") != "doubao_ark"
                or not isinstance(binding.get("model"), str)
                or not binding["model"]
            ):
                return False
        else:
            if set(binding) != {
                "operation",
                "browser_context",
                "backend_risk",
                "risk_level",
            }:
                return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


class BrowserApprovalHandler:
    """解释单个 Browser 工具的一次性审批 Binding。"""

    def __init__(self, tool_name: str) -> None:
        if tool_name not in _BROWSER_APPROVAL_TOOLS:
            raise ValueError("browser approval tool is invalid")
        self._tool_name = tool_name

    def validate_request_binding(
        self, *, arguments: dict, binding: dict, session_key: str
    ) -> bool:
        return _valid_browser_binding(
            self._tool_name, arguments, binding, session_key
        )

    def validate_grant_binding(
        self, *, arguments: dict, binding: dict, session_key: str
    ) -> bool:
        return _valid_browser_binding(
            self._tool_name, arguments, binding, session_key
        )

    def build_session_rule(self, grant: object) -> None:
        return None

    def session_rule_matches(self, rule: object, runtime_context: object) -> bool:
        return False


_BROWSER_APPROVAL_HANDLERS = {
    tool_name: BrowserApprovalHandler(tool_name)
    for tool_name in _BROWSER_APPROVAL_TOOLS
}


def register_browser_approval_handlers() -> None:
    """随 Browser 工具组注册每个工具唯一的审批 Handler。"""
    from hermes.approval_handlers import (
        get_approval_handler,
        register_approval_handler,
    )

    for tool_name, handler in _BROWSER_APPROVAL_HANDLERS.items():
        registered = get_approval_handler(tool_name)
        if registered is None:
            register_approval_handler(tool_name, handler)
        elif registered is not handler:
            raise ValueError(
                f"approval handler already registered: {tool_name}"
            )


def _browser_grant_matches(
    approval_grant: object,
    tool_name: str,
    arguments: dict,
    *,
    session_key: str,
    binding: dict,
) -> bool:
    fingerprint = approval_binding_fingerprint(
        tool_name,
        arguments,
        session_key=session_key,
        binding=binding,
    )
    return bool(
        approval_grant_identity_matches(
            approval_grant, tool_name, arguments
        )
        and getattr(approval_grant, "scope", None) == "once"
        and getattr(approval_grant, "session_key", None) == session_key
        and getattr(approval_grant, "fingerprint", None) == fingerprint
        and getattr(approval_grant, "binding", None) == binding
        and _valid_browser_binding(
            tool_name, arguments, binding, session_key
        )
    )


def approved_browser_media_snapshots_candidate(
    approval_grant: object,
    arguments: dict,
    *,
    session_key: str,
) -> tuple[dict, ...] | None:
    """读取仅绑定本次页面分析的可信浏览器产物快照。"""
    try:
        normalized_session_key = normalize_approval_session_key(session_key)
        binding = approval_grant.binding
        if (
            not approval_grant_identity_matches(
                approval_grant, _ANALYZE_TOOL, arguments
            )
            or approval_grant.scope != "once"
            or approval_grant.session_key != normalized_session_key
            or not _valid_browser_binding(
                _ANALYZE_TOOL,
                arguments,
                binding,
                normalized_session_key,
            )
        ):
            return None
        return normalize_browser_media_snapshots(binding["media_snapshots"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def approved_browser_operation_context_candidate(
    approval_grant: object,
    tool_name: str,
    arguments: dict,
    *,
    session_key: str,
) -> dict | None:
    """读取仅绑定本次浏览器高风险操作的可信上下文。"""
    try:
        normalized_session_key = normalize_approval_session_key(session_key)
        binding = approval_grant.binding
        if (
            not approval_grant_identity_matches(
                approval_grant, tool_name, arguments
            )
            or approval_grant.scope != "once"
            or approval_grant.session_key != normalized_session_key
            or not _valid_browser_binding(
                tool_name, arguments, binding, normalized_session_key
            )
        ):
            return None
        return normalize_browser_context(binding["browser_context"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def browser_operation_state_matches(
    approval_grant: object,
    approved_context: object,
    current_context: object,
) -> bool:
    """执行前确认可信 Grant 中的页面状态与当前复检状态完全一致。"""
    if approval_grant is None:
        return True
    try:
        return (
            normalize_browser_context(approved_context)
            == normalize_browser_context(current_context)
        )
    except ValueError:
        return False


def browser_media_snapshot_matches(
    approved_snapshot: object,
    current_snapshot: object,
) -> bool:
    """执行前确认截图 artifact 的内容和页面身份均未变化。"""
    try:
        return (
            normalize_browser_media_snapshots([approved_snapshot])
            == normalize_browser_media_snapshots([current_snapshot])
        )
    except ValueError:
        return False


def assess_external_media_analysis(
    tool_name: str,
    arguments: dict,
    *,
    session_key: str,
    media_snapshots: Sequence[dict],
    provider: str,
    model: str,
    source_context: Mapping,
    remote_approval: bool,
    approval_grant: object = None,
    security_policy: ApprovalSecurityPolicy | None = None,
    backend_context: Mapping | None = None,
    intelligent_advisor: IntelligentApprovalAdvisor | None = None,
) -> ApprovalAssessment:
    """评估浏览器产物外发给模型服务的高风险一次性操作。"""
    if tool_name != _ANALYZE_TOOL:
        raise ValueError("external media analysis tool is invalid")
    normalized_session_key = normalize_approval_session_key(session_key)
    normalized_arguments = dict(arguments)
    normalized_provider = str(provider or "").strip()
    normalized_model = str(model or "").strip()
    if normalized_provider != "doubao_ark" or not normalized_model:
        raise ValueError("external media analysis provider is invalid")
    context = normalize_browser_context(source_context)
    snapshots = normalize_browser_media_snapshots(media_snapshots)
    binding = {
        "operation": tool_name,
        "browser_context": context,
        "media_snapshots": list(snapshots),
        "provider": normalized_provider,
        "model": normalized_model,
        "backend_risk": _backend_fingerprint_payload(backend_context),
        "risk_level": HIGH.value,
    }
    grant_matches = _browser_grant_matches(
        approval_grant,
        tool_name,
        normalized_arguments,
        session_key=normalized_session_key,
        binding=binding,
    )
    if grant_matches:
        decision, reason, error_type, error, decision_source = (
            ALLOW,
            "approved browser media analysis matches the current artifact",
            None,
            None,
            "once_grant",
        )
    elif approval_grant is not None:
        decision, reason, error_type, error, decision_source = (
            DENY,
            "browser media analysis approval grant no longer matches this request",
            "approval_stale",
            "approved browser artifact state changed; request approval again",
            "grant_validation",
        )
    else:
        decision, reason, error_type, error, decision_source = (
            ASK,
            "external page analysis sends a browser screenshot to a third-party model service",
            None,
            None,
            "remote_approval" if remote_approval else "interactive_approval",
        )
    details, fingerprint = _approval_details(
        tool_name,
        normalized_arguments,
        session_key=normalized_session_key,
        binding=binding,
        operation_type="browser.external_analysis",
        risk_level=HIGH,
        reason=reason,
        decision_source=decision_source,
        allowed_scopes=("once",),
    )
    assessment = ApprovalAssessment(
        tool_name=tool_name,
        decision=decision,
        risk_level=HIGH,
        fingerprint=fingerprint,
        reason=reason,
        normalized_arguments=normalized_arguments,
        details=details,
        session_key=normalized_session_key,
        error_type=error_type,
        error=error,
        fatal=False,
    )
    return apply_intelligent_approval(
        assessment,
        security_policy=security_policy or DEFAULT_APPROVAL_SECURITY_POLICY,
        advisor=intelligent_advisor,
    )


def assess_browser_operation(
    tool_name: str,
    arguments: dict,
    *,
    session_key: str,
    source_context: Mapping,
    risk_level: ApprovalRiskLevel,
    remote_approval: bool,
    approval_grant: object = None,
    security_policy: ApprovalSecurityPolicy | None = None,
    backend_context: Mapping | None = None,
    intelligent_advisor: IntelligentApprovalAdvisor | None = None,
) -> ApprovalAssessment:
    """评估上传、控制台和产物删除等浏览器一次性操作。"""
    if (
        tool_name not in _BROWSER_OPERATION_RISKS
        or risk_level != _BROWSER_OPERATION_RISKS[tool_name]
    ):
        raise ValueError("browser approval tool or risk level is invalid")
    normalized_session_key = normalize_approval_session_key(session_key)
    normalized_arguments = dict(arguments)
    context = normalize_browser_context(source_context)
    binding = {
        "operation": tool_name,
        "browser_context": context,
        "backend_risk": _backend_fingerprint_payload(backend_context),
        "risk_level": risk_level.value,
    }
    grant_matches = _browser_grant_matches(
        approval_grant,
        tool_name,
        normalized_arguments,
        session_key=normalized_session_key,
        binding=binding,
    )
    if grant_matches:
        decision, reason, error_type, error, decision_source = (
            ALLOW,
            "approved browser operation matches the current request",
            None,
            None,
            "once_grant",
        )
    elif approval_grant is not None:
        decision, reason, error_type, error, decision_source = (
            DENY,
            "browser operation approval grant no longer matches this request",
            "approval_stale",
            "browser operation changed; request approval again",
            "grant_validation",
        )
    else:
        decision, reason, error_type, error, decision_source = (
            ASK,
            "browser operation requires an explicit one-time approval",
            None,
            None,
            "remote_approval" if remote_approval else "interactive_approval",
        )
    details, fingerprint = _approval_details(
        tool_name,
        normalized_arguments,
        session_key=normalized_session_key,
        binding=binding,
        operation_type="browser.high_risk_operation",
        risk_level=risk_level,
        reason=reason,
        decision_source=decision_source,
        allowed_scopes=("once",),
    )
    assessment = ApprovalAssessment(
        tool_name=tool_name,
        decision=decision,
        risk_level=risk_level,
        fingerprint=fingerprint,
        reason=reason,
        normalized_arguments=normalized_arguments,
        details=details,
        session_key=normalized_session_key,
        error_type=error_type,
        error=error,
        fatal=False,
    )
    return apply_intelligent_approval(
        assessment,
        security_policy=security_policy or DEFAULT_APPROVAL_SECURITY_POLICY,
        advisor=intelligent_advisor,
    )
