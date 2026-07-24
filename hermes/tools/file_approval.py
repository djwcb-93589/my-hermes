"""File 工具的审批决策、Binding 与执行前复检。"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from hermes.approval_policy import (
    ALLOW,
    ASK,
    CRITICAL,
    DENY,
    HIGH,
    LOW,
    MEDIUM,
    ApprovalAssessment,
    ApprovalRiskLevel,
    _approval_details,
    _backend_fingerprint_payload,
    apply_intelligent_approval,
    approval_binding_fingerprint,
    approval_grant_identity_matches,
    normalize_approval_session_key,
    normalize_risk_level,
)
from hermes.file_state import (
    FileStateSnapshotError,
    capture_file_state_snapshot,
    file_state_snapshot_matches,
    normalize_file_state_snapshot,
)
from hermes.path_policy import PATH_POLICY_DENIED_ERROR_TYPE


_FILE_WRITE_ACTIONS = frozenset({"write", "append", "replace"})
_FILE_READ_ACTIONS = frozenset({"read", "read_range"})
_FILE_METADATA_ACTIONS = frozenset({"list", "stat"})
_FILE_CONTEXT_ACTIONS = frozenset({"pwd", "context"})
_FILE_PATH_ACTIONS = (
    _FILE_WRITE_ACTIONS | _FILE_READ_ACTIONS | _FILE_METADATA_ACTIONS
)
_FILE_ACTIONS = _FILE_PATH_ACTIONS | _FILE_CONTEXT_ACTIONS
_BACKEND_RISK_KEYS = frozenset({
    "backend_type",
    "host_mounts",
    "docker_socket",
    "remote_host",
})
_RISK_ORDER = {
    LOW: 0,
    MEDIUM: 1,
    HIGH: 2,
    CRITICAL: 3,
}


@dataclass(frozen=True, slots=True)
class FilePolicyDenial:
    """File 用户策略或 hardline 规则产生的不可审批拒绝。"""

    error_type: str
    reason: str
    error: str
    decision_source: str


@dataclass(frozen=True, slots=True)
class FileSessionGrantRule:
    """按 action、路径范围和副作用能力匹配的 File 会话授权规则。"""

    actions: frozenset[str]
    path_under: str | None = None
    all_accessible: bool = False
    allow_sensitive: bool = False
    allow_overwrite: bool = False
    max_risk: ApprovalRiskLevel = MEDIUM


def _path_is_under(path: str, parent: str) -> bool:
    """使用结构化路径比较，兼容 Windows 不同盘符。"""
    try:
        return os.path.commonpath((
            os.path.normcase(path),
            os.path.normcase(parent),
        )) == os.path.normcase(parent)
    except ValueError:
        return False


def _active_security_policy(security_policy):
    if security_policy is not None:
        return security_policy
    from hermes.approval_security import DEFAULT_APPROVAL_SECURITY_POLICY

    return DEFAULT_APPROVAL_SECURITY_POLICY


def _file_policy_denial(
    security_policy,
    *,
    action: str,
    normalized_path: str | None,
) -> FilePolicyDenial | None:
    """按 hardline、受保护路径和 action/path 组合检查 File 操作。"""
    _validate_file_rule_actions(security_policy._denied_file_rules)
    _validate_file_rule_actions(security_policy._approval_file_rules)
    _validate_file_action(action)
    if normalized_path is None:
        return None
    hardline_paths = security_policy._hardline_protected_paths
    protected_paths = security_policy._protected_paths
    if action in _FILE_WRITE_ACTIONS and any(
        _path_is_under(normalized_path, protected)
        for protected in hardline_paths
    ):
        return FilePolicyDenial(
            error_type="hardline_denied",
            reason="File 操作尝试修改审批配置或系统安全关键路径",
            error="file operation is blocked by a hardline safety rule",
            decision_source="hardline",
        )
    if action in _FILE_WRITE_ACTIONS and any(
        _path_is_under(normalized_path, protected)
        for protected in protected_paths
    ):
        return FilePolicyDenial(
            error_type="configured_deny_rule",
            reason="File 操作尝试修改用户配置的受保护路径",
            error="file operation targets a configured protected path",
            decision_source="user_deny_rule",
        )
    for rule in security_policy._denied_file_rules:
        if (
            action in rule.actions
            and _path_is_under(
                normalized_path,
                rule.path_under,
            )
        ):
            return FilePolicyDenial(
                error_type="configured_deny_rule",
                reason="File action 与目标路径命中用户拒绝组合规则",
                error="file operation is blocked by a configured deny rule",
                decision_source="user_deny_rule",
            )
    return None


def _requires_file_approval(
    security_policy,
    *,
    action: str,
    normalized_path: str | None,
) -> bool:
    """判断操作是否命中远程 File 审批规则。"""
    _validate_file_rule_actions(security_policy._approval_file_rules)
    _validate_file_action(action)
    if normalized_path is None:
        return False
    return any(
        action in rule.actions
        and _path_is_under(
            normalized_path,
            rule.path_under,
        )
        for rule in security_policy._approval_file_rules
    )


def _validate_file_action(action: object) -> str:
    """拒绝当前 File 工具未声明的 action。"""
    if not isinstance(action, str) or action not in _FILE_ACTIONS:
        raise ValueError("file action is invalid")
    return action


def _validate_file_rule_actions(rules: Sequence) -> None:
    """在使用规则前确认配置只引用当前 File 工具支持的 action。"""
    for rule in rules:
        actions = getattr(rule, "actions", None)
        if not isinstance(actions, frozenset) or not actions:
            raise ValueError("file approval rule actions are invalid")
        if any(
            not isinstance(action, str) or action not in _FILE_ACTIONS
            for action in actions
        ):
            raise ValueError("file approval rule contains an unknown action")


def is_sensitive_file_path(
    abs_path: str,
    patterns: Sequence,
) -> bool:
    """判断路径是否命中配置的敏感文件模式。"""
    normalized = abs_path.replace("\\", "/").lower()
    return any(pattern.search(normalized) for pattern in patterns)


def requires_file_state_snapshot(arguments: dict) -> bool:
    """判断审批身份是否必须绑定执行前文件状态。"""
    action = arguments.get("action")
    return (
        action in {"replace", "append"}
        or (
            action == "write"
            and bool(arguments.get("overwrite", False))
        )
    )


def _file_operation_mutates_existing(
    arguments: dict,
    file_snapshot: dict | None,
) -> bool:
    """把覆盖写、替换和已有文件追加统一收敛为覆盖能力。"""
    if not file_snapshot or file_snapshot.get("exists") is not True:
        return False
    action = arguments.get("action")
    return (
        action in {"replace", "append"}
        or (
            action == "write"
            and bool(arguments.get("overwrite", False))
        )
    )


def _file_risk_level(action: str) -> ApprovalRiskLevel:
    if action in _FILE_WRITE_ACTIONS:
        return HIGH
    if action in _FILE_READ_ACTIONS:
        return MEDIUM
    return LOW


def _normalize_backend_risk(value: object) -> dict:
    """严格校验审批记录中的非敏感 backend 风险画像。"""
    if not isinstance(value, Mapping) or set(value) != _BACKEND_RISK_KEYS:
        raise ValueError("file backend risk binding is invalid")
    backend_type = value.get("backend_type")
    if not isinstance(backend_type, str) or not backend_type:
        raise ValueError("file backend type binding is invalid")
    for field_name in _BACKEND_RISK_KEYS - {"backend_type"}:
        if not isinstance(value.get(field_name), bool):
            raise ValueError("file backend risk flag is invalid")
    return {
        "backend_type": backend_type,
        "host_mounts": value["host_mounts"],
        "docker_socket": value["docker_socket"],
        "remote_host": value["remote_host"],
    }


def _normalize_file_binding(
    arguments: dict,
    binding: dict,
    *,
    session_key: str,
) -> dict:
    """从原始参数严格重建并校验 File Binding 的结构语义。"""
    normalize_approval_session_key(session_key)
    if not isinstance(arguments, dict) or not isinstance(binding, dict):
        raise ValueError("file approval binding is invalid")
    if set(binding) != {
        "abs_path",
        "file_snapshot",
        "backend_risk",
        "risk_level",
    }:
        raise ValueError("file approval binding fields are invalid")

    action = arguments.get("action")
    if not isinstance(action, str) or action not in _FILE_ACTIONS:
        raise ValueError("file action is invalid")
    if "overwrite" in arguments and not isinstance(
        arguments["overwrite"],
        bool,
    ):
        raise ValueError("file overwrite argument is invalid")

    abs_path = binding.get("abs_path")
    if action in _FILE_PATH_ACTIONS:
        if (
            not isinstance(arguments.get("path"), str)
            or not arguments["path"].strip()
            or not isinstance(abs_path, str)
            or not abs_path
            or not os.path.isabs(abs_path)
        ):
            raise ValueError("file approval path binding is invalid")
    elif abs_path is not None:
        raise ValueError("file context action cannot bind a path")

    snapshot = binding.get("file_snapshot")
    if snapshot is not None:
        normalized_snapshot = normalize_file_state_snapshot(snapshot)
        if normalized_snapshot != snapshot:
            raise ValueError("file snapshot binding is not normalized")
        if normalized_snapshot["abs_path"] != abs_path:
            raise ValueError("file snapshot does not match approved path")
        snapshot = normalized_snapshot
    if requires_file_state_snapshot(arguments) and snapshot is None:
        raise ValueError("file operation requires an approval snapshot")

    risk_level = normalize_risk_level(binding.get("risk_level"))
    if risk_level != _file_risk_level(action):
        raise ValueError("file risk binding does not match action")

    backend_risk = _normalize_backend_risk(binding.get("backend_risk"))
    return {
        "abs_path": abs_path,
        "file_snapshot": snapshot,
        "backend_risk": backend_risk,
        "risk_level": risk_level.value,
    }


def _file_grant_matches(
    approval_grant: object,
    arguments: dict,
    *,
    fingerprint: str,
    binding: dict,
    session_key: str,
) -> bool:
    """File grant 必须覆盖完整参数、路径、状态、会话和指纹。"""
    try:
        return (
            approval_grant_identity_matches(
                approval_grant,
                "file",
                arguments,
            )
            and approval_grant.fingerprint == fingerprint
            and approval_grant.session_key == session_key
            and approval_grant.binding == binding
            and _FILE_APPROVAL_HANDLER.validate_grant_binding(
                arguments=arguments,
                binding=approval_grant.binding,
                session_key=session_key,
            )
        )
    except (AttributeError, TypeError, ValueError):
        return False


def approved_file_path_candidate(
    approval_grant: object,
    arguments: dict,
    *,
    session_key: str,
) -> str | None:
    """仅从同一参数和会话的可信 File grant 读取绝对路径候选。"""
    try:
        normalized_session_key = normalize_approval_session_key(session_key)
        if (
            not approval_grant_identity_matches(
                approval_grant,
                "file",
                arguments,
            )
            or approval_grant.session_key != normalized_session_key
            or not _FILE_APPROVAL_HANDLER.validate_grant_binding(
                arguments=arguments,
                binding=approval_grant.binding,
                session_key=normalized_session_key,
            )
        ):
            return None
        approved_path = approval_grant.binding.get("abs_path")
        return approved_path if isinstance(approved_path, str) else None
    except (AttributeError, TypeError, ValueError):
        return None


def approved_file_snapshot_candidate(
    approval_grant: object,
    arguments: dict,
    *,
    session_key: str,
) -> dict | None:
    """读取可信 File grant 绑定的获批文件状态快照。"""
    try:
        normalized_session_key = normalize_approval_session_key(session_key)
        if (
            not approval_grant_identity_matches(
                approval_grant,
                "file",
                arguments,
            )
            or approval_grant.session_key != normalized_session_key
            or not _FILE_APPROVAL_HANDLER.validate_grant_binding(
                arguments=arguments,
                binding=approval_grant.binding,
                session_key=normalized_session_key,
            )
        ):
            return None
        snapshot = approval_grant.binding.get("file_snapshot")
        return dict(snapshot) if isinstance(snapshot, dict) else None
    except (AttributeError, TypeError, ValueError):
        return None


def approved_file_state_matches(
    backend,
    abs_path: str,
    snapshot: object,
    *,
    path_policy,
) -> bool:
    """执行前重新捕获文件状态并与获批快照逐字段比较。"""
    normalized_snapshot = normalize_file_state_snapshot(snapshot)
    if normalized_snapshot["abs_path"] != abs_path:
        return False
    return file_state_snapshot_matches(
        backend,
        abs_path,
        normalized_snapshot,
        path_policy=path_policy,
    )


def capture_file_approval_snapshot(
    backend,
    abs_path: str,
    *,
    path_policy,
) -> dict:
    """在创建审批前捕获 File Binding 使用的稳定文件状态。"""
    return capture_file_state_snapshot(
        backend,
        abs_path,
        path_policy=path_policy,
    )


def assess_file_operation(
    arguments: dict,
    *,
    normalized_path: str | None,
    session_key: str,
    remote_approval: bool,
    sensitive: bool,
    allow_sensitive: bool,
    approval_grant: object = None,
    file_snapshot: dict | None = None,
    security_policy=None,
    backend_context: Mapping | None = None,
    intelligent_advisor=None,
) -> ApprovalAssessment:
    """对 File 操作按完整参数和最终绝对路径生成唯一决策。"""
    del allow_sensitive
    normalized_arguments = dict(arguments)
    normalized_session_key = normalize_approval_session_key(session_key)
    action = str(arguments.get("action", ""))
    active_security_policy = _active_security_policy(security_policy)
    policy_denial = _file_policy_denial(
        active_security_policy,
        action=action,
        normalized_path=normalized_path,
    )
    if file_snapshot is not None:
        file_snapshot = normalize_file_state_snapshot(file_snapshot)
        if file_snapshot["abs_path"] != normalized_path:
            raise ValueError("file snapshot does not match normalized path")
    if policy_denial is not None or sensitive:
        risk_level = CRITICAL
    else:
        risk_level = _file_risk_level(action)

    binding = {
        "abs_path": normalized_path,
        "file_snapshot": file_snapshot,
        "backend_risk": _backend_fingerprint_payload(backend_context),
        "risk_level": risk_level.value,
    }
    fingerprint = approval_binding_fingerprint(
        "file",
        normalized_arguments,
        session_key=normalized_session_key,
        binding=binding,
    )

    grant_matches = _file_grant_matches(
        approval_grant,
        arguments,
        fingerprint=fingerprint,
        binding=binding,
        session_key=normalized_session_key,
    )
    if policy_denial is not None:
        decision = DENY
        reason = policy_denial.reason
        error_type = policy_denial.error_type
        error = policy_denial.error
        fatal = True
        decision_source = policy_denial.decision_source
    elif sensitive:
        decision = DENY
        reason = "critical File 操作不允许创建 once 或 session grant"
        error_type = "sensitive_access_denied"
        error = "critical sensitive file operation is denied by approval policy"
        fatal = True
        decision_source = "approval_policy"
    elif grant_matches:
        decision = ALLOW
        reason = "已批准操作与当前 File 参数和目标路径完全一致"
        error_type = None
        error = None
        fatal = False
        decision_source = "once_grant"
    elif approval_grant is not None:
        decision = DENY
        reason = "File approval grant 与当前操作身份不一致"
        error_type = "approval_stale"
        error = "approved file operation changed; request approval again"
        fatal = False
        decision_source = "grant_validation"
    elif _file_session_grant_matches(
        session_key=normalized_session_key,
        action=action,
        normalized_path=normalized_path,
        sensitive=sensitive,
        overwrite=_file_operation_mutates_existing(
            arguments,
            file_snapshot,
        ),
        risk_level=risk_level,
    ):
        decision = ALLOW
        reason = "操作匹配当前 session 的结构化 File grant"
        error_type = None
        error = None
        fatal = False
        decision_source = "session_grant"
    elif (
        remote_approval
        and active_security_policy.remote_default_allow
        and not _requires_file_approval(
            active_security_policy,
            action=action,
            normalized_path=normalized_path,
        )
    ):
        decision = ALLOW
        reason = "file operation is outside the remote approval blacklist"
        error_type = None
        error = None
        fatal = False
        decision_source = "remote_blacklist_default_allow"
    elif remote_approval:
        decision = ASK
        reason = "File 写入或修改操作需要显式审批"
        error_type = None
        error = None
        fatal = False
        decision_source = "approval_policy"
    elif action in _FILE_READ_ACTIONS:
        decision = ALLOW
        reason = "普通文件只读操作不需要审批"
        error_type = None
        error = None
        fatal = False
        decision_source = "static_allowlist"
    elif action in _FILE_METADATA_ACTIONS:
        decision = ALLOW
        reason = "普通文件元数据操作不需要审批"
        error_type = None
        error = None
        fatal = False
        decision_source = "static_allowlist"
    else:
        decision = ALLOW
        reason = "File 上下文操作不需要审批"
        error_type = None
        error = None
        fatal = False
        decision_source = "static_allowlist"

    details, fingerprint = _approval_details(
        "file",
        normalized_arguments,
        session_key=normalized_session_key,
        binding=binding,
        operation_type=f"file.{action}",
        risk_level=risk_level,
        reason=reason,
        decision_source=decision_source,
    )
    assessment = ApprovalAssessment(
        tool_name="file",
        decision=decision,
        risk_level=risk_level,
        fingerprint=fingerprint,
        reason=reason,
        normalized_arguments=normalized_arguments,
        details=details,
        normalized_path=normalized_path,
        session_key=normalized_session_key,
        error_type=error_type,
        error=error,
        fatal=fatal,
    )
    return apply_intelligent_approval(
        assessment,
        security_policy=active_security_policy,
        advisor=intelligent_advisor,
    )


def assess_file_path_policy_denial(
    *,
    session_key: str | None = None,
) -> ApprovalAssessment:
    """把 File denied_paths 命中收敛为最高优先级拒绝。"""
    return ApprovalAssessment(
        tool_name="file",
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


def _file_session_grant_matches(
    *,
    session_key: str,
    action: str,
    normalized_path: str | None,
    sensitive: bool,
    overwrite: bool,
    risk_level: ApprovalRiskLevel,
) -> bool:
    """通过通用 session grant 存储调用 File Handler 匹配规则。"""
    from hermes.approval_policy import session_grant_matches

    return session_grant_matches(
        "file",
        {
            "session_key": session_key,
            "action": action,
            "normalized_path": normalized_path,
            "sensitive": sensitive,
            "overwrite": overwrite,
            "risk_level": risk_level.value,
        },
    )


class FileApprovalHandler:
    """解释 File Binding，并构造和匹配 File session rule。"""

    def validate_request_binding(
        self,
        *,
        arguments: dict,
        binding: dict,
        session_key: str,
    ) -> bool:
        try:
            return _normalize_file_binding(
                arguments,
                binding,
                session_key=session_key,
            ) == binding
        except (TypeError, ValueError):
            return False

    def validate_grant_binding(
        self,
        *,
        arguments: dict,
        binding: dict,
        session_key: str,
    ) -> bool:
        return self.validate_request_binding(
            arguments=arguments,
            binding=binding,
            session_key=session_key,
        )

    def build_session_rule(self, grant) -> FileSessionGrantRule | None:
        """从可信 Grant Binding 构造可复用的低风险 File 规则。"""
        try:
            if not self.validate_grant_binding(
                arguments=grant.arguments,
                binding=grant.binding,
                session_key=grant.session_key,
            ):
                return None
            risk_level = normalize_risk_level(
                grant.binding.get("risk_level")
            )
            if _RISK_ORDER[risk_level] > _RISK_ORDER[MEDIUM]:
                return None
            action = str(grant.arguments.get("action") or "")
            path = grant.binding.get("abs_path")
            snapshot = grant.binding.get("file_snapshot")
            if not isinstance(path, str) or not path:
                return None
            all_accessible = action in (
                _FILE_READ_ACTIONS | _FILE_METADATA_ACTIONS
            )
            return FileSessionGrantRule(
                actions=frozenset({action}),
                path_under=None if all_accessible else os.path.dirname(path),
                all_accessible=all_accessible,
                allow_sensitive=False,
                allow_overwrite=_file_operation_mutates_existing(
                    grant.arguments,
                    snapshot,
                ),
                max_risk=risk_level,
            )
        except (AttributeError, TypeError, ValueError):
            return None

    def session_rule_matches(
        self,
        rule: object,
        runtime_context: dict,
    ) -> bool:
        """按 action、路径、敏感性、副作用和风险严格匹配规则。"""
        if not isinstance(rule, FileSessionGrantRule):
            return False
        try:
            if not isinstance(runtime_context, dict):
                return False
            action = runtime_context.get("action")
            normalized_path = runtime_context.get("normalized_path")
            sensitive = runtime_context.get("sensitive")
            overwrite = runtime_context.get("overwrite")
            risk_level = normalize_risk_level(
                runtime_context.get("risk_level")
            )
            if (
                not isinstance(action, str)
                or not isinstance(sensitive, bool)
                or not isinstance(overwrite, bool)
                or action not in rule.actions
                or (sensitive and not rule.allow_sensitive)
                or (overwrite and not rule.allow_overwrite)
                or _RISK_ORDER[risk_level] > _RISK_ORDER[rule.max_risk]
            ):
                return False
            if rule.all_accessible:
                return action in (
                    _FILE_READ_ACTIONS | _FILE_METADATA_ACTIONS
                )
            return (
                isinstance(normalized_path, str)
                and bool(rule.path_under)
                and _path_is_under(normalized_path, rule.path_under)
            )
        except (TypeError, ValueError):
            return False


_FILE_APPROVAL_HANDLER = FileApprovalHandler()


def register_file_approval_handler() -> None:
    """随 File 工具注册唯一 Handler。"""
    from hermes.approval_handlers import (
        get_approval_handler,
        register_approval_handler,
    )

    registered = get_approval_handler("file")
    if registered is None:
        register_approval_handler("file", _FILE_APPROVAL_HANDLER)
    elif registered is not _FILE_APPROVAL_HANDLER:
        raise ValueError("approval handler already registered: file")
