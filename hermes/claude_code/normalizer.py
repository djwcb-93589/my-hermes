"""有界地解释 Claude Code PTY 追加流，不改变原始 cursor。"""

from __future__ import annotations

import codecs
import hashlib
import re
from dataclasses import dataclass

from hermes.redaction import redact_explicit_secrets


MAX_RAW_BUFFER_CHARS = 32_768
MAX_NORMALIZED_TEXT_CHARS = 32_768
MAX_CURRENT_LINE_CHARS = 4_096
MAX_PENDING_ESCAPE_CHARS = 256

_NORMALIZED_TRUNCATION_MARKER = "[… earlier normalized output truncated …]"
_CURSOR_GAP_MARKER = "[… ProcessManager cursor gap …]"
_ESCAPE_TRUNCATION_MARKER = "[… incomplete ANSI sequence truncated …]"
_SPINNER_CHARS_RE = re.compile(
    r"[|/\\\-⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◐◓◑◒⣾⣽⣻⢿⡿⣟⣯⣷]+"
)
_OAUTH_CODE_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:oauth|authorization|verification|device)"
    r"(?:[ _-]+(?:authorization|verification|device))?"
    r"[ _-]+code\s*[:=]\s*['\"]?)(?P<value>[^\s,'\";&]+)"
)
_OAUTH_CODE_SENTENCE_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:oauth|authorization|verification|device|one[ -]?time)"
    r"[ _-]+code\s+(?:is\s+)?['\"]?)(?P<value>[^\s,'\";&]+)"
)
_QUERY_CREDENTIAL_RE = re.compile(
    r"(?i)(?P<prefix>[?&#](?:code|token|access_token|refresh_token)=)"
    r"(?P<value>[^&\s]+)"
)
_AUTHORIZATION_HEADER_RE = re.compile(
    r"(?i)(?P<prefix>(?<![A-Za-z0-9_-])['\"]?authorization['\"]?"
    r"\s*[:=]\s*['\"]?(?:(?:bearer|basic|token)\s+)?)"
    r"(?P<value>[^\s,'\";&]+)"
)
_RAW_API_KEY_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]*"
)
_REDACTED_SECRET_SUFFIX_RE = re.compile(
    r"(?i)(?P<prefix><secret>)[A-Za-z0-9_./+=~-]+"
)


def redact_claude_code_output(text: object | None) -> str:
    """复用通用脱敏，并补充 Claude 登录流程中的明确 code 形式。"""

    redacted = redact_explicit_secrets(text)
    redacted = _AUTHORIZATION_HEADER_RE.sub(
        lambda match: f"{match.group('prefix')}<secret>",
        redacted,
    )
    redacted = _OAUTH_CODE_RE.sub(
        lambda match: f"{match.group('prefix')}<secret>",
        redacted,
    )
    redacted = _OAUTH_CODE_SENTENCE_RE.sub(
        lambda match: f"{match.group('prefix')}<secret>",
        redacted,
    )
    redacted = _QUERY_CREDENTIAL_RE.sub(
        lambda match: f"{match.group('prefix')}<secret>",
        redacted,
    )
    redacted = _RAW_API_KEY_PREFIX_RE.sub("<secret>", redacted)
    return _REDACTED_SECRET_SUFFIX_RE.sub(
        lambda match: match.group("prefix"),
        redacted,
    )


@dataclass(frozen=True, slots=True)
class NormalizedOutputDelta:
    """一次 feed 的有界语义变化和对应原始 cursor 区间。"""

    text: str
    normalized_output: str
    cursor_start: int
    cursor_end: int
    cursor_gap: bool
    gap_start: int | None
    gap_end: int | None
    redraw_only: bool
    limits_hit: tuple[str, ...]


