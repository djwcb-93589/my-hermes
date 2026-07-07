"""
memory 工具的端到端 + 基础设施测试。

主体走 LLM e2e：发送自然语言 prompt,LLM 调 memory 工具,我们对
响应或文件状态做断言。覆盖：add/read、空白拒绝、ambiguous 返回 matches、
replace 与其它 entry 重复被拒、超限字段名为 candidate_chars。

3 项基础设施测试（原子写入失败、并发互不覆、lock 超时）靠 monkeypatch /
多线程 / 预占锁触发,LLM 端无法可靠制造,保留为直接调用形式。

用法：
    python memory_test.py            # 跑全部
    python memory_test.py e2e        # 只跑 LLM e2e 部分（需 OPENAI_API_KEY）
    python memory_test.py infra      # 只跑基础设施部分
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable

from hermes.config import _config
from hermes.conversation import run_conversation
from hermes.db import init_db, create_session
from hermes.prompt import build_system_prompt
from hermes.tools import register_all
from hermes.tools import memory as mem


TEST_DB_PATH = str(mem.HERMES_HOME / "database" / "memory_test.db")

# 测试函数签名：
#   (ctx) -> (ok, msg)
# ctx 提供 run(prompt)、entries()、file_text()、reset() 等工具
CtxFn = Callable[..., tuple[bool, str]]


# ---------------------------------------------------------------------------
# 测试上下文：把 memory 模块的存储路径重定向到临时目录,避免污染真实 memory
# ---------------------------------------------------------------------------

class _Ctx:
    """每个测试共享一个临时目录 + DB session；测试之间用 reset() 清空。"""

    def __init__(self, store: Path, conn, system_prompt: str, session_key: str):
        self.store = store
        self.conn = conn
        self.system_prompt = system_prompt
        self.session_key = session_key
        # 每个测试用独立 session_id,避免 messages 跨测试串扰
        self._sessions: list[str] = []

    def run(self, prompt: str) -> tuple[dict, str]:
        """发自然语言 prompt,返回 (raw result dict, lowercase final_response)。"""
        sid = create_session(self.conn)
        self._sessions.append(sid)
        result = run_conversation(
            prompt, self.conn, sid, self.system_prompt,
            session_key=self.session_key,
        )
        return result, (result.get("final_response") or "").lower()

    def entries(self) -> list[str]:
        return mem.load_memory(mem.MEMORY_FILE)

    def memory_file_text(self) -> str:
        return mem.MEMORY_FILE.read_text(encoding="utf-8") if mem.MEMORY_FILE.exists() else ""

    def reset(self) -> None:
        """清空沙箱文件、残留 lock/tmp 文件。"""
        for p in [mem.MEMORY_FILE, mem.USER_FILE]:
            if p.exists():
                p.unlink()
        for p in [
            mem._lock_path_for(mem.MEMORY_FILE),
            mem._lock_path_for(mem.USER_FILE),
            mem.MEMORY_FILE.with_suffix(mem.MEMORY_FILE.suffix + ".tmp"),
            mem.USER_FILE.with_suffix(mem.USER_FILE.suffix + ".tmp"),
        ]:
            if p.exists():
                p.unlink()

    def seed(self, *entries: str, target: str = "memory") -> None:
        """直接写种子条目,绕过 LLM。用于 ambiguous / replace_dedup 等需要预填的测试。"""
        file_path = mem.USER_FILE if target == "user" else mem.MEMORY_FILE
        file_path.write_text(mem.render_entries(list(entries)), encoding="utf-8")


def _setup_ctx() -> _Ctx:
    """重定向 memory 存储路径到临时目录,初始化 DB + system prompt。"""
    tmp = Path(tempfile.mkdtemp(prefix="hermes-mem-e2e-"))
    original = {
        "MEMORY_DIR": mem.MEMORY_DIR,
        "MEMORY_FILE": mem.MEMORY_FILE,
        "USER_FILE": mem.USER_FILE,
    }
    mem.MEMORY_DIR = tmp
    mem.MEMORY_FILE = tmp / "MEMORY.md"
    mem.USER_FILE = tmp / "USER.md"

    register_all()
    conn = init_db(TEST_DB_PATH)
    system_prompt = build_system_prompt(os.getcwd())
    session_key = "memory-test"
    return _Ctx(tmp, conn, system_prompt, session_key), original


def _teardown(ctx: _Ctx, original: dict) -> None:
    ctx.conn.close()
    # 还原模块属性
    mem.MEMORY_DIR = original["MEMORY_DIR"]
    mem.MEMORY_FILE = original["MEMORY_FILE"]
    mem.USER_FILE = original["USER_FILE"]
    shutil.rmtree(ctx.store, ignore_errors=True)


# ---------------------------------------------------------------------------
# LLM e2e 测试
# ---------------------------------------------------------------------------

def test_add_and_read(ctx: _Ctx) -> tuple[bool, str]:
    ctx.reset()
    prompt = (
        "Use the memory tool to add an entry with the exact text "
        "'mem_e2e_marker_42' to long-term memory. Then call read and "
        "list verbatim every entry returned."
    )
    result, lower = ctx.run(prompt)
    if "mem_e2e_marker_42" not in lower:
        return False, f"marker missing in response: {lower[:200]!r}"
    entries = ctx.entries()
    if "mem_e2e_marker_42" not in entries:
        return False, f"file missing marker; entries={entries}"
    return True, f"add → read confirmed; entries={entries}"


def test_blank_add_rejected(ctx: _Ctx) -> tuple[bool, str]:
    ctx.reset()
    prompt = (
        "Use the memory tool with action=add and content='   ' (exactly "
        "three space characters, do NOT strip or alter it). "
        "Report verbatim the error_type field from the tool response."
    )
    result, lower = ctx.run(prompt)
    if "invalid_args" not in lower:
        return False, f"expected invalid_args; got {lower[:200]!r}"
    if ctx.entries():
        return False, f"blank add should not write; file={ctx.memory_file_text()!r}"
    return True, "blank content rejected, file unchanged"


def test_blank_remove_rejected(ctx: _Ctx) -> tuple[bool, str]:
    ctx.reset()
    ctx.seed("real entry")
    before = ctx.memory_file_text()
    prompt = (
        "Use the memory tool with action=remove and content='   ' "
        "(exactly three spaces, do NOT strip). Report the error_type verbatim."
    )
    result, lower = ctx.run(prompt)
    if "invalid_args" not in lower:
        return False, f"expected invalid_args; got {lower[:200]!r}"
    if ctx.memory_file_text() != before:
        return False, "blank remove modified the file"
    return True, "blank remove rejected, file unchanged"


def test_ambiguous_returns_matches(ctx: _Ctx) -> tuple[bool, str]:
    ctx.reset()
    ctx.seed(
        "foo alpha", "foo beta", "foo gamma",
        "foo delta", "foo epsilon", "foo zeta",
    )
    before = ctx.entries()
    prompt = (
        "Use the memory tool with action=remove and content='foo' (this "
        "should match multiple entries). Report verbatim: "
        "(a) the error_type, (b) whether the response contains a 'matches' "
        "field, (c) how many candidates 'matches' lists."
    )
    result, lower = ctx.run(prompt)
    if "ambiguous_match" not in lower:
        return False, f"expected ambiguous_match; got {lower[:200]!r}"
    if "matches" not in lower:
        return False, f"response missing 'matches' keyword: {lower[:200]!r}"
    # 检查文件未变
    if ctx.entries() != before:
        return False, "ambiguous remove modified file"
    return True, "ambiguous_match returned with matches, file unchanged"


def test_replace_dedup_rejected(ctx: _Ctx) -> tuple[bool, str]:
    ctx.reset()
    ctx.seed("apple_a_unique", "apple_b_unique")
    before = ctx.entries()
    prompt = (
        "Use the memory tool to replace the entry matched by old_text "
        "'apple_a_unique' with the new content 'apple_b_unique' (this is "
        "identical to the other existing entry). Report the error_type verbatim."
    )
    result, lower = ctx.run(prompt)
    if "duplicate" not in lower:
        return False, f"expected duplicate; got {lower[:200]!r}"
    if ctx.entries() != before:
        return False, f"file modified despite duplicate; before={before}, after={ctx.entries()}"
    return True, "replace with duplicate content rejected"


def test_limit_returns_candidate_chars(ctx: _Ctx) -> tuple[bool, str]:
    ctx.reset()
    # 压小 MEMORY_CHAR_LIMIT,LLM 写一条长内容必然超限
    original_limit = mem.MEMORY_CHAR_LIMIT
    mem.MEMORY_CHAR_LIMIT = 10
    try:
        prompt = (
            "Use the memory tool to add an entry with this exact content: "
            "'this_is_a_very_long_string_that_will_exceed_the_limit'. "
            "Report verbatim the error_type and the name of the field that "
            "starts with 'candidate' and tells how many chars the rejected "
            "write would have produced."
        )
        result, lower = ctx.run(prompt)
    finally:
        mem.MEMORY_CHAR_LIMIT = original_limit
    if "limit_exceeded" not in lower:
        return False, f"expected limit_exceeded; got {lower[:200]!r}"
    if "candidate_chars" not in lower:
        return False, f"response missing 'candidate_chars' field name: {lower[:200]!r}"
    if "new_content_chars" in lower:
        return False, "response still mentions old field name new_content_chars"
    return True, "limit_exceeded reports candidate_chars"


E2E_TESTS: list[tuple[str, CtxFn]] = [
    ("add_and_read",              test_add_and_read),
    ("blank_add_rejected",        test_blank_add_rejected),
    ("blank_remove_rejected",     test_blank_remove_rejected),
    ("ambiguous_returns_matches", test_ambiguous_returns_matches),
    ("replace_dedup_rejected",    test_replace_dedup_rejected),
    ("limit_returns_candidate_chars", test_limit_returns_candidate_chars),
]


def run_e2e_suite(ctx: _Ctx) -> tuple[int, int]:
    """跑所有 LLM e2e 测试,返回 (passed, failed)。"""
    passed = failed = 0
    for label, fn in E2E_TESTS:
        print(f"\n  --- [{label}]")
        try:
            ok, msg = fn(ctx)
        except Exception as exc:
            ok, msg = False, f"crashed: {exc!r}"
        if ok:
            print(f"  ✓ PASS: {msg}")
            passed += 1
        else:
            print(f"  ✗ FAIL: {msg}")
            failed += 1
    return passed, failed


# ---------------------------------------------------------------------------
# 基础设施测试（直接调用,不走 LLM）
# ---------------------------------------------------------------------------

def test_atomic_write_failure(ctx: _Ctx) -> tuple[bool, str]:
    ctx.reset()
    ctx.seed("stable entry")
    original = ctx.memory_file_text()

    original_writer = mem._atomic_write_text

    def boom(path, text):
        raise OSError("simulated write failure")

    mem._atomic_write_text = boom
    try:
        d = json.loads(mem.handle_memory({"action": "add", "content": "another entry"}))
        assert d["ok"] is False and d["error_type"] == "io_error", d
        assert "simulated write failure" in d["error"], d
        assert ctx.memory_file_text() == original, "file must be unchanged on write failure"
    finally:
        mem._atomic_write_text = original_writer

    lock_path = mem._lock_path_for(mem.MEMORY_FILE)
    if lock_path.exists():
        return False, "lock file leaked after failure"
    return True, "atomic write failure handled cleanly"


def test_concurrent_writes(ctx: _Ctx) -> tuple[bool, str]:
    ctx.reset()
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def worker(content: str) -> None:
        try:
            barrier.wait(timeout=2)
            d = json.loads(mem.handle_memory({"action": "add", "content": content}))
            if not d["ok"]:
                errors.append(f"add {content!r} failed: {d}")
        except Exception as exc:
            errors.append(repr(exc))

    t1 = threading.Thread(target=worker, args=("thread-a-marker",))
    t2 = threading.Thread(target=worker, args=("thread-b-marker",))
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)

    if errors:
        return False, f"threads errored: {errors}"
    entries = ctx.entries()
    if "thread-a-marker" not in entries or "thread-b-marker" not in entries:
        return False, f"lost a write; entries={entries}"
    if len(entries) != 2:
        return False, f"expected 2 entries, got {len(entries)}: {entries}"
    return True, "both concurrent adds persisted"


def test_lock_timeout(ctx: _Ctx) -> tuple[bool, str]:
    ctx.reset()
    lock_path = mem._lock_path_for(mem.MEMORY_FILE)
    fd = mem._acquire_lock(lock_path, timeout=0.5)
    try:
        original_timeout = mem._LOCK_TIMEOUT
        mem._LOCK_TIMEOUT = 0.3
        try:
            d = json.loads(mem.handle_memory({"action": "add", "content": "should not get in"}))
            if d["ok"] is not False or d.get("error_type") != "lock_timeout":
                return False, f"expected lock_timeout; got {d}"
            if ctx.entries():
                return False, "lock_timeout must not write"
        finally:
            mem._LOCK_TIMEOUT = original_timeout
    finally:
        mem._release_lock(lock_path, fd)
    return True, "lock_timeout returned, file unchanged"


INFRA_TESTS: list[tuple[str, CtxFn]] = [
    ("atomic_write_failure", test_atomic_write_failure),
    ("concurrent_writes",    test_concurrent_writes),
    ("lock_timeout",         test_lock_timeout),
]


def run_infra_suite(ctx: _Ctx) -> tuple[int, int]:
    passed = failed = 0
    for label, fn in INFRA_TESTS:
        print(f"\n  --- [{label}]")
        try:
            ok, msg = fn(ctx)
        except Exception as exc:
            ok, msg = False, f"crashed: {exc!r}"
        if ok:
            print(f"  ✓ PASS: {msg}")
            passed += 1
        else:
            print(f"  ✗ FAIL: {msg}")
            failed += 1
    return passed, failed


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    # Windows 控制台默认 GBK,无法输出 ✓/✗;强制 UTF-8
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) >= 2 and sys.argv[1] not in ("all", "e2e", "infra"):
        print("Usage: python memory_test.py [all|e2e|infra]")
        sys.exit(2)
    target = sys.argv[1] if len(sys.argv) >= 2 else "all"

    ctx, original = _setup_ctx()
    try:
        e2e_passed = e2e_failed = infra_passed = infra_failed = 0
        if target in ("all", "e2e"):
            print(f"\n{'=' * 64}\n  LLM e2e suite\n{'=' * 64}")
            e2e_passed, e2e_failed = run_e2e_suite(ctx)
        if target in ("all", "infra"):
            print(f"\n{'=' * 64}\n  Infrastructure suite (no LLM)\n{'=' * 64}")
            infra_passed, infra_failed = run_infra_suite(ctx)

        total_passed = e2e_passed + infra_passed
        total_failed = e2e_failed + infra_failed
        print(f"\n{'=' * 64}")
        print(f"  Summary: {total_passed} passed / {total_failed} failed")
        print(f"{'=' * 64}")
        sys.exit(0 if total_failed == 0 else 1)
    finally:
        _teardown(ctx, original)


if __name__ == "__main__":
    main()
