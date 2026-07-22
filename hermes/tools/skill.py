"""
skill 工具:本地 skill 子系统(view / list / manage / patch)。

每个 skill 是 ``HERMES_HOME/skills/<name>/SKILL.md``,YAML frontmatter +
Markdown body。所有写操作走"文件锁 + 原子替换"完整事务;所有外部输入
(name)走严格校验,resolve 后必须仍在 skills 根目录子树内。
"""

from __future__ import annotations

from dataclasses import asdict
import json
import os
import re
import shutil
from pathlib import Path

import yaml

from hermes._io_utils import LockTimeout, atomic_write_text, file_lock
from hermes.config import HERMES_HOME
from hermes.skill_security import get_skill_trust_state, scan_skill_content


SKILLS_DIR = HERMES_HOME / "skills"

# 文件锁参数
_LOCK_TIMEOUT = 5.0
_LOCK_POLL = 0.05

# skill name 只允许字母数字下划线短横线
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# frontmatter 顶层字段白名单
_ALLOWED_FM_FIELDS = {"name", "description", "version", "platforms", "metadata"}


# ---------------------------------------------------------------------------
# 路径与名称校验(单一入口,所有路径生成都走这里)
# ---------------------------------------------------------------------------

def _validate_name(name: str) -> tuple[bool, str]:
    """skill 名称校验。拒绝空、``.``、``..``、路径分隔符、绝对路径等。"""
    if not name:
        return False, "name is empty"
    if name in (".", ".."):
        return False, f"name must not be {name!r}"
    if not _NAME_RE.match(name):
        return False, "name must match [A-Za-z0-9_-]+"
    return True, ""


def _resolve_skill_dir(name: str) -> tuple[Path | None, str]:
    """返回 skill 目录的绝对路径并做防御性校验。

    校验两步:
      1. 名称通过 ``_validate_name``
      2. resolve 后必须仍在 SKILLS_DIR 子树内(拒绝符号链接 / ``../`` 等)
    返回 (path, reason);path 为 None 表示拒绝,reason 给出原因。
    """
    ok, reason = _validate_name(name)
    if not ok:
        return None, reason

    root_real = SKILLS_DIR.resolve()
    target_real = (SKILLS_DIR / name).resolve()
    if target_real == root_real:
        return None, "name resolves to skills root"
    if not str(target_real).startswith(str(root_real) + os.sep):
        return None, "resolved path escapes skills root"
    if target_real.parent != root_real:
        # 必须是直接子目录,不允许更深嵌套
        return None, "name must be a direct child of skills root"
    return target_real, ""


# ---------------------------------------------------------------------------
# frontmatter 安全解析
# ---------------------------------------------------------------------------

def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """把 ``---`` 包裹的 frontmatter 与正文拆开。返回 (fm_text, body) 或 None。"""
    if not text.startswith("---"):
        return None  # 无 frontmatter
    # 用行边界精确匹配,避免 body 里的 --- 被误判
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    fm_lines: list[str] = []
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            fm_text = "".join(fm_lines)
            body = "".join(lines[i + 1:]).lstrip("\n")
            return fm_text, body
        fm_lines.append(line)
    return None  # 没有闭合 ---


def _parse_frontmatter_safe(text: str) -> tuple[dict, str, str | None]:
    """安全解析 SKILL.md。返回 (metadata, body, error)。

    metadata 字段从白名单 {name, description, version, platforms, metadata} 选取,
    其它字段过滤掉。error 非空表示解析失败。
    """
    split = _split_frontmatter(text)
    if split is None:
        # 无 frontmatter —— 整个文本视作 body,允许但 metadata 为空
        return {}, text.lstrip(), None

    fm_text, body = split
    try:
        data = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        return {}, text, f"frontmatter yaml parse error: {exc}"

    if not isinstance(data, dict):
        return {}, text, "frontmatter top level must be a mapping"

    cleaned = {k: v for k, v in data.items() if k in _ALLOWED_FM_FIELDS}
    return cleaned, body, None


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """兼容旧接口:返回 (metadata, body)。出错时返回 ({}, 原文)。"""
    metadata, body, error = _parse_frontmatter_safe(text)
    if error:
        return {}, text
    return metadata, body

# 把技能信息重新渲染成带 frontmatter 的 Markdown
def _render_skill(
    name: str,
    body: str,
    *,
    description: str = "",
    version: str | None = None,
    platforms=None,
    metadata=None,
) -> str:
    """渲染 frontmatter + body。frontmatter 用 yaml.safe_dump 保证序列化正确。"""
    fm: dict = {"name": name}
    if description:
        fm["description"] = description
    if version:
        fm["version"] = version
    if platforms:
        fm["platforms"] = platforms
    if metadata:
        fm["metadata"] = metadata

    fm_text = yaml.safe_dump(
        fm, default_flow_style=False, allow_unicode=True, sort_keys=False,
    ).strip()
    return f"---\n{fm_text}\n---\n\n{body}"


