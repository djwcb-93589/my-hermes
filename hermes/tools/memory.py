"""
memory 工具：长期记忆存储（MEMORY.md / USER.md）。

条目之间用 ``§`` 分隔。写操作走"文件锁 + 原子替换"完整事务：
  获取锁 → 重读最新 entries → 校验 → 写 tmp → fsync → os.replace → 释放锁
任何校验失败、写入失败时,旧文件保持不变。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from pathlib import Path

from hermes.config import (
    HERMES_HOME,
    MEMORY_CHAR_LIMIT,
    USER_CHAR_LIMIT,
)


MEMORY_DIR = HERMES_HOME / "memories"
MEMORY_FILE = MEMORY_DIR / "MEMORY.md"
USER_FILE = MEMORY_DIR / "USER.md"
ENTRY_SEP = "\n\n§\n\n"

# 文件锁参数：拿不到锁时轮询,超过 _LOCK_TIMEOUT 秒抛 _LockTimeout。
_LOCK_TIMEOUT = 5.0
_LOCK_POLL = 0.05


# ---------------------------------------------------------------------------
# 编码 / 解码
# ---------------------------------------------------------------------------

def parse_entries(text: str) -> list[str]:
    """把 ``§`` 分隔的正文拆成条目列表（去空白、过滤空段）。"""
    if not text.strip():
        return []
    return [entry.strip() for entry in text.split("§") if entry.strip()]


def render_entries(entries: list[str]) -> str:
    """把条目列表拼回 ``§`` 分隔的正文。"""
    return ENTRY_SEP.join(entries)


def load_memory(file_path: Path) -> list[str]:
    """从文件加载条目列表。文件不存在视作空。"""
    if not file_path.exists():
        return []
    return parse_entries(file_path.read_text(encoding="utf-8"))


def _current_chars(file_path: Path) -> int:
    """当前文件字符数；不存在算 0。"""
    if not file_path.exists():
        return 0
    return len(file_path.read_text(encoding="utf-8"))


def render_memory_section(
    *,
    include_long: bool = True,
    include_user: bool = True,
) -> str | None:
    """渲染长期记忆和用户档案为 system prompt 段落。

    返回拼好的纯文本段落;两类记忆都为空时返回 None。
    调用方不感知文件路径、分隔符和字符限额,这些细节由本模块独占。
    """
    sections: list[str] = []
    if include_long:
        section = _render_single_section(
            MEMORY_FILE, "# Memory", MEMORY_CHAR_LIMIT
        )
        if section is not None:
            sections.append(section)
    if include_user:
        section = _render_single_section(
            USER_FILE, "# User Profile", USER_CHAR_LIMIT
        )
        if section is not None:
            sections.append(section)
    if not sections:
        return None
    return "\n\n".join(sections)


def _render_single_section(
    file_path: Path,
    header: str,
    char_limit: int,
) -> str | None:
    """渲染单个记忆文件为带标题的段落;空文件返回 None。"""
    entries = load_memory(file_path)
    if not entries:
        return None
    used = _current_chars(file_path)
    return (
        f"{header} ({len(entries)} entries, "
        f"{used}/{char_limit} chars)\n"
        f"{render_entries(entries)}"
    )


def _atomic_write_text(file_path: Path, text: str) -> None:
    """同目录写临时文件 → flush/fsync → os.replace 原子替换。

    异常时清理临时文件并重新抛出,旧文件不受影响。
    """
    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _try_save(
    file_path: Path,
    entries: list[str],
    char_limit: int,
) -> tuple[bool, dict]:
    """尝试写入。超限返回 (False, error_dict) 且不修改文件；成功返回 (True, info)。"""
    text = render_entries(entries)
    if len(text) > char_limit:
        return False, {
            "error_type": "limit_exceeded",
            "used_chars": _current_chars(file_path),
            "limit_chars": char_limit,
            "candidate_chars": len(text),
            "exceeds_by": len(text) - char_limit,
            "error": "write would exceed char limit; file unchanged",
        }
    file_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(file_path, text)
    return True, {"size": len(text)}


# ---------------------------------------------------------------------------
# 文件锁（标准库 O_CREAT | O_EXCL 实现,跨进程互斥）
# ---------------------------------------------------------------------------

class _LockTimeout(Exception):
    """文件锁等待超时。"""


def _lock_path_for(file_path: Path) -> Path:
    """锁文件与目标文件同目录,加 ``.lock`` 后缀。"""
    return file_path.with_suffix(file_path.suffix + ".lock")


def _acquire_lock(lock_path: Path, timeout: float = _LOCK_TIMEOUT) -> int:
    """``O_CREAT | O_EXCL`` 抢锁,返回 fd。超时抛 _LockTimeout。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise _LockTimeout()
            time.sleep(_LOCK_POLL)


