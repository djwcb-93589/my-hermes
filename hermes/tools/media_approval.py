"""Media 工具的审批决策、Binding 与执行前状态复检。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from hermes.approval_policy import (
    ALLOW,
    ASK,
    CRITICAL,
    DENY,
    HIGH,
    ApprovalAssessment,
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
from hermes.config import SENSITIVE_FILE_PATTERNS
from hermes.file_state import (
    FileStateSnapshotError,
    file_state_snapshot_matches,
    normalize_file_state_snapshot,
)
from hermes.path_policy import ALLOW_ALL_PATH_POLICY
from hermes.path_policy import PATH_POLICY_DENIED_ERROR_TYPE


_MEDIA_TOOL_NAME = "media_analyze"
_MEDIA_PROVIDER = "doubao_ark"


def has_symlink_component(path: Path) -> bool:
    """拒绝目标自身及已有父目录中的符号链接。"""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def is_sensitive_media_path(abs_path: str) -> bool:
    """复用全局敏感文件规则阻断媒体外传。"""
    normalized = abs_path.replace("\\", "/").lower()
    return any(pattern.search(normalized) for pattern in SENSITIVE_FILE_PATTERNS)


def normalize_media_analysis_snapshots(value: object) -> tuple[dict, ...]:
    """规范化媒体外传审批绑定的有序文件状态快照。"""
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("media snapshots must be a non-empty sequence")
    snapshots: list[dict] = []
    for item in value:
        snapshot = normalize_file_state_snapshot(item)
        if snapshot["exists"] is not True or snapshot["file_type"] != "file":
            raise ValueError("media snapshot must describe an existing file")
        snapshots.append(snapshot)
    return tuple(snapshots)


def _media_binding(
    *,
    normalized_paths: Sequence[str],
    media_snapshots: Sequence[dict],
    provider: str,
    model: str,
    backend_context: Mapping | None,
) -> dict:
    """从执行入口提供的真实状态构造完整媒体 Binding。"""
    normalized_provider = str(provider or "").strip()
    normalized_model = str(model or "").strip()
    if normalized_provider != _MEDIA_PROVIDER:
        raise ValueError("media analysis provider is invalid")
    snapshots = normalize_media_analysis_snapshots(media_snapshots)
    paths = [str(path) for path in normalized_paths]
    if paths != [snapshot["abs_path"] for snapshot in snapshots]:
        raise ValueError("media snapshots do not match normalized paths")
    return {
        "normalized_paths": paths,
        "media_snapshots": list(snapshots),
        "provider": normalized_provider,
        "model": normalized_model,
        "backend_risk": _backend_fingerprint_payload(backend_context),
        "risk_level": HIGH.value,
    }


def _valid_media_binding(arguments: dict, binding: dict, session_key: str) -> bool:
    """严格校验媒体 Binding 的字段和值域及路径顺序。"""
    try:
        normalize_approval_session_key(session_key)
        if not isinstance(arguments, dict) or set(binding) != {
            "normalized_paths",
            "media_snapshots",
            "provider",
            "model",
            "backend_risk",
            "risk_level",
        }:
            return False
        snapshots = normalize_media_analysis_snapshots(binding["media_snapshots"])
        paths = binding["normalized_paths"]
        if (
            not isinstance(paths, list)
            or not all(isinstance(path, str) and path for path in paths)
            or paths != [snapshot["abs_path"] for snapshot in snapshots]
            or binding["provider"] != _MEDIA_PROVIDER
            or not isinstance(binding["model"], str)
            or binding["risk_level"] != HIGH.value
            or _backend_fingerprint_payload(binding["backend_risk"])
            != binding["backend_risk"]
        ):
            return False
        argument_paths = arguments.get("paths")
        return (
            isinstance(argument_paths, list)
            and len(argument_paths) == len(paths)
            and all(isinstance(path, str) and path.strip() for path in argument_paths)
        )
    except (KeyError, TypeError, ValueError):
        return False


class MediaApprovalHandler:
    """解释 Media 一次性审批 Binding。"""

    def validate_request_binding(
        self, *, arguments: dict, binding: dict, session_key: str
    ) -> bool:
        return _valid_media_binding(arguments, binding, session_key)

    def validate_grant_binding(
        self, *, arguments: dict, binding: dict, session_key: str
    ) -> bool:
        return _valid_media_binding(arguments, binding, session_key)

    def build_session_rule(self, grant: object) -> None:
        return None

    def session_rule_matches(self, rule: object, runtime_context: object) -> bool:
        return False


_MEDIA_APPROVAL_HANDLER = MediaApprovalHandler()


def register_media_approval_handler() -> None:
    """随工具注册唯一的 Media 审批 Handler。"""
    from hermes.approval_handlers import (
        get_approval_handler,
        register_approval_handler,
    )

    registered = get_approval_handler(_MEDIA_TOOL_NAME)
    if registered is None:
        register_approval_handler(
            _MEDIA_TOOL_NAME, _MEDIA_APPROVAL_HANDLER
        )
    elif registered is not _MEDIA_APPROVAL_HANDLER:
        raise ValueError(
            f"approval handler already registered: {_MEDIA_TOOL_NAME}"
        )


def assess_media_path_policy_denial(
    *, session_key: str | None = None
) -> ApprovalAssessment:
    """将媒体路径策略命中收敛为不可审批的硬拒绝。"""
    return ApprovalAssessment(
        tool_name=_MEDIA_TOOL_NAME,
        decision=DENY,
        risk_level=CRITICAL,
        fingerprint="",
        reason="configured filesystem policy denied the referenced path",
        normalized_arguments={},
        details={"decision_source": "filesystem_policy"},
        session_key=session_key,
        error_type=PATH_POLICY_DENIED_ERROR_TYPE,
        error="path is blocked by the configured filesystem policy",
        fatal=True,
    )


def approved_media_snapshots_candidate(
    approval_grant: object,
    arguments: dict,
    *,
    session_key: str,
) -> tuple[dict, ...] | None:
    """读取只绑定本次媒体分析请求的可信文件状态快照。"""
    try:
        normalized_session_key = normalize_approval_session_key(session_key)
        binding = approval_grant.binding
        if (
            not approval_grant_identity_matches(
                approval_grant, _MEDIA_TOOL_NAME, arguments
            )
            or approval_grant.scope != "once"
            or approval_grant.session_key != normalized_session_key
            or not _valid_media_binding(
                arguments, binding, normalized_session_key
            )
        ):
            return None
        return normalize_media_analysis_snapshots(binding["media_snapshots"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def approved_media_state_matches(
    backend: object,
    sources: Sequence[object],
    snapshots: Sequence[dict],
) -> bool:
    """在审批恢复和外发前逐项确认文件状态仍与 Binding 一致。"""
    if len(sources) != len(snapshots):
        return False
    path_policy = getattr(backend, "path_policy", ALLOW_ALL_PATH_POLICY)
    try:
        for item, snapshot in zip(sources, snapshots, strict=True):
            abs_path = str(getattr(item, "abs_path"))
            path = Path(abs_path)
            if (
                snapshot.get("abs_path") != abs_path
                or has_symlink_component(path)
                or path.is_symlink()
                or not path.is_file()
                or is_sensitive_media_path(abs_path)
                or not file_state_snapshot_matches(
                    backend,
                    abs_path,
                    snapshot,
                    path_policy=path_policy,
                )
            ):
                return False
    except (FileStateSnapshotError, OSError, TypeError, ValueError):
        return False
    return True


def assess_media_analysis(
    arguments: dict,
    *,
    normalized_paths: Sequence[str],
    media_snapshots: Sequence[dict],
    session_key: str,
    remote_approval: bool,
    provider: str,
    model: str,
    approval_grant: object = None,
    security_policy: ApprovalSecurityPolicy | None = None,
    backend_context: Mapping | None = None,
    intelligent_advisor: IntelligentApprovalAdvisor | None = None,
) -> ApprovalAssessment:
    """为外部媒体分析创建仅可执行一次的高风险审批身份。"""
    normalized_session_key = normalize_approval_session_key(session_key)
    normalized_arguments = dict(arguments)
    binding = _media_binding(
        normalized_paths=normalized_paths,
        media_snapshots=media_snapshots,
        provider=provider,
        model=model,
        backend_context=backend_context,
    )
    fingerprint = approval_binding_fingerprint(
        _MEDIA_TOOL_NAME,
        normalized_arguments,
        session_key=normalized_session_key,
        binding=binding,
    )
    grant_matches = (
        approval_grant_identity_matches(
            approval_grant, _MEDIA_TOOL_NAME, arguments
        )
        and getattr(approval_grant, "scope", None) == "once"
        and getattr(approval_grant, "session_key", None)
        == normalized_session_key
        and getattr(approval_grant, "fingerprint", None) == fingerprint
        and getattr(approval_grant, "binding", None) == binding
        and _valid_media_binding(
            normalized_arguments, binding, normalized_session_key
        )
    )
    if grant_matches:
        decision = ALLOW
        reason = "approved media analysis matches the current files and request"
        error_type = error = None
        fatal = False
        decision_source = "once_grant"
    elif approval_grant is not None:
        decision = DENY
        reason = "media analysis approval grant no longer matches this request"
        error_type = "approval_stale"
        error = "approved media file state changed; request approval again"
        fatal = False
        decision_source = "grant_validation"
    else:
        decision = ASK
        reason = (
            "external media analysis sends local files to a third-party "
            "model service"
        )
        error_type = error = None
        fatal = False
        decision_source = (
            "remote_approval" if remote_approval else "interactive_approval"
        )
    details, fingerprint = _approval_details(
        _MEDIA_TOOL_NAME,
        normalized_arguments,
        session_key=normalized_session_key,
        binding=binding,
        operation_type="media.external_analysis",
        risk_level=HIGH,
        reason=reason,
        decision_source=decision_source,
        allowed_scopes=("once",),
    )
    assessment = ApprovalAssessment(
        tool_name=_MEDIA_TOOL_NAME,
        decision=decision,
        risk_level=HIGH,
        fingerprint=fingerprint,
        reason=reason,
        normalized_arguments=normalized_arguments,
        details=details,
        session_key=normalized_session_key,
        error_type=error_type,
        error=error,
        fatal=fatal,
    )
    return apply_intelligent_approval(
        assessment,
        security_policy=security_policy or DEFAULT_APPROVAL_SECURITY_POLICY,
        advisor=intelligent_advisor,
    )
