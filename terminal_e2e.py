"""
terminal 工具的端到端测试，覆盖 Local / Docker / SSH 三种后端。

通过 run_conversation 发送自然语言 prompt；由 LLM 决定调用 terminal
工具执行哪条 shell 命令，再由当前后端去执行。我们断言（a）LLM 的确
调用了 terminal 工具，（b）响应或文件系统状态符合预期。

用法：
    python terminal_e2e.py local     # 仅 LocalBackend
    python terminal_e2e.py docker    # 仅 DockerBackend（需要 `docker` 守护进程）
    python terminal_e2e.py ssh       # 仅 SSHBackend（config.yaml 里需配 terminal.ssh_*）
    python terminal_e2e.py all       # 依次跑三种

前置条件：
    - 已设置 OPENAI_API_KEY（或项目根有 .env）
    - docker：本地 docker 守护进程在跑
    - ssh：config.yaml 里填好 terminal.ssh_host / ssh_user
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable
import shutil

from hermes import backends as bm
from hermes.config import _config, HERMES_HOME
from hermes.conversation import run_conversation
from hermes.db import init_db, create_session
from hermes.prompt import build_system_prompt
from hermes.tools import register_all


TEST_DB_PATH = str(HERMES_HOME / "database" / "terminal_e2e.db")

# 测试函数签名：
#   (backend, conn, session_id, system_prompt, executed, session_key)
#   -> (passed: bool, message: str)
# `executed` 是一个 list，runner 在每次调用前会清空；测试过程中通过
# 一次性安装的 spy 往里追加 (command, returncode)。
TestFn = Callable[..., tuple[bool, str]]


# ---------------------------------------------------------------------------
# 测试用例。
# ---------------------------------------------------------------------------

def test_echo(backend, conn, session_id, system_prompt, executed, session_key):
    prompt = (
        "Use the terminal tool to run a shell command that prints the text "
        "'hermes_test_ok'. Tell me the exact output you saw."
    )
    result = run_conversation(prompt, conn, session_id, system_prompt, session_key=session_key)
    if not executed:
        return False, "LLM never called terminal"
    output = (result.get("final_response") or "").strip()
    if "hermes_test_ok" in output.lower():
        return True, f"ran {executed[0][0]!r}"
    return False, f"expected 'hermes_test_ok' in response, got {output[:120]!r}"


def test_pwd(backend, conn, session_id, system_prompt, executed, session_key):
    prompt = (
        "Use the terminal tool to run a shell command that shows the current "
        "working directory. Report the path."
    )
    result = run_conversation(prompt, conn, session_id, system_prompt, session_key=session_key)
    if not executed:
        return False, "LLM never called terminal"
    output = (result.get("final_response") or "").strip()
    # 任何绝对路径都包含斜杠 —— /d/my-hermes、/workspace、~ 都能匹配。
    if "/" in output:
        return True, f"reported path containing '/': {output[:80]!r}"
    return False, f"no '/' in response, got {output[:120]!r}"


def test_date(backend, conn, session_id, system_prompt, executed, session_key):
    prompt = "Use the terminal tool to run 'date'. Tell me the year you see."
    result = run_conversation(prompt, conn, session_id, system_prompt, session_key=session_key)
    if not executed:
        return False, "LLM never called terminal"
    output = (result.get("final_response") or "").strip()
    if "20" in output:  # 4 位年份，如 2026
        return True, f"reported year containing '20': {output[:80]!r}"
    return False, f"no '20' in response, got {output[:120]!r}"


def test_no_wsl(backend, conn, session_id, system_prompt, executed, session_key):
    """确认不在 WSL 里。主要对 Windows local 有意义。"""
    prompt = "Use the terminal tool to run 'uname -a' and report the full output verbatim."
    result = run_conversation(prompt, conn, session_id, system_prompt, session_key=session_key)
    if not executed:
        return False, "LLM never called terminal"
    output = (result.get("final_response") or "").lower()
    print(f"    uname output snippet: {output[:200]!r}")
    # WSL 内核会在 uname 里输出 "microsoft-standard" 或 "Microsoft"。
    if "microsoft" in output or "wsl" in output:
        return False, f"looks like WSL: {output[:120]!r}"
    return True, "no WSL/Microsoft markers in uname"


def test_cwd_in_project(backend, conn, session_id, system_prompt, executed, session_key):
    """确认 cwd 包含项目目录名。仅对 LocalBackend 跑。"""
    if type(backend).__name__ != "LocalBackend":
        return True, f"skipped for {type(backend).__name__}"

    prompt = "Use the terminal tool to run pwd and tell me the exact path."
    result = run_conversation(prompt, conn, session_id, system_prompt, session_key=session_key)
    if not executed:
        return False, "LLM never called terminal"
    output = (result.get("final_response") or "")
    if "my-hermes" in output.lower():
        return True, f"cwd contains 'my-hermes': {output[:80]!r}"
    return False, f"cwd missing project name; got {output[:120]!r}"


def test_file_roundtrip(backend, conn, session_id, system_prompt, executed, session_key):
    """terminal 创建文件 → Python 在 host 路径看到 → 内容一致。

    这是最强的一致性检查：Git Bash 和 Python 看到的是同一个 Windows
    文件系统位置（terminal 走 bash 侧写入，Python 走 host 侧读取）。
    仅对 LocalBackend 跑。
    """
    if type(backend).__name__ != "LocalBackend":
        return True, f"skipped for {type(backend).__name__}"

    target = Path(backend.cwd) / "hermes_roundtrip.txt"
    if target.exists():
        target.unlink()

    prompt = (
        "Use the terminal tool to create a file named 'hermes_roundtrip.txt' "
        "in the current directory containing exactly the text 'roundtrip_ok' "
        "(no extra output)."
    )
    result = run_conversation(prompt, conn, session_id, system_prompt, session_key=session_key)
    if not executed:
        return False, "LLM never called terminal"

    if not target.exists():
        return False, f"Python did not see file at {target} (cwd={backend.cwd!r})"

    content = target.read_text(encoding="utf-8").strip()
    target.unlink()  # 清理，保证重跑时干净
    if "roundtrip_ok" in content:
        return True, f"Python saw the file; content={content!r}"
    return False, f"file existed but content mismatch: {content!r}"

    #新增两个测试样例
def test_bash_dialect_and_quoting(backend, conn, session_id, system_prompt, executed, session_key):
    """确认 LocalBackend 真的是 Git Bash/Bash 语义，并能处理空格和中文路径。"""
    if type(backend).__name__ != "LocalBackend":
        return True, f"skipped for {type(backend).__name__}"

    base = Path(backend.cwd)
    case_dir = base / "gbash tricky dir 中文"
    target = case_dir / "space name 中文.txt"

    if case_dir.exists():
        shutil.rmtree(case_dir)

    prompt = (
        "Use the terminal tool to run exactly this Bash command as one line. "
        "Then report the exact output:\n"
        "set -euo pipefail; "
        "mkdir -p 'gbash tricky dir 中文'; "
        "printf '%s\\n' 'bash_ok' > 'gbash tricky dir 中文/space name 中文.txt'; "
        "if [[ \"$(cat 'gbash tricky dir 中文/space name 中文.txt')\" == 'bash_ok' ]]; "
        "then echo BASH_DIALECT_OK; fi"
    )

    result = run_conversation(prompt, conn, session_id, system_prompt, session_key=session_key)
    if not executed:
        return False, "LLM never called terminal"

    output = (result.get("final_response") or "")
    if "BASH_DIALECT_OK" not in output:
        return False, f"missing BASH_DIALECT_OK in response; got {output[:160]!r}"

    if not target.exists():
        return False, f"Python did not see expected file at {target}"

    content = target.read_text(encoding="utf-8").strip()
    shutil.rmtree(case_dir)

    if content == "bash_ok":
        return True, "Bash syntax, quoting, spaces, and Chinese path all worked"
    return False, f"file content mismatch: {content!r}"

def test_cwd_persistence_with_tricky_path(backend, conn, session_id, system_prompt, executed, session_key):
    """确认 cd 后的 cwd 能跨 terminal 调用持久化，并能被 Python 正确理解。"""
    if type(backend).__name__ != "LocalBackend":
        return True, f"skipped for {type(backend).__name__}"

    original_cwd = Path(backend.cwd)
    root = original_cwd / "cwd persist dir 中文"
    inner = root / "inner space"

    if root.exists():
        shutil.rmtree(root)

    try:
        prompt1 = (
            "Use the terminal tool to run exactly this Bash command as one line. "
            "Then report the exact output:\n"
            "set -euo pipefail; "
            "mkdir -p 'cwd persist dir 中文/inner space'; "
            "cd 'cwd persist dir 中文/inner space'; "
            "printf '%s\\n' 'persist_ok' > marker.txt; "
            "pwd -P"
        )

        result1 = run_conversation(prompt1, conn, session_id, system_prompt, session_key=session_key)
        if not executed:
            return False, "first command did not call terminal"

        output1 = (result1.get("final_response") or "")
        if "cwd persist dir" not in output1.lower():
            return False, f"first pwd did not show target dir; got {output1[:160]!r}"

        # 这里检查 Python 侧 backend.cwd 是否已经更新到子目录。
        if not Path(backend.cwd).exists():
            return False, f"backend.cwd is not a valid host path after cd: {backend.cwd!r}"

        if "inner space" not in str(backend.cwd).lower():
            return False, f"backend.cwd was not updated to inner dir: {backend.cwd!r}"

        executed.clear()

        prompt2 = (
            "Use the terminal tool to run exactly this Bash command as one line. "
            "Do not cd first. Report the exact output:\n"
            "pwd -P; cat marker.txt"
        )

        result2 = run_conversation(prompt2, conn, session_id, system_prompt, session_key=session_key)
        if not executed:
            return False, "second command did not call terminal"

        output2 = (result2.get("final_response") or "")
        if "persist_ok" not in output2:
            return False, f"cwd did not persist or marker.txt not found; got {output2[:200]!r}"

        return True, f"cwd persisted across calls; backend.cwd={backend.cwd!r}"

    finally:
        # 避免这个测试污染后续测试。
        backend.cwd = str(original_cwd)
        if root.exists():
            shutil.rmtree(root)

'''
TESTS: list[tuple[str, TestFn]] = [
    ("echo",          test_echo),
    ("pwd",           test_pwd),
    ("date",          test_date),
    ("no_wsl",        test_no_wsl),
    ("cwd_in_project", test_cwd_in_project),
    ("file_roundtrip", test_file_roundtrip),
]
'''
TESTS: list[tuple[str, TestFn]] = [
    ("bash_dialect_and_quoting", test_bash_dialect_and_quoting),
    ("cwd_persistence_with_tricky_path", test_cwd_persistence_with_tricky_path),
]


# ---------------------------------------------------------------------------
# Runner。
# ---------------------------------------------------------------------------

def install_backend(backend_type: str, session_key: str):
    """把 _config 配成目标 backend 类型，再为该 session 新建一个 backend。

    先清理所有缓存的 backend，确保类型切换生效。
    """
    bm.cleanup_all_backends()
    _config.setdefault("terminal", {})["backend"] = backend_type
    backend = bm.get_backend(session_key=session_key)
    print(f"  Installed backend: {type(backend).__name__}, cwd={backend.cwd}, session={session_key!r}")
    return backend


def spy_execute(backend) -> list[tuple[str, int]]:
    """包装 backend.execute，记录每次调用的 (command, returncode)。"""
    calls: list[tuple[str, int]] = []
    original = backend.execute

    def wrapped(command, timeout=None):
        result = original(command, timeout=timeout)
        calls.append((command, result.get("returncode", -1)))
        return result

    backend.execute = wrapped
    return calls


def run_one(backend_type: str) -> bool:
    """对给定 backend 跑全部 TESTS。全部通过才返回 True。"""
    sep = "=" * 64
    print(f"\n{sep}\n  Backend: {backend_type}\n{sep}")

    # 不同 backend 用不同 session_key，避免彼此污染状态。conversation 会
    # 把同一个 key 转发给 terminal 工具，后者用它查 session 级 backend。
    session_key = f"e2e-{backend_type}"

    try:
        backend = install_backend(backend_type, session_key)
    except Exception as exc:
        print(f"  ✗ FAILED to install backend: {exc!r}")
        return False

    executed = spy_execute(backend)

    register_all()
    conn = init_db(TEST_DB_PATH)
    session_id = create_session(conn)
    system_prompt = build_system_prompt(os.getcwd())

    passed = 0
    failed = 0
    for label, fn in TESTS:
        executed.clear()
        print(f"\n  --- [{label}]")
        try:
            ok, msg = fn(backend, conn, session_id, system_prompt, executed, session_key)
        except Exception as exc:
            ok, msg = False, f"crashed: {exc!r}"

        # 打印 LLM 实际跑了什么。
        if executed:
            cmds = ", ".join(f"{c!r}({rc})" for c, rc in executed)
            print(f"    executed: {cmds}")

        if ok:
            print(f"  ✓ PASS: {msg}")
            passed += 1
        else:
            print(f"  ✗ FAIL: {msg}")
            failed += 1

    conn.close()
    # 只清理本 session 的 backend；其它 run 残留的实例会被下一次
    # install_backend() 里的 cleanup_all_backends() 清掉。
    bm.cleanup_backend(session_key)

    print(f"\n  Result for {backend_type}: {passed} passed / {failed} failed")
    return failed == 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("local", "docker", "ssh", "all"):
        print("Usage: python terminal_e2e.py [local|docker|ssh|all]")
        sys.exit(2)

    target = sys.argv[1]

    if target == "all":
        results: list[tuple[str, bool]] = []
        for backend_type in ("local", "docker", "ssh"):
            try:
                ok = run_one(backend_type)
            except Exception as exc:
                print(f"\n  !!! {backend_type} crashed: {exc!r}")
                ok = False
            results.append((backend_type, ok))

        print(f"\n{'=' * 64}\n  Summary\n{'=' * 64}")
        for backend_type, ok in results:
            print(f"    {backend_type:8s}  {'PASS' if ok else 'FAIL'}")
        sys.exit(0 if all(ok for _, ok in results) else 1)
    else:
        ok = run_one(target)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