def _release_lock(lock_path: Path, fd: int) -> None:
    try:
        os.close(fd)
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def _file_lock(file_path: Path, timeout: float = _LOCK_TIMEOUT):
    """简单的跨进程文件锁上下文。"""
    lock_path = _lock_path_for(file_path)
    fd = _acquire_lock(lock_path, timeout)
    try:
        yield
    finally:
        _release_lock(lock_path, fd)


# ---------------------------------------------------------------------------
# 匹配 / 校验
# ---------------------------------------------------------------------------

def _find_matches(entries: list[str], needle: str) -> list[int]:
    """子串匹配（大小写不敏感）,返回所有命中条目的下标。"""
    if not needle:
        return []
    nl = needle.lower()
    return [i for i, e in enumerate(entries) if nl in e.lower()]


def _is_duplicate(
    entries: list[str],
    content: str,
    exclude_idx: int | None = None,
) -> bool:
    """strip 后完全相同视作重复。``exclude_idx`` 用于 replace 时排除被替换项。"""
    cl = content.strip()
    if not cl:
        return False
    for i, e in enumerate(entries):
        if i == exclude_idx:
            continue
        if e.strip() == cl:
            return True
    return False


# 不可见 Unicode 控制字符：零宽空格、双向控制、BOM 等。
_INVISIBLE_RE = re.compile(
    "[​-‏ - ⁠-⁯﻿]"
)

# 轻量 prompt-injection / 凭据泄漏拦截。低耦合 helper,不做完整安全系统。
_DANGEROUS_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"ignore\s+(all\s+)?previous\s+(instructions|prompts|rules)",
        r"disregard\s+(all\s+)?(prior|previous|above)",
        r"forget\s+(all\s+)?(previous|prior)\s+(instructions|rules)",
        r"reveal\s+your\s+(system\s+)?(prompt|instructions)",
        r"\bapi[_\s-]?key\b",
        r"\bsecret[_\s-]?key\b",
        r"\baccess[_\s-]?token\b",
        r"\bbearer[_\s-]?token\b",
        r"\bprivate[_\s-]?key\b",
        r"\bpassword\b",
    ]
]


def _security_scan(text: str) -> tuple[bool, str]:
    """轻量安全检查,返回 (ok, reason)。reason 非空即拒绝。"""
    if _INVISIBLE_RE.search(text):
        return False, "blocked: contains invisible Unicode control characters"
    for pat in _DANGEROUS_PATTERNS:
        m = pat.search(text)
        if m:
            return False, f"blocked: pattern {m.group(0)!r}"
    return True, ""


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _json(obj: dict) -> str:
    """JSON 序列化,关闭 ensure_ascii 以便中文内容直读。"""
    return json.dumps(obj, ensure_ascii=False)


def _capacity(file_path: Path, char_limit: int) -> dict:
    return {"used_chars": _current_chars(file_path), "limit_chars": char_limit}


def _do_write(
    file_path: Path,
    char_limit: int,
    mutate,
    target: str,
) -> dict:
    """写事务：拿锁 → 重读 entries → mutate → 校验 → 原子写 → 释放锁。

    mutate(entries) -> (new_entries, info) | (None, error_info)。
    new_entries 为 None 表示校验失败,文件不写。
    """
    try:
        # 锁文件与目标文件同目录；必须先建目录再获取锁。
        file_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "target": target,
                "error_type": "io_error", "error": str(exc),
                **_capacity(file_path, char_limit)}

    try:
        with _file_lock(file_path):
            entries = load_memory(file_path)
            new_entries, info = mutate(entries)
            if new_entries is None:
                return {"ok": False, "target": target, **info,
                        **_capacity(file_path, char_limit)}
            try:
                ok, save_info = _try_save(file_path, new_entries, char_limit)
            except OSError as exc:
                # 写入异常：临时文件已自行清理,旧文件未变
                return {"ok": False, "target": target, **info,
                        "error_type": "io_error", "error": str(exc),
                        **_capacity(file_path, char_limit)}
            if not ok:
                return {"ok": False, "target": target, **info, **save_info}
            return {"ok": True, "target": target,
                    "entry_count": len(new_entries), **info, **save_info,
                    **_capacity(file_path, char_limit)}
    except _LockTimeout:
        return {"ok": False, "target": target, "error_type": "lock_timeout",
                "error": "could not acquire memory file lock in time",
                **_capacity(file_path, char_limit)}


