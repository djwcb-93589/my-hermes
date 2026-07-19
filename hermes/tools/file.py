"""file 工具：基于 backend 抽象的安全文件操作。

所有文件操作都通过当前 session 的 backend（按 session_key 取）。LocalBackend
直接读写本机；Docker/SSH 默认返回 ``unsupported_backend``，避免误读写宿主机。

路径守卫
--------
- 相对路径以 ``backend.cwd`` 为基准（terminal cd 会改 cwd）。
- LocalBackend 可访问操作系统用户有权限访问的全部本机路径。
- 用户配置的统一禁止路径在敏感文件判断和审批之前硬拒绝。
- 敏感文件（.env、私钥、数据库等）属于 critical，不能创建 grant。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from hermes.approval import (
    build_assessment_response,
    is_remote_approval,
)
from hermes.approval_policy import (
    DENY,
    approved_file_path_candidate,
    approved_file_snapshot_candidate,
    assess_file_operation,
    assess_path_policy_denial,
)
from hermes.backends import get_backend, UnsupportedBackendError
from hermes.config import SENSITIVE_FILE_PATTERNS
from hermes.file_state import (
    FileStateSnapshotError,
    capture_file_state_snapshot,
    file_state_snapshot_matches,
)
from hermes.path_policy import (
    ALLOW_ALL_PATH_POLICY,
    PathAccessDeniedError,
)
from hermes.redaction import redact_file_content


# 单次读取的字节上限。超过则 truncated=true，调用方再用 offset 续读。
READ_LIMIT = 100_000
REPLACE_LIMIT = READ_LIMIT
_CONTEXT_ACTIONS = {"pwd", "context"}
_PATH_ACTIONS = {"read", "read_range", "write", "append", "replace", "list", "stat"}

def _is_sensitive(abs_path: str) -> bool:
    """路径是否命中配置的敏感文件模式。"""
    norm = abs_path.replace("\\", "/").lower()
    return any(pat.search(norm) for pat in SENSITIVE_FILE_PATTERNS)


def _json(obj: dict) -> str:
    """JSON 序列化，关闭 ensure_ascii 以便中文内容直读。"""
    return json.dumps(obj, ensure_ascii=False)


def _file_context(backend) -> dict:
    cwd = getattr(backend, "cwd", "")
    path_policy = getattr(backend, "path_policy", ALLOW_ALL_PATH_POLICY)
    return {
        "cwd": cwd,
        "filesystem_scope": "host",
        "denied_paths_configured": path_policy.denied_paths_configured,
        "denied_paths_count": path_policy.denied_paths_count,
    }


def _file_error(backend, payload: dict) -> str:
    obj = {"ok": False, **payload}
    obj.update(_file_context(backend))
    return _json(obj)


def _file_exists(backend, abs_path: str) -> bool:
    """通过 backend 检查路径是否存在，避免 tool 层直接耦合本机 FS。"""
    try:
        backend.stat_file(abs_path)
        return True
    except FileNotFoundError:
        return False


def _requires_file_state_snapshot(args: dict) -> bool:
    """标记必须把审批时状态绑定进 grant 的修改操作。"""
    action = args.get("action")
    return (
        action in {"replace", "append"}
        or (action == "write" and bool(args.get("overwrite", False)))
    )


def handle_file(args, *, allow_sensitive: bool = False, **kwargs):
    """file 工具入口。按 action 分发到具体操作。"""
    action = args.get("action")
    rel_path = args.get("path", "")
    session_key = kwargs.get("session_key") or "default"
    backend = get_backend(session_key=session_key)
    remote_approval = is_remote_approval(kwargs)
    approval_grant = kwargs.get("approval_grant")

    # 该字段只允许可信内部调用通过 keyword-only 参数传入，模型 JSON 不得控制。
    if any(
        field in args
        for field in ("allow_sensitive", "approval_grant", "session_grant")
    ):
        return _file_error(
            backend,
            {
                "path": rel_path,
                "error_type": "invalid_args",
                "error": "unexpected internal-only argument",
            },
        )

    if action in _CONTEXT_ACTIONS:
        assessment = assess_file_operation(
            args,
            normalized_path=None,
            session_key=session_key,
            remote_approval=remote_approval,
            sensitive=False,
            allow_sensitive=False,
            approval_grant=approval_grant,
            security_policy=backend.tool_approval_policy,
            backend_context=backend.approval_risk_context(),
            intelligent_advisor=backend.intelligent_approval_advisor,
        )
        policy_response = build_assessment_response(
            assessment,
            f"执行 File {action} 操作",
            denial_payload=_file_context(backend),
        )
        if policy_response is not None:
            return policy_response
        return _json({"ok": True, **_file_context(backend)})

    if action not in _PATH_ACTIONS:
        return _file_error(
            backend,
            {
                "error_type": "unknown_action",
                "error": f"unknown action: {action!r}",
            },
        )

    if not isinstance(rel_path, str) or not rel_path.strip():
        return _file_error(
            backend,
            {
                "path": rel_path,
                "error_type": "invalid_args",
                "error": "path is required for this action",
            },
        )

    # Backend 先处理 cwd 和平台路径形式，策略再生成审批与执行共用的真实路径。
    try:
        resolved_path = backend.resolve_path(rel_path)
        path_policy = getattr(
            backend,
            "path_policy",
            ALLOW_ALL_PATH_POLICY,
        )
        abs_path = path_policy.require_allowed(
            resolved_path,
            cwd=backend.cwd,
        )
        approved_abs_path = approved_file_path_candidate(
            approval_grant,
            args,
            session_key=session_key,
        )
        if approved_abs_path is not None:
            # 审批恢复固定使用审批时展示的规范化绝对路径，避免 cwd 或路径文本漂移。
            abs_path = path_policy.require_allowed(
                approved_abs_path,
                cwd=backend.cwd,
            )
    except PathAccessDeniedError:
        return build_assessment_response(
            assess_path_policy_denial(
                "file",
                session_key=session_key,
            ),
            f"执行 File {action} 操作",
            denial_payload=_file_context(backend),
        )
    except Exception as exc:
        return _file_error(backend, {"path": rel_path,
                           "error_type": "invalid_path", "error": str(exc)})

    # denied_paths 已在 grant 解析前后各检查一次，审批只能影响后续敏感层。
    sensitive = _is_sensitive(abs_path)
    # hardline、用户路径规则和 sensitive 必须在快照读取与 grant 复检之前收敛。
    guardrail_assessment = assess_file_operation(
        args,
        normalized_path=abs_path,
        session_key=session_key,
        remote_approval=remote_approval,
        sensitive=sensitive,
        allow_sensitive=False,
        approval_grant=None,
        file_snapshot=None,
        security_policy=backend.tool_approval_policy,
        backend_context=backend.approval_risk_context(),
        intelligent_advisor=None,
    )
    if guardrail_assessment.decision == DENY:
        return build_assessment_response(
            guardrail_assessment,
            f"执行 File {action} 操作",
            denial_payload={
                "path": rel_path,
                "abs_path": abs_path,
                **_file_context(backend),
            },
        )
    file_snapshot = approved_file_snapshot_candidate(
        approval_grant,
        args,
        session_key=session_key,
    )
    if file_snapshot is not None:
        try:
            snapshot_matches = file_state_snapshot_matches(
                backend,
                abs_path,
                file_snapshot,
                path_policy=path_policy,
            )
        except PathAccessDeniedError:
            return build_assessment_response(
                assess_path_policy_denial(
                    "file",
                    session_key=session_key,
                ),
                f"执行 File {action} 操作",
                denial_payload=_file_context(backend),
            )
        except PermissionError:
            return _file_error(backend, {
                "path": rel_path,
                "abs_path": abs_path,
                "error_type": "os_permission_denied",
                "error": "permission denied while validating approved file state",
            })
        except (FileStateSnapshotError, OSError, ValueError):
            snapshot_matches = False
        if not snapshot_matches:
            return _file_error(backend, {
                "path": rel_path,
                "abs_path": abs_path,
                "error_type": "approval_stale",
                "error": (
                    "approved file state changed; request approval again"
                ),
                "fatal": False,
            })
    elif (
        remote_approval
        and approval_grant is None
        and not sensitive
        and _requires_file_state_snapshot(args)
    ):
        try:
            file_snapshot = capture_file_state_snapshot(
                backend,
                abs_path,
                path_policy=path_policy,
            )
        except UnsupportedBackendError as exc:
            return _file_error(backend, {
                "path": rel_path,
                "abs_path": abs_path,
                "error_type": "unsupported_backend",
                "error": str(exc),
            })
        except PermissionError:
            return _file_error(backend, {
                "path": rel_path,
                "abs_path": abs_path,
                "error_type": "os_permission_denied",
                "error": "permission denied while capturing file state",
            })
        except (FileStateSnapshotError, OSError, ValueError):
            return _file_error(backend, {
                "path": rel_path,
                "abs_path": abs_path,
                "error_type": "approval_snapshot_unavailable",
                "error": "could not capture a stable file state for approval",
            })

    assessment = assess_file_operation(
        args,
        normalized_path=abs_path,
        session_key=session_key,
        remote_approval=remote_approval,
        sensitive=sensitive,
        allow_sensitive=allow_sensitive is True,
        approval_grant=approval_grant,
        file_snapshot=file_snapshot,
        security_policy=backend.tool_approval_policy,
        backend_context=backend.approval_risk_context(),
        intelligent_advisor=backend.intelligent_approval_advisor,
    )
    denial_payload = {
        "path": rel_path,
        "abs_path": abs_path,
        **_file_context(backend),
    }
    policy_response = build_assessment_response(
        assessment,
        f"执行 File {action} 操作",
        denial_payload=denial_payload,
    )
    if policy_response is not None:
        return policy_response

    try:
        if action == "read":
            return _do_read(backend, abs_path, rel_path, args, require_range=False)
        if action == "read_range":
            return _do_read(backend, abs_path, rel_path, args, require_range=True)
        if action == "write":
            return _do_write(backend, abs_path, rel_path, args)
        if action == "append":
            return _do_append(backend, abs_path, rel_path, args)
        if action == "replace":
            return _do_replace(backend, abs_path, rel_path, args)
        if action == "list":
            return _do_list(backend, abs_path, rel_path)
        if action == "stat":
            return _do_stat(backend, abs_path, rel_path)
    except UnsupportedBackendError as exc:
        return _file_error(backend, {"path": rel_path,
                           "error_type": "unsupported_backend", "error": str(exc)})
    except FileNotFoundError:
        return _file_error(backend, {"path": rel_path,
                           "error_type": "not_found", "error": "file does not exist"})
    except PermissionError:
        return _file_error(backend, {"path": rel_path,
                           "error_type": "os_permission_denied",
                           "error": "operating system denied access to the path"})
    except IsADirectoryError:
        return _file_error(backend, {"path": rel_path,
                           "error_type": "is_directory", "error": "path is a directory"})
    except NotADirectoryError:
        return _file_error(backend, {"path": rel_path,
                           "error_type": "not_directory",
                           "error": "expected a directory"})
    except Exception as exc:
        return _file_error(backend, {"path": rel_path,
                           "error_type": "io_error", "error": str(exc)})


# --- 各 action 的处理 ---

def _do_read(backend, abs_path, rel_path, args, require_range: bool):
    """read / read_range 共用：按字节读，UTF-8 解码（errors=replace）。"""
    offset = int(args.get("offset", 0))
    limit_arg = args.get("limit")

    if require_range:
        if offset < 0 or not limit_arg or int(limit_arg) <= 0:
            return _file_error(backend, {"error_type": "invalid_args",
                               "error": "read_range requires offset>=0 and limit>0"})

    limit = READ_LIMIT if limit_arg is None else min(int(limit_arg), READ_LIMIT)
    data = backend.read_file(abs_path, offset=offset, limit=limit)
    size = len(data)
    # stat 一次拿总大小，truncated 严格按"是否还有未读字节"判断
    total = None
    truncated = False
    try:
        total = backend.stat_file(abs_path)["size"]
        truncated = offset + size < total
    except Exception:
        pass  # ponytail: stat 失败就保守当未截断
    decoded = data.decode("utf-8", errors="replace")
    return _json({
        "ok": True,
        "path": rel_path,
        "abs_path": abs_path,
        # 只处理返回给模型的副本，磁盘内容和写入类 action 均保持原样。
        "content": redact_file_content(decoded),
        "size": size,
        "offset": offset,
        "total_size": total,
        "truncated": truncated,
        **_file_context(backend),
    })


def _do_write(backend, abs_path, rel_path, args):
    """write：默认不覆盖；overwrite=true 时原子替换。"""
    content = args.get("content", "")
    overwrite = bool(args.get("overwrite", False))

    if _file_exists(backend, abs_path) and not overwrite:
        return _file_error(backend, {"path": rel_path, "abs_path": abs_path,
                           "error_type": "exists",
                           "error": "file exists; pass overwrite=true to replace"})

    data = content.encode("utf-8")
    backend.write_file(abs_path, data, mode="write")
    return _json({"ok": True, "path": rel_path, "abs_path": abs_path,
                  "size": len(data), "mode": "write",
                  **_file_context(backend)})


def _do_append(backend, abs_path, rel_path, args):
    """append：在末尾追加，不存在则新建。"""
    content = args.get("content", "")
    data = content.encode("utf-8")
    backend.write_file(abs_path, data, mode="append")
    return _json({"ok": True, "path": rel_path, "abs_path": abs_path,
                  "appended": len(data), "mode": "append",
                  **_file_context(backend)})


def _do_replace(backend, abs_path, rel_path, args):
    """replace_in_file：找到 find 字符串替换成 replace。默认替换全部匹配。"""
    find = args.get("find", "")
    replace_text = args.get("replace", "")
    replace_all = bool(args.get("all", True))

    if not find:
        return _file_error(backend, {"error_type": "invalid_args",
                           "error": "find is required"})

    info = backend.stat_file(abs_path)
    if not info.get("is_file"):
        return _file_error(backend, {"path": rel_path, "abs_path": abs_path,
                           "error_type": "not_file",
                           "error": "replace only supports regular files"})

    file_size = int(info.get("size", 0))
    if file_size > REPLACE_LIMIT:
        return _file_error(backend, {"path": rel_path, "abs_path": abs_path,
                           "error_type": "file_too_large",
                           "error": (
                               f"replace supports files up to {REPLACE_LIMIT} bytes"
                           ),
                           "size": file_size, "limit": REPLACE_LIMIT})

    data = backend.read_file(abs_path, offset=0, limit=file_size)
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return _file_error(backend, {"path": rel_path, "abs_path": abs_path,
                           "error_type": "decode_error",
                           "error": f"file is not valid UTF-8 text: {exc}"})

    count = content.count(find)
    if count == 0:
        return _file_error(backend, {"path": rel_path,
                           "error_type": "not_found_in_file",
                           "error": "find string not present in file"})

    if replace_all:
        new_content = content.replace(find, replace_text)
        done = count
    else:
        new_content = content.replace(find, replace_text, 1)
        done = 1

    backend.write_file(abs_path, new_content.encode("utf-8"), mode="write")
    return _json({"ok": True, "path": rel_path, "abs_path": abs_path,
                  "replacements": done, "matches_found": count,
                  "replaced_all": replace_all, **_file_context(backend)})


def _do_list(backend, abs_path, rel_path):
    """list：返回目录下条目名。"""
    entries = backend.list_dir(abs_path)
    return _json({"ok": True, "path": rel_path, "abs_path": abs_path,
                  "entries": entries, "count": len(entries),
                  **_file_context(backend)})


def _do_stat(backend, abs_path, rel_path):
    """stat：返回文件元数据。"""
    info = backend.stat_file(abs_path)
    mtime = info.get("mtime")
    try:
        utc_time = datetime.fromtimestamp(mtime, timezone.utc)
        local_time = utc_time.astimezone()
    except (OSError, OverflowError, TypeError, ValueError):
        pass
    else:
        info["mtime_utc"] = utc_time.isoformat(timespec="seconds")
        info["mtime_local"] = local_time.isoformat(timespec="seconds")
        info["mtime_timezone"] = local_time.tzname() or str(local_time.tzinfo)
    return _json({"ok": True, "path": rel_path, "abs_path": abs_path,
                  **info, **_file_context(backend)})


def register(registry):
    registry.register(
        name="file",
        toolset="file",
        schema={
            "name": "file",
            "description": (
                "IMPORTANT PATH RULE: every relative path resolves from the "
                "current session cwd, which is shared with and persisted by "
                "the terminal tool. After terminal changes directory, use a "
                "path relative to that new cwd; never prefix the cwd directory "
                "name again. Local file operations use host scope and may "
                "access paths outside the project when the operating system "
                "allows it. Prefer "
                "this tool over terminal for file content, directory listings, "
                "and metadata. Actions: "
                "read, read_range, write, append, replace, list, stat, "
                "pwd, context. "
                "Gateway remote sessions run pwd/context and ordinary "
                "stat/list/read/read_range operations without approval when "
                "the normalized path is neither blocked nor sensitive. "
                "write/append/replace and sensitive path operations require "
                "different handling: ordinary modifications require a "
                "once approval, while critical sensitive paths are denied. "
                "Approval binds the complete arguments, normalized absolute "
                "path, and operation fingerprint. Do not retry an operation "
                "while approval is pending. "
                "replace, overwrite writes, and append operations also bind "
                "a file-state snapshot; execution returns "
                "error_type=approval_stale if the target changes first. "
                "Paths are relative to backend.cwd unless absolute. "
                "Paths blocked by the shared filesystem policy are always "
                "rejected with error_type=path_policy_denied before approval; "
                "do not try another tool to bypass that result. Critical "
                "hardline protected paths and configured File action/path "
                "deny rules are also evaluated before once/session grants. "
                "Structured File path checks are strong policy enforcement; "
                "they do not rely on Terminal command parsing. "
                "Paths matching configured sensitive file patterns are "
                "critical and denied rather than approvable. "
                "write defaults to no-overwrite; "
                "pass overwrite=true to replace (atomic via tmp + os.replace). "
                "Reads capped at 100KB; truncated=true means more data "
                "available. replace only supports UTF-8 files up to 100KB. "
                "Call again with offset for ranged reads. Docker/SSH backends "
                "stat returns mtime_utc and mtime_local with timezone data. "
                "All successful results include cwd, filesystem_scope, and "
                "the configured denied-path count without exposing paths. Docker/SSH "
                "backends return error_type=unsupported_backend for IO actions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "read_range", "write", "append",
                                 "replace", "list", "stat", "pwd", "context"],
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative or absolute path; not required for "
                            "pwd/context. Relative paths start at the current "
                            "session cwd shared with terminal. After `cd work`, "
                            "use `report.md`, not `work/report.md`."
                        ),
                    },
                    "content": {"type": "string", "description": "for write/append"},
                    "offset": {"type": "integer", "minimum": 0,
                               "description": "byte offset (read/read_range/replace)"},
                    "limit": {"type": "integer", "minimum": 1,
                              "description": "max bytes to read"},
                    "overwrite": {"type": "boolean", "default": False,
                                  "description": "write: replace if exists"},
                    "find": {"type": "string", "description": "replace: substring to find"},
                    "replace": {"type": "string", "description": "replace: replacement text"},
                    "all": {"type": "boolean", "default": True,
                            "description": "replace: replace all occurrences (default true)"},
                },
                "required": ["action"],
            },
        },
        handler=handle_file,
        execution_environments=("cli", "gateway", "cron", "delegate"),
        unattended_allowed=True,
        approval_mode="interactive_or_remote",
        risk_level="high",
        default_enabled_environments=("cli", "cron"),
    )
