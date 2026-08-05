"""识别当前真实用户消息中的显式 Claude Code 请求。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class ClaudeCodeRequestOperation(str, Enum):
    """显式请求可以获得的最小受管操作集合。"""

    START = "start"
    POLL = "poll"
    REQUEST_INTERRUPT = "request_interrupt"
    TERMINATE = "terminate"


@dataclass(frozen=True, slots=True)
class ClaudeCodeExplicitRequest:
    """只保存分类结果，不保存用户正文或历史上下文。"""

    operation: ClaudeCodeRequestOperation


_CLAUDE_MARKER = r"(?:claude\s*[- ]\s*code|claude-code)"
_MARKER = rf"(?:{_CLAUDE_MARKER}|\bcc\b)"
_MARKER_RE = re.compile(rf"(?i){_MARKER}")
_START_INTENT_RE = re.compile(
    rf"(?ix)(?:"
    rf"(?:使用|用|让|交给|启动|调用|委托|通过)\s*{_MARKER}"
    rf"|{_MARKER}\s*(?:帮我|替我|来|执行|完成|处理|修改|检查|修复|运行|"
    rf"实现|编写|创建|更新|审查|分析|测试)"
    rf"|(?:use|run|start|invoke|call|ask|let|have|delegate\s+to)\s+"
    rf"{_MARKER}"
    rf"|{_MARKER}\s+(?:to\s+)?(?:fix|modify|implement|complete|run|execute|"
    rf"update|refactor|add|remove|write|create|test|review|check|build|handle)"
    rf")"
)
_TASK_RE = re.compile(
    r"(?ix)(?:"
    r"修改|修复|检查|完成|实现|执行|运行|更新|重构|添加|删除|编写|创建|做|"
    r"测试|审查|分析|处理|迁移|提交|查找|生成|构建|fix|modify|implement|"
    r"complete|run|execute|update|refactor|add|remove|write|create|test|do|"
    r"review|check|build|handle|migrate|commit"
    r")"
)
_NEGATION_RE = re.compile(
    rf"(?ix)(?:"
    rf"(?:do\s+not|don't|never)\s+(?:want\s+to\s+|need\s+to\s+)?"
    rf"(?:use|using|run|let|ask|start|invoke|call)?\s*{_MARKER}"
    rf"|without\s+(?:using\s+|use\s+of\s+)?{_MARKER}"
    rf"|no\s+need\s+to\s+(?:use|run|start|invoke|call)\s*{_MARKER}"
    rf"|no\s+(?:need\s+for\s+)?{_MARKER}"
    rf"|(?:不要|别|禁止|无需|不必|不让|不交给|不启动|不调用|不使用|不用|不需要)\s*"
    rf"(?:使用|用|让|交给|启动|调用)?\s*{_MARKER}"
    rf"|(?:不想|不打算)\s*(?:使用|用|让|交给|启动|调用)?\s*{_MARKER}"
    rf"|{_MARKER}.{{0,48}}(?:不要|别|禁止|不让|不执行|不使用|"
    rf"do\s+not|don't|never|without)"
    rf")"
)
_REFERENCE_RE = re.compile(
    rf"(?ix)(?:"
    rf"(?:文档(?:中|里)?|翻译|引用|示例|例子|比较|说明|what\s+is|"
    rf"do\s+you\s+support|translate|documentation|example|compare)"
    rf".{{0,100}}{_MARKER}"
    rf"|[\"'“‘][^\r\n]{{0,160}}{_MARKER}"
    rf")"
)
_INTERRUPT_RE = (
    r"(?:中断|打断|暂停|interrupt|pause|ctrl\s*[- ]?c)"
)
_TERMINATE_RE = (
    r"(?:终止|结束|杀掉|停止|关闭|kill|terminate|stop|shutdown)"
)
_POLL_RE = (
    r"(?:状态|输出|进度|运行到哪|在哪里|做到哪|正在做什么|最新|status|"
    r"output|progress|running|latest|doing|where|完成了吗|结束了吗|结果)"
)


def _message_surface(message: str) -> str:
    """移除代码块、引用行和行内代码，避免把示例当成当前请求。"""

    lines: list[str] = []
    in_fence = False
    for line in message.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or line.lstrip().startswith(">"):
            continue
        lines.append(line)
    surface = "\n".join(lines)
    return re.sub(r"`[^`\r\n]*`", " ", surface)


def _near_marker(surface: str, phrase: str) -> bool:
    """只接受操作词与 Claude 标识位于有限范围内的显式表达。"""

    return bool(
        re.search(
            rf"(?ix)(?:{phrase}).{{0,80}}{_MARKER}|{_MARKER}.{{0,80}}(?:{phrase})",
            surface,
        )
    )


class ClaudeCodeExplicitRequestDetector:
    """只分析当前一条真实人类消息，不读取历史、模型输出或 Tool 参数。"""

    def detect(self, message: object) -> ClaudeCodeExplicitRequest | None:
        if not isinstance(message, str):
            return None
        surface = _message_surface(message).strip()
        if not surface or not _MARKER_RE.search(surface):
            return None
        if _NEGATION_RE.search(surface) or _REFERENCE_RE.search(surface):
            return None

        if _START_INTENT_RE.search(surface) and _TASK_RE.search(surface):
            return ClaudeCodeExplicitRequest(ClaudeCodeRequestOperation.START)

        if _near_marker(surface, _TERMINATE_RE):
            return ClaudeCodeExplicitRequest(
                ClaudeCodeRequestOperation.TERMINATE
            )
        if _near_marker(surface, _INTERRUPT_RE):
            return ClaudeCodeExplicitRequest(
                ClaudeCodeRequestOperation.REQUEST_INTERRUPT
            )
        if _near_marker(surface, _POLL_RE):
            return ClaudeCodeExplicitRequest(ClaudeCodeRequestOperation.POLL)
        return None


_DEFAULT_DETECTOR = ClaudeCodeExplicitRequestDetector()


def detect_claude_code_request(
    message: object,
) -> ClaudeCodeExplicitRequest | None:
    """使用无状态识别器分类当前真实用户消息。"""

    return _DEFAULT_DETECTOR.detect(message)


__all__ = [
    "ClaudeCodeExplicitRequest",
    "ClaudeCodeExplicitRequestDetector",
    "ClaudeCodeRequestOperation",
    "detect_claude_code_request",
]