class ClaudeCodeOutputNormalizer:
    """跨 chunk 保存最小终端解释状态，所有内部缓冲均有硬上限。"""

    def __init__(
        self,
        *,
        max_raw_buffer: int = MAX_RAW_BUFFER_CHARS,
        max_normalized_text: int = MAX_NORMALIZED_TEXT_CHARS,
        max_current_line: int = MAX_CURRENT_LINE_CHARS,
        max_pending_escape: int = MAX_PENDING_ESCAPE_CHARS,
        initial_cursor: int = 0,
    ) -> None:
        for name, value in (
            ("max_raw_buffer", max_raw_buffer),
            ("max_normalized_text", max_normalized_text),
            ("max_current_line", max_current_line),
            ("max_pending_escape", max_pending_escape),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(initial_cursor, bool)
            or not isinstance(initial_cursor, int)
            or initial_cursor < 0
        ):
            raise ValueError("initial_cursor must be a non-negative integer")

        self._max_raw_buffer = max_raw_buffer
        self._max_normalized_text = max_normalized_text
        self._max_current_line = max_current_line
        self._max_pending_escape = max_pending_escape
        self._expected_cursor = initial_cursor
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._raw_tail = ""
        self._history = ""
        self._line: list[str] = []
        self._column = 0
        self._saved_column = 0
        self._pending_escape = ""
        self._history_truncated = False
        self._last_committed_signature = ""
        self._last_dynamic_signature = ""
        self._changed_fragments: list[str] = []
        self._limits_hit: set[str] = set()

    @property
    def normalized_output(self) -> str:
        """返回有界脱敏文本；它从不参与原始 cursor 计算。"""

        return self._bounded_normalized_output()

    def feed(
        self,
        raw: str | bytes,
        *,
        cursor_start: int,
        cursor_end: int,
        cursor_gap: bool = False,
        final: bool = False,
    ) -> NormalizedOutputDelta:
        """消费一次追加页，并保留跨页 escape、重绘和无换行文本状态。"""

        self._validate_cursor_range(cursor_start, cursor_end)
        if not isinstance(raw, (str, bytes)):
            raise TypeError("raw output must be text or bytes")
        if not isinstance(cursor_gap, bool) or not isinstance(final, bool):
            raise TypeError("cursor_gap and final must be booleans")

        previous_expected_cursor = self._expected_cursor
        detected_gap = cursor_gap or cursor_start != previous_expected_cursor
        gap_start = (
            min(previous_expected_cursor, cursor_start)
            if detected_gap
            else None
        )
        gap_end = (
            max(previous_expected_cursor, cursor_start)
            if detected_gap
            else None
        )
        self._expected_cursor = cursor_end
        self._changed_fragments = []
        self._limits_hit = set()
        previous_line = self._line_text()

        if detected_gap:
            self._reset_after_cursor_gap()

        decoded = self._decode(raw, final=final)
        self._append_raw_tail(decoded)
        self._consume(decoded)
        if final and self._pending_escape:
            self._pending_escape = ""
            self._limits_hit.add("pending_escape")
            self._append_history(_ESCAPE_TRUNCATION_MARKER)
            self._changed_fragments.append(_ESCAPE_TRUNCATION_MARKER)

        current_line = self._line_text()
        current_signature = self._semantic_signature(current_line)
        if (
            current_line != previous_line
            and current_signature
            and current_signature != self._last_dynamic_signature
        ):
            self._changed_fragments.append(
                redact_claude_code_output(current_line)
            )
            self._last_dynamic_signature = current_signature

        changed_text = self._bounded_changed_text(self._changed_fragments)
        redraw_only = bool(decoded) and not changed_text
        return NormalizedOutputDelta(
            text=changed_text,
            normalized_output=self._bounded_normalized_output(),
            cursor_start=cursor_start,
            cursor_end=cursor_end,
            cursor_gap=detected_gap,
            gap_start=gap_start,
            gap_end=gap_end,
            redraw_only=redraw_only,
            limits_hit=tuple(sorted(self._limits_hit)),
        )

    @staticmethod
    def _validate_cursor_range(cursor_start: int, cursor_end: int) -> None:
        for name, value in (
            ("cursor_start", cursor_start),
            ("cursor_end", cursor_end),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if cursor_end < cursor_start:
            raise ValueError("cursor_end must not precede cursor_start")

    def _decode(self, raw: str | bytes, *, final: bool) -> str:
        if isinstance(raw, bytes):
            decoded = self._decoder.decode(raw, final=final)
            if final:
                self._decoder = codecs.getincrementaldecoder("utf-8")(
                    errors="replace"
                )
            return decoded

        prefix = ""
        pending_bytes, _ = self._decoder.getstate()
        if pending_bytes or final:
            prefix = self._decoder.decode(b"", final=True)
            self._decoder = codecs.getincrementaldecoder("utf-8")(
                errors="replace"
            )
        return prefix + raw

    def _append_raw_tail(self, text: str) -> None:
        combined = self._raw_tail + text
        if len(combined) > self._max_raw_buffer:
            combined = combined[-self._max_raw_buffer :]
            self._limits_hit.add("raw_buffer")
        safe_tail = redact_claude_code_output(combined)
        if len(safe_tail) > self._max_raw_buffer:
            safe_tail = safe_tail[-self._max_raw_buffer :]
            self._limits_hit.add("raw_buffer")
        self._raw_tail = safe_tail

    def _reset_after_cursor_gap(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending_escape = ""
        self._line = []
        self._column = 0
        self._saved_column = 0
        self._history = ""
        self._history_truncated = False
        self._last_committed_signature = ""
        self._last_dynamic_signature = ""
        self._append_history(_CURSOR_GAP_MARKER)
        self._changed_fragments.append(_CURSOR_GAP_MARKER)

    def _consume(self, text: str) -> None:
        combined = self._pending_escape + text
        self._pending_escape = ""
        index = 0
        while index < len(combined):
            character = combined[index]
            if character == "\x1b":
                sequence, next_index = self._read_escape(combined, index)
                if sequence is None:
                    pending = combined[index:]
                    if len(pending) > self._max_pending_escape:
                        self._limits_hit.add("pending_escape")
                        self._append_history(_ESCAPE_TRUNCATION_MARKER)
                        self._changed_fragments.append(
                            _ESCAPE_TRUNCATION_MARKER
                        )
                    else:
                        self._pending_escape = pending
                    break
                self._apply_escape(sequence)
                index = next_index
                continue
            if character == "\r":
                self._column = 0
            elif character == "\n":
                self._commit_line()
                self._line = []
                self._column = 0
            elif character == "\b":
                self._column = max(0, self._column - 1)
            elif character == "\t":
                target = ((self._column // 8) + 1) * 8
                while self._column < target:
                    self._write_character(" ")
            elif ord(character) >= 32 and character != "\x7f":
                self._write_character(character)
            index += 1

    def _read_escape(
        self,
        text: str,
        start: int,
    ) -> tuple[str | None, int]:
        if start + 1 >= len(text):
            return None, start
        introducer = text[start + 1]
        if introducer == "[":
            for index in range(start + 2, len(text)):
                if 0x40 <= ord(text[index]) <= 0x7E:
                    return text[start : index + 1], index + 1
            return None, start
        if introducer in {"]", "P", "^", "_"}:
            index = start + 2
            while index < len(text):
                if introducer == "]" and text[index] == "\x07":
                    return text[start : index + 1], index + 1
                if (
                    text[index] == "\x1b"
                    and index + 1 < len(text)
                    and text[index + 1] == "\\"
                ):
                    return text[start : index + 2], index + 2
                index += 1
            return None, start
        if introducer in {"(", ")", "*", "+", "#", "%"}:
            if start + 2 >= len(text):
                return None, start
            return text[start : start + 3], start + 3
        return text[start : start + 2], start + 2

    def _apply_escape(self, sequence: str) -> None:
        if sequence == "\x1b7":
            self._saved_column = self._column
            return
        if sequence == "\x1b8":
            self._column = self._saved_column
            self._limit_column()
            return
        if not sequence.startswith("\x1b["):
            return
        final = sequence[-1]
        parameters = sequence[2:-1]
        values = self._numeric_parameters(parameters)
        amount = values[0] if values and values[0] > 0 else 1

        if final == "m":
            return
        if final in {"C", "a"}:
            self._column += amount
            self._limit_column()
        elif final in {"D", "j"}:
            self._column = max(0, self._column - amount)
        elif final in {"G", "`"}:
            self._column = max(0, amount - 1)
            self._limit_column()
        elif final in {"H", "f"}:
            column = values[1] if len(values) > 1 and values[1] > 0 else 1
            self._column = max(0, column - 1)
            self._limit_column()
        elif final == "K":
            self._erase_line(values[0] if values else 0)
        elif final == "J":
            self._erase_display(values[0] if values else 0)
        elif final == "s":
            self._saved_column = self._column
        elif final == "u":
            self._column = self._saved_column
            self._limit_column()
        elif final in {"E", "F"}:
            self._column = 0
        # A/B 等垂直移动属于屏幕重绘信号；保留文本但不伪造历史行位置。

    @staticmethod
    def _numeric_parameters(parameters: str) -> list[int]:
        cleaned = parameters.lstrip("?")
        if not cleaned:
            return []
        values: list[int] = []
        for item in cleaned.split(";"):
            try:
                values.append(int(item) if item else 0)
            except ValueError:
                return []
        return values

    def _erase_line(self, mode: int) -> None:
        if mode == 2:
            self._line = []
            self._column = 0
        elif mode == 1:
            end = min(self._column + 1, len(self._line))
            self._line[:end] = [" "] * end
        else:
            del self._line[min(self._column, len(self._line)) :]

    def _erase_display(self, mode: int) -> None:
        if mode in {0, 1, 2, 3}:
            current = self._line_text()
            if self._semantic_signature(current):
                self._commit_line()
            self._line = []
            self._column = 0

    def _write_character(self, character: str) -> None:
        self._limit_column()
        if self._column >= self._max_current_line:
            return
        if self._column > len(self._line):
            self._line.extend(" " for _ in range(self._column - len(self._line)))
        if self._column == len(self._line):
            self._line.append(character)
        else:
            self._line[self._column] = character
        self._column += 1

    def _limit_column(self) -> None:
        if self._column < self._max_current_line:
            return
        shift = self._column - self._max_current_line + 1
        if shift > 0:
            del self._line[: min(shift, len(self._line))]
            self._column -= shift
            self._saved_column = max(0, self._saved_column - shift)
            self._limits_hit.add("current_line")

    def _commit_line(self) -> None:
        line = redact_claude_code_output(self._line_text())
        signature = self._semantic_signature(line)
        if not signature or signature == self._last_committed_signature:
            return
        self._append_history(line)
        self._changed_fragments.append(line)
        self._last_committed_signature = signature
        self._last_dynamic_signature = signature

    def _append_history(self, text: str) -> None:
        safe_text = redact_claude_code_output(text).rstrip()
        if not safe_text:
            return
        addition = f"{safe_text}\n"
        self._history += addition
        if len(self._history) > self._max_normalized_text:
            self._history = self._history[-self._max_normalized_text :]
            self._history_truncated = True
            self._limits_hit.add("normalized_text")

    def _line_text(self) -> str:
        return "".join(self._line).rstrip()

    def _bounded_normalized_output(self) -> str:
        current = redact_claude_code_output(self._line_text())
        output = f"{self._history}{current}".rstrip()
        if self._history_truncated or len(output) > self._max_normalized_text:
            budget = max(
                0,
                self._max_normalized_text
                - len(_NORMALIZED_TRUNCATION_MARKER)
                - 1,
            )
            output = (
                f"{_NORMALIZED_TRUNCATION_MARKER}\n{output[-budget:]}"
                if budget
                else _NORMALIZED_TRUNCATION_MARKER[
                    : self._max_normalized_text
                ]
            )
        return redact_claude_code_output(
            output[-self._max_normalized_text :]
        )

    def _bounded_changed_text(self, fragments: list[str]) -> str:
        unique: list[str] = []
        for fragment in fragments:
            safe_fragment = redact_claude_code_output(fragment).strip()
            if safe_fragment and (not unique or unique[-1] != safe_fragment):
                unique.append(safe_fragment)
        return "\n".join(unique)[-self._max_normalized_text :]

    @staticmethod
    def _semantic_signature(text: str) -> str:
        safe_text = redact_claude_code_output(text)
        without_spinner = _SPINNER_CHARS_RE.sub("", safe_text)
        semantic_text = " ".join(without_spinner.split()).casefold()
        if not semantic_text:
            return ""
        return hashlib.sha256(semantic_text.encode("utf-8")).hexdigest()


__all__ = [
    "MAX_CURRENT_LINE_CHARS",
    "MAX_NORMALIZED_TEXT_CHARS",
    "MAX_PENDING_ESCAPE_CHARS",
    "MAX_RAW_BUFFER_CHARS",
    "ClaudeCodeOutputNormalizer",
    "NormalizedOutputDelta",
    "redact_claude_code_output",
]
