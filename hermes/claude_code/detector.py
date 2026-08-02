"""集中使用多条弱证据识别 Claude Code 事件与逻辑状态。"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections import deque
from dataclasses import dataclass, replace

from hermes.claude_code.contracts import (
    ClaudeCodeActionKind,
    ClaudeCodeActionRequired,
    ClaudeCodeEvent,
    ClaudeCodeEventType,
    ClaudeCodeState,
)
from hermes.claude_code.normalizer import (
    NormalizedOutputDelta,
    redact_claude_code_output,
)


MAX_EVENT_TEXT_CHARS = 2_048
MAX_DETECTION_CONTEXT_CHARS = 8_192
MAX_RECENT_EVENT_FINGERPRINTS = 128
MAX_ACTION_OPTIONS = 8
MAX_OUTBOUND_INPUT_EVIDENCE = 16
OUTBOUND_INPUT_TTL_SECONDS = 30.0
OUTBOUND_INPUT_OBSERVATION_BUDGET = 4
OUTBOUND_INPUT_PREFIX_CHARS = 16
RECENT_MATCHED_ECHO_TTL_SECONDS = 10.0
RECENT_MATCHED_ECHO_MATCH_BUDGET = 2
MAX_ECHO_MATCH_LINES = 16
_EVENT_TRUNCATION_MARKER = "\n[… event text truncated …]\n"

_TERMINAL_PROCESS_STATUSES = frozenset(
    {"exited", "killed", "lost", "failed_start"}
)
_EXIT_PROCESS_STATUSES = frozenset({"exited", "killed", "failed_start"})
_INTERRUPT_EXIT_CODES = frozenset({-15, -2, 130})

_READY_RE = re.compile(
    r"(?i)(?:welcome\s+to\s+claude|how\s+can\s+i\s+help|"
    r"what\s+would\s+you\s+like|enter\s+(?:a\s+)?(?:task|prompt)|"
    r"ready\s+(?:for|to\s+accept)|可以开始|请输入任务)"
)
_PROGRESS_RE = re.compile(
    r"(?i)\b(?:analys(?:e|ing)|inspect(?:ing)?|read(?:ing)?|"
    r"search(?:ing)?|edit(?:ing)?|writ(?:e|ing)|creat(?:e|ing)|"
    r"updat(?:e|ing)|implement(?:ing)?|fix(?:ing)?|run(?:ning)?|"
    r"check(?:ing)?|test(?:ing)?|build(?:ing)?|review(?:ing)?)\b|"
    r"(?:正在|开始)(?:分析|读取|检查|修改|执行|运行|测试)"
)
_COMPLETION_RE = re.compile(
    r"(?i)(?:\b(?:completed|finished|done|implemented|resolved)\b|"
    r"\bsuccessfully\b|(?:任务|修改|实现)(?:已经|已)?完成|完成总结)"
)
_NEGATED_COMPLETION_RE = re.compile(
    r"(?i)\b(?:not|isn['’]?t|wasn['’]?t|incomplete)\s+"
    r"(?:successfully\s+)?(?:completed|finished|done)\b|"
    r"\bnothing\s+(?:was\s+)?done\b|"
    r"(?:尚未|未)(?:成功)?(?:完成|结束)"
)
_FAILURE_RE = re.compile(
    r"(?i)(?:\b(?:error|fatal|failed|failure|crash(?:ed)?|exception|"
    r"unable\s+to|cannot|could\s+not|command\s+not\s+found|"
    r"timed\s+out|connection\s+refused|"
    r"authentication\s+(?:failed|required))\b|"
    r"(?:失败|错误|异常|无法继续|命令不存在|需要认证))"
)
_AUTH_RE = re.compile(
    r"(?i)(?:\b(?:sign[ -]?in|log[ -]?in|authenticate|authentication|"
    r"oauth|api[ _-]?key|access[ _-]?token|credential|password|"
    r"passcode|authorization\s+code|verification\s+code|"
    r"device\s+code|one[ -]?time\s+code)\b|"
    r"登录|认证|凭据|令牌|密码|验证码)"
)
_AUTH_REQUIRED_RE = re.compile(
    r"(?i)(?:\b(?:required|please|must|to\s+continue|visit|"
    r"open\s+(?:the\s+)?browser|enter)\b|需要|请登录|请认证)"
)
_DESTRUCTIVE_RE = re.compile(
    r"(?i)(?:\b(?:delete|remove|overwrite|erase|reset|format|drop|"
    r"destroy|purge|force)\b|删除|覆盖|清空|重置|破坏性|强制)"
)
_EXTERNAL_RE = re.compile(
    r"(?i)(?:\b(?:network|internet|external|connect|download|upload|"
    r"fetch|remote|outside\s+(?:the\s+)?workspace)\b|"
    r"网络|外部访问|工作区外|下载|上传)"
)
_APPROVAL_RE = re.compile(
    r"(?i)(?:\b(?:allow|approve|permission|confirm|proceed|continue|"
    r"authorize|grant|run\s+(?:this|the)\s+command|"
    r"modify\s+(?:this|the)\s+file)\b|允许|批准|权限|确认|继续执行)"
)
_CLARIFICATION_RE = re.compile(
    r"(?i)(?:\b(?:clarify|provide|specify|which|what|where|when|how|"
    r"choose|select|need\s+(?:more|additional))\b|"
    r"请提供|请说明|请选择|需要更多|哪个|什么|哪里|如何)"
)
_PROMPT_VERB_RE = re.compile(
    r"(?i)(?:\b(?:enter|select|choose|provide|confirm|allow|approve)\b"
    r"[^\n]{0,120}:\s*$|(?:请输入|请选择|请确认|是否允许)[^\n]*$)"
)
_INLINE_OPTION_RE = re.compile(
    r"(?i)(?:\[(?:y|yes)/(?:n|no)\]|\((?:y|yes)/(?:n|no)\)|"
    r"\byes\s*/\s*no\b|\ballow\s*/\s*deny\b)"
)
_LINE_OPTION_RE = re.compile(
    r"(?m)^\s*(?:\d{1,2}[.)]|[-*]|\[[ xX]?\])\s+(.{1,160})\s*$"
)
_ECHO_PROMPT_PREFIX_RE = re.compile(
    r"(?i)^\s*(?:[│┃|]\s*)?"
    r"(?:(?:you|human|user)\s*:\s*|[>❯›»$#]+\s*)"
)


@dataclass(frozen=True, slots=True)
class _OutboundInputEvidence:
    """不含明文的短生命周期 outbound input 证据。"""

    fingerprint: str
    normalized_length: int
    prefix_fingerprints: tuple[str, ...]
    input_kind: str
    sent_at: float
    cursor_before: int
    cursor_after: int
    remaining_observations: int
    line_id: int


@dataclass(frozen=True, slots=True)
class _MatchedEchoEvidence:
    """短暂吸收同一 PTY echo 的有限重复重绘。"""

    fingerprint: str
    normalized_length: int
    prefix_fingerprints: tuple[str, ...]
    expires_at: float
    remaining_matches: int


@dataclass(frozen=True, slots=True)
class _EchoIsolationResult:
    """保留完整输出，同时只把非 echo 文本交给语义检测。"""

    semantic_text: str
    metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """检测器对单次观察给出的新事件、状态和待决动作。"""

    state: ClaudeCodeState
    events: tuple[ClaudeCodeEvent, ...]
    action_required: ClaudeCodeActionRequired | None
    activity_detected: bool = False


class ClaudeCodeOutputDetector:
    """按进程事实、增量文本和提示结构组合判断，未知时安全降级。"""

    def __init__(self) -> None:
        self._state = ClaudeCodeState.STARTING
        self._action_required: ClaudeCodeActionRequired | None = None
        self._last_action_fingerprint: str | None = None
        self._recent_fingerprints: deque[str] = deque()
        self._recent_fingerprint_set: set[str] = set()
        self._completion_seen = False
        self._failure_seen = False
        self._progress_seen = False
        self._ready_seen = False
        self._last_process_status: str | None = None
        self._context_complete = True
        self._semantic_context = ""
        self._last_semantic_context_fingerprint = ""
        self._outbound_response_pending = False
        self._input_fingerprint_key = secrets.token_bytes(32)
        self._outbound_inputs: deque[_OutboundInputEvidence] = deque(
            maxlen=MAX_OUTBOUND_INPUT_EVIDENCE
        )
        self._matched_echoes: deque[_MatchedEchoEvidence] = deque(
            maxlen=MAX_OUTBOUND_INPUT_EVIDENCE
        )
        self._pending_input_hasher = self._new_input_hasher()
        self._pending_input_prefix_hasher = self._new_input_hasher()
        self._pending_input_prefix_fingerprints: list[str] = []
        self._next_input_line_id = 1
        self._pending_input_line_id = self._next_input_line_id
        self._pending_input_length = 0
        self._pending_input_cursor_before = 0
        self._pending_input_last_sent_at = 0.0
        self._pending_input_previous_was_cr = False

    def begin_task(self) -> None:
        """记录首个任务已送达，不保存任务文本。"""

        self._state = ClaudeCodeState.STARTING
        self._action_required = None
        self._last_action_fingerprint = None
        self._completion_seen = False
        self._failure_seen = False
        self._progress_seen = False
        self._ready_seen = False
        self._context_complete = True
        self._outbound_response_pending = False
        self._clear_semantic_context()
        self._clear_recent_event_fingerprints()

    def acknowledge_input(self) -> None:
        """输入明确送达后清除旧提示，但不保存回答内容。"""

        self._action_required = None
        self._last_action_fingerprint = None
        self._completion_seen = False
        self._failure_seen = False
        self._progress_seen = False
        self._ready_seen = False
        self._state = ClaudeCodeState.STARTING
        self._clear_semantic_context()
        self._clear_recent_event_fingerprints()

    def acknowledge_interrupt(self) -> None:
        """中断明确送达后使当前待处理动作失效，保留观察历史。"""

        self._action_required = None
        self._last_action_fingerprint = None
        if self._state in {
            ClaudeCodeState.WAITING_INPUT,
            ClaudeCodeState.WAITING_APPROVAL,
        }:
            self._state = ClaudeCodeState.UNKNOWN

    def record_outbound_input(
        self,
        data: str,
        *,
        input_kind: str,
        sent_at: float,
        cursor_before: int,
        cursor_after: int,
    ) -> None:
        """只保存脱敏规范化 HMAC，不保留 outbound input 明文。"""

        if not isinstance(data, str):
            raise TypeError("outbound input must be text")
        if input_kind not in {"write", "submit"}:
            raise ValueError("input_kind must be write or submit")
        if cursor_before < 0 or cursor_after < cursor_before:
            raise ValueError("outbound input cursor range is invalid")
        if self._action_required is not None and (
            input_kind == "submit" or "\r" in data or "\n" in data
        ):
            self._outbound_response_pending = True

        if (
            self._pending_input_length
            and sent_at - self._pending_input_last_sent_at
            > OUTBOUND_INPUT_TTL_SECONDS
        ):
            self._reset_pending_input()

        safe_data = redact_claude_code_output(data)
        for character in safe_data:
            if character == "\n":
                if self._pending_input_previous_was_cr:
                    self._pending_input_previous_was_cr = False
                    continue
                self._capture_pending_input(
                    input_kind=input_kind,
                    sent_at=sent_at,
                    cursor_after=cursor_after,
                )
                self._reset_pending_input()
                continue
            if character == "\r":
                self._capture_pending_input(
                    input_kind=input_kind,
                    sent_at=sent_at,
                    cursor_after=cursor_after,
                )
                self._reset_pending_input()
                self._pending_input_previous_was_cr = True
                continue

            self._pending_input_previous_was_cr = False
            if character.isspace():
                continue
            if self._pending_input_length == 0:
                self._pending_input_cursor_before = cursor_before
            encoded = character.encode("utf-8", errors="replace")
            self._pending_input_hasher.update(encoded)
            self._pending_input_length += 1
            if (
                len(self._pending_input_prefix_fingerprints)
                < OUTBOUND_INPUT_PREFIX_CHARS
            ):
                self._pending_input_prefix_hasher.update(encoded)
                self._pending_input_prefix_fingerprints.append(
                    self._pending_input_prefix_hasher.copy().hexdigest()
                )

        if self._pending_input_length:
            self._pending_input_last_sent_at = sent_at
            self._capture_pending_input(
                input_kind=input_kind,
                sent_at=sent_at,
                cursor_after=cursor_after,
            )
        if input_kind == "submit":
            self._reset_pending_input()

    def _capture_pending_input(
        self,
        *,
        input_kind: str,
        sent_at: float,
        cursor_after: int,
    ) -> None:
        if self._pending_input_length == 0:
            return
        evidence = _OutboundInputEvidence(
            fingerprint=self._pending_input_hasher.copy().hexdigest(),
            normalized_length=self._pending_input_length,
            prefix_fingerprints=tuple(
                self._pending_input_prefix_fingerprints
            ),
            input_kind=input_kind,
            sent_at=sent_at,
            cursor_before=self._pending_input_cursor_before,
            cursor_after=cursor_after,
            remaining_observations=OUTBOUND_INPUT_OBSERVATION_BUDGET,
            line_id=self._pending_input_line_id,
        )
        self._outbound_inputs = deque(
            (
                existing
                for existing in self._outbound_inputs
                if existing.line_id != evidence.line_id
            ),
            maxlen=MAX_OUTBOUND_INPUT_EVIDENCE,
        )
        self._outbound_inputs.append(evidence)

    def _reset_pending_input(self) -> None:
        self._pending_input_hasher = self._new_input_hasher()
        self._pending_input_prefix_hasher = self._new_input_hasher()
        self._pending_input_prefix_fingerprints = []
        self._next_input_line_id += 1
        self._pending_input_line_id = self._next_input_line_id
        self._pending_input_length = 0
        self._pending_input_cursor_before = 0
        self._pending_input_last_sent_at = 0.0
        self._pending_input_previous_was_cr = False

    def _new_input_hasher(self) -> hmac.HMAC:
        return hmac.new(
            self._input_fingerprint_key,
            digestmod=hashlib.sha256,
        )

    def _clear_outbound_input_evidence(self) -> None:
        self._outbound_inputs.clear()
        self._matched_echoes.clear()
        self._outbound_response_pending = False
        self._reset_pending_input()

    def _isolate_input_echo(
        self,
        candidate: str,
        *,
        cursor_start: int,
        cursor_end: int,
        timestamp: float,
        enabled: bool,
    ) -> _EchoIsolationResult:
        self._prune_outbound_input_evidence(timestamp)
        if not candidate or not enabled:
            return _EchoIsolationResult(candidate, {})

        eligible = tuple(
            evidence
            for evidence in self._outbound_inputs
            if cursor_end > max(evidence.cursor_after, cursor_start)
        )
        lines = candidate.splitlines()
        matched_lines: set[int] = set()
        matched_keys: set[tuple[str, int]] = set()
        matched_prefixes: dict[tuple[str, int], tuple[str, ...]] = {}
        matched_line_ids: set[int] = set()
        matched_recent: set[tuple[str, int]] = set()
        suspected_lines: set[int] = set()
        suspected_line_ids: set[int] = set()

        for evidence in reversed(eligible):
            key = (evidence.fingerprint, evidence.normalized_length)
            if key in matched_keys or evidence.line_id in matched_line_ids:
                continue
            line_indexes = self._find_echo_lines(
                lines,
                fingerprint=evidence.fingerprint,
                normalized_length=evidence.normalized_length,
                claimed=matched_lines,
            )
            if line_indexes:
                matched_lines.update(line_indexes)
                matched_keys.add(key)
                matched_prefixes[key] = evidence.prefix_fingerprints
                matched_line_ids.add(evidence.line_id)

        for evidence in tuple(self._matched_echoes):
            key = (evidence.fingerprint, evidence.normalized_length)
            line_indexes = self._find_echo_lines(
                lines,
                fingerprint=evidence.fingerprint,
                normalized_length=evidence.normalized_length,
                claimed=matched_lines,
            )
            if line_indexes:
                matched_lines.update(line_indexes)
                matched_recent.add(key)

        for evidence in reversed(eligible):
            if evidence.line_id in matched_line_ids:
                continue
            line_indexes = self._find_echo_prefix_lines(
                lines,
                fingerprints=evidence.prefix_fingerprints,
                claimed=matched_lines | suspected_lines,
            )
            if line_indexes:
                suspected_lines.update(line_indexes)
                suspected_line_ids.add(evidence.line_id)
        for evidence in tuple(self._matched_echoes):
            line_indexes = self._find_echo_prefix_lines(
                lines,
                fingerprints=evidence.prefix_fingerprints,
                claimed=matched_lines | suspected_lines,
            )
            if line_indexes:
                suspected_lines.update(line_indexes)

        self._advance_outbound_input_evidence(
            eligible=eligible,
            matched_keys=matched_keys,
            matched_prefixes=matched_prefixes,
            matched_line_ids=matched_line_ids,
            matched_recent=matched_recent,
            preserved_line_ids=suspected_line_ids,
            timestamp=timestamp,
        )
        if matched_lines:
            semantic_text = self._without_lines(
                lines,
                matched_lines | suspected_lines,
            )
            metadata: dict[str, object] = {
                "source": "input_echo" if not semantic_text else "mixed",
                "input_echo_lines": len(matched_lines),
            }
            if suspected_lines:
                metadata["input_echo_unconfirmed"] = True
            if semantic_text:
                metadata["contains_input_echo"] = True
            return _EchoIsolationResult(semantic_text, metadata)

        if suspected_lines:
            semantic_text = self._without_lines(lines, suspected_lines)
            metadata = {
                "source": (
                    "unconfirmed_after_input"
                    if not semantic_text
                    else "mixed"
                ),
                "input_echo_unconfirmed": True,
            }
            return _EchoIsolationResult(semantic_text, metadata)

        return _EchoIsolationResult(candidate, {})

    def _prune_outbound_input_evidence(self, timestamp: float) -> None:
        self._outbound_inputs = deque(
            (
                evidence
                for evidence in self._outbound_inputs
                if evidence.remaining_observations > 0
                and max(0.0, timestamp - evidence.sent_at)
                <= OUTBOUND_INPUT_TTL_SECONDS
            ),
            maxlen=MAX_OUTBOUND_INPUT_EVIDENCE,
        )
        self._matched_echoes = deque(
            (
                evidence
                for evidence in self._matched_echoes
                if evidence.remaining_matches > 0
                and timestamp <= evidence.expires_at
            ),
            maxlen=MAX_OUTBOUND_INPUT_EVIDENCE,
        )

    def _advance_outbound_input_evidence(
        self,
        *,
        eligible: tuple[_OutboundInputEvidence, ...],
        matched_keys: set[tuple[str, int]],
        matched_prefixes: dict[tuple[str, int], tuple[str, ...]],
        matched_line_ids: set[int],
        matched_recent: set[tuple[str, int]],
        preserved_line_ids: set[int],
        timestamp: float,
    ) -> None:
        eligible_ids = {id(evidence) for evidence in eligible}
        remaining_inputs: deque[_OutboundInputEvidence] = deque(
            maxlen=MAX_OUTBOUND_INPUT_EVIDENCE
        )
        for evidence in self._outbound_inputs:
            key = (evidence.fingerprint, evidence.normalized_length)
            if key in matched_keys or evidence.line_id in matched_line_ids:
                continue
            if evidence.line_id in preserved_line_ids:
                remaining_inputs.append(evidence)
                continue
            if id(evidence) not in eligible_ids:
                remaining_inputs.append(evidence)
                continue
            remaining = evidence.remaining_observations - 1
            if remaining > 0:
                remaining_inputs.append(
                    replace(evidence, remaining_observations=remaining)
                )
        self._outbound_inputs = remaining_inputs

        recent: deque[_MatchedEchoEvidence] = deque(
            maxlen=MAX_OUTBOUND_INPUT_EVIDENCE
        )
        for evidence in self._matched_echoes:
            key = (evidence.fingerprint, evidence.normalized_length)
            if key in matched_keys:
                continue
            remaining = evidence.remaining_matches
            if key in matched_recent:
                remaining -= 1
            if remaining > 0:
                recent.append(
                    replace(evidence, remaining_matches=remaining)
                )
        for fingerprint, normalized_length in matched_keys:
            recent.append(
                _MatchedEchoEvidence(
                    fingerprint=fingerprint,
                    normalized_length=normalized_length,
                    prefix_fingerprints=matched_prefixes[
                        (fingerprint, normalized_length)
                    ],
                    expires_at=(
                        timestamp + RECENT_MATCHED_ECHO_TTL_SECONDS
                    ),
                    remaining_matches=RECENT_MATCHED_ECHO_MATCH_BUDGET,
                )
            )
        self._matched_echoes = recent

    def _find_echo_lines(
        self,
        lines: list[str],
        *,
        fingerprint: str,
        normalized_length: int,
        claimed: set[int],
    ) -> set[int]:
        scan_limit = min(len(lines), MAX_ECHO_MATCH_LINES)
        for start in range(scan_limit):
            if start in claimed:
                continue
            variants = (lines[start],)
            without_prefix = _ECHO_PROMPT_PREFIX_RE.sub(
                "",
                lines[start],
                count=1,
            )
            if without_prefix != lines[start]:
                variants += (without_prefix,)
            for first_line in variants:
                hasher = self._new_input_hasher()
                length = self._update_echo_hasher(hasher, first_line)
                for end in range(start, scan_limit):
                    if end in claimed:
                        break
                    if end > start:
                        length += self._update_echo_hasher(
                            hasher,
                            lines[end],
                        )
                    if length > normalized_length:
                        break
                    if length == normalized_length and hmac.compare_digest(
                        hasher.copy().hexdigest(),
                        fingerprint,
                    ):
                        return set(range(start, end + 1))
        return set()

    def _find_echo_prefix_lines(
        self,
        lines: list[str],
        *,
        fingerprints: tuple[str, ...],
        claimed: set[int],
    ) -> set[int]:
        if not fingerprints:
            return set()
        normalized_length = len(fingerprints)
        scan_limit = min(len(lines), MAX_ECHO_MATCH_LINES)
        for start in range(scan_limit):
            if start in claimed:
                continue
            variants = (lines[start],)
            without_prefix = _ECHO_PROMPT_PREFIX_RE.sub(
                "",
                lines[start],
                count=1,
            )
            if without_prefix != lines[start]:
                variants += (without_prefix,)
            for first_line in variants:
                hasher = self._new_input_hasher()
                length = 0
                matched_end: int | None = None
                for end in range(start, scan_limit):
                    if end in claimed:
                        break
                    line = first_line if end == start else lines[end]
                    for character in redact_claude_code_output(line):
                        if character.isspace():
                            continue
                        if length == normalized_length:
                            break
                        hasher.update(
                            character.encode("utf-8", errors="replace")
                        )
                        length += 1
                    if length and hmac.compare_digest(
                        hasher.copy().hexdigest(),
                        fingerprints[length - 1],
                    ):
                        matched_end = end
                    elif matched_end is not None:
                        break
                    if length == normalized_length:
                        break
                if matched_end is not None:
                    return set(range(start, matched_end + 1))
        return set()

    @staticmethod
    def _update_echo_hasher(hasher: hmac.HMAC, text: str) -> int:
        length = 0
        safe_text = redact_claude_code_output(text)
        for character in safe_text:
            if character.isspace():
                continue
            hasher.update(character.encode("utf-8", errors="replace"))
            length += 1
        return length

    @staticmethod
    def _without_lines(lines: list[str], excluded: set[int]) -> str:
        return "\n".join(
            line for index, line in enumerate(lines) if index not in excluded
        ).strip()

    def _clear_semantic_context(self) -> None:
        self._semantic_context = ""
        self._last_semantic_context_fingerprint = ""

    def _append_semantic_context(self, text: str) -> None:
        safe_text = redact_claude_code_output(text).strip()
        if not safe_text:
            return
        fingerprint = hashlib.sha256(
            " ".join(safe_text.split()).casefold().encode("utf-8")
        ).hexdigest()
        if fingerprint == self._last_semantic_context_fingerprint:
            return
        combined = f"{self._semantic_context}\n{safe_text}".strip()
        self._semantic_context = combined[-MAX_DETECTION_CONTEXT_CHARS:]
        self._last_semantic_context_fingerprint = fingerprint

    def detect(
        self,
        *,
        process_id: str,
        delta: NormalizedOutputDelta,
        process_status: str | None,
        exit_code: int | None,
        timestamp: float,
        task_submitted: bool,
        interrupt_requested: bool,
        lost: bool = False,
        observation_errors: tuple[tuple[str, str, str], ...] = (),
    ) -> DetectionResult:
        """生成本轮新事件；不轮询、不输入，也不执行任何审批。"""

        events: list[ClaudeCodeEvent] = []
        previous_action_fingerprint = self._last_action_fingerprint
        output_candidate = redact_claude_code_output(delta.text).strip()

        if delta.cursor_gap:
            self._context_complete = False
            self._action_required = None
            self._last_action_fingerprint = None
            self._completion_seen = False
            self._failure_seen = False
            self._progress_seen = False
            self._ready_seen = False
            self._clear_semantic_context()
            self._clear_outbound_input_evidence()
            self._clear_recent_event_fingerprints()
            self._append_event(
                events,
                event_type=ClaudeCodeEventType.CURSOR_GAP,
                process_id=process_id,
                cursor_start=(
                    delta.gap_start
                    if delta.gap_start is not None
                    else delta.cursor_start
                ),
                cursor_end=(
                    delta.gap_end
                    if delta.gap_end is not None
                    else delta.cursor_start
                ),
                timestamp=timestamp,
                text="ProcessManager output has a cursor gap",
                metadata={
                    "context_complete": False,
                    "next_page_cursor_start": delta.cursor_start,
                },
            )

        for phase, error_type, safe_message in observation_errors:
            self._append_event(
                events,
                event_type=ClaudeCodeEventType.READ_ERROR,
                process_id=process_id,
                cursor_start=delta.cursor_start,
                cursor_end=delta.cursor_end,
                timestamp=timestamp,
                text=safe_message,
                metadata={"phase": phase, "error_type": error_type},
            )
        if observation_errors:
            self._action_required = None
            self._last_action_fingerprint = None

        isolation = self._isolate_input_echo(
            output_candidate,
            cursor_start=delta.cursor_start,
            cursor_end=delta.cursor_end,
            timestamp=timestamp,
            enabled=not delta.cursor_gap,
        )
        candidate = isolation.semantic_text.strip()
        if output_candidate:
            output_metadata = dict(isolation.metadata)
            if delta.limits_hit:
                output_metadata["limits_hit"] = delta.limits_hit
            self._append_event(
                events,
                event_type=ClaudeCodeEventType.OUTPUT,
                process_id=process_id,
                cursor_start=delta.cursor_start,
                cursor_end=delta.cursor_end,
                timestamp=timestamp,
                text=output_candidate,
                metadata=output_metadata,
            )
        elif delta.limits_hit:
            self._append_event(
                events,
                event_type=ClaudeCodeEventType.OUTPUT,
                process_id=process_id,
                cursor_start=delta.cursor_start,
                cursor_end=delta.cursor_end,
                timestamp=timestamp,
                text="Claude Code output reached a normalization limit",
                metadata={"limits_hit": delta.limits_hit},
            )

        if candidate and not delta.cursor_gap:
            self._append_semantic_context(candidate)
        context = self._semantic_context[-MAX_DETECTION_CONTEXT_CHARS:]
        prompt_structure = bool(
            candidate and self._looks_like_prompt(candidate)
        )
        progress_signal = bool(candidate and _PROGRESS_RE.search(candidate))
        completion_signal = bool(
            candidate
            and _COMPLETION_RE.search(candidate)
            and not _NEGATED_COMPLETION_RE.search(candidate)
            and not prompt_structure
        )
        failure_signal = bool(candidate and _FAILURE_RE.search(candidate))
        ready_signal = bool(candidate and _READY_RE.search(candidate))
        resumed_after_input = bool(
            candidate
            and self._outbound_response_pending
            and not prompt_structure
        )
        if resumed_after_input:
            self._outbound_response_pending = False
            self._action_required = None
            self._last_action_fingerprint = None
            self._progress_seen = True

        if not delta.cursor_gap:
            if progress_signal:
                self._progress_seen = True
                self._append_event(
                    events,
                    event_type=ClaudeCodeEventType.PROGRESS,
                    process_id=process_id,
                    cursor_start=delta.cursor_start,
                    cursor_end=delta.cursor_end,
                    timestamp=timestamp,
                    text=candidate,
                )
            if completion_signal:
                self._completion_seen = True
                self._append_event(
                    events,
                    event_type=ClaudeCodeEventType.COMPLETION_SIGNAL,
                    process_id=process_id,
                    cursor_start=delta.cursor_start,
                    cursor_end=delta.cursor_end,
                    timestamp=timestamp,
                    text=candidate,
                )
            if failure_signal:
                self._failure_seen = True
                self._append_event(
                    events,
                    event_type=ClaudeCodeEventType.FAILURE_SIGNAL,
                    process_id=process_id,
                    cursor_start=delta.cursor_start,
                    cursor_end=delta.cursor_end,
                    timestamp=timestamp,
                    text=candidate,
                )
            if ready_signal:
                self._ready_seen = True

            action_source = (
                candidate
                if ready_signal
                else self._action_source(candidate, context)
            )
            if self._context_complete:
                action = self._classify_action(
                    action_source,
                    delta.cursor_end,
                )
            elif self._looks_like_prompt(candidate):
                action = self._action(
                    ClaudeCodeActionKind.UNKNOWN_PROMPT,
                    "Claude Code emitted a prompt after an output gap",
                    candidate,
                    self._extract_options(candidate),
                    "unknown",
                    delta.cursor_end,
                )
                action_source = candidate
            else:
                action = None
            if (
                not task_submitted
                and ready_signal
                and action is not None
                and action.kind
                in {
                    ClaudeCodeActionKind.CLARIFICATION,
                    ClaudeCodeActionKind.UNKNOWN_PROMPT,
                }
            ):
                action = None
            if action is not None:
                self._outbound_response_pending = False
                self._record_action(
                    events,
                    action=action,
                    source_text=action_source,
                    process_id=process_id,
                    cursor_start=delta.cursor_start,
                    cursor_end=delta.cursor_end,
                    timestamp=timestamp,
                )
            elif candidate and (
                progress_signal
                or completion_signal
                or failure_signal
                or ready_signal
            ):
                self._action_required = None
                self._last_action_fingerprint = None

        if self._process_just_exited(process_status):
            self._append_event(
                events,
                event_type=ClaudeCodeEventType.PROCESS_EXIT,
                process_id=process_id,
                cursor_start=delta.cursor_end,
                cursor_end=delta.cursor_end,
                timestamp=timestamp,
                text="Claude Code process entered a terminal state",
                metadata={
                    "process_status": process_status,
                    "exit_code": exit_code,
                },
            )

        has_observation_error = bool(observation_errors)
        self._state = self._resolve_state(
            process_status=process_status,
            exit_code=exit_code,
            task_submitted=task_submitted,
            interrupt_requested=interrupt_requested,
            lost=lost,
            cursor_gap=delta.cursor_gap,
            has_observation_error=has_observation_error,
            context_complete=self._context_complete,
            candidate=bool(candidate),
            context=context,
            progress_signal=progress_signal,
            completion_signal=completion_signal,
            failure_signal=failure_signal,
            ready_signal=ready_signal,
        )
        if lost or process_status in _TERMINAL_PROCESS_STATUSES:
            self._action_required = None
            self._last_action_fingerprint = None
            self._clear_outbound_input_evidence()
        elif self._state == ClaudeCodeState.WORKING:
            self._action_required = None
            self._last_action_fingerprint = None

        action_changed = (
            previous_action_fingerprint != self._last_action_fingerprint
        )
        activity_detected = action_changed or any(
            self._event_counts_as_activity(event) for event in events
        )
        self._last_process_status = process_status
        return DetectionResult(
            state=self._state,
            events=tuple(events),
            action_required=self._action_required,
            activity_detected=activity_detected,
        )

    def _resolve_state(
        self,
        *,
        process_status: str | None,
        exit_code: int | None,
        task_submitted: bool,
        interrupt_requested: bool,
        lost: bool,
        cursor_gap: bool,
        has_observation_error: bool,
        context_complete: bool,
        candidate: bool,
        context: str,
        progress_signal: bool,
        completion_signal: bool,
        failure_signal: bool,
        ready_signal: bool,
    ) -> ClaudeCodeState:
        if lost or process_status == "lost":
            return ClaudeCodeState.LOST
        if cursor_gap:
            return ClaudeCodeState.UNKNOWN

        terminal = process_status in _TERMINAL_PROCESS_STATUSES
        if terminal:
            if process_status == "failed_start":
                return ClaudeCodeState.FAILED
            if interrupt_requested and (
                process_status == "killed" or exit_code in _INTERRUPT_EXIT_CODES
            ):
                return ClaudeCodeState.INTERRUPTED
            if (
                process_status == "exited"
                and exit_code == 0
                and task_submitted
                and self._completion_seen
                and context_complete
                and not cursor_gap
                and not has_observation_error
            ):
                return ClaudeCodeState.COMPLETED
            if (
                (exit_code is not None and exit_code != 0)
                or self._failure_seen
                or failure_signal
            ):
                return ClaudeCodeState.FAILED
            return ClaudeCodeState.UNKNOWN

        if process_status is None or has_observation_error:
            return ClaudeCodeState.UNKNOWN
        if not context_complete:
            return ClaudeCodeState.UNKNOWN
        if process_status == "starting":
            return ClaudeCodeState.STARTING

        action = self._action_required
        if action is not None:
            if action.kind == ClaudeCodeActionKind.CLARIFICATION:
                return ClaudeCodeState.WAITING_INPUT
            if action.kind == ClaudeCodeActionKind.UNKNOWN_PROMPT:
                return ClaudeCodeState.UNKNOWN
            return ClaudeCodeState.WAITING_APPROVAL

        if not task_submitted and (
            ready_signal or self._ready_seen or _READY_RE.search(context)
        ):
            return ClaudeCodeState.READY
        if (
            task_submitted
            and completion_signal
            and ready_signal
        ):
            return ClaudeCodeState.READY
        if task_submitted and (
            progress_signal
            or self._progress_seen
            or completion_signal
            or self._state == ClaudeCodeState.WORKING
        ):
            return ClaudeCodeState.WORKING
        if candidate:
            return ClaudeCodeState.UNKNOWN
        if self._state in {
            ClaudeCodeState.READY,
            ClaudeCodeState.WORKING,
            ClaudeCodeState.WAITING_INPUT,
            ClaudeCodeState.WAITING_APPROVAL,
            ClaudeCodeState.UNKNOWN,
        }:
            return self._state
        return ClaudeCodeState.STARTING

    def _classify_action(
        self,
        candidate: str,
        cursor: int,
    ) -> ClaudeCodeActionRequired | None:
        if not candidate:
            return None
        prompt_like = self._looks_like_prompt(candidate)
        auth = bool(_AUTH_RE.search(candidate))
        destructive = bool(_DESTRUCTIVE_RE.search(candidate))
        external = bool(_EXTERNAL_RE.search(candidate))
        approval = bool(_APPROVAL_RE.search(candidate))
        clarification = bool(_CLARIFICATION_RE.search(candidate))
        options = self._extract_options(candidate)

        if auth and (
            prompt_like
            or approval
            or clarification
            or _AUTH_REQUIRED_RE.search(candidate)
        ):
            return self._action(
                ClaudeCodeActionKind.AUTHENTICATION,
                "Claude Code requires authentication",
                candidate,
                options,
                "high",
                cursor,
            )
        if destructive and prompt_like and (approval or options):
            return self._action(
                ClaudeCodeActionKind.DESTRUCTIVE_ACTION,
                "Claude Code requests confirmation for a destructive action",
                candidate,
                options,
                "critical",
                cursor,
            )
        if external and prompt_like and (approval or options):
            return self._action(
                ClaudeCodeActionKind.EXTERNAL_ACCESS,
                "Claude Code requests external access",
                candidate,
                options,
                "high",
                cursor,
            )
        if approval and prompt_like:
            return self._action(
                ClaudeCodeActionKind.APPROVAL,
                "Claude Code requests permission or confirmation",
                candidate,
                options,
                "medium",
                cursor,
            )
        if clarification and prompt_like:
            return self._action(
                ClaudeCodeActionKind.CLARIFICATION,
                "Claude Code requests additional information",
                candidate,
                options,
                "low",
                cursor,
            )
        if prompt_like:
            return self._action(
                ClaudeCodeActionKind.UNKNOWN_PROMPT,
                "Claude Code emitted an unclassified interactive prompt",
                candidate,
                options,
                "unknown",
                cursor,
            )
        return None

    @staticmethod
    def _action_source(candidate: str, context: str) -> str:
        """提示或选项跨页出现时补入有界上下文。"""

        if (
            ClaudeCodeOutputDetector._looks_like_prompt(candidate)
            or _LINE_OPTION_RE.search(candidate)
        ):
            contextual = redact_claude_code_output(context).strip()
            if contextual:
                return contextual[-MAX_DETECTION_CONTEXT_CHARS:]
        return candidate

    @staticmethod
    def _action(
        kind: ClaudeCodeActionKind,
        summary: str,
        prompt_text: str,
        options: tuple[str, ...],
        risk: str,
        cursor: int,
    ) -> ClaudeCodeActionRequired:
        return ClaudeCodeActionRequired(
            kind=kind,
            summary=summary,
            prompt_text=ClaudeCodeOutputDetector._bounded_text(prompt_text),
            options=options,
            risk=risk,
            cursor=cursor,
        )

    @staticmethod
    def _looks_like_prompt(text: str) -> bool:
        tail = text.rstrip()[-2_048:]
        lines = [line.strip() for line in tail.splitlines() if line.strip()]
        last_line = lines[-1] if lines else tail.strip()
        question = last_line.endswith(("?", "？"))
        prompt_verb = bool(_PROMPT_VERB_RE.search(tail))
        options = bool(_INLINE_OPTION_RE.search(tail)) or len(
            _LINE_OPTION_RE.findall(tail)
        ) >= 2
        return question or prompt_verb or options

    @staticmethod
    def _extract_options(text: str) -> tuple[str, ...]:
        options: list[str] = []
        inline = _INLINE_OPTION_RE.search(text)
        if inline:
            lowered = inline.group(0).casefold()
            options.extend(
                ("allow", "deny")
                if "allow" in lowered
                else ("yes", "no")
            )
        for match in _LINE_OPTION_RE.finditer(text):
            option = redact_claude_code_output(match.group(1)).strip()
            option = option[:160]
            if option and option not in options:
                options.append(option)
            if len(options) >= MAX_ACTION_OPTIONS:
                break
        return tuple(options[:MAX_ACTION_OPTIONS])

    @staticmethod
    def _action_event_type(
        kind: ClaudeCodeActionKind,
    ) -> ClaudeCodeEventType:
        if kind == ClaudeCodeActionKind.AUTHENTICATION:
            return ClaudeCodeEventType.AUTH_REQUIRED
        if kind == ClaudeCodeActionKind.CLARIFICATION:
            return ClaudeCodeEventType.QUESTION
        if kind == ClaudeCodeActionKind.UNKNOWN_PROMPT:
            return ClaudeCodeEventType.UNKNOWN_PROMPT
        return ClaudeCodeEventType.APPROVAL_REQUEST

    def _record_action(
        self,
        events: list[ClaudeCodeEvent],
        *,
        action: ClaudeCodeActionRequired,
        source_text: str,
        process_id: str,
        cursor_start: int,
        cursor_end: int,
        timestamp: float,
    ) -> None:
        """仅在动作内容实质变化时保存待决动作并生成事件。"""

        action_fingerprint = self._action_fingerprint(action, source_text)
        if action_fingerprint == self._last_action_fingerprint:
            return
        self._action_required = action
        self._last_action_fingerprint = action_fingerprint
        self._append_event(
            events,
            event_type=self._action_event_type(action.kind),
            process_id=process_id,
            cursor_start=cursor_start,
            cursor_end=cursor_end,
            timestamp=timestamp,
            text=action.prompt_text or action.summary,
            metadata={
                "kind": action.kind.value,
                "risk": action.risk,
                "options": action.options,
                "prompt_fingerprint": action_fingerprint,
            },
        )

    def _process_just_exited(self, process_status: str | None) -> bool:
        return (
            process_status in _EXIT_PROCESS_STATUSES
            and self._last_process_status not in _EXIT_PROCESS_STATUSES
        )

    def _clear_recent_event_fingerprints(self) -> None:
        """在可信交互边界丢弃旧去重窗口，不保存事件正文。"""

        self._recent_fingerprints.clear()
        self._recent_fingerprint_set.clear()

    @staticmethod
    def _event_counts_as_activity(event: ClaudeCodeEvent) -> bool:
        if event.event_type != ClaudeCodeEventType.OUTPUT:
            return True
        return event.metadata.get("source") not in {
            "input_echo",
            "unconfirmed_after_input",
        }

    def _append_event(
        self,
        events: list[ClaudeCodeEvent],
        *,
        event_type: ClaudeCodeEventType,
        process_id: str,
        cursor_start: int,
        cursor_end: int,
        timestamp: float,
        text: str,
        metadata: dict[str, object] | None = None,
    ) -> bool:
        full_safe_text = redact_claude_code_output(text)
        safe_text = self._bounded_text(full_safe_text)
        safe_metadata = dict(metadata or {})
        if len(full_safe_text) > MAX_EVENT_TEXT_CHARS:
            safe_metadata["event_text_truncated"] = True
        fingerprint = self._event_fingerprint(
            event_type,
            full_safe_text,
            safe_metadata,
            cursor_start,
            cursor_end,
        )
        if fingerprint in self._recent_fingerprint_set:
            return False
        self._recent_fingerprints.append(fingerprint)
        self._recent_fingerprint_set.add(fingerprint)
        while len(self._recent_fingerprints) > MAX_RECENT_EVENT_FINGERPRINTS:
            removed = self._recent_fingerprints.popleft()
            self._recent_fingerprint_set.discard(removed)
        events.append(
            ClaudeCodeEvent(
                event_type=event_type,
                process_id=process_id,
                cursor_start=cursor_start,
                cursor_end=cursor_end,
                timestamp=timestamp,
                text=safe_text,
                metadata=safe_metadata,
            )
        )
        return True

    @staticmethod
    def _bounded_text(text: str) -> str:
        safe_text = redact_claude_code_output(text)
        if len(safe_text) <= MAX_EVENT_TEXT_CHARS:
            return safe_text
        content_budget = MAX_EVENT_TEXT_CHARS - len(
            _EVENT_TRUNCATION_MARKER
        )
        head_budget = content_budget // 2
        tail_budget = content_budget - head_budget
        return (
            f"{safe_text[:head_budget]}"
            f"{_EVENT_TRUNCATION_MARKER}"
            f"{safe_text[-tail_budget:]}"
        )

    @staticmethod
    def _event_fingerprint(
        event_type: ClaudeCodeEventType,
        text: str,
        metadata: dict[str, object],
        cursor_start: int,
        cursor_end: int,
    ) -> str:
        cursor_identity = (
            (cursor_start, cursor_end)
            if event_type
            in {
                ClaudeCodeEventType.CURSOR_GAP,
                ClaudeCodeEventType.PROCESS_EXIT,
                ClaudeCodeEventType.READ_ERROR,
            }
            else None
        )
        payload = (
            event_type.value,
            " ".join(text.split()).casefold(),
            tuple(sorted((key, repr(value)) for key, value in metadata.items())),
            cursor_identity,
        )
        return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()

    @staticmethod
    def _action_fingerprint(
        action: ClaudeCodeActionRequired,
        source_text: str,
    ) -> str:
        payload = (
            action.kind.value,
            " ".join(action.prompt_text.split()).casefold(),
            tuple(" ".join(option.split()).casefold() for option in action.options),
            action.risk,
            " ".join(
                redact_claude_code_output(source_text).split()
            ).casefold(),
        )
        return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()


__all__ = [
    "MAX_ACTION_OPTIONS",
    "MAX_DETECTION_CONTEXT_CHARS",
    "MAX_ECHO_MATCH_LINES",
    "MAX_EVENT_TEXT_CHARS",
    "MAX_OUTBOUND_INPUT_EVIDENCE",
    "MAX_RECENT_EVENT_FINGERPRINTS",
    "OUTBOUND_INPUT_OBSERVATION_BUDGET",
    "OUTBOUND_INPUT_PREFIX_CHARS",
    "OUTBOUND_INPUT_TTL_SECONDS",
    "RECENT_MATCHED_ECHO_MATCH_BUDGET",
    "RECENT_MATCHED_ECHO_TTL_SECONDS",
    "ClaudeCodeOutputDetector",
    "DetectionResult",
]
