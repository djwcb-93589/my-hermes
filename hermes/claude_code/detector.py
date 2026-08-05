"""集中使用多条弱证据识别 Claude Code 事件与逻辑状态。"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections import deque
from dataclasses import dataclass, field, replace

from hermes.claude_code.contracts import (
    CLAUDE_CODE_ACTIVE_PROCESS_STATUSES,
    MAX_NATIVE_INTERACTION_OPTIONS,
    MAX_NATIVE_INTERACTION_PROMPT_CHARS,
    ClaudeCodeActionKind,
    ClaudeCodeActionRequired,
    ClaudeCodeEvent,
    ClaudeCodeEventType,
    ClaudeCodeState,
    build_claude_code_action_id,
)
from hermes.claude_code.normalizer import (
    NormalizedOutputDelta,
    redact_claude_code_output,
)


MAX_EVENT_TEXT_CHARS = 2_048
MAX_DETECTION_CONTEXT_CHARS = 8_192
MAX_RECENT_EVENT_FINGERPRINTS = 128
MAX_OUTBOUND_INPUT_EVIDENCE = 16
MAX_SUPPRESSED_ACTION_FINGERPRINTS = 128
MAX_PENDING_INTERACTION_OBSERVATIONS = 4
OUTBOUND_INPUT_TTL_SECONDS = 30.0
OUTBOUND_INPUT_OBSERVATION_BUDGET = 4
OUTBOUND_INPUT_PREFIX_CHARS = 16
RECENT_MATCHED_ECHO_TTL_SECONDS = 10.0
RECENT_MATCHED_ECHO_MATCH_BUDGET = 2
MAX_ECHO_MATCH_LINES = 16
MAX_FOLDER_TRUST_HEADING_GAP_CHARS = 512
MAX_RUNTIME_PERMISSION_PANEL_LINES = 32
_EVENT_TRUNCATION_MARKER = "\n[… event text truncated …]\n"
_INPUT_ECHO_OMITTED_TEXT = "[input echo omitted]"
_CURSOR_GAP_OUTPUT_OMITTED_TEXT = "[output omitted after cursor gap]"

_TERMINAL_PROCESS_STATUSES = frozenset(
    {"exited", "killed", "lost", "failed_start"}
)
_EXIT_PROCESS_STATUSES = frozenset({"exited", "killed", "failed_start"})
_INTERRUPT_EXIT_CODES = frozenset({-15, -2, 130})

_READY_WELCOME_RE = re.compile(
    r"(?i)\bwelcome\s+(?:back|to\s+claude(?:\s+code)?)\b"
)
_READY_MANUAL_MODE_RE = re.compile(
    r"(?i)\bmanual\s+mode\s*(?:(?::|\bis\b)\s*)?(?:on|enabled)\b"
)
_READY_TASK_INPUT_RE = re.compile(
    r"(?im)(?:\bhow\s+can\s+i\s+help\b|"
    r"\bwhat\s+would\s+you\s+like(?:\s+to\s+do)?\b|"
    r"\benter\s+(?:a\s+)?(?:task|prompt)\b|"
    r"\bready\s+(?:for|to\s+accept)\b|"
    r"^\s*(?:[>$❯›»])\s*$|可以开始|请输入任务)"
)
_READY_DOLLAR_INPUT_RE = re.compile(r"(?m)^\s*\$\s*$")
_READY_EFFORT_UI_LINE_RE = re.compile(
    r"(?ix)^\s*effort\s*:\s*"
    r"[a-z][a-z0-9_-]{0,15}\s*[\u00b7\u2022]\s*/\s*effort\s*$"
)
_INTERRUPT_MENU_RE = re.compile(
    r"(?is)(?:"
    r"press\s+ctrl\s*[- ]?c\s+again(?:\s+to\s+exit)?"
    r"|interrupted\b[^\n]{0,160}\bwhat\s+should\s+claude\s+do\s+instead"
    r"|what\s+should\s+claude\s+do\s+instead"
    r")"
)
_READY_UI_LINE_RE = re.compile(
    r"(?ix)^\s*(?:"
    r"welcome\s+(?:back|to\s+claude(?:\s+code)?)|"
    r"manual\s+mode\s*(?:(?::|\bis\b)\s*)?(?:on|enabled)|"
    r"how\s+can\s+i\s+help|"
    r"what\s+would\s+you\s+like(?:\s+to\s+do)?|"
    r"enter\s+(?:a\s+)?(?:task|prompt)|"
    r"ready\s+(?:for|to\s+accept)(?:\s+(?:your\s+)?"
    r"(?:next\s+)?(?:task|prompt))?|"
    r"[>\u276f\u2794\$]"
    r")\s*[!?.,:;]*\s*$"
)
_FOLDER_TRUST_QUESTION_RE = re.compile(
    r"(?i)\b(?:do\s+you\s+trust\s+(?:the\s+)?files?\s+in\s+"
    r"(?:this|the)\s+(?:folder|directory)|"
    r"is\s+this\s+(?:a\s+)?project\b[^\n?]{0,160}\bcreated\b"
    r"[^\n?]{0,160}\btrust\b)"
)
_FOLDER_TRUST_HEADING_RE = re.compile(
    r"(?im)^\s*quick\s+safety\s+check\s*:?\s*$"
)
_FOLDER_TRUST_YES_OPTION_RE = re.compile(
    r"(?im)^\s*y[.)]\s+yes\b[^\n]{0,160}\btrust\b"
)
_FOLDER_TRUST_NO_OPTION_RE = re.compile(
    r"(?im)^\s*n[.)]\s+no\b[^\n]{0,160}\b(?:exit|quit|leave)\b"
)
_FOLDER_TRUST_RESPONSE_RE = re.compile(
    r"(?im)\b(?:enter|select|choose)\s+"
    r"(?:y(?:es)?\s*/\s*n(?:o)?|a(?:lways)?)\s*:\s*$"
)
_RUNTIME_PERMISSION_TITLE_RE = re.compile(
    r"(?im)^\s*(?:"
    r"(?:permission|approval)\s+required\b[^\n]*"
    r"|(?:allow|approve)\s+(?:this\s+)?(?:action|operation|request)"
    r"\b[^\n]*[?？]"
    r"|(?:use\s+(?:tool|skill)\b[^\n]*|run\s+command\b[^\n]*)[?？]"
    r"|(?:权限|批准).*(?:需要|请求|确认)"
    r"|(?:是否(?:允许|批准)).*[？?]"
    r")\s*$"
)
_RUNTIME_PERMISSION_NUMBERED_OPTION_RE = re.compile(
    r"(?m)^\s*(?P<number>\d+)[.)]\s+.+\s*$"
)
_RUNTIME_PERMISSION_RESPONSE_RE = re.compile(
    r"(?im)^\s*(?:"
    r"(?:enter|choose|select)\s+(?:an?\s+)?(?:selection|option)"
    r"(?:\s*\[[^\]\n]{1,64}\])?"
    r"|press\s+enter"
    r"|escape\s+to\s+cancel"
    r")[^\n]{0,160}:?\s*$"
)
_RUNTIME_PERMISSION_ALLOW_OPTION_RE = re.compile(
    r"(?i)\b(?:yes|allow|approve|proceed|continue|grant|run|use)\b|"
    r"(?:允许|批准|继续)"
)
_RUNTIME_PERMISSION_DENY_OPTION_RE = re.compile(
    r"(?i)\b(?:no|deny|reject|cancel|decline|exit|abort)\b|"
    r"(?:拒绝|取消|退出)"
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
    r"(?m)^\s*(?:(?:\d+|[A-Za-z])[.)]|[-*]|\[[ xX]?\])\s+.+\s*$"
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
    semantic_text: str
    display_text: str
    metadata: dict[str, object]
    excluded_lines: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """检测器对单次观察给出的新事件、状态和待决动作。"""

    state: ClaudeCodeState
    events: tuple[ClaudeCodeEvent, ...]
    action_required: ClaudeCodeActionRequired | None
    activity_detected: bool = False
    display_output: str = ""
    discard_interaction_view: bool = field(
        default=False,
        repr=False,
    )


class ClaudeCodeOutputDetector:
    """按进程事实、增量文本和提示结构组合判断，未知时安全降级。"""

    def __init__(self) -> None:
        self._state = ClaudeCodeState.STARTING
        self._action_required: ClaudeCodeActionRequired | None = None
        self._last_action_fingerprint: str | None = None
        self._suppressed_action_fingerprints: deque[str] = deque()
        self._suppressed_action_fingerprint_set: set[str] = set()
        self._recent_fingerprints: deque[str] = deque()
        self._recent_fingerprint_set: set[str] = set()
        self._completion_seen = False
        self._failure_seen = False
        self._progress_seen = False
        self._ready_seen = False
        self._round_activity_seen = False
        self._pending_ready_process_id: str | None = None
        self._pending_ready_session_owner: str | None = None
        self._pending_ready_cursor: int | None = None
        self._pending_ready_evidence: frozenset[str] = frozenset()
        self._pending_ready_fresh_evidence: frozenset[str] = frozenset()
        self._session_ready_process_id: str | None = None
        self._session_ready_session_owner: str | None = None
        self._session_ready_evidence: frozenset[str] = frozenset()
        self._last_process_status: str | None = None
        self._context_complete = True
        self._semantic_context = ""
        self._last_semantic_context_fingerprint = ""
        self._interaction_context = ""
        self._pending_interaction_observations = 0
        self._outbound_response_pending = False
        self._input_fingerprint_key = secrets.token_bytes(32)
        self._native_action_key = secrets.token_bytes(32)
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
        self._clear_suppressed_action_fingerprints()
        self._completion_seen = False
        self._failure_seen = False
        self._progress_seen = False
        self._ready_seen = False
        self._round_activity_seen = False
        self._clear_pending_ready()
        self._context_complete = True
        self._outbound_response_pending = False
        self._clear_semantic_context()
        self._clear_interaction_context()
        self._clear_recent_event_fingerprints()

    def acknowledge_input(self) -> None:
        """输入明确送达后清除旧提示，但不保存回答内容。"""

        self.invalidate_current_action()
        self._clear_suppressed_action_fingerprints()
        self._completion_seen = False
        self._failure_seen = False
        self._progress_seen = False
        self._ready_seen = False
        self._round_activity_seen = False
        self._clear_pending_ready()
        self._state = ClaudeCodeState.STARTING
        self._clear_semantic_context()
        self._clear_recent_event_fingerprints()

    def invalidate_current_action(
        self,
        *,
        suppress_reappearance: bool = False,
    ) -> None:
        """使当前原生提示失效，但保留输出、cursor 和历史事件。"""

        if suppress_reappearance and self._action_required is not None:
            self._remember_suppressed_action_fingerprint(
                self._action_fingerprint(self._action_required)
            )
        self._action_required = None
        self._last_action_fingerprint = None
        self._clear_pending_ready()
        self._clear_interaction_context()
        if self._state in {
            ClaudeCodeState.WAITING_INPUT,
            ClaudeCodeState.WAITING_APPROVAL,
        }:
            self._state = ClaudeCodeState.UNKNOWN

    def acknowledge_interrupt(self) -> None:
        """中断明确送达后使当前待处理动作失效，保留观察历史。"""

        self.invalidate_current_action(suppress_reappearance=True)

    def acknowledge_input_delivery_unknown(self) -> None:
        """送达未知时抑制旧提示的单纯重绘，不推断输入是否成功。"""

        self.invalidate_current_action(suppress_reappearance=True)

    def clear_native_interaction_view(self) -> None:
        """删除当前动作中的原生副本，但保留安全状态事实。"""

        if self._action_required is not None:
            self._action_required = replace(
                self._action_required,
                raw_prompt_text=None,
                raw_options=None,
                native_prompt_fingerprint=None,
            )
            self._last_action_fingerprint = self._action_fingerprint(
                self._action_required
            )
        self._clear_interaction_context()

    def _current_native_interaction(
        self,
    ) -> ClaudeCodeActionRequired | None:
        """仅供 ObservationState 读取当前原生动作，不属于检测结果。"""

        return self._action_required

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
            return _EchoIsolationResult(candidate, candidate, {})

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
            excluded_lines = matched_lines | suspected_lines
            semantic_text = self._without_lines(
                lines,
                excluded_lines,
            )
            metadata: dict[str, object] = {
                "source": "input_echo" if not semantic_text else "mixed",
                "input_echo_lines": len(matched_lines),
            }
            if suspected_lines:
                metadata["input_echo_unconfirmed"] = True
            if semantic_text:
                metadata["contains_input_echo"] = True
            return _EchoIsolationResult(
                semantic_text,
                semantic_text,
                metadata,
                frozenset(excluded_lines),
            )

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
            return _EchoIsolationResult(
                semantic_text,
                semantic_text,
                metadata,
                frozenset(suspected_lines),
            )

        return _EchoIsolationResult(candidate, candidate, {})

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

    def _clear_pending_ready(self) -> None:
        """清除仅用于跨观察确认 READY 的有限结构化证据。"""

        self._pending_ready_process_id = None
        self._pending_ready_session_owner = None
        self._pending_ready_cursor = None
        self._pending_ready_evidence = frozenset()
        self._pending_ready_fresh_evidence = frozenset()

    def _remember_pending_ready(
        self,
        *,
        process_id: str,
        session_owner: str,
        cursor: int,
        evidence: frozenset[str],
        fresh_evidence: frozenset[str],
    ) -> None:
        """只保留进程、游标和有限 READY 类别，不保留输出正文。"""

        allowed_evidence = {"welcome", "manual_mode", "task_input"}
        bounded_evidence = frozenset(
            evidence & allowed_evidence
        )
        bounded_fresh_evidence = frozenset(
            fresh_evidence & allowed_evidence
        )
        if not bounded_evidence or not bounded_fresh_evidence:
            self._clear_pending_ready()
            return
        self._pending_ready_process_id = process_id
        self._pending_ready_session_owner = session_owner
        self._pending_ready_cursor = cursor
        self._pending_ready_evidence = bounded_evidence
        self._pending_ready_fresh_evidence = bounded_fresh_evidence

    def _pending_ready_matches(
        self,
        *,
        process_id: str,
        session_owner: str,
    ) -> bool:
        return (
            self._pending_ready_process_id == process_id
            and self._pending_ready_session_owner == session_owner
            and self._pending_ready_cursor is not None
        )

    def _clear_session_ready_profile(self) -> None:
        """清除绑定当前受管进程的 Session READY 基线。"""

        self._session_ready_process_id = None
        self._session_ready_session_owner = None
        self._session_ready_evidence = frozenset()

    def _session_ready_profile_matches(
        self,
        *,
        process_id: str,
        session_owner: str,
    ) -> bool:
        return (
            self._session_ready_process_id == process_id
            and self._session_ready_session_owner == session_owner
            and len(self._session_ready_evidence) >= 2
        )

    def _remember_session_ready_profile(
        self,
        *,
        process_id: str,
        session_owner: str,
        evidence: frozenset[str],
    ) -> None:
        """仅记录有限 READY 类别，不保存输出或任务正文。"""

        bounded_evidence = frozenset(
            evidence & {"welcome", "manual_mode", "task_input"}
        )
        if len(bounded_evidence) < 2:
            return
        self._session_ready_process_id = process_id
        self._session_ready_session_owner = session_owner
        self._session_ready_evidence = bounded_evidence

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

    def _clear_interaction_context(self) -> None:
        """清理仅供当前原生 Prompt 提取的短暂文本视图。"""

        self._interaction_context = ""
        self._pending_interaction_observations = 0

    def _append_interaction_context(self, text: str) -> None:
        """只保留已按 echo 掩码过滤的有界原生终端文本。"""

        interaction_text = text.strip()
        if not interaction_text:
            return
        combined = f"{self._interaction_context}\n{interaction_text}".strip()
        self._interaction_context = combined[-MAX_NATIVE_INTERACTION_PROMPT_CHARS:]
        self._pending_interaction_observations = (
            MAX_PENDING_INTERACTION_OBSERVATIONS
        )

    def _expire_pending_interaction_context(self) -> bool:
        """限制未形成动作的原生解析上下文跨观察保留时间。"""

        if not self._interaction_context:
            return False
        self._pending_interaction_observations -= 1
        if self._pending_interaction_observations > 0:
            return False
        self._clear_interaction_context()
        return True

    @staticmethod
    def _interaction_candidate(
        *,
        safe_output: str,
        interaction_delta: NormalizedOutputDelta,
        excluded_lines: frozenset[int],
    ) -> str:
        """将安全视图的 echo 行掩码同步应用到并行原生视图。"""

        interaction_output = interaction_delta.text
        if not interaction_output:
            return ""
        if (
            redact_claude_code_output(interaction_output).strip()
            != safe_output
        ):
            return ""
        safe_lines = safe_output.splitlines()
        interaction_lines = interaction_output.splitlines()
        if len(safe_lines) != len(interaction_lines):
            return ""
        return ClaudeCodeOutputDetector._without_lines(
            interaction_lines,
            set(excluded_lines),
        )

    def _native_action_view(
        self,
        action: ClaudeCodeActionRequired,
        *,
        interaction_candidate: str,
        interaction_source: str = "",
        allow_interrupt_fallback: bool = False,
        process_id: str,
        session_owner: str,
        cursor_start: int,
        cursor_end: int,
        timestamp: float,
    ) -> tuple[ClaudeCodeActionRequired, bool]:
        """只在已有安全 Action 后附加短暂原生 Prompt 视图。"""

        had_interaction_input = bool(interaction_candidate or interaction_source)
        if not interaction_source and interaction_candidate:
            interaction_source = self._native_action_source(
                action,
                process_id=process_id,
                session_owner=session_owner,
                cursor_start=cursor_start,
                cursor_end=cursor_end,
                timestamp=timestamp,
            )
        if not interaction_source:
            retained_action = self._retained_native_action_view(
                action,
                interaction_candidate=interaction_candidate,
            )
            if retained_action is not None:
                return retained_action, False
            if allow_interrupt_fallback:
                interaction_source = self._safe_interrupt_menu_source(action)
        if not interaction_source:
            return action, had_interaction_input
        raw_options = self._extract_options(
            interaction_source,
            redact_output=False,
        )[:MAX_NATIVE_INTERACTION_OPTIONS]
        if not raw_options:
            raw_options = action.options[:MAX_NATIVE_INTERACTION_OPTIONS]
        native_prompt_fingerprint = self._native_prompt_fingerprint(
            interaction_source,
            raw_options,
        )
        return replace(
            action,
            action_id=self._native_action_id(
                action,
                native_prompt_fingerprint,
            ),
            raw_prompt_text=interaction_source,
            raw_options=raw_options,
            native_prompt_fingerprint=native_prompt_fingerprint,
        ), False

    def _retained_native_action_view(
        self,
        action: ClaudeCodeActionRequired,
        *,
        interaction_candidate: str,
    ) -> ClaudeCodeActionRequired | None:
        """按未消费 Action 身份保留临时原生视图，兼容同语义重绘。"""

        current = self._action_required
        if (
            current is None
            or current.raw_prompt_text is None
            or current.raw_options is None
            or current.process_id != action.process_id
            or current.session_owner != action.session_owner
            or (
                current.action_id != action.action_id
                and self._safe_action_fingerprint(current)
                != self._safe_action_fingerprint(action)
            )
        ):
            return None
        if current.action_id == action.action_id:
            return current
        safe_fragment = redact_claude_code_output(interaction_candidate).strip()
        if not safe_fragment or not self._action_view_fragment_matches(
            action,
            safe_fragment,
        ):
            return None
        return current

    @classmethod
    def _action_view_fragment_matches(
        cls,
        action: ClaudeCodeActionRequired,
        fragment: str,
    ) -> bool:
        """只接受仍能证明属于同一 Action 的局部原生片段。"""

        if (
            action.kind == ClaudeCodeActionKind.UNKNOWN_PROMPT
            and cls._has_interrupt_menu_semantics(action.prompt_text)
        ):
            return cls._has_interrupt_menu_semantics(fragment)
        if cls._is_runtime_permission_prompt(action.prompt_text):
            return cls._has_runtime_permission_fragment(fragment)
        if cls._has_folder_trust_semantics(action.prompt_text):
            return cls._has_folder_trust_semantics(fragment)
        return bool(
            cls._looks_like_prompt(fragment)
            or _INLINE_OPTION_RE.search(fragment)
            or _LINE_OPTION_RE.search(fragment)
        )

    @classmethod
    def _interrupt_menu_interaction_source(
        cls,
        action: ClaudeCodeActionRequired,
        interaction_delta: NormalizedOutputDelta,
        excluded_lines: frozenset[int],
    ) -> str:
        """从同一 cursor 页提取已遮蔽 echo 的中断菜单范围。"""

        if (
            action.kind != ClaudeCodeActionKind.UNKNOWN_PROMPT
            or not cls._has_interrupt_menu_semantics(action.prompt_text)
            or interaction_delta.cursor_gap
            or not interaction_delta.text
        ):
            return ""
        safe_lines = tuple(
            redact_claude_code_output(line)
            for line in interaction_delta.text.splitlines()
        )
        source_lines = tuple(interaction_delta.text.splitlines())
        if len(safe_lines) != len(source_lines):
            return ""
        selected_lines = cls._interrupt_menu_line_indices(
            "\n".join(safe_lines),
            action.options,
        )
        if not selected_lines or any(
            index in excluded_lines for index in selected_lines
        ):
            return ""
        source = "\n".join(safe_lines[index] for index in selected_lines)
        if not cls._has_interrupt_menu_semantics(source):
            return ""
        return source[-MAX_NATIVE_INTERACTION_PROMPT_CHARS :].strip()

    @classmethod
    def _safe_interrupt_menu_source(
        cls,
        action: ClaudeCodeActionRequired,
    ) -> str:
        """无可靠原生映射时仅保留当前安全 Action 的中断范围。"""

        if (
            action.kind != ClaudeCodeActionKind.UNKNOWN_PROMPT
            or not cls._has_interrupt_menu_semantics(action.prompt_text)
        ):
            return ""
        return cls._bounded_interrupt_menu_source(
            action.prompt_text,
            action.options,
        )

    @classmethod
    def _bounded_interrupt_menu_source(
        cls,
        text: str,
        options: tuple[str, ...],
    ) -> str:
        safe_text = redact_claude_code_output(text).strip()
        if not safe_text:
            return ""
        selected_lines = cls._interrupt_menu_line_indices(safe_text, options)
        if not selected_lines:
            return ""
        source = "\n".join(
            safe_text.splitlines()[index] for index in selected_lines
        )
        return source[-MAX_NATIVE_INTERACTION_PROMPT_CHARS :].strip()

    @staticmethod
    def _interrupt_menu_line_indices(
        text: str,
        options: tuple[str, ...],
    ) -> tuple[int, ...]:
        lines = text.splitlines()
        if not lines:
            return ()
        matches = tuple(_INTERRUPT_MENU_RE.finditer(text))
        if not matches:
            return ()
        marker_line = text[: matches[-1].start()].count("\n")
        option_keys = {
            " ".join(option.split()).casefold()
            for option in options
            if option.strip()
        }

        def is_option_or_gap(index: int) -> bool:
            line = lines[index].strip()
            if not line:
                return True
            if not option_keys:
                return False
            if not (
                _LINE_OPTION_RE.fullmatch(line)
                or _INLINE_OPTION_RE.search(line)
            ):
                return False
            normalized = " ".join(line.split()).casefold()
            return any(
                normalized == option_key
                or normalized.endswith(option_key)
                for option_key in option_keys
            )

        start = marker_line
        while start > 0 and is_option_or_gap(start - 1):
            start -= 1
        end = marker_line + 1
        while end < len(lines) and is_option_or_gap(end):
            end += 1
        return tuple(
            index
            for index in range(start, end)
            if index == marker_line or is_option_or_gap(index)
        )

    def _clear_native_view_for_safe_action(
        self,
        action: ClaudeCodeActionRequired,
    ) -> None:
        """原生增量无法安全映射时仅撤销临时视图，不改变安全动作。"""

        current = self._action_required
        if (
            current is None
            or self._safe_action_fingerprint(current)
            != self._safe_action_fingerprint(action)
            or (
                current.raw_prompt_text is None
                and current.raw_options is None
                and current.native_prompt_fingerprint is None
            )
        ):
            return
        self._action_required = replace(
            current,
            raw_prompt_text=None,
            raw_options=None,
            native_prompt_fingerprint=None,
        )
        self._last_action_fingerprint = self._action_fingerprint(
            self._action_required
        )

    def _native_action_source(
        self,
        action: ClaudeCodeActionRequired,
        *,
        process_id: str,
        session_owner: str,
        cursor_start: int,
        cursor_end: int,
        timestamp: float,
    ) -> str:
        """仅接受可映射回当前安全动作的原生 Prompt 后缀。"""

        context = self._interaction_context
        if (
            action.kind == ClaudeCodeActionKind.UNKNOWN_PROMPT
            and self._has_interrupt_menu_semantics(action.prompt_text)
            and self._has_interrupt_menu_semantics(context)
        ):
            return context[-MAX_NATIVE_INTERACTION_PROMPT_CHARS:]
        folder_trust_source = self._folder_trust_action_source(context)
        if folder_trust_source:
            equivalent_action = self._classify_action(
                redact_claude_code_output(folder_trust_source).strip(),
                process_id=process_id,
                session_owner=session_owner,
                cursor_start=cursor_start,
                cursor_end=cursor_end,
                timestamp=timestamp,
            )
            if (
                equivalent_action is not None
                and self._safe_action_fingerprint(equivalent_action)
                == self._safe_action_fingerprint(action)
            ):
                return folder_trust_source[
                    -MAX_NATIVE_INTERACTION_PROMPT_CHARS:
                ]
        runtime_permission_context = (
            self._runtime_permission_native_source_context(
                action,
                context=context,
            )
        )
        runtime_permission_source = self._runtime_permission_action_source(
            runtime_permission_context,
            maximum_chars=MAX_NATIVE_INTERACTION_PROMPT_CHARS,
        )
        if runtime_permission_source:
            equivalent_action = self._classify_action(
                redact_claude_code_output(runtime_permission_source).strip(),
                process_id=process_id,
                session_owner=session_owner,
                cursor_start=cursor_start,
                cursor_end=cursor_end,
                timestamp=timestamp,
            )
            if (
                equivalent_action is not None
                and self._safe_action_fingerprint(equivalent_action)
                == self._safe_action_fingerprint(action)
            ):
                return runtime_permission_source
        lines = [line for line in context.splitlines() if line.strip()]
        for start in range(len(lines) - 1, -1, -1):
            source = "\n".join(lines[start:])[
                -MAX_NATIVE_INTERACTION_PROMPT_CHARS:
            ]
            equivalent_action = self._classify_action(
                redact_claude_code_output(source).strip(),
                process_id=process_id,
                session_owner=session_owner,
                cursor_start=cursor_start,
                cursor_end=cursor_end,
                timestamp=timestamp,
            )
            if (
                equivalent_action is not None
                and self._safe_action_fingerprint(equivalent_action)
                == self._safe_action_fingerprint(action)
            ):
                return source
        return ""

    def _runtime_permission_native_source_context(
        self,
        action: ClaudeCodeActionRequired,
        *,
        context: str,
    ) -> str:
        """仅用当前临时原生视图补齐同一权限面板的分段重绘。"""

        current = self._action_required
        if (
            not context
            or current is None
            or current.raw_prompt_text is None
            or current.raw_options is None
            or action.kind != ClaudeCodeActionKind.APPROVAL
            or not self._is_runtime_permission_prompt(current.prompt_text)
            or not self._is_runtime_permission_prompt(action.prompt_text)
        ):
            return context
        return self._with_current_candidate(
            current.raw_prompt_text,
            context,
        )

    def _native_prompt_fingerprint(
        self,
        prompt_text: str,
        options: tuple[str, ...],
    ) -> str:
        """用每个 Detector 私有 HMAC 标识原生内容，不保留其明文身份。"""

        payload = (
            "claude-code-native-prompt-v1",
            " ".join(prompt_text.split()),
            tuple(" ".join(option.split()) for option in options),
        )
        return hmac.new(
            self._native_action_key,
            repr(payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _native_action_id(
        self,
        action: ClaudeCodeActionRequired,
        native_prompt_fingerprint: str,
    ) -> str:
        """把临时原生内容的不可逆身份并入不透明 action id。"""

        payload = (
            "claude-code-native-action-v1",
            action.action_id,
            native_prompt_fingerprint,
        )
        return "ccact_" + hmac.new(
            self._native_action_key,
            repr(payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def detect(
        self,
        *,
        process_id: str,
        session_owner: str,
        delta: NormalizedOutputDelta,
        interaction_delta: NormalizedOutputDelta,
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
        if self._pending_ready_process_id is not None and (
            self._pending_ready_process_id != process_id
            or self._pending_ready_session_owner != session_owner
        ):
            self._clear_pending_ready()
        if self._session_ready_process_id is not None and (
            self._session_ready_process_id != process_id
            or self._session_ready_session_owner != session_owner
        ):
            self._clear_session_ready_profile()
        output_candidate = redact_claude_code_output(delta.text).strip()
        interaction_safe_candidate = redact_claude_code_output(
            interaction_delta.text
        ).strip()
        # 原生视图中的秘密值变化可能在安全视图中折叠为同一个
        # <secret>。此时仍需用同一安全投影确认并刷新当前原生提示，
        # 但不把该重复内容写入公开输出或事件。
        semantic_output = output_candidate or interaction_safe_candidate

        if delta.cursor_gap:
            self._context_complete = False
            self._action_required = None
            self._last_action_fingerprint = None
            self._completion_seen = False
            self._failure_seen = False
            self._progress_seen = False
            self._ready_seen = False
            self._round_activity_seen = False
            self._clear_pending_ready()
            self._clear_session_ready_profile()
            self._clear_semantic_context()
            self._clear_interaction_context()
            self._outbound_response_pending = False
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
            self._clear_pending_ready()
            self._clear_session_ready_profile()
            self._clear_interaction_context()
        if delta.limits_hit:
            self._clear_pending_ready()
            self._clear_session_ready_profile()

        outbound_evidence_present = bool(
            self._outbound_inputs or self._matched_echoes
        )
        isolation = self._isolate_input_echo(
            semantic_output,
            cursor_start=delta.cursor_start,
            cursor_end=delta.cursor_end,
            timestamp=timestamp,
            enabled=True,
        )
        candidate = isolation.semantic_text.strip()
        display_output = isolation.display_text.strip()
        interaction_candidate = self._interaction_candidate(
            safe_output=semantic_output,
            interaction_delta=interaction_delta,
            excluded_lines=isolation.excluded_lines,
        )
        if delta.cursor_gap:
            candidate = ""
            interaction_candidate = ""
            display_output = _CURSOR_GAP_OUTPUT_OMITTED_TEXT
            isolation.metadata["source"] = "cursor_gap"
            if outbound_evidence_present:
                isolation.metadata["input_echo_unconfirmed"] = True
        pending_interaction_expired = False
        context_before = self._semantic_context[-MAX_DETECTION_CONTEXT_CHARS:]
        if candidate and not delta.cursor_gap:
            self._append_semantic_context(candidate)
        if interaction_candidate and not delta.cursor_gap:
            self._append_interaction_context(interaction_candidate)
        elif isolation.excluded_lines and not candidate:
            self._clear_interaction_context()
            pending_interaction_expired = True
        elif not delta.cursor_gap:
            pending_interaction_expired = (
                self._expire_pending_interaction_context()
            )
        context = self._semantic_context[-MAX_DETECTION_CONTEXT_CHARS:]
        (
            ready_ui_prefix,
            ready_ui_tail,
            ready_tail_fragment_evidence,
        ) = self._split_ready_ui_tail(candidate)
        has_ready_ui_tail = bool(ready_ui_tail)
        has_non_ready_ui_prefix = bool(ready_ui_prefix)
        action_candidate = ready_ui_prefix if has_ready_ui_tail else candidate
        isolated_dollar_ui = self._is_isolated_dollar_ui(candidate)
        folder_trust_semantics = bool(
            action_candidate
            and self._has_folder_trust_semantics(action_candidate)
        )
        prompt_structure = bool(
            action_candidate
            and (
                self._looks_like_prompt(action_candidate)
                or folder_trust_semantics
            )
        )
        progress_signal = bool(
            action_candidate
            and not folder_trust_semantics
            and _PROGRESS_RE.search(action_candidate)
        )
        completion_signal = bool(
            action_candidate
            and _COMPLETION_RE.search(action_candidate)
            and not _NEGATED_COMPLETION_RE.search(action_candidate)
            and not prompt_structure
        )
        failure_signal = bool(
            action_candidate
            and not folder_trust_semantics
            and _FAILURE_RE.search(action_candidate)
        )
        ready_ui_only = (
            not delta.cursor_gap
            and not delta.limits_hit
            and not observation_errors
            and self._is_ready_ui_only_output(
                candidate,
                context_before=context_before,
                progress_signal=progress_signal,
                completion_signal=completion_signal,
                failure_signal=failure_signal,
            )
        )
        (
            _ready_fragment_evidence,
            _has_ready_ui_fragment,
            has_non_ready_ui_content,
        ) = self._ready_ui_fragment_evidence(candidate)
        ready_signal = bool(
            (ready_ui_tail or candidate)
            and process_status in CLAUDE_CODE_ACTIVE_PROCESS_STATUSES
            and self._is_verified_ready_signal(
                ready_ui_tail or candidate,
                context,
            )
        )
        round_activity_before_observation = self._round_activity_seen
        round_activity_marked = False
        if (
            candidate
            and has_non_ready_ui_content
            and not isolated_dollar_ui
            and not self._looks_like_prompt(action_candidate)
        ):
            # 仅记录本轮确实出现的非 UI 输出，供 Session profile 补足历史证据。
            self._round_activity_seen = True
            round_activity_marked = True
        ready_context_evidence = self._ready_evidence(context_before) & {
            "welcome",
            "manual_mode",
        }
        if folder_trust_semantics or (
            prompt_structure and _AUTH_RE.search(action_candidate)
        ):
            self._clear_session_ready_profile()
        session_profile_matches = self._session_ready_profile_matches(
            process_id=process_id,
            session_owner=session_owner,
        )
        session_profile_fragment_evidence = frozenset(
            ready_tail_fragment_evidence
            & {"welcome", "manual_mode", "task_input"}
        )
        session_profile_common = bool(
            session_profile_matches
            and process_status in CLAUDE_CODE_ACTIVE_PROCESS_STATUSES
            and has_ready_ui_tail
            and session_profile_fragment_evidence
            and self._round_activity_seen
            and not isolated_dollar_ui
            and not delta.cursor_gap
            and not delta.limits_hit
            and not observation_errors
            and self._action_required is None
        )
        session_profile_candidate = bool(
            session_profile_common
            and not has_non_ready_ui_prefix
            and not progress_signal
            and not completion_signal
            and not failure_signal
        )
        session_profile_history_evidence = (
            self._session_ready_evidence
            if session_profile_matches
            else frozenset()
        )
        ready_state_signal = (
            ready_signal
            and ready_ui_only
            and not isolated_dollar_ui
        )
        if (
            task_submitted
            and session_profile_matches
            and not self._round_activity_seen
        ):
            # 已建立 Session profile 后，历史证据不能在没有本轮真实活动时恢复 READY。
            ready_state_signal = False
        if output_candidate:
            output_metadata = dict(isolation.metadata)
            if delta.limits_hit:
                output_metadata["limits_hit"] = delta.limits_hit
            output_metadata["ready_ui_only"] = ready_ui_only
            output_metadata["ui_non_activity"] = bool(
                isolated_dollar_ui
                and not has_non_ready_ui_content
            )
            self._append_event(
                events,
                event_type=ClaudeCodeEventType.OUTPUT,
                process_id=process_id,
                cursor_start=delta.cursor_start,
                cursor_end=delta.cursor_end,
                timestamp=timestamp,
                text=display_output or _INPUT_ECHO_OMITTED_TEXT,
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
                metadata={
                    "limits_hit": delta.limits_hit,
                    "ready_ui_only": False,
                    "ui_non_activity": False,
                },
            )
        resumed_after_input = bool(
            action_candidate
            and self._outbound_response_pending
            and not prompt_structure
        )
        if resumed_after_input:
            # 输入后的首段普通文本可能仍是未匹配的回显，不能单独构成进度。
            self._outbound_response_pending = False
            self._action_required = None
            self._last_action_fingerprint = None
            self._clear_suppressed_action_fingerprints()

        if candidate and not prompt_structure and (
            progress_signal
            or completion_signal
            or failure_signal
            or ready_state_signal
        ):
            self._clear_suppressed_action_fingerprints()

        action: ClaudeCodeActionRequired | None = None
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
            interrupt_menu_action = self._interrupt_menu_action(
                candidate,
                process_id=process_id,
                session_owner=session_owner,
                cursor_start=delta.cursor_start,
                cursor_end=delta.cursor_end,
                timestamp=timestamp,
                process_status=process_status,
                interrupt_requested=interrupt_requested,
            )
            action = interrupt_menu_action
            action_source = action_candidate
            if action is None:
                action_source = (
                    action_candidate
                    if ready_state_signal
                    else self._action_source(action_candidate, context_before)
                )
                if self._context_complete:
                    action = self._classify_action(
                        action_source,
                        process_id=process_id,
                        session_owner=session_owner,
                        cursor_start=delta.cursor_start,
                        cursor_end=delta.cursor_end,
                        timestamp=timestamp,
                    )
                elif self._looks_like_prompt(action_candidate):
                    action = self._action(
                        ClaudeCodeActionKind.UNKNOWN_PROMPT,
                        "Claude Code emitted a prompt after an output gap",
                        action_candidate,
                        "unknown",
                        process_id=process_id,
                        session_owner=session_owner,
                        cursor_start=delta.cursor_start,
                        cursor_end=delta.cursor_end,
                        timestamp=timestamp,
                    )
                    action_source = action_candidate
                else:
                    action = None
            if (
                ready_state_signal
                and action is not None
                and not self._has_interrupt_menu_semantics(action.prompt_text)
                and action.kind
                in {
                    ClaudeCodeActionKind.CLARIFICATION,
                    ClaudeCodeActionKind.UNKNOWN_PROMPT,
                }
                and self._action_required is None
                and not self._has_startup_interaction_semantics(
                    action.prompt_text
                )
            ):
                action = None
            if action is not None or self._action_required is not None:
                ready_state_signal = False
            elif session_profile_candidate:
                ready_state_signal = True
            if (
                round_activity_marked
                and not round_activity_before_observation
                and (action is not None or self._action_required is not None)
            ):
                # 交互提示可能带有普通文本，不能把本次提示误记为任务活动。
                self._round_activity_seen = False
            if action is not None:
                interaction_source = ""
                if interrupt_menu_action is not None:
                    interaction_source = (
                        self._interrupt_menu_interaction_source(
                            action,
                            interaction_delta,
                            isolation.excluded_lines,
                        )
                    )
                action, native_view_unavailable = self._native_action_view(
                    action,
                    interaction_candidate=interaction_candidate,
                    interaction_source=interaction_source,
                    allow_interrupt_fallback=(
                        interrupt_menu_action is not None
                    ),
                    process_id=process_id,
                    session_owner=session_owner,
                    cursor_start=delta.cursor_start,
                    cursor_end=delta.cursor_end,
                    timestamp=timestamp,
                )
                if native_view_unavailable:
                    self._clear_native_view_for_safe_action(action)
                self._outbound_response_pending = False
                self._record_action(
                    events,
                    action=action,
                    process_id=process_id,
                    cursor_start=delta.cursor_start,
                    cursor_end=delta.cursor_end,
                    timestamp=timestamp,
                )
            elif candidate and (
                progress_signal
                or completion_signal
                or failure_signal
                or ready_state_signal
            ):
                self._action_required = None
                self._last_action_fingerprint = None
                self._clear_interaction_context()

            pending_ready_matches = self._pending_ready_matches(
                process_id=process_id,
                session_owner=session_owner,
            )
            if pending_ready_matches:
                pending_evidence = (
                    self._pending_ready_evidence
                    | ready_tail_fragment_evidence
                    | ready_context_evidence
                    | session_profile_history_evidence
                )
                pending_fresh_evidence = (
                    self._pending_ready_fresh_evidence
                    | ready_tail_fragment_evidence
                )
                pending_degraded = bool(
                    self._action_required is not None
                    or action is not None
                    or progress_signal
                    or completion_signal
                    or failure_signal
                    or has_non_ready_ui_content
                )
                pending_confirmation_allowed = bool(
                    not candidate
                    or (
                        ready_tail_fragment_evidence
                        and not isolated_dollar_ui
                    )
                )
                if (
                    process_status not in CLAUDE_CODE_ACTIVE_PROCESS_STATUSES
                    or pending_degraded
                    or self._pending_ready_cursor is None
                    or delta.cursor_end < self._pending_ready_cursor
                    or not pending_fresh_evidence
                ):
                    self._clear_pending_ready()
                elif (
                    pending_confirmation_allowed
                    and len(pending_evidence) >= 2
                ):
                    ready_state_signal = True
                    self._ready_seen = True
                    self._clear_pending_ready()
                else:
                    self._pending_ready_cursor = delta.cursor_end
                    self._pending_ready_evidence = frozenset(
                        pending_evidence
                        & {"welcome", "manual_mode", "task_input"}
                    )
                    self._pending_ready_fresh_evidence = frozenset(
                        pending_fresh_evidence
                        & {"welcome", "manual_mode", "task_input"}
                    )

            if (
                not ready_state_signal
                and process_status in CLAUDE_CODE_ACTIVE_PROCESS_STATUSES
                and has_ready_ui_tail
                and has_non_ready_ui_prefix
                and ready_tail_fragment_evidence
                and not action
                and self._action_required is None
            ):
                self._remember_pending_ready(
                    process_id=process_id,
                    session_owner=session_owner,
                    cursor=delta.cursor_end,
                    evidence=(
                        ready_tail_fragment_evidence
                        | ready_context_evidence
                        | session_profile_history_evidence
                    ),
                    fresh_evidence=ready_tail_fragment_evidence,
                )
            if (
                ready_state_signal
                and process_status in CLAUDE_CODE_ACTIVE_PROCESS_STATUSES
                and not delta.cursor_gap
                and not delta.limits_hit
                and not observation_errors
                and not progress_signal
                and not completion_signal
                and not failure_signal
                and action is None
                and self._action_required is None
            ):
                self._remember_session_ready_profile(
                    process_id=process_id,
                    session_owner=session_owner,
                    evidence=self._ready_evidence(context),
                )
            if ready_state_signal:
                self._ready_seen = True

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
            ready_signal=ready_state_signal,
            allow_ready_history=not (
                bool(candidate)
                and (has_non_ready_ui_content or isolated_dollar_ui)
            ),
        )
        if lost or process_status in _TERMINAL_PROCESS_STATUSES:
            self._action_required = None
            self._last_action_fingerprint = None
            self._clear_pending_ready()
            self._clear_session_ready_profile()
            self._clear_interaction_context()
            self._clear_outbound_input_evidence()
        elif self._state == ClaudeCodeState.WORKING:
            self._action_required = None
            self._last_action_fingerprint = None
            self._clear_interaction_context()

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
            action_required=self._safe_action(self._action_required),
            activity_detected=activity_detected,
            display_output=(
                display_output
                if display_output
                else _INPUT_ECHO_OMITTED_TEXT if output_candidate else ""
            ),
            discard_interaction_view=bool(
                (isolation.excluded_lines and not candidate)
                or pending_interaction_expired
            ),
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
        allow_ready_history: bool,
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

        if ready_signal or (allow_ready_history and self._ready_seen):
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

    def _is_verified_ready_signal(
        self,
        candidate: str,
        context: str,
    ) -> bool:
        """仅在本轮新增文本补齐多条就绪证据时确认 READY。"""

        if self._action_required is not None:
            return False
        if (
            self._has_startup_interaction_semantics(candidate)
            or self._has_startup_interaction_semantics(context)
        ):
            return False
        candidate_evidence = self._ready_evidence(candidate)
        if not candidate_evidence:
            return False
        return len(self._ready_evidence(context)) >= 2

    @classmethod
    def _is_ready_ui_only_output(
        cls,
        candidate: str,
        *,
        context_before: str,
        progress_signal: bool,
        completion_signal: bool,
        failure_signal: bool,
    ) -> bool:
        """只标记已验证且不含任务正文的纯 READY 界面增量。"""

        if not candidate:
            return False
        if progress_signal or completion_signal or failure_signal:
            return False
        lines = tuple(
            line.strip()
            for line in candidate.splitlines()
            if line.strip()
        )
        if not lines or not all(cls._is_ready_ui_line(line) for line in lines):
            return False

        evidence = cls._ready_evidence(candidate)
        if "task_input" not in evidence:
            return True
        trusted_context_evidence = cls._ready_evidence(context_before) & {
            "welcome",
            "manual_mode",
        }
        return bool(
            trusted_context_evidence
            or evidence & {"welcome", "manual_mode"}
        )

    @classmethod
    def _ready_ui_fragment_evidence(
        cls,
        candidate: str,
    ) -> tuple[frozenset[str], bool, bool]:
        """提取 READY UI 行的有限类别，并标记是否混入普通内容。"""

        lines = tuple(
            line.strip()
            for line in candidate.splitlines()
            if line.strip()
        )
        ready_lines = tuple(
            line for line in lines if cls._is_ready_ui_line(line)
        )
        if not ready_lines:
            return frozenset(), False, bool(lines)
        evidence = cls._ready_evidence("\n".join(ready_lines))
        return evidence, True, len(ready_lines) != len(lines)

    @classmethod
    def _split_ready_ui_tail(
        cls,
        candidate: str,
    ) -> tuple[str, str, frozenset[str]]:
        """将正文与尾部连续 READY UI 分开，保留有界文本范围。"""

        lines = candidate.splitlines()
        if not lines:
            return candidate, "", frozenset()
        tail_end = len(lines)
        while tail_end > 0 and not lines[tail_end - 1].strip():
            tail_end -= 1
        tail_start = tail_end
        found_ready_line = False
        while tail_start > 0:
            line = lines[tail_start - 1].strip()
            if line and cls._is_ready_ui_line(line):
                tail_start -= 1
                found_ready_line = True
                continue
            if not line and found_ready_line:
                tail_start -= 1
                continue
            break
        if not found_ready_line:
            return candidate, "", frozenset()
        prefix = "\n".join(lines[:tail_start]).strip()
        tail = "\n".join(lines[tail_start:tail_end]).strip()
        return prefix, tail, cls._ready_evidence(tail)

    @classmethod
    def _ready_ui_tail_fragment_evidence(
        cls,
        candidate: str,
    ) -> tuple[frozenset[str], bool, bool]:
        """提取尾部 READY UI 的类别，并标记是否混入正文。"""

        prefix, tail, evidence = cls._split_ready_ui_tail(candidate)
        return evidence, bool(tail), bool(prefix)

    @staticmethod
    def _is_isolated_dollar_ui(candidate: str) -> bool:
        """只识别规范化后完整独立的美元提示行。"""

        return bool(_READY_DOLLAR_INPUT_RE.fullmatch(candidate.strip()))

    @staticmethod
    def _has_interrupt_menu_semantics(text: str) -> bool:
        """识别本次中断后的有限菜单语义，不依赖历史输出单词。"""

        return bool(_INTERRUPT_MENU_RE.search(text))

    @staticmethod
    def _is_ready_ui_line(line: str) -> bool:
        """确认单行只包含欢迎语、模式提示或已识别的任务输入提示。"""

        normalized = line.strip()
        if not normalized:
            return True
        if (
            _READY_UI_LINE_RE.fullmatch(normalized)
            or _READY_DOLLAR_INPUT_RE.fullmatch(normalized)
            or _READY_EFFORT_UI_LINE_RE.fullmatch(normalized)
        ):
            return True
        return bool(
            any(ord(character) > 127 for character in normalized)
            and _READY_TASK_INPUT_RE.fullmatch(normalized)
        )

    @staticmethod
    def _ready_evidence(text: str) -> frozenset[str]:
        """将欢迎、模式与输入提示分成独立 READY 证据。"""

        evidence: set[str] = set()
        if _READY_WELCOME_RE.search(text):
            evidence.add("welcome")
        if _READY_MANUAL_MODE_RE.search(text):
            evidence.add("manual_mode")
        if (
            _READY_TASK_INPUT_RE.search(text)
            or _READY_DOLLAR_INPUT_RE.search(text)
        ):
            evidence.add("task_input")
        return frozenset(evidence)

    @classmethod
    def _has_startup_interaction_semantics(cls, text: str) -> bool:
        """避免欢迎页历史掩盖本轮新的启动交互。"""

        if not text:
            return False
        if (
            cls._has_folder_trust_semantics(text)
            or _AUTH_RE.search(text)
            or _APPROVAL_RE.search(text)
        ):
            return True
        if _INLINE_OPTION_RE.search(text) or len(
            _LINE_OPTION_RE.findall(text)
        ) >= 2:
            return True
        return cls._looks_like_prompt(text) and not bool(
            _READY_TASK_INPUT_RE.search(text)
        )

    @staticmethod
    def _has_folder_trust_semantics(text: str) -> bool:
        """识别目录信任交互的有界片段，完整性另由组合证据确认。"""

        return bool(
            _FOLDER_TRUST_QUESTION_RE.search(text)
            or _FOLDER_TRUST_HEADING_RE.search(text)
            or _FOLDER_TRUST_YES_OPTION_RE.search(text)
        )

    def _interrupt_menu_action(
        self,
        candidate: str,
        *,
        process_id: str,
        session_owner: str,
        cursor_start: int,
        cursor_end: int,
        timestamp: float,
        process_status: str | None,
        interrupt_requested: bool,
    ) -> ClaudeCodeActionRequired | None:
        """仅在已请求中断且进程仍 active 时生成中断菜单动作。"""

        if not (
            interrupt_requested
            and process_status in CLAUDE_CODE_ACTIVE_PROCESS_STATUSES
            and candidate
            and self._has_interrupt_menu_semantics(candidate)
            and not self._is_runtime_permission_prompt(candidate)
            and not self._has_runtime_permission_semantics(candidate)
        ):
            return None
        return self._action(
            ClaudeCodeActionKind.UNKNOWN_PROMPT,
            "Claude Code interruption menu is waiting for user input",
            candidate,
            "low",
            process_id=process_id,
            session_owner=session_owner,
            cursor_start=cursor_start,
            cursor_end=cursor_end,
            timestamp=timestamp,
        )

    def _classify_action(
        self,
        candidate: str,
        *,
        process_id: str,
        session_owner: str,
        cursor_start: int,
        cursor_end: int,
        timestamp: float,
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
        folder_trust = self._is_folder_trust_prompt(candidate)
        runtime_permission = self._is_runtime_permission_prompt(candidate)
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
                "high",
                process_id=process_id,
                session_owner=session_owner,
                cursor_start=cursor_start,
                cursor_end=cursor_end,
                timestamp=timestamp,
            )
        if folder_trust:
            return self._action(
                ClaudeCodeActionKind.APPROVAL,
                "Claude Code requests folder trust confirmation",
                candidate,
                "medium",
                process_id=process_id,
                session_owner=session_owner,
                cursor_start=cursor_start,
                cursor_end=cursor_end,
                timestamp=timestamp,
            )
        if self._has_folder_trust_semantics(candidate):
            return None
        if self._has_runtime_permission_semantics(candidate) and not (
            runtime_permission
        ):
            return None
        if destructive and prompt_like and (approval or options):
            return self._action(
                ClaudeCodeActionKind.DESTRUCTIVE_ACTION,
                "Claude Code requests confirmation for a destructive action",
                candidate,
                "critical",
                process_id=process_id,
                session_owner=session_owner,
                cursor_start=cursor_start,
                cursor_end=cursor_end,
                timestamp=timestamp,
            )
        if external and prompt_like and (approval or options):
            return self._action(
                ClaudeCodeActionKind.EXTERNAL_ACCESS,
                "Claude Code requests external access",
                candidate,
                "high",
                process_id=process_id,
                session_owner=session_owner,
                cursor_start=cursor_start,
                cursor_end=cursor_end,
                timestamp=timestamp,
            )
        if runtime_permission:
            return self._action(
                ClaudeCodeActionKind.APPROVAL,
                "Claude Code requests runtime permission confirmation",
                candidate,
                "medium",
                process_id=process_id,
                session_owner=session_owner,
                cursor_start=cursor_start,
                cursor_end=cursor_end,
                timestamp=timestamp,
            )
        if (
            approval
            and prompt_like
            and self._has_explicit_approval_prompt(candidate)
        ):
            return self._action(
                ClaudeCodeActionKind.APPROVAL,
                "Claude Code requests permission or confirmation",
                candidate,
                "medium",
                process_id=process_id,
                session_owner=session_owner,
                cursor_start=cursor_start,
                cursor_end=cursor_end,
                timestamp=timestamp,
            )
        if clarification and prompt_like:
            return self._action(
                ClaudeCodeActionKind.CLARIFICATION,
                "Claude Code requests additional information",
                candidate,
                "low",
                process_id=process_id,
                session_owner=session_owner,
                cursor_start=cursor_start,
                cursor_end=cursor_end,
                timestamp=timestamp,
            )
        if prompt_like:
            return self._action(
                ClaudeCodeActionKind.UNKNOWN_PROMPT,
                "Claude Code emitted an unclassified interactive prompt",
                candidate,
                "unknown",
                process_id=process_id,
                session_owner=session_owner,
                cursor_start=cursor_start,
                cursor_end=cursor_end,
                timestamp=timestamp,
            )
        return None

    @staticmethod
    def _is_folder_trust_prompt(text: str) -> bool:
        """用问题、明确 y/n 选项和输入提示组合确认目录信任 Prompt。"""

        return bool(
            _FOLDER_TRUST_QUESTION_RE.search(text)
            and _FOLDER_TRUST_YES_OPTION_RE.search(text)
            and _FOLDER_TRUST_NO_OPTION_RE.search(text)
            and _FOLDER_TRUST_RESPONSE_RE.search(text)
        )

    @staticmethod
    def _has_runtime_permission_fragment(text: str) -> bool:
        """识别可用于恢复运行中权限面板的有界片段。"""

        return bool(
            _RUNTIME_PERMISSION_TITLE_RE.search(text)
            or _RUNTIME_PERMISSION_NUMBERED_OPTION_RE.search(text)
            or _RUNTIME_PERMISSION_RESPONSE_RE.search(text)
        )

    @staticmethod
    def _has_runtime_permission_semantics(text: str) -> bool:
        """仅以明确权限标题或问题阻止不完整面板过早分类。"""

        return bool(_RUNTIME_PERMISSION_TITLE_RE.search(text))

    @staticmethod
    def _is_runtime_permission_prompt(text: str) -> bool:
        """用标题、选项和回复提示的组合确认运行中权限面板。"""

        if not _RUNTIME_PERMISSION_TITLE_RE.search(text):
            return False
        options = tuple(_RUNTIME_PERMISSION_NUMBERED_OPTION_RE.finditer(text))
        option_numbers = {match.group("number") for match in options}
        has_allow_or_deny_option = any(
            _RUNTIME_PERMISSION_ALLOW_OPTION_RE.search(match.group(0))
            or _RUNTIME_PERMISSION_DENY_OPTION_RE.search(match.group(0))
            for match in options
        )
        return len(option_numbers) >= 2 or (
            bool(_RUNTIME_PERMISSION_RESPONSE_RE.search(text))
            and has_allow_or_deny_option
        )

    @staticmethod
    def _has_explicit_approval_prompt(text: str) -> bool:
        """避免普通编号列表仅因出现权限词而被当作确认请求。"""

        if _PROMPT_VERB_RE.search(text) or _INLINE_OPTION_RE.search(text):
            return True
        return any(
            _APPROVAL_RE.search(line)
            and line.rstrip().endswith(("?", "？"))
            for line in text.splitlines()
        )

    @staticmethod
    def _action_source(
        candidate: str,
        context: str,
        *,
        redact_context: bool = True,
    ) -> str:
        """保留最新 Prompt 块，避免无关历史输出改变其语义身份。"""

        if not (
            ClaudeCodeOutputDetector._looks_like_prompt(candidate)
            or _LINE_OPTION_RE.search(candidate)
            or ClaudeCodeOutputDetector._has_folder_trust_semantics(
                candidate
            )
            or ClaudeCodeOutputDetector._has_runtime_permission_fragment(
                candidate
            )
        ):
            return candidate
        contextual = (
            redact_claude_code_output(context).strip()
            if redact_context
            else context.strip()
        )
        contextual = ClaudeCodeOutputDetector._with_current_candidate(
            contextual,
            candidate,
        )
        if not contextual:
            return candidate
        runtime_permission_source = (
            ClaudeCodeOutputDetector._runtime_permission_action_source(
                contextual,
                candidate=candidate,
                maximum_chars=MAX_DETECTION_CONTEXT_CHARS,
            )
        )
        if runtime_permission_source and (
            ClaudeCodeOutputDetector._has_runtime_permission_fragment(
                candidate
            )
        ):
            return runtime_permission_source
        folder_trust_source = (
            ClaudeCodeOutputDetector._folder_trust_action_source(contextual)
        )
        if folder_trust_source and (
            ClaudeCodeOutputDetector._has_folder_trust_semantics(candidate)
            or _FOLDER_TRUST_RESPONSE_RE.search(candidate)
        ):
            return folder_trust_source
        lines = [line for line in contextual.splitlines() if line.strip()]
        for index in range(len(lines) - 1, -1, -1):
            if ClaudeCodeOutputDetector._looks_like_prompt(lines[index]):
                return "\n".join(lines[index:])[
                    -MAX_DETECTION_CONTEXT_CHARS:
                ]
        return contextual[-MAX_DETECTION_CONTEXT_CHARS:]

    @staticmethod
    def _with_current_candidate(context: str, candidate: str) -> str:
        """保证临时恢复文本的尾部包含本轮非空 candidate。"""

        contextual = context.strip()
        current = candidate.strip()
        if not contextual:
            return current
        if not current or contextual.endswith(current):
            return contextual
        return f"{contextual}\n{current}"

    @classmethod
    def _runtime_permission_action_source(
        cls,
        context: str,
        *,
        candidate: str = "",
        maximum_chars: int,
    ) -> str:
        """从最近权限标题恢复有界面板，不吸收标题前的普通输出。"""

        contextual = cls._with_current_candidate(context, candidate)
        matches = tuple(_RUNTIME_PERMISSION_TITLE_RE.finditer(contextual))
        if not matches:
            return ""
        return cls._bounded_runtime_permission_panel(
            contextual[matches[-1].start():],
            maximum_chars=maximum_chars,
        )

    @staticmethod
    def _bounded_runtime_permission_panel(
        panel: str,
        *,
        maximum_chars: int,
    ) -> str:
        """同时限制权限面板的行数和字符数，并保留标题与当前尾部。"""

        if maximum_chars <= 0:
            return ""
        lines = panel.splitlines()
        if len(lines) > MAX_RUNTIME_PERMISSION_PANEL_LINES:
            lines = [
                lines[0],
                *lines[-(MAX_RUNTIME_PERMISSION_PANEL_LINES - 1):],
            ]
        bounded = "\n".join(lines).strip()
        if len(bounded) <= maximum_chars:
            return bounded
        title = lines[0].strip() if lines else ""
        marker = " [… permission panel truncated …] "
        tail_size = maximum_chars - len(title) - len(marker)
        if tail_size <= 0:
            return ""
        return f"{title}{marker}{bounded[-tail_size:]}"

    @staticmethod
    def _folder_trust_action_source(context: str) -> str:
        """从最后一个目录信任问题开始保留有界交互块。"""

        matches = tuple(_FOLDER_TRUST_QUESTION_RE.finditer(context))
        if not matches:
            return ""
        start = matches[-1].start()
        headings = tuple(
            _FOLDER_TRUST_HEADING_RE.finditer(context[:start])
        )
        if headings and start - headings[-1].end() <= (
            MAX_FOLDER_TRUST_HEADING_GAP_CHARS
        ):
            start = headings[-1].start()
        return context[start:][
            -MAX_DETECTION_CONTEXT_CHARS:
        ]

    @staticmethod
    def _action(
        kind: ClaudeCodeActionKind,
        summary: str,
        prompt_text: str,
        risk: str,
        *,
        process_id: str,
        session_owner: str,
        cursor_start: int,
        cursor_end: int,
        timestamp: float,
    ) -> ClaudeCodeActionRequired:
        safe_prompt_text = redact_claude_code_output(prompt_text).strip()
        options = ClaudeCodeOutputDetector._extract_options(safe_prompt_text)
        return ClaudeCodeActionRequired(
            kind=kind,
            summary=summary,
            prompt_text=safe_prompt_text,
            options=options,
            risk=risk,
            cursor=cursor_end,
            action_id=build_claude_code_action_id(
                process_id=process_id,
                session_owner=session_owner,
                kind=kind,
                prompt_text=safe_prompt_text,
                options=options,
                cursor_start=cursor_start,
                cursor_end=cursor_end,
            ),
            process_id=process_id,
            session_owner=session_owner,
            cursor_start=cursor_start,
            cursor_end=cursor_end,
            created_at=timestamp,
        )

    @staticmethod
    def _safe_action(
        action: ClaudeCodeActionRequired | None,
    ) -> ClaudeCodeActionRequired | None:
        """移除只供当前交互使用的原生视图后再写入公开 Snapshot。"""

        if action is None:
            return None
        if action.raw_prompt_text is None and action.raw_options is None:
            return action
        return replace(
            action,
            raw_prompt_text=None,
            raw_options=None,
            native_prompt_fingerprint=None,
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
    def _extract_options(
        text: str,
        *,
        redact_output: bool = True,
    ) -> tuple[str, ...]:
        options_with_positions: list[tuple[int, str]] = []
        line_matches = tuple(_LINE_OPTION_RE.finditer(text))
        for inline in _INLINE_OPTION_RE.finditer(text):
            if any(
                match.start() <= inline.start() < match.end()
                for match in line_matches
            ):
                continue
            option = (
                redact_claude_code_output(inline.group(0)).strip()
                if redact_output
                else inline.group(0).strip()
            )
            if option:
                options_with_positions.append((inline.start(), option))
        for match in line_matches:
            option = (
                redact_claude_code_output(match.group(0)).strip()
                if redact_output
                else match.group(0).strip()
            )
            if option:
                options_with_positions.append((match.start(), option))
        options_with_positions.sort(key=lambda item: item[0])
        return tuple(
            option for _, option in options_with_positions[
                :MAX_NATIVE_INTERACTION_OPTIONS
            ]
        )

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
        process_id: str,
        cursor_start: int,
        cursor_end: int,
        timestamp: float,
    ) -> None:
        """仅在动作内容实质变化时保存待决动作并生成事件。"""

        action_fingerprint = self._action_fingerprint(action)
        if action_fingerprint in self._suppressed_action_fingerprint_set:
            self._action_required = None
            self._last_action_fingerprint = None
            self._clear_interaction_context()
            return
        if action_fingerprint == self._last_action_fingerprint:
            if (
                self._action_required is not None
                and action.raw_prompt_text is not None
                and action.raw_options is not None
            ):
                self._action_required = replace(
                    self._action_required,
                    raw_prompt_text=action.raw_prompt_text,
                    raw_options=action.raw_options,
                )
            self._clear_interaction_context()
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
                "action_id": action.action_id,
                "prompt_fingerprint": action_fingerprint,
            },
        )
        self._clear_interaction_context()

    def _process_just_exited(self, process_status: str | None) -> bool:
        return (
            process_status in _EXIT_PROCESS_STATUSES
            and self._last_process_status not in _EXIT_PROCESS_STATUSES
        )

    def _clear_recent_event_fingerprints(self) -> None:
        """在可信交互边界丢弃旧去重窗口，不保存事件正文。"""

        self._recent_fingerprints.clear()
        self._recent_fingerprint_set.clear()

    def _remember_suppressed_action_fingerprint(self, fingerprint: str) -> None:
        """在有确定输入边界后，有界地抑制旧提示的纯重绘。"""

        if fingerprint in self._suppressed_action_fingerprint_set:
            return
        self._suppressed_action_fingerprints.append(fingerprint)
        self._suppressed_action_fingerprint_set.add(fingerprint)
        while (
            len(self._suppressed_action_fingerprints)
            > MAX_SUPPRESSED_ACTION_FINGERPRINTS
        ):
            removed = self._suppressed_action_fingerprints.popleft()
            self._suppressed_action_fingerprint_set.discard(removed)

    def _clear_suppressed_action_fingerprints(self) -> None:
        """仅在用户明确开始新输入边界时清除过期的重绘抑制。"""

        self._suppressed_action_fingerprints.clear()
        self._suppressed_action_fingerprint_set.clear()

    @staticmethod
    def _event_counts_as_activity(event: ClaudeCodeEvent) -> bool:
        if event.event_type != ClaudeCodeEventType.OUTPUT:
            return True
        if (
            event.metadata.get("ready_ui_only") is True
            or event.metadata.get("ui_non_activity") is True
        ):
            return False
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
    def _safe_action_fingerprint(
        action: ClaudeCodeActionRequired,
    ) -> str:
        payload = (
            action.kind.value,
            " ".join(action.prompt_text.split()).casefold(),
            tuple(" ".join(option.split()).casefold() for option in action.options),
            action.risk,
        )
        return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()

    @classmethod
    def _action_fingerprint(
        cls,
        action: ClaudeCodeActionRequired,
    ) -> str:
        payload = (
            cls._safe_action_fingerprint(action),
            action.native_prompt_fingerprint or "",
        )
        return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()


__all__ = [
    "MAX_DETECTION_CONTEXT_CHARS",
    "MAX_ECHO_MATCH_LINES",
    "MAX_EVENT_TEXT_CHARS",
    "MAX_OUTBOUND_INPUT_EVIDENCE",
    "MAX_PENDING_INTERACTION_OBSERVATIONS",
    "MAX_RECENT_EVENT_FINGERPRINTS",
    "OUTBOUND_INPUT_OBSERVATION_BUDGET",
    "OUTBOUND_INPUT_PREFIX_CHARS",
    "OUTBOUND_INPUT_TTL_SECONDS",
    "RECENT_MATCHED_ECHO_MATCH_BUDGET",
    "RECENT_MATCHED_ECHO_TTL_SECONDS",
    "ClaudeCodeOutputDetector",
    "DetectionResult",
]
