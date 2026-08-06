"""识别当前真实用户消息中的显式 Claude Code 请求。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class ClaudeCodeRequestOperation(str, Enum):
    """显式请求可以获得的最小受管操作集合。"""

    START = "start"
    POLL = "poll"
    SEND_INSTRUCTION = "send_instruction"
    REQUEST_INTERRUPT = "request_interrupt"
    TERMINATE = "terminate"


@dataclass(frozen=True, slots=True)
class ClaudeCodeExplicitRequest:
    """只保存分类结果，不保存用户正文或历史上下文。"""

    operation: ClaudeCodeRequestOperation


_CLAUDE_MARKER = r"(?:claude\s*[- ]\s*code|claude-code)"
_CC_MARKER = r"(?<![A-Za-z0-9_])cc(?![A-Za-z0-9_])"
_MARKER = rf"(?:{_CLAUDE_MARKER}|{_CC_MARKER})"
_MARKER_TOKEN = (
    rf"(?:[\"'“‘「『]?\s*{_MARKER}\s*"
    rf"[\"'”’」』]?)"
)
_MARKER_RE = re.compile(rf"(?i){_MARKER}")
_START_INTENT_RE = re.compile(
    rf"(?ix)(?:"
    rf"(?:使用|用|让|交给|启动|调用|委托|通过)\s*{_MARKER_TOKEN}"
    rf"|{_MARKER_TOKEN}\s*(?:帮我|替我|来|执行|完成|处理|修改|检查|修复|运行|启动|停止|"
    rf"终止|结束|关闭|"
    rf"实现|编写|创建|更新|审查|分析|测试)"
    rf"|(?:use|run|start|invoke|call|ask|let|have|delegate\s+to)\s+"
    rf"{_MARKER_TOKEN}"
    rf"|{_MARKER_TOKEN}\s+(?:to\s+)?(?:fix|modify|implement|complete|run|execute|"
    rf"update|refactor|add|remove|write|create|test|review|check|build|handle|"
    rf"start|stop|terminate|end|close|shutdown)"
    rf")"
)
_TASK_RE = re.compile(
    r"(?ix)(?:"
    r"修改|修复|检查|完成|实现|执行|运行|启动|停止|终止|结束|关闭|读取|读出|输出|写入|保存|更新|重构|添加|删除|编写|创建|做|"
    r"测试|审查|分析|处理|迁移|提交|查找|生成|构建|fix|modify|implement|"
    r"complete|run|execute|start|stop|terminate|end|close|shutdown|update|refactor|add|remove|"
    r"(?<![A-Za-z0-9_])(?:read|print|write|save|output)(?![A-Za-z0-9_])|"
    r"create|test|do|"
    r"review|check|build|handle|migrate|commit"
    r")"
)
_NEGATION_RE = re.compile(
    rf"(?ix)(?:"
    rf"(?:do\s+not|don't|never)\s+(?:want\s+to\s+|need\s+to\s+)?"
    rf"(?:use|using|run|let|ask|start|invoke|call)?\s*{_MARKER_TOKEN}"
    rf"|without\s+(?:using\s+|use\s+of\s+)?{_MARKER_TOKEN}"
    rf"|no\s+need\s+to\s+(?:use|run|start|invoke|call)\s*{_MARKER_TOKEN}"
    rf"|no\s+(?:need\s+for\s+)?{_MARKER_TOKEN}"
    rf"|(?:stop|no\s+longer)\s+(?:using|use|letting|let|asking|ask|"
    rf"starting|start|invoking|invoke|calling|call|running|run)?\s*"
    rf"{_MARKER_TOKEN}"
    rf"|(?:停止|不再|别再)\s*(?:使用|用|让|交给|启动|调用|委托)?\s*"
    rf"{_MARKER_TOKEN}"
    rf"|(?:不要|别|禁止|无需|不必|不让|不交给|不启动|不调用|不使用|不用|不需要)\s*"
    rf"(?:使用|用|让|交给|启动|调用)?\s*{_MARKER_TOKEN}"
    rf"|(?:不想|不打算)\s*(?:使用|用|让|交给|启动|调用)?\s*{_MARKER_TOKEN}"
    rf")"
)
_REFERENCE_SEGMENT_RE = re.compile(
    r"(?ix)^\s*(?:请\s*)?(?:"
    r"翻译|translate|引用|quote|"
    r"(?:文档(?:中|里)?|documentation)(?:写着|提到|说明|记录|内容)?|"
    r"(?:示例|例子|example)(?:\s*[:：]|\b)|"
    r"(?:比较|compare)(?:一下|一番)?|"
    r"(?:说明|explain)(?:一下)?(?:\s+|[:：])"
    r")"
)
_SEGMENT_SPLIT_RE = re.compile(r"[\r\n,，;；。！？!?]+")
_WHOLE_QUOTED_RE = re.compile(
    r'''(?is)^\s*(?:"[^"\r\n]{1,512}"|'[^'\r\n]{1,512}'|'''
    r'''“[^”\r\n]{1,512}”|‘[^’\r\n]{1,512}’|'''
    r'''「[^」\r\n]{1,512}」|『[^』\r\n]{1,512}』)\s*'''
    r'''[.!?。！？]?\s*$'''
)
_INTERRUPT_RE = (
    r"(?:中断|打断|暂停|interrupt|pause|ctrl\s*[- ]?c)"
)
_TERMINATE_RE = (
    r"(?:终止|结束|杀掉|停止|关闭|kill|terminate|stop|shutdown)"
)
_POLL_QUERY_RE = re.compile(
    rf"(?ix)(?:"
    rf"(?:查看|查询|获取|显示|汇报|看看|问一下|check|show|"
    rf"what(?:'s|\s+is)|how\s+far|where\s+is)"
    rf".{{0,40}}{_MARKER_TOKEN}.{{0,60}}"
    rf"(?:当前|现在|目前|最新|状态|进度|输出|结果|完成了吗|结束了吗|"
    rf"运行到哪|在哪里|做到哪|正在做什么|有结果了吗|status|progress|"
    rf"output|latest|where|doing)"
    rf"|{_MARKER_TOKEN}\s*(?:"
    rf"(?:当前|现在|目前|最新)\s*(?:是什么|是)?\s*(?:有)?\s*"
    rf"(?:状态|进度|输出|结果|完成了吗|结束了吗|运行到哪|在哪里|"
    rf"做到哪|正在做什么|有结果了吗|status|progress|output|latest)"
    rf"|(?:完成了吗|结束了吗|运行到哪|在哪里|做到哪|正在做什么|"
    rf"有结果了吗|status|progress)"
    rf"|(?:有\s*)?(?:输出|结果)\s*(?:了吗|了什么|是什么|吗)"
    rf"|(?:状态|进度)\s*(?:如何|怎样|是什么|吗)?"
    rf")"
    rf"|{_MARKER_TOKEN}.{{0,20}}(?:检查|查看|查询)\s*"
    rf"(?:当前|现在|目前|最新)\s*(?:状态|进度|输出|结果)"
    rf")"
)
_SESSION_REFERENCE_RE = (
    r"(?:当前|刚才|这个|本轮|正在运行(?:的)?|现有|已有|"
    r"current|previous|running|existing|this)"
)
_SESSION_NOUN_RE = r"(?:会话|任务|进程|运行|session|task|process|run)"
_SEND_INSTRUCTION_REQUEST_RE = re.compile(
    rf"(?ix)(?:"
    # 明确把任务交给已知的当前或最近 Claude Code。
    rf"(?:请\s*)?(?:让|交给|用|使用)\s*"
    rf"{_SESSION_REFERENCE_RE}\s*(?:的\s*)?{_MARKER_TOKEN}"
    rf"(?:\s*{_SESSION_NOUN_RE})?"
    # 支持 Claude Code 名称在前、随后明确说明其当前受管对象的中文语序。
    rf"|(?:请\s*)?(?:让|交给|用|使用)\s*{_MARKER_TOKEN}\s*"
    rf"{_SESSION_REFERENCE_RE}\s*(?:的\s*)?{_SESSION_NOUN_RE}"
    # “继续让 CC”本身明确承接已有会话；孤立“继续”仍不构成授权。
    rf"|(?:继续|接着|再)\s*(?:请\s*)?(?:让|交给|用|使用)\s*{_MARKER_TOKEN}"
    # “给当前 Claude Code 一个新任务”必须同时指明既有会话和新任务语义。
    rf"|(?:给|向)\s*{_SESSION_REFERENCE_RE}\s*(?:的\s*)?{_MARKER_TOKEN}"
    rf"(?:\s*{_SESSION_NOUN_RE})?\s*(?:一个\s*)?"
    rf"(?:新任务|任务|新指令|指令)"
    # 英文只覆盖同样明确的续接和当前会话指向。
    rf"|(?:please\s+)?(?:continue|resume|again)\s+(?:with\s+)?{_MARKER_TOKEN}\b"
    rf"|(?:ask|have|let)\s+(?:the\s+)?"
    rf"(?:current|previous|running|this)\s+{_MARKER_TOKEN}"
    rf")"
)
_CONTROL_TARGET_RE = (
    r"(?:当前|刚才|现在|正在运行的|这个|该|本轮|"
    r"the\s+(?:current|previous|running)|current|previous|running|this)"
)
_CONTROL_NOUN_RE = r"(?:任务|进程|会话|运行|task|process|session|run)"
_CONTROL_TARGET_OBJECT_RE = (
    rf"(?:{_CONTROL_TARGET_RE})\s*(?:的\s*)?{_CONTROL_NOUN_RE}"
)
_CONTROL_FILLER_RE = r"(?:一下|一会儿|先|please)?\s*"
_CONTROL_MARKER_OBJECT_RE = (
    rf"(?:the\s+)?{_MARKER_TOKEN}(?:\s*{_CONTROL_NOUN_RE})?"
)
_CONTROL_TARGET_MARKER_OBJECT_RE = (
    rf"(?:{_CONTROL_TARGET_RE})\s*(?:的\s*)?"
    rf"{_CONTROL_MARKER_OBJECT_RE}"
)
_CONTROL_EXPLICIT_OBJECT_RE = (
    rf"(?:{_CONTROL_TARGET_OBJECT_RE}|{_CONTROL_TARGET_MARKER_OBJECT_RE}|"
    rf"{_CONTROL_NOUN_RE})"
)
_CONTROL_BOUNDARY_RE = r"(?!\s*[A-Za-z0-9_\u3400-\u9fff])"
_INTERRUPT_CONTROL_RE = re.compile(
    rf"(?ix)(?:"
    rf"{_INTERRUPT_RE}\s*{_CONTROL_FILLER_RE}"
    rf"(?:{_CONTROL_TARGET_MARKER_OBJECT_RE}|{_CONTROL_MARKER_OBJECT_RE})"
    rf"{_CONTROL_BOUNDARY_RE}"
    rf"|{_MARKER_TOKEN}\s*{_INTERRUPT_RE}\s*"
    rf"{_CONTROL_FILLER_RE}{_CONTROL_EXPLICIT_OBJECT_RE}"
    rf"{_CONTROL_BOUNDARY_RE}"
    rf")"
)
_TERMINATE_CONTROL_RE = re.compile(
    rf"(?ix)(?:"
    rf"{_TERMINATE_RE}\s*{_CONTROL_FILLER_RE}"
    rf"(?:{_CONTROL_TARGET_MARKER_OBJECT_RE}|{_CONTROL_MARKER_OBJECT_RE})"
    rf"{_CONTROL_BOUNDARY_RE}"
    rf"|{_MARKER_TOKEN}\s*{_TERMINATE_RE}\s*"
    rf"{_CONTROL_FILLER_RE}{_CONTROL_EXPLICIT_OBJECT_RE}"
    rf"{_CONTROL_BOUNDARY_RE}"
    rf")"
)
_CONTROL_POLITE_PREFIX_RE = re.compile(
    r"(?ix)^(?:请(?:\s*先)?|先|麻烦(?:\s*先)?|"
    r"please(?:\s+first)?|first)$"
)
_TASK_CLAUSE_SPLIT_RE = re.compile(
    r"(?ix)"
    r"(?:\s*(?:但是|不过|而是|但|并且|以及|且|和)\s*)"
    r"|(?:\s+\b(?:and|then|but)\b\s*)"
    r"|(?=\s*(?:不要|别|禁止|无需|不必|不让|不交给|不启动|"
    r"不调用|不使用|不用|不需要|do\s+not|don't|never|without))"
)
_NEGATED_TASK_CLAUSE_RE = re.compile(
    r"(?ix)^\s*(?:不要|别|禁止|无需|不必|不让|不交给|不启动|"
    r"不调用|不使用|不用|不需要|do\s+not|don't|never|without)"
)
_NON_EXECUTION_CLAUSE_RE = re.compile(
    r"(?ix)^\s*(?:你\s*)?(?:支持|能否|是否|可以|能|会不会|了解|知道)"
)


def _has_task_evidence(surface: str) -> bool:
    """只接受正向可执行子句，排除安全限制和能力询问。"""

    for raw_clause in _TASK_CLAUSE_SPLIT_RE.split(surface):
        clause = raw_clause.strip()
        if not clause:
            continue
        if (
            _NEGATED_TASK_CLAUSE_RE.match(clause)
            or _NON_EXECUTION_CLAUSE_RE.match(clause)
        ):
            continue
        if _TASK_RE.search(clause):
            return True
    return False


def _is_send_instruction_request(surface: str) -> bool:
    """只授权明确指向既有受管会话且带有新任务正文的续接请求。"""

    return bool(
        _SEND_INSTRUCTION_REQUEST_RE.search(surface)
        and _has_task_evidence(surface)
    )


def _has_standalone_control_match(
    pattern: re.Pattern[str],
    surface: str,
) -> bool:
    """仅接受独立的控制句，避免把任务正文中的控制动词冒泡为权限操作。"""

    match = pattern.search(surface)
    if match is None:
        return False
    prefix = surface[: match.start()].strip()
    return not prefix or _CONTROL_POLITE_PREFIX_RE.fullmatch(prefix) is not None


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


def _candidate_surface(surface: str) -> str:
    """去除局部引用片段，保留后续独立的直接请求。"""

    candidates: list[str] = []
    for raw_segment in _SEGMENT_SPLIT_RE.split(surface):
        segment = raw_segment.strip()
        if not segment:
            continue
        if (
            _MARKER_RE.search(segment)
            and _REFERENCE_SEGMENT_RE.match(segment)
        ):
            continue
        candidates.append(segment)
    return " ".join(candidates).strip()


class ClaudeCodeExplicitRequestDetector:
    """只分析当前一条真实人类消息，不读取历史、模型输出或 Tool 参数。"""

    def detect(self, message: object) -> ClaudeCodeExplicitRequest | None:
        if not isinstance(message, str):
            return None
        surface = _message_surface(message).strip()
        if not surface:
            return None
        candidate_surface = _candidate_surface(surface)
        if not candidate_surface or not _MARKER_RE.search(candidate_surface):
            return None
        if (
            _NEGATION_RE.search(candidate_surface)
            or _WHOLE_QUOTED_RE.fullmatch(candidate_surface)
        ):
            return None

        if _has_standalone_control_match(
            _TERMINATE_CONTROL_RE,
            candidate_surface,
        ):
            return ClaudeCodeExplicitRequest(
                ClaudeCodeRequestOperation.TERMINATE
            )
        if _has_standalone_control_match(
            _INTERRUPT_CONTROL_RE,
            candidate_surface,
        ):
            return ClaudeCodeExplicitRequest(
                ClaudeCodeRequestOperation.REQUEST_INTERRUPT
            )
        if _POLL_QUERY_RE.search(candidate_surface):
            return ClaudeCodeExplicitRequest(ClaudeCodeRequestOperation.POLL)

        if _is_send_instruction_request(candidate_surface):
            return ClaudeCodeExplicitRequest(
                ClaudeCodeRequestOperation.SEND_INSTRUCTION
            )

        if (
            _START_INTENT_RE.search(candidate_surface)
            and _has_task_evidence(candidate_surface)
        ):
            return ClaudeCodeExplicitRequest(ClaudeCodeRequestOperation.START)
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