# ---------------------------------------------------------------------------
# 原子写入 + 文件锁(同 memory.py 实现思路)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# JSON 返回 helper
# ---------------------------------------------------------------------------

def _json(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _ok(**info) -> str:
    return _json({"ok": True, **info})


def _err(error_type: str, error: str, **extra) -> str:
    return _json({"ok": False, "error_type": error_type, "error": error, **extra})


# ---------------------------------------------------------------------------
# 工具入口
# ---------------------------------------------------------------------------

def discover_skills() -> list[dict]:
    """列出所有 skill 摘要(name/description/version/relative_path/error)。"""
    if not SKILLS_DIR.exists():
        return []

    out: list[dict] = []
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        # 复用 name 校验:目录名非法就直接跳过,不抛错,不影响后续扫描。
        # 否则会出现"列表能看到但 view/manage 拒绝操作"的尴尬。
        ok_name, _ = _validate_name(skill_dir.name)
        if not ok_name:
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue

        rel = f"skills/{skill_dir.name}"
        text = skill_file.read_text(encoding="utf-8")
        metadata, _, error = _parse_frontmatter_safe(text)

        entry: dict = {
            "name": metadata.get("name", skill_dir.name) if not error else skill_dir.name,
            "description": metadata.get("description", "") if not error else "",
            "relative_path": rel,
        }
        if not error:
            if "version" in metadata:
                entry["version"] = metadata["version"]
            if "platforms" in metadata:
                entry["platforms"] = metadata["platforms"]
            if "metadata" in metadata:
                entry["metadata"] = metadata["metadata"]
        else:
            entry["error"] = error
        out.append(entry)
    return out


def _load_skill_payload(name: str) -> dict:
    """加载单个 skill 的完整内容,返回结构化字典。

    供本模块的工具入口和对外接口 ``load_skill_body`` 共用。返回的字典
    使用与工具结果一致的键名(ok/error_type/name/body/...),但调用方
    可以直接当作数据结构使用,不需要再 JSON 解析。

    成功时返回 {"ok": True, "name": ..., "body": ..., ...};
    失败时返回 {"ok": False, "error_type": ..., "error": ..., ...}。
    """
    skill_dir, reason = _resolve_skill_dir(name)
    if skill_dir is None:
        return {"ok": False, "error_type": "invalid_name", "error": reason}

    skill_file = skill_dir / "SKILL.md"
    if not skill_file.exists():
        return {
            "ok": False,
            "error_type": "not_found",
            "error": f"skill {name!r} does not exist",
            "name": name,
        }

    text = skill_file.read_text(encoding="utf-8")
    metadata, body, error = _parse_frontmatter_safe(text)
    if error:
        return {
            "ok": False,
            "error_type": "parse_error",
            "error": error,
            "name": name,
            "relative_path": f"skills/{name}",
        }

    risk_report = scan_skill_content(body)
    risk = {
        "level": risk_report.risk_level,
        "findings": [asdict(finding) for finding in risk_report.findings],
    }
    trust_state = get_skill_trust_state(name, text)
    return {
        "ok": True,
        "name": metadata.get("name", name),
        "relative_path": f"skills/{name}",
        "risk": risk,
        "trusted": trust_state.trusted,
        "trust_stale": trust_state.trust_stale,
        "description": metadata.get("description", ""),
        "version": metadata.get("version"),
        "platforms": metadata.get("platforms"),
        "metadata": metadata.get("metadata"),
        "body": body,
    }


def load_skill_body(name: str) -> dict:
    """加载单个 skill 的完整内容,返回结构化数据。

    供需要 skill 正文的外部模块(如 cron 预加载)使用,不走工具 handler
    的 JSON 返回格式。调用方直接拿字典,不需要再 json.loads。

    返回的字典键名与工具结果一致:成功时含 ok/name/body/description 等;
    失败时 ok=False 且 error_type 说明原因。本函数不做风险等级拦截--
    需要按风险等级决定是否使用 skill 的调用方,应自行读取 ``risk`` 字段。
    """
    return _load_skill_payload(name)


def render_skills_section() -> str | None:
    """渲染可用 skill 列表为 system prompt 段落。

    返回拼好的纯文本段落;无 skill 时返回 None。调用方不感知字段名、
    渲染格式、风险等级显示策略,这些细节由本模块独占。
    """
    skills = discover_skills()
    if not skills:
        return None
    lines = [
        f"- **{skill['name']}**: {skill['description']}"
        for skill in skills
    ]
    return "# Available Skills\n" + "\n".join(lines)


def handle_skill_view(args, **kwargs):
    """按 name 加载完整正文，并返回风险报告与当前内容版本的信任状态。"""
    name = args.get("name", "")
    payload = _load_skill_payload(name)
    if not payload.get("ok"):
        # _err 期望关键字参数,这里把 payload 里的字段透传
        return _err(
            payload.get("error_type", "unknown"),
            payload.get("error", "skill load failed"),
            **{k: v for k, v in payload.items() if k not in ("ok", "error_type", "error")},
        )

    risk_report_level = payload["risk"]["level"]
    common = {
        "name": payload["name"],
        "relative_path": payload["relative_path"],
        "risk": payload["risk"],
        "trusted": payload["trusted"],
        "trust_stale": payload["trust_stale"],
    }

    # Gateway 等无人值守调用没有可靠的交互确认能力；CLI 仍完整展示正文。
    if kwargs.get("interactive_approval") is False:
        if risk_report_level == "high":
            return _err(
                "safety_blocked",
                "high-risk skill is blocked in unattended mode",
                status="blocked",
                requires_confirmation=True,
                **common,
            )
        if risk_report_level == "medium" and not payload["trusted"]:
            return _err(
                "permission_denied",
                "untrusted medium-risk skill requires interactive confirmation",
                status="confirmation_required",
                requires_confirmation=True,
                **common,
            )

    return _ok(
        description=payload["description"],
        version=payload["version"],
        platforms=payload["platforms"],
        metadata=payload["metadata"],
        body=payload["body"],
        **common,
    )


def handle_skill_list(args, **kwargs):
    """列出所有 skill 摘要。"""
    skills = discover_skills()
    return _ok(skills=skills, count=len(skills))


def handle_skill_manage(args, **kwargs):
    """skill 写操作:create / edit / delete / patch。"""
    action = args.get("action", "")
    name = args.get("name", "")

    if action == "create":
        return _do_create(name, args)
    if action == "edit":
        return _do_edit(name, args)
    if action == "delete":
        return _do_delete(name)
    if action == "patch":
        return _do_patch(name, args)
    return _err("unknown_action", f"unknown action: {action!r}")


# --- 各 action ---

def _do_create(name: str, args: dict) -> str:
    skill_dir, reason = _resolve_skill_dir(name)
    if skill_dir is None:
        return _err("invalid_name", reason)

    skill_file = skill_dir / "SKILL.md"
    if skill_file.exists():
        return _err("exists", f"skill {name!r} already exists", name=name)

    body = args.get("body", "")
    description = args.get("description", "")
    version = args.get("version")
    platforms = args.get("platforms")
    metadata = args.get("metadata")
    content = _render_skill(
        name, body, description=description,
        version=version, platforms=platforms, metadata=metadata,
    )

    try:
        # 锁文件需要父目录存在,先建目录(mkdir 本身幂等,即使没拿到锁也无害)
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        skill_dir.mkdir(parents=True, exist_ok=True)
        with file_lock(skill_file):
            # 锁内二次检查,防并发 create
            if skill_file.exists():
                return _err("exists", f"skill {name!r} was created concurrently", name=name)
            atomic_write_text(skill_file, content)
    except LockTimeout:
        return _err("lock_timeout", "could not acquire skill file lock")
    except OSError as exc:
        return _err("io_error", str(exc))

    return _ok(name=name, action="create", size=len(content))


def _do_edit(name: str, args: dict) -> str:
    skill_dir, reason = _resolve_skill_dir(name)
    if skill_dir is None:
        return _err("invalid_name", reason)

    skill_file = skill_dir / "SKILL.md"
    if not skill_file.exists():
        return _err("not_found", f"skill {name!r} does not exist", name=name)

    try:
        with file_lock(skill_file):
            text = skill_file.read_text(encoding="utf-8")
            metadata, old_body, error = _parse_frontmatter_safe(text)
            if error:
                return _err("parse_error", error, name=name)

            # None 表示调用方未提供,保留原值
            new_description = (
                args["description"] if args.get("description") is not None
                else metadata.get("description", "")
            )
            new_body = args.get("body") if args.get("body") is not None else old_body
            new_version = (
                args.get("version") if args.get("version") is not None
                else metadata.get("version")
            )
            new_platforms = (
                args.get("platforms") if args.get("platforms") is not None
                else metadata.get("platforms")
            )
            new_metadata = (
                args.get("metadata") if args.get("metadata") is not None
                else metadata.get("metadata")
            )

            content = _render_skill(
                name, new_body, description=new_description,
                version=new_version, platforms=new_platforms, metadata=new_metadata,
            )
            atomic_write_text(skill_file, content)
    except LockTimeout:
        return _err("lock_timeout", "could not acquire skill file lock")
    except OSError as exc:
        return _err("io_error", str(exc))

    return _ok(name=name, action="edit", size=len(content))


def _do_delete(name: str) -> str:
    skill_dir, reason = _resolve_skill_dir(name)
    if skill_dir is None:
        return _err("invalid_name", reason)

    if not skill_dir.exists():
        return _err("not_found", f"skill {name!r} does not exist", name=name)

    # 防御:_resolve_skill_dir 已保证 resolve 在 SKILLS_DIR 直接子树,
    # 这里再核对一次 realpath,防 SKILLS_DIR 在 mkdir 后被换链接。
    real_target = skill_dir.resolve()
    real_root = SKILLS_DIR.resolve()
    if real_target == real_root:
        return _err("forbidden", "cannot delete skills root directory")
    if real_target.parent != real_root:
        return _err("forbidden", "target is not a direct child of skills root")

    try:
        shutil.rmtree(skill_dir)
    except OSError as exc:
        return _err("io_error", str(exc))

    return _ok(name=name, action="delete")


def _do_patch(name: str, args: dict) -> str:
    skill_dir, reason = _resolve_skill_dir(name)
    if skill_dir is None:
        return _err("invalid_name", reason)

    skill_file = skill_dir / "SKILL.md"
    if not skill_file.exists():
        return _err("not_found", f"skill {name!r} does not exist", name=name)

    old_text = args.get("old_text", "")
    new_text = args.get("new_text", "")

    if not old_text:
        return _err("invalid_args", "old_text is required for patch")
    if new_text == old_text:
        return _err("invalid_args", "new_text must differ from old_text")

    try:
        with file_lock(skill_file):
            text = skill_file.read_text(encoding="utf-8")
            count = text.count(old_text)
            if count == 0:
                return _err("no_match", f"old_text not found in skill {name!r}", name=name)
            if count > 1:
                return _err(
                    "ambiguous_match",
                    f"old_text appears {count} times; provide more specific old_text",
                    name=name, match_count=count,
                )
            new_content = text.replace(old_text, new_text, 1)
            atomic_write_text(skill_file, new_content)
    except LockTimeout:
        return _err("lock_timeout", "could not acquire skill file lock")
    except OSError as exc:
        return _err("io_error", str(exc))

    return _ok(name=name, action="patch", size=len(new_content))


# ---------------------------------------------------------------------------
# 工具注册
# ---------------------------------------------------------------------------

def register(registry):
    """注册 skill_view / skills_list / skill_manage 三个工具。"""
    registry.register(
        name="skill_view",
        toolset="skill_read",
        schema={
            "name": "skill_view",
            "description": (
                "Load full content of a skill by name (frontmatter + body). "
                "Returns structured JSON with name/description/version/platforms/"
                "metadata/body fields plus risk and content-bound trust state. "
                "Skills live under skills/<name>/SKILL.md."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "skill name; must match [A-Za-z0-9_-]+",
                    },
                },
                "required": ["name"],
            },
        },
        handler=handle_skill_view,
        execution_environments=("cli", "gateway", "cron", "delegate"),
        unattended_allowed=True,
        approval_mode="none",
        risk_level="low",
        default_enabled_environments=("cli", "cron"),
    )
    registry.register(
        name="skills_list",
        toolset="skill_read",
        schema={
            "name": "skills_list",
            "description": (
                "List all available skills with name, description, version, "
                "relative_path and metadata summary."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        handler=handle_skill_list,
        execution_environments=("cli", "gateway", "cron", "delegate"),
        unattended_allowed=True,
        approval_mode="none",
        risk_level="low",
        default_enabled_environments=("cli", "cron"),
    )
    registry.register(
        name="skill_manage",
        toolset="skill_manage",
        schema={
            "name": "skill_manage",
            "description": (
                "Create / edit / delete / patch a skill. Names must match "
                "[A-Za-z0-9_-]+; path traversal is rejected. Writes are "
                "serialized via a per-file lock and applied atomically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "edit", "delete", "patch"],
                    },
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "version": {"type": "string"},
                    "platforms": {"type": "array", "items": {"type": "string"}},
                    "metadata": {"type": "object"},
                    "body": {
                        "type": "string",
                        "description": "create/edit: full Markdown body",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "patch: unique substring to find",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "patch: replacement text",
                    },
                },
                "required": ["action", "name"],
            },
        },
        handler=handle_skill_manage,
        execution_environments=("cli", "gateway", "cron"),
        unattended_allowed=True,
        approval_mode="none",
        risk_level="medium",
        default_enabled_environments=("cli", "cron"),
    )