def handle_memory(args, **kwargs):
    """memory 工具入口。按 action 分发。"""
    action = args.get("action", "")
    target = args.get("target", "memory")
    # 统一清理空白,避免"写进去但 read 时被 parse_entries 滤掉"
    content = (args.get("content") or "").strip()
    old_text = (args.get("old_text") or "").strip()

    if target not in ("memory", "user"):
        return _json({"ok": False, "error_type": "invalid_target",
                      "error": f"target must be 'memory' or 'user', got {target!r}"})

    file_path = USER_FILE if target == "user" else MEMORY_FILE
    char_limit = USER_CHAR_LIMIT if target == "user" else MEMORY_CHAR_LIMIT

    # read：只读,不强制加锁(写入走原子替换,读旧/新版本都可接受)
    if action == "read":
        entries = load_memory(file_path)
        return _json({
            "ok": True, "target": target, "entries": entries,
            "entry_count": len(entries), **_capacity(file_path, char_limit),
        })

    if action not in ("add", "remove", "replace"):
        return _json({"ok": False, "error_type": "unknown_action",
                      "error": f"unknown action: {action!r}"})

    # 写操作前置校验：分隔符 + 安全扫描（这些不依赖文件状态,放锁外做）
    if "§" in content or "§" in old_text:
        return _json({"ok": False, "target": target, "error_type": "invalid_content",
                      "error": "content must not contain the § separator",
                      **_capacity(file_path, char_limit)})

    for text in (content, old_text):
        ok, reason = _security_scan(text)
        if not ok:
            return _json({"ok": False, "target": target, "error_type": "blocked_content",
                          "error": reason, **_capacity(file_path, char_limit)})

    # 按 action 校验必填字段,然后定义 mutate 闭包
    if action == "add":
        if not content:
            return _json({"ok": False, "target": target, "error_type": "invalid_args",
                          "error": "content is required for add",
                          **_capacity(file_path, char_limit)})

        def mutate(entries):
            if _is_duplicate(entries, content):
                return None, {"error_type": "duplicate",
                              "error": "an identical entry already exists; not written"}
            return entries + [content], {"action": "add"}

    elif action == "remove":
        if not content:
            return _json({"ok": False, "target": target, "error_type": "invalid_args",
                          "error": "content (substring to match) is required for remove",
                          **_capacity(file_path, char_limit)})

        def mutate(entries):
            matches = _find_matches(entries, content)
            if not matches:
                return None, {"error_type": "no_match",
                              "error": f"no entry contains {content!r}"}
            if len(matches) > 1:
                return None, {
                    "error_type": "ambiguous_match",
                    "error": f"multiple entries match {content!r}; provide more specific content",
                    "match_count": len(matches),
                    "matches": [entries[i] for i in matches[:5]],
                }
            idx = matches[0]
            new_entries = entries[:idx] + entries[idx + 1:]
            return new_entries, {"action": "remove"}

    else:  # replace
        if not content:
            return _json({"ok": False, "target": target, "error_type": "invalid_args",
                          "error": "content (new text) is required for replace",
                          **_capacity(file_path, char_limit)})
        if not old_text:
            return _json({"ok": False, "target": target, "error_type": "invalid_args",
                          "error": "old_text is required for replace",
                          **_capacity(file_path, char_limit)})

        def mutate(entries):
            matches = _find_matches(entries, old_text)
            if not matches:
                return None, {"error_type": "no_match",
                              "error": f"no entry contains old_text {old_text!r}"}
            if len(matches) > 1:
                return None, {
                    "error_type": "ambiguous_match",
                    "error": f"multiple entries match old_text {old_text!r}; provide more specific old_text",
                    "match_count": len(matches),
                    "matches": [entries[i] for i in matches[:5]],
                }
            idx = matches[0]
            # 重复检测:允许和被替换项相同,但不能和其他 entry 重复
            if _is_duplicate(entries, content, exclude_idx=idx):
                return None, {"error_type": "duplicate",
                              "error": "replacement text duplicates another existing entry"}
            new_entries = list(entries)
            new_entries[idx] = content
            return new_entries, {"action": "replace"}

    return _json(_do_write(file_path, char_limit, mutate, target))


def register(registry):
    registry.register(
        name="memory",
        toolset="memory",
        schema={
            "name": "memory",
            "description": (
                "Manage persistent memory (MEMORY.md or USER.md). Entries are "
                "joined with § separators. Actions: "
                "add (dedup by strip), remove (unique substring match), "
                "replace (unique old_text match → content), read. Writes that "
                "would exceed the char limit are rejected and the file stays "
                "unchanged. Response includes used_chars / limit_chars for "
                "capacity tracking. On ambiguous match, up to 5 candidate "
                "entries are returned in `matches`. Content with invisible "
                "Unicode or credential/injection patterns is blocked. Writes "
                "are serialized per-file via a lock and applied atomically."
                " Storage directories are initialized automatically; if an "
                "operation fails, report the structured error instead of using "
                "terminal to create or repair memory paths. Successful reads "
                "and writes return entry_count; writes also return action."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove", "replace", "read"],
                    },
                    "target": {
                        "type": "string",
                        "enum": ["memory", "user"],
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "add: new entry text; remove: substring to match; "
                            "replace: the replacement text. Whitespace is stripped."
                        ),
                    },
                    "old_text": {
                        "type": "string",
                        "description": "replace only: substring identifying the entry to replace. Whitespace is stripped.",
                    },
                },
                "required": ["action"],
            },
        },
        handler=handle_memory,
        execution_environments=("cli", "gateway", "cron"),
        unattended_allowed=True,
        approval_mode="none",
        risk_level="medium",
        default_enabled_environments=("cli", "cron"),
    )
