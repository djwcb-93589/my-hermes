"""
file 工具的端到端测试。

通过 run_conversation 发送自然语言 prompt；LLM 调用 file 工具完成
文件操作。覆盖：相对路径基准、terminal cd 后 cwd 一致性、路径穿越
被拒、覆盖保护、大文件截断、复杂路径/追加/列表/元数据、read_range/replace/敏感文件守卫、Docker/SSH 行为。

用法：
    python file_e2e.py local     # 仅 LocalBackend
    python file_e2e.py all       # 等价于 local（Docker/SSH 文件 IO 未实现）
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Callable

from hermes import backends as bm
from hermes.config import _config, HERMES_HOME
from hermes.conversation import run_conversation
from hermes.db import init_db, create_session
from hermes.prompt import build_system_prompt
from hermes.tools import register_all


TEST_DB_PATH = str(HERMES_HOME / "database" / "file_e2e.db")

# 测试函数签名：(backend, conn, session_id, system_prompt, sandbox, session_key) -> (ok, msg)
TestFn = Callable[..., tuple[bool, str]]


def _setup_sandbox(backend) -> Path:
    """在 backend.cwd 下建一个唯一沙箱目录，避免不同 session 互相干扰。"""
    sandbox = Path(backend.cwd) / f"file_e2e_sandbox_{backend._session_id[:6]}"
    if sandbox.exists():
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True)
    # 让 backend 真的 cd 进去，之后 file 工具的相对路径都以沙箱为基准。
    # LocalBackend 在 Windows 下跑 Git Bash，必须把 D:\... 转成 /d/...。
    shell_path = backend._cwd_to_shell(str(sandbox))
    result = backend.execute(f"cd {shlex.quote(shell_path)}")
    if result["returncode"] != 0:
        raise RuntimeError(result["output"])
    backend.file_root = str(sandbox)
    return sandbox


def _teardown_sandbox(sandbox: Path) -> None:
    if sandbox.exists():
        shutil.rmtree(sandbox, ignore_errors=True)


def test_write_read_roundtrip(backend, conn, session_id, system_prompt, sandbox, session_key):
    """LLM 写文件再读回来。"""
    prompt = (
        "Use the file tool to write a file named 'hello.txt' in the current "
        "directory with the content 'hermes_file_ok'. Then read it back and "
        "tell me the exact content you observed."
    )
    result = run_conversation(prompt, conn, session_id, system_prompt, session_key=session_key)
    output = (result.get("final_response") or "").lower()
    if "hermes_file_ok" not in output:
        return False, f"missing hermes_file_ok in response: {output[:120]!r}"
    target = sandbox / "hello.txt"
    if not target.exists():
        return False, f"file not created at {target}"
    if "hermes_file_ok" not in target.read_text(encoding="utf-8"):
        return False, f"content mismatch: {target.read_text()!r}"
    return True, "write→read roundtrip confirmed via Python-side check"


def test_traversal_blocked(backend, conn, session_id, system_prompt, sandbox, session_key):
    """LLM 尝试读 ../../etc/passwd 应该被拒。"""
    prompt = (
        "Use the file tool to attempt reading '../../../etc/passwd'. "
        "Report exactly the error_type and error message you receive."
    )
    result = run_conversation(prompt, conn, session_id, system_prompt, session_key=session_key)
    output = (result.get("final_response") or "").lower()
    if "forbidden" not in output and "escapes" not in output and "traversal" not in output:
        return False, f"expected forbidden/escapes in response: {output[:200]!r}"
    return True, "path traversal was rejected"


def test_overwrite_protection(backend, conn, session_id, system_prompt, sandbox, session_key):
    """先写入再尝试不加 overwrite 写一次,应该返回 exists 错误。"""
    seed = sandbox / "seed.txt"
    seed.write_text("original", encoding="utf-8")
    prompt = (
        "Use the file tool to write 'modified' to 'seed.txt' in the current "
        "directory WITHOUT passing overwrite=true. Report the error_type "
        "and error message verbatim."
    )
    result = run_conversation(prompt, conn, session_id, system_prompt, session_key=session_key)
    output = (result.get("final_response") or "").lower()
    if "exists" not in output:
        return False, f"expected 'exists' error in response: {output[:200]!r}"
    # 同时确认文件确实没被覆盖
    if seed.read_text(encoding="utf-8") != "original":
        return False, "file was modified despite no overwrite flag"
    return True, "overwrite protection works"


def test_truncation(backend, conn, session_id, system_prompt, sandbox, session_key):
    """写一个大于 100KB 的文件,read 应返回 truncated=true。"""
    big = sandbox / "big.txt"
    big.write_text("B" * 150_000, encoding="utf-8")
    prompt = (
        "Use the file tool to read 'big.txt' in the current directory. "
        "Report whether the response says truncated is true, and the size."
    )
    result = run_conversation(prompt, conn, session_id, system_prompt, session_key=session_key)
    output = (result.get("final_response") or "").lower()
    if (
        "truncat" not in output
        or "true" not in output
        or ("100000" not in output and "100,000" not in output)
    ):
        return False, f"expected truncated=true and size 100000 in response: {output[:200]!r}"
    return True, "truncation flag surfaced to LLM"


def test_nested_unicode_append_list_stat(backend, conn, session_id, system_prompt, sandbox, session_key):
    """复杂路径 + Unicode 文件名 + append/list/stat/read 的组合场景。LocalBackend 专用。"""
    if type(backend).__name__ != "LocalBackend":
        return True, f"skipped for {type(backend).__name__}"

    nested_dir = sandbox / "level one" / "中文 子目录"
    nested_dir.mkdir(parents=True, exist_ok=True)
    target = nested_dir / "report 中文.md"
    rel_path = "level one/中文 子目录/report 中文.md"
    rel_dir = "level one/中文 子目录"

    initial = "# Hermes 文件测试\nalpha=1\nemoji=🐍\n"
    appended = "status=appended\n"
    expected = initial + appended

    prompt = (
        "Use the file tool only. Do not use the terminal tool.\n"
        f"First write exactly {initial!r} to {rel_path!r} in the current directory.\n"
        f"Then append exactly {appended!r} to the same file.\n"
        f"Then list directory {rel_dir!r}.\n"
        f"Then stat {rel_path!r}.\n"
        f"Finally read {rel_path!r} and report the final content, the listed entries, and the file size."
    )
    result = run_conversation(prompt, conn, session_id, system_prompt, session_key=session_key)
    output = result.get("final_response") or ""

    if not target.exists():
        return False, f"file not created at {target}"

    actual = target.read_text(encoding="utf-8")
    if actual != expected:
        return False, f"content mismatch: {actual!r}"

    expected_size = len(expected.encode("utf-8"))
    if "report 中文.md" not in output and "report" not in output.lower():
        return False, f"list result not surfaced in response: {output[:240]!r}"
    if str(expected_size) not in output and f"{expected_size:,}" not in output:
        return False, f"stat size {expected_size} not surfaced in response: {output[:240]!r}"
    if "status=appended" not in output:
        return False, f"final appended content not surfaced in response: {output[:240]!r}"

    return True, "complex Unicode path append/list/stat/read verified"


def test_read_range_replace_and_sensitive_guard(backend, conn, session_id, system_prompt, sandbox, session_key):
    """
    read_range 续读大文件尾部 + replace 单次替换 + 敏感文件默认拒绝。
    LocalBackend 专用。
    """
    if type(backend).__name__ != "LocalBackend":
        return True, f"skipped for {type(backend).__name__}"

    big = sandbox / "big_tail.txt"
    config = sandbox / "config.txt"
    secret = sandbox / ".env"

    tail = "TAIL_TOKEN_Ω\nEND\n"
    big.write_bytes(("B" * 100_000).encode("utf-8") + tail.encode("utf-8"))
    config.write_text("mode=old\nmode=old\n", encoding="utf-8")
    secret.write_text("OPENAI_API_KEY=should_not_be_read\n", encoding="utf-8")

    prompt = (
        "Use the file tool only. Do not use the terminal tool.\n"
        "Do these operations in order and report each result:\n"
        "1. read 'big_tail.txt' and report truncated plus size.\n"
        "2. read_range 'big_tail.txt' with offset=100000 and limit=80, and report the content.\n"
        "3. replace in 'config.txt' with find='mode=old', replace='mode=new', all=false, then report replacements.\n"
        "4. read 'config.txt' and report its content.\n"
        "5. read '.env' without allow_sensitive=true and report the error_type and error."
    )
    result = run_conversation(prompt, conn, session_id, system_prompt, session_key=session_key)
    output = (result.get("final_response") or "").lower()

    final_config = config.read_text(encoding="utf-8")
    if final_config != "mode=new\nmode=old\n":
        return False, f"replace(all=false) did not update exactly one occurrence: {final_config!r}"

    if "truncat" not in output or "true" not in output:
        return False, f"initial read truncation not surfaced: {output[:260]!r}"
    if "tail_token" not in output and "tail token" not in output:
        return False, f"read_range tail content not surfaced: {output[:260]!r}"
    if "mode=new" not in output or "mode=old" not in output:
        return False, f"post-replace content not surfaced: {output[:260]!r}"
    if "forbidden" not in output or "sensitive" not in output:
        return False, f"sensitive file guard not surfaced: {output[:260]!r}"

    return True, "read_range, single replace, and sensitive-file guard verified"


def test_docker_ssh_unsupported(backend, conn, session_id, system_prompt, sandbox, session_key):
    """Docker/SSH 应返回 unsupported_backend。LocalBackend 跳过此测试。"""
    if type(backend).__name__ == "LocalBackend":
        return True, "skipped for LocalBackend"
    prompt = (
        "Use the file tool to read any file. Report the error_type verbatim."
    )
    result = run_conversation(prompt, conn, session_id, system_prompt, session_key=session_key)
    output = (result.get("final_response") or "").lower()
    if "unsupported_backend" not in output:
        return False, f"expected unsupported_backend error_type: {output[:200]!r}"
    return True, "Docker/SSH returns unsupported_backend"


TESTS: list[tuple[str, TestFn]] = [
    ("write_read_roundtrip", test_write_read_roundtrip),
    ("traversal_blocked",    test_traversal_blocked),
    ("overwrite_protection", test_overwrite_protection),
    ("truncation",           test_truncation),
    ("nested_unicode_append_list_stat", test_nested_unicode_append_list_stat),
    ("read_range_replace_and_sensitive_guard", test_read_range_replace_and_sensitive_guard),
    ("docker_ssh_unsupported", test_docker_ssh_unsupported),
]


def install_backend(backend_type: str, session_key: str):
    bm.cleanup_all_backends()
    _config.setdefault("terminal", {})["backend"] = backend_type
    backend = bm.get_backend(session_key=session_key)
    print(f"  Installed backend: {type(backend).__name__}, cwd={backend.cwd}, session={session_key!r}")
    return backend


def run_one(backend_type: str) -> bool:
    sep = "=" * 64
    print(f"\n{sep}\n  Backend: {backend_type}\n{sep}")
    session_key = f"file-e2e-{backend_type}"

    try:
        backend = install_backend(backend_type, session_key)
    except Exception as exc:
        print(f"  ✗ FAILED to install backend: {exc!r}")
        return False

    register_all()
    conn = init_db(TEST_DB_PATH)
    session_id = create_session(conn)
    system_prompt = build_system_prompt(os.getcwd())

    # Docker/SSH 在 LocalBackend 之外无法 setup_sandbox（read 都不支持）
    # —— 先建沙箱，如果 backend 不支持 cd，setup 会抛，单独跑 unsupported 测试
    sandbox = None
    try:
        sandbox = _setup_sandbox(backend)
    except Exception as exc:
        print(f"  ! sandbox setup failed ({exc!r}); only unsupported test will run")

    passed = failed = 0
    for label, fn in TESTS:
        # 沙箱不可用就跳过非 unsupported 测试
        if sandbox is None and label != "docker_ssh_unsupported":
            print(f"\n  --- [{label}] SKIPPED (no sandbox)")
            continue
        print(f"\n  --- [{label}]")
        try:
            ok, msg = fn(backend, conn, session_id, system_prompt, sandbox, session_key)
        except Exception as exc:
            ok, msg = False, f"crashed: {exc!r}"
        if ok:
            print(f"  ✓ PASS: {msg}")
            passed += 1
        else:
            print(f"  ✗ FAIL: {msg}")
            failed += 1

    conn.close()
    if sandbox is not None:
        _teardown_sandbox(sandbox)
    bm.cleanup_backend(session_key)

    print(f"\n  Result for {backend_type}: {passed} passed / {failed} failed")
    return failed == 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("local", "all"):
        print("Usage: python file_e2e.py [local|all]")
        sys.exit(2)
    target = sys.argv[1]

    if target == "all":
        # 当前只有 LocalBackend 实现了文件 IO
        ok = run_one("local")
        sys.exit(0 if ok else 1)
    else:
        ok = run_one(target)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
