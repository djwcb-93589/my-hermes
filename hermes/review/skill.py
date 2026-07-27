"""尚未注册的 Skill Review Driver。"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping

from hermes.review.contracts import (
    ForegroundReviewEvent,
    ReviewClaim,
    ReviewKind,
    ReviewRunSpec,
)
from hermes.review.skill_store import SkillReviewStore
from hermes.tools import (
    ApprovalMode,
    ExecutionEnvironment,
    ToolPolicy,
    ToolRiskLevel,
)


logger = logging.getLogger(__name__)


SKILL_REVIEW_SYSTEM_PROMPT = "You are a background skill review agent."

SKILL_REVIEW_INSTRUCTION = (
    "Review only the supplied fixed evidence window. First decide whether this "
    "work produced a reusable method. Prefer improving a Skill actually loaded "
    "in this window, then another relevant Skill you are permitted to curate; "
    "create a new Skill only when no suitable Skill exists. Do not store one-off "
    "facts, user preferences, project state, or temporary task progress in a "
    "Skill. Before changing an existing Skill, call skill_view, read its revision "
    "and governance_revision, then call skill_manage with both expected_revision "
    "and expected_governance_revision. Never overwrite a user-managed, system, "
    "external, or pinned Skill. You may only create, edit, patch, write_file, or "
    "remove_file. If this evidence has insufficient reusable value, reply exactly: "
    "Nothing to improve."
)


_MAX_EVIDENCE_TEXT = 1_600
_MAX_TOOL_RESULT_TEXT = 1_000
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:api[_-]?key|token|password|secret)\b"
    r"\s*[:=]\s*['\"]?)(?P<value>[^\s,;\"'\]}]+)"
)
_BEARER_VALUE_RE = re.compile(
    r"(?i)(?P<prefix>authorization\s*[:=]\s*bearer\s+)"
    r"(?P<value>[^\s,;\"'\]}]+)"
)
_RAW_KEY_RE = re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{6,}")


def _redact_sensitive_text(text: str) -> str:
    """在不依赖具体工具模块的前提下移除明确凭证值。"""
    text = _BEARER_VALUE_RE.sub(
        lambda match: f"{match.group('prefix')}<secret>",
        text,
    )
    text = _SENSITIVE_VALUE_RE.sub(
        lambda match: f"{match.group('prefix')}<secret>",
        text,
    )
    return _RAW_KEY_RE.sub("<secret>", text)


def _summarize_text(value: object, *, limit: int) -> str:
    """脱敏并限制证据文本，避免把大输出或二进制内容交给审视模型。"""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = str(value)
    if "\x00" in text:
        return "[binary content omitted]"
    text = _redact_sensitive_text(text).strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}\n[truncated]"


def _tool_call_details(tool_call: object) -> tuple[str, str, str]:
    """从已持久化工具调用中提取名称、参数和调用标识。"""
    if not isinstance(tool_call, Mapping):
        return "unknown", "", ""
    function = tool_call.get("function")
    if not isinstance(function, Mapping):
        return "unknown", "", str(tool_call.get("id", ""))
    name = function.get("name")
    arguments = function.get("arguments")
    return (
        name if isinstance(name, str) and name else "unknown",
        _summarize_text(arguments, limit=_MAX_EVIDENCE_TEXT),
        str(tool_call.get("id", "")),
    )


def _is_tool_error(content: str) -> bool:
    """识别需要保留的结构化或文本工具失败。"""
    if content.lstrip().lower().startswith("(error:"):
        return True
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and (
        payload.get("ok") is False
        or "error" in payload
        or bool(payload.get("error_type"))
    )


def _unique_entries(entries: list[str]) -> list[str]:
    """保留首次出现的证据，裁剪重复日志。"""
    return list(dict.fromkeys(entries))


def _build_review_messages(messages: list[dict]) -> list[dict]:
    """把固定窗口按角色和工具证据整理为单个确定性审视输入。"""
    user_entries: list[str] = []
    assistant_entries: list[str] = []
    tool_entries: list[str] = []
    tool_errors: list[str] = []
    loaded_skill_names: set[str] = set()
    call_names: dict[str, str] = {}

    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        content = _summarize_text(
            message.get("content", ""),
            limit=_MAX_EVIDENCE_TEXT,
        )
        if role == "user" and content:
            user_entries.append(content)
        elif role == "assistant":
            if content:
                assistant_entries.append(content)
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    name, arguments, call_id = _tool_call_details(tool_call)
                    if call_id:
                        call_names[call_id] = name
                    if name == "skill_view":
                        try:
                            parsed_arguments = json.loads(arguments)
                        except (TypeError, ValueError):
                            parsed_arguments = None
                        if isinstance(parsed_arguments, dict):
                            skill_name = parsed_arguments.get("name")
                            if isinstance(skill_name, str) and skill_name:
                                loaded_skill_names.add(skill_name)
                    details = f"Tool call: {name}"
                    if arguments:
                        details = f"{details}\nParameters: {arguments}"
                    tool_entries.append(details)
        elif role == "tool":
            call_id = str(message.get("tool_call_id", ""))
            tool_name = call_names.get(call_id, "unknown")
            result = _summarize_text(
                message.get("content", ""),
                limit=_MAX_TOOL_RESULT_TEXT,
            )
            if not result:
                continue
            entry = f"Tool result: {tool_name}\n{result}"
            if _is_tool_error(result):
                tool_errors.append(entry)
            else:
                tool_entries.append(entry)

    lines = ["Fixed-window Skill Review evidence:"]
    if user_entries:
        lines.append("\nUser goals and corrections:")
        lines.extend(f"- {entry}" for entry in _unique_entries(user_entries))
    if tool_entries:
        lines.append("\nKey tool calls, parameters, and results:")
        lines.extend(f"- {entry}" for entry in _unique_entries(tool_entries))
    if tool_errors:
        lines.append("\nTool errors and failure reasons:")
        lines.extend(f"- {entry}" for entry in _unique_entries(tool_errors))
    if loaded_skill_names:
        lines.append("\nSkills loaded in this window:")
        lines.extend(f"- {name}" for name in sorted(loaded_skill_names))
    if assistant_entries:
        lines.append("\nFinal result from this window:")
        lines.append(assistant_entries[-1])
    else:
        lines.append("\nFinal result from this window:\n[no textual final result]")
    return [{"role": "user", "content": "\n".join(lines)}]


class SkillReviewDriver:
    """将独立的 Skill Review 存储状态适配为通用 Review 运行契约。"""

    kind = ReviewKind.SKILL

    def __init__(
        self,
        *,
        store: SkillReviewStore,
        skill_interval: int,
        claim_ttl_seconds: float,
        retry_cooldown_seconds: float,
        max_iterations: int,
    ):
        self.store = store
        self.skill_interval = skill_interval
        self.claim_ttl_seconds = claim_ttl_seconds
        self.retry_cooldown_seconds = retry_cooldown_seconds
        self.max_iterations = max_iterations

    def record_progress(self, conn, event: ForegroundReviewEvent) -> None:
        if not event.completed:
            return
        message_upto = self.store.get_last_message_id(conn, event.session_id)
        if message_upto is None:
            logger.warning("skill review skipped progress without messages")
            return
        self.store.record_progress(
            conn,
            event.session_id,
            tool_batches=event.tool_batches,
            message_upto=message_upto,
        )

    def claim_due(self, conn, session_id: str) -> ReviewClaim | None:
        raw_claim = self.store.claim_due(
            conn,
            session_id,
            skill_interval=self.skill_interval,
            claim_ttl_seconds=self.claim_ttl_seconds,
        )
        if raw_claim is None:
            return None
        return ReviewClaim(
            kind=ReviewKind.SKILL,
            session_id=raw_claim["session_id"],
            token=raw_claim["claim_token"],
            payload={
                "tool_batch_upto": raw_claim["tool_batch_upto"],
                "message_after": raw_claim["message_after"],
                "message_upto": raw_claim["message_upto"],
            },
        )

    def validate_claim(self, claim: ReviewClaim) -> bool:
        if not isinstance(claim, ReviewClaim) or claim.kind is not ReviewKind.SKILL:
            return False
        if not isinstance(claim.session_id, str) or not claim.session_id.strip():
            return False
        if not isinstance(claim.token, str) or not claim.token:
            return False
        if not isinstance(claim.payload, Mapping):
            return False
        tool_batch_upto = claim.payload.get("tool_batch_upto")
        message_after = claim.payload.get("message_after")
        message_upto = claim.payload.get("message_upto")
        return (
            not isinstance(tool_batch_upto, bool)
            and isinstance(tool_batch_upto, int)
            and tool_batch_upto > 0
            and not isinstance(message_after, bool)
            and isinstance(message_after, int)
            and message_after >= 0
            and not isinstance(message_upto, bool)
            and isinstance(message_upto, int)
            and message_upto > message_after
        )

    def claim_is_valid(self, conn, claim: ReviewClaim) -> bool:
        return self.store.claim_is_valid(conn, claim.session_id, claim.token)

    def prepare_run(self, conn, claim: ReviewClaim) -> ReviewRunSpec:
        if not self.validate_claim(claim):
            raise ValueError("invalid skill review claim")
        if not self.claim_is_valid(conn, claim):
            raise RuntimeError("skill review claim expired before loading")
        window = self.store.load_message_window(
            conn,
            claim.session_id,
            after_message_id=claim.payload["message_after"],
            upto_message_id=claim.payload["message_upto"],
        )
        if not window:
            raise ValueError("skill review message window is empty")
        if not self.claim_is_valid(conn, claim):
            raise RuntimeError("skill review claim expired after loading")
        return ReviewRunSpec(
            messages=_build_review_messages(window),
            system_prompt=SKILL_REVIEW_SYSTEM_PROMPT,
            instruction=SKILL_REVIEW_INSTRUCTION,
            tool_policy=ToolPolicy(
                ExecutionEnvironment.BACKGROUND_REVIEW,
                enabled_toolsets=frozenset({"skill_read", "skill_manage"}),
                unattended=True,
                allowed_approval_modes=frozenset({ApprovalMode.NONE.value}),
                max_risk_level=ToolRiskLevel.MEDIUM,
            ),
            max_iterations=self.max_iterations,
            tool_context={
                "skill_actor": "background_review",
                "interactive_approval": False,
            },
        )

    def complete(self, conn, claim: ReviewClaim) -> bool:
        return self.store.complete(conn, claim.session_id, claim.token)

    def fail(self, conn, claim: ReviewClaim, error: str) -> bool:
        return self.store.fail(
            conn,
            claim.session_id,
            claim.token,
            error=error,
            retry_cooldown_seconds=self.retry_cooldown_seconds,
        )
