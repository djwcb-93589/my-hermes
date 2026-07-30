"""Computer Use 的低打扰风险判断与一次性审批 Binding。"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hermes.approval_handlers import (
    get_approval_handler,
    register_approval_handler,
)
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
    TrustedApprovalGrant,
    _approval_details,
    approval_binding_fingerprint,
    approval_grant_identity_matches,
    normalize_approval_session_key,
)

from .contracts import ComputerUseAction


_TOOL_NAME = "computer_use"
_ACTION_VALUES = frozenset(action.value for action in ComputerUseAction)
_LOW_ACTIONS = frozenset(
    {
        ComputerUseAction.CAPTURE.value,
        ComputerUseAction.WAIT.value,
        ComputerUseAction.LIST_APPS.value,
        ComputerUseAction.LIST_WINDOWS.value,
        ComputerUseAction.CLICK.value,
        ComputerUseAction.DOUBLE_CLICK.value,
        ComputerUseAction.RIGHT_CLICK.value,
        ComputerUseAction.MIDDLE_CLICK.value,
        ComputerUseAction.SCROLL.value,
        ComputerUseAction.FOCUS_APP.value,
    }
)
_MEDIUM_ACTIONS = frozenset(
    {
        ComputerUseAction.DRAG.value,
        ComputerUseAction.TYPE.value,
        ComputerUseAction.SET_VALUE.value,
        ComputerUseAction.KEY.value,
    }
)
_BINDING_KEYS = frozenset(
    {
        "action",
        "delivery_mode",
        "target",
        "key_combo",
        "text_digest",
        "text_length",
        "risk_level",
    }
)
_TARGET_BINDING_KEYS = frozenset(
    {"app", "window_title", "pid", "window_id"}
)
_KEY_ALIASES = {
    "command": "cmd",
    "cmd": "cmd",
    "⌘": "cmd",
    "control": "ctrl",
    "ctrl": "ctrl",
    "option": "alt",
    "alt": "alt",
    "⌥": "alt",
    "windows": "win",
    "window": "win",
    "win": "win",
    "super": "win",
    "del": "delete",
    "delete": "delete",
}
_KEY_SORT_ORDER = {
    "cmd": 0,
    "ctrl": 1,
    "alt": 2,
    "shift": 3,
    "win": 4,
}
_HARDLINE_COMBINATIONS = (
    frozenset({"win", "l"}),
    frozenset({"ctrl", "alt", "delete"}),
    frozenset({"cmd", "ctrl", "q"}),
    frozenset({"cmd", "shift", "q"}),
    frozenset({"cmd", "alt", "shift", "q"}),
    frozenset({"cmd", "shift", "backspace"}),
    frozenset({"cmd", "alt", "backspace"}),
)
_CLOSE_APP_COMBINATIONS = (
    frozenset({"alt", "f4"}),
    frozenset({"cmd", "q"}),
)
_TERMINAL_MARKERS = (
    "terminal",
    "windows terminal",
    "powershell",
    "pwsh",
    "command prompt",
    "cmd",
    "git bash",
    "bash",
    "zsh",
    "iterm",
    "konsole",
    "xterm",
)
_DANGEROUS_TERMINAL_PATTERNS = (
    re.compile(
        r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:bash|sh|zsh)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\brm\s+-(?=[a-z]*r)(?=[a-z]*f)[a-z]+\s+/",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bsudo\s+rm\s+-(?=[a-z]*r)(?=[a-z]*f)[a-z]+\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bmkfs(?:\.\w+)?\b", re.IGNORECASE),
    re.compile(r"\bdd\b[^\n]*\bof\s*=\s*/dev/", re.IGNORECASE),
    re.compile(
        r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;",
    ),
    re.compile(r"\bfork\s+bomb\b", re.IGNORECASE),
    re.compile(r"\bformat\s+c\s*:", re.IGNORECASE),
    re.compile(r"\b(?:shutdown|reboot|poweroff)\b", re.IGNORECASE),
)
_MAX_APPROVAL_TEXT_SUMMARY = 120
_MAX_SUMMARY_LENGTH = 160


@dataclass(frozen=True, slots=True)
class _OperationClassification:
    """保存当前动作的风险等级、说明和高风险类别。"""

    risk_level: ApprovalRiskLevel
    reason: str
    high_kind: str | None = None


def assess_computer_use_operation(
    arguments: dict,
    *,
    session_key: str,
    target_context: Mapping[str, Any] | None = None,
    remote_approval: bool = False,
    interactive_approval: bool = True,
    approval_grant: object = None,
) -> ApprovalAssessment:
    """基于当前动作和目标窗口生成低打扰审批结论。"""

    normalized_session = normalize_approval_session_key(session_key)
    (
        normalized_arguments,
        target,
        key_combo,
        text,
    ) = _normalize_operation_arguments(
        arguments,
        target_context=target_context,
    )
    classification = _classify_operation(
        action=normalized_arguments["action"],
        target=target,
        key_combo=key_combo,
        text=text,
    )
    binding = _build_binding(
        normalized_arguments,
        target=target,
        key_combo=key_combo,
        text=text,
        risk_level=classification.risk_level,
    )

    if classification.risk_level == CRITICAL:
        decision = DENY
        reason = classification.reason
        error_type = "hardline_denied"
        error = "operation is blocked by the Computer Use hardline policy"
        fatal = True
        decision_source = "hardline_policy"
    elif (
        classification.risk_level == HIGH
        and not _has_explicit_target(target)
    ):
        decision = DENY
        reason = "high-risk Computer Use operation requires a target window"
        error_type = "target_context_required"
        error = "a high-risk Computer Use operation requires a target window"
        fatal = False
        decision_source = "target_binding"
    elif classification.risk_level == HIGH and _grant_matches(
        approval_grant,
        normalized_arguments=normalized_arguments,
        binding=binding,
        session_key=normalized_session,
    ):
        decision = ALLOW
        reason = "trusted once approval grant matches the current operation"
        error_type = None
        error = None
        fatal = False
        decision_source = "once_grant"
    elif classification.risk_level == HIGH:
        if remote_approval or interactive_approval:
            decision = ASK
            reason = classification.reason
            error_type = None
            error = None
            fatal = False
            decision_source = (
                "remote_approval"
                if remote_approval
                else "interactive_approval"
            )
        else:
            decision = DENY
            reason = "high-risk Computer Use operation has no approval path"
            error_type = "approval_unavailable"
            error = "approval is unavailable for this high-risk operation"
            fatal = False
            decision_source = "approval_unavailable"
    else:
        decision = ALLOW
        reason = classification.reason
        error_type = None
        error = None
        fatal = False
        decision_source = "direct_allow"

    allowed_scopes = (
        ("once",) if classification.risk_level == HIGH else ()
    )
    details, fingerprint = _approval_details(
        _TOOL_NAME,
        normalized_arguments,
        session_key=normalized_session,
        binding=binding,
        operation_type=(
            f"computer_use.{normalized_arguments['action']}"
        ),
        risk_level=classification.risk_level,
        reason=reason,
        decision_source=decision_source,
        allowed_scopes=allowed_scopes,
    )
    return ApprovalAssessment(
        tool_name=_TOOL_NAME,
        decision=decision,
        risk_level=classification.risk_level,
        fingerprint=fingerprint,
        reason=reason,
        normalized_arguments=normalized_arguments,
        details=details,
        session_key=normalized_session,
        error_type=error_type,
        error=error,
        fatal=fatal,
    )


def summarize_computer_use_operation(
    arguments: Mapping[str, Any],
    target_context: Mapping[str, Any] | None = None,
) -> str:
    """生成不含截图、元素 token 或完整长文本的审批摘要。"""

    try:
        normalized, target, key_combo, text = _normalize_operation_arguments(
            dict(arguments),
            target_context=target_context,
        )
    except (TypeError, ValueError):
        return "Computer Use operation"

    classification = _classify_operation(
        action=normalized["action"],
        target=target,
        key_combo=key_combo,
        text=text,
    )
    if classification.high_kind == "close_app":
        return _limit_summary(
            "关闭应用："
            f"{_display_key_combo(key_combo or [])}"
            f"（{_display_target(target)}）"
        )
    if classification.high_kind == "terminal_text":
        return _limit_summary(
            "在终端输入高风险命令："
            f"'{_summarize_text(text or '')}'"
        )
    return _limit_summary(f"Computer Use 操作：{normalized['action']}")


class ComputerUseApprovalHandler:
    """重新构造 Computer Use Binding 的一次性审批校验器。"""

    def validate_request_binding(
        self,
        *,
        arguments: dict,
        binding: dict,
        session_key: str,
    ) -> bool:
        """校验待审批请求的动作、目标和风险绑定。"""

        return _valid_computer_use_binding(arguments, binding, session_key)

    def validate_grant_binding(
        self,
        *,
        arguments: dict,
        binding: dict,
        session_key: str,
    ) -> bool:
        """校验一次性 Grant 的动作、目标和风险绑定。"""

        return _valid_computer_use_binding(arguments, binding, session_key)

    def build_session_rule(self, grant: object) -> None:
        """Computer Use 不支持 session grant。"""

        return None

    def session_rule_matches(
        self,
        rule: object,
        runtime_context: dict,
    ) -> bool:
        """Computer Use 永不匹配 session grant 规则。"""

        return False


_COMPUTER_USE_APPROVAL_HANDLER = ComputerUseApprovalHandler()


def register_computer_use_approval_handler() -> None:
    """幂等注册唯一的 Computer Use 审批 Handler。"""

    registered = get_approval_handler(_TOOL_NAME)
    if registered is None:
        register_approval_handler(
            _TOOL_NAME,
            _COMPUTER_USE_APPROVAL_HANDLER,
        )
    elif registered is not _COMPUTER_USE_APPROVAL_HANDLER:
        raise ValueError(
            f"approval handler already registered: {_TOOL_NAME}"
        )


def _normalize_operation_arguments(
    arguments: dict,
    *,
    target_context: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], list[str] | None, str | None]:
    """复制 JSON 参数，并规范化动作、目标、按键和文本字段。"""

    if not isinstance(arguments, dict):
        raise ValueError("computer use arguments must be an object")
    normalized = _json_safe_value(arguments)
    if not isinstance(normalized, dict):
        raise ValueError("computer use arguments must be an object")

    action = _normalize_action(arguments.get("action"))
    normalized["action"] = action
    raw_target = (
        target_context
        if target_context is not None
        else arguments.get("target_context")
    )
    target = _normalize_target_context(raw_target)
    normalized["target_context"] = dict(target)

    raw_delivery_mode = arguments.get("delivery_mode")
    if raw_delivery_mode is None:
        normalized.pop("delivery_mode", None)
    else:
        delivery_mode = str(raw_delivery_mode).strip().casefold()
        if delivery_mode not in {"background", "foreground"}:
            raise ValueError("computer use delivery_mode is invalid")
        normalized["delivery_mode"] = delivery_mode

    key_combo: list[str] | None = None
    if action == ComputerUseAction.KEY.value:
        key_combo = _normalize_key_combo(arguments.get("keys"))
        normalized["keys"] = list(key_combo)

    text: str | None = None
    if action == ComputerUseAction.TYPE.value:
        text = _require_string(arguments.get("text"), field="text")
        normalized["text"] = text
    elif action == ComputerUseAction.SET_VALUE.value:
        text = _require_string(arguments.get("value"), field="value")
        normalized["value"] = text

    return normalized, target, key_combo, text


def _normalize_action(value: object) -> str:
    """将公开动作值收敛为 Computer Use 支持的字符串。"""

    if not isinstance(value, str):
        raise ValueError("computer use action is required")
    action = value.strip().casefold()
    if action not in _ACTION_VALUES:
        raise ValueError("computer use action is invalid")
    return action


def _normalize_target_context(
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """构造可绑定到当前窗口的规范化目标上下文。"""

    if value is not None and not isinstance(value, Mapping):
        raise ValueError("computer use target_context must be an object")
    source = value if isinstance(value, Mapping) else {}
    is_terminal = source.get("is_terminal")
    if is_terminal is not None and type(is_terminal) is not bool:
        raise ValueError("computer use is_terminal must be a boolean")
    return {
        "app": _normalize_target_text(source.get("app")),
        "window_title": _normalize_target_text(
            source.get("window_title")
        ),
        "pid": _normalize_target_identifier(source.get("pid")),
        "window_id": _normalize_target_identifier(source.get("window_id")),
        "is_terminal": is_terminal,
    }


def _normalize_target_text(value: object) -> str:
    """仅接受字符串目标名称，避免将复杂对象写入 Binding。"""

    return value.strip() if isinstance(value, str) else ""


def _normalize_target_identifier(value: object) -> int | None:
    """仅接受非布尔整数的进程或窗口标识。"""

    return value if type(value) is int else None


def _normalize_key_combo(value: object) -> list[str]:
    """无视按键顺序和常见别名，生成稳定的组合键集合。"""

    if isinstance(value, str):
        raw_parts: Sequence[object] = (value,)
    elif isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raw_parts = value
    else:
        raise ValueError("computer use keys must be a string or sequence")

    keys: set[str] = set()
    for raw_part in raw_parts:
        if not isinstance(raw_part, str):
            raise ValueError("computer use keys must contain only strings")
        for part in re.split(r"[+-]", raw_part):
            key = part.strip().casefold()
            if not key:
                raise ValueError("computer use keys must not contain empty values")
            keys.add(_KEY_ALIASES.get(key, key))
    if not keys:
        raise ValueError("computer use keys must not be empty")
    return sorted(
        keys,
        key=lambda key: (_KEY_SORT_ORDER.get(key, 100), key),
    )


def _require_string(value: object, *, field: str) -> str:
    """校验需要完整保留以供摘要和摘要计算的文本字段。"""

    if not isinstance(value, str):
        raise ValueError(f"computer use {field} must be a string")
    return value


def _json_safe_value(value: Any) -> Any:
    """复制为 JSON 可序列化的值，不把未知对象强制转成字符串。"""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("computer use arguments contain a non-finite number")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("computer use argument names must be strings")
            normalized[key] = _json_safe_value(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_json_safe_value(item) for item in value]
    raise ValueError("computer use arguments are not JSON serializable")


def _classify_operation(
    *,
    action: str,
    target: Mapping[str, Any],
    key_combo: Sequence[str] | None,
    text: str | None,
) -> _OperationClassification:
    """按硬阻止、关闭应用和终端文本顺序归类当前动作。"""

    combo = frozenset(key_combo or ())
    if action == ComputerUseAction.KEY.value and any(
        required <= combo for required in _HARDLINE_COMBINATIONS
    ):
        return _OperationClassification(
            risk_level=CRITICAL,
            reason="system-level key combination is hardline denied",
        )
    if action == ComputerUseAction.KEY.value and combo in _CLOSE_APP_COMBINATIONS:
        return _OperationClassification(
            risk_level=HIGH,
            reason="application-closing key combination requires once approval",
            high_kind="close_app",
        )
    if (
        action
        in {ComputerUseAction.TYPE.value, ComputerUseAction.SET_VALUE.value}
        and text is not None
        and _is_terminal_target(target)
        and _is_dangerous_terminal_text(text)
    ):
        return _OperationClassification(
            risk_level=HIGH,
            reason="dangerous terminal command requires once approval",
            high_kind="terminal_text",
        )
    if action in _LOW_ACTIONS:
        return _OperationClassification(
            risk_level=LOW,
            reason="low-impact Computer Use operation is directly allowed",
        )
    if action in _MEDIUM_ACTIONS:
        return _OperationClassification(
            risk_level=MEDIUM,
            reason="ordinary Computer Use operation is directly allowed",
        )
    raise ValueError("computer use action classification is invalid")


def _is_terminal_target(target: Mapping[str, Any]) -> bool:
    """仅在目标明确为终端时启用危险文本判断。"""

    if target.get("is_terminal") is True:
        return True
    app = str(target.get("app") or "").casefold()
    window_title = str(target.get("window_title") or "").casefold()
    target_text = f"{app}\n{window_title}"
    return any(marker in target_text for marker in _TERMINAL_MARKERS)


def _is_dangerous_terminal_text(text: str) -> bool:
    """只匹配少量明确的破坏性终端命令模式。"""

    return any(pattern.search(text) is not None for pattern in _DANGEROUS_TERMINAL_PATTERNS)


def _has_explicit_target(target: Mapping[str, Any]) -> bool:
    """确认高风险操作可以绑定到一个可辨识的目标窗口。"""

    has_identity = bool(target.get("app") or target.get("window_title"))
    pid = target.get("pid")
    window_id = target.get("window_id")
    has_numeric_target = (
        (type(pid) is int and pid > 0)
        or (type(window_id) is int and window_id > 0)
    )
    return has_identity or has_numeric_target


def _build_binding(
    normalized_arguments: Mapping[str, Any],
    *,
    target: Mapping[str, Any],
    key_combo: Sequence[str] | None,
    text: str | None,
    risk_level: ApprovalRiskLevel,
) -> dict[str, Any]:
    """以固定字段集合绑定动作、窗口、按键、文本摘要和风险等级。"""

    delivery_mode = normalized_arguments.get("delivery_mode")
    if delivery_mode is not None and not isinstance(delivery_mode, str):
        raise ValueError("computer use delivery_mode is invalid")
    return {
        "action": normalized_arguments["action"],
        "delivery_mode": delivery_mode,
        "target": {
            "app": target["app"],
            "window_title": target["window_title"],
            "pid": target["pid"],
            "window_id": target["window_id"],
        },
        "key_combo": list(key_combo) if key_combo is not None else None,
        "text_digest": _text_digest(text) if text is not None else None,
        "text_length": len(text) if text is not None else None,
        "risk_level": risk_level.value,
    }


def _text_digest(text: str) -> str:
    """使用完整原始文本计算 SHA-256，不将文本写入 Binding。"""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _grant_matches(
    approval_grant: object,
    *,
    normalized_arguments: dict[str, Any],
    binding: dict[str, Any],
    session_key: str,
) -> bool:
    """确认可信 once Grant 仍绑定到当前动作和目标窗口。"""

    try:
        fingerprint = approval_binding_fingerprint(
            _TOOL_NAME,
            normalized_arguments,
            session_key=session_key,
            binding=binding,
        )
        return bool(
            isinstance(approval_grant, TrustedApprovalGrant)
            and approval_grant_identity_matches(
                approval_grant,
                _TOOL_NAME,
                normalized_arguments,
            )
            and approval_grant.scope == "once"
            and approval_grant.session_key == session_key
            and approval_grant.fingerprint == fingerprint
            and approval_grant.binding == binding
            and _COMPUTER_USE_APPROVAL_HANDLER.validate_grant_binding(
                arguments=normalized_arguments,
                binding=binding,
                session_key=session_key,
            )
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _valid_computer_use_binding(
    arguments: dict,
    binding: dict,
    session_key: str,
) -> bool:
    """从当前规范化参数重建严格固定的 Computer Use Binding。"""

    try:
        normalize_approval_session_key(session_key)
        if (
            not isinstance(arguments, dict)
            or not isinstance(binding, dict)
            or set(binding) != _BINDING_KEYS
            or not isinstance(binding.get("target"), dict)
            or set(binding["target"]) != _TARGET_BINDING_KEYS
        ):
            return False
        normalized, target, key_combo, text = _normalize_operation_arguments(
            arguments,
            target_context=None,
        )
        classification = _classify_operation(
            action=normalized["action"],
            target=target,
            key_combo=key_combo,
            text=text,
        )
        if (
            classification.risk_level == HIGH
            and not _has_explicit_target(target)
        ):
            return False
        expected = _build_binding(
            normalized,
            target=target,
            key_combo=key_combo,
            text=text,
            risk_level=classification.risk_level,
        )
        return binding == expected
    except (KeyError, TypeError, ValueError):
        return False


def _display_key_combo(key_combo: Sequence[str]) -> str:
    """将规范化组合键转换为简短的人类可读文本。"""

    labels = {
        "cmd": "Cmd",
        "ctrl": "Ctrl",
        "alt": "Alt",
        "shift": "Shift",
        "win": "Win",
        "delete": "Delete",
        "backspace": "Backspace",
        "escape": "Escape",
        "enter": "Enter",
        "tab": "Tab",
    }
    return "+".join(labels.get(key, key.upper()) for key in key_combo)


def _display_target(target: Mapping[str, Any]) -> str:
    """返回有限长度的目标应用或窗口名称。"""

    return _limit_summary(
        str(target.get("app") or target.get("window_title") or "目标窗口"),
        limit=72,
    )


def _summarize_text(text: str) -> str:
    """展示有限文本摘要，并隐藏明显 Base64 内容。"""

    normalized = re.sub(r"\s+", " ", text).strip()
    if (
        "base64" in normalized.casefold()
        or re.search(r"[A-Za-z0-9+/]{80,}={0,2}", normalized) is not None
    ):
        return "内容已隐藏"
    return _limit_summary(normalized, limit=_MAX_APPROVAL_TEXT_SUMMARY)


def _limit_summary(value: str, *, limit: int = _MAX_SUMMARY_LENGTH) -> str:
    """将审批展示文本限制在固定长度以内。"""

    normalized = value.replace("\x00", " ").strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: max(limit - 1, 0)]}…"
