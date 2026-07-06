"""
file 工具的端到端测试。

通过 run_conversation 发送自然语言 prompt；LLM 调用 file 工具完成
文件操作。覆盖：相对路径基准、terminal cd 后 cwd 一致性、路径穿越
被拒、覆盖保护、大文件截断、Docker/SSH 行为。

用法：
    python file_e2e.py local     # 仅 LocalBackend
    python file_e2e.py all       # 等价于 local（Docker/SSH 文件 IO 未实现）
"""

from __future__ import annotations

import json
import os
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
    # 让 backend 真的 cd 进去，之后 file 工具的相对路径都以沙箱为基准
    backend.execute(f"cd {sandbox}")
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
    if "truncat" not in output:
        return False, f"expected 'truncat' mention in response: {output[:200]!r}"
    return True, "truncation flag surfaced to LLM"


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
