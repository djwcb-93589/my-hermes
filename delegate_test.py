"""
delegate 工具单元 + 隔离测试。

不依赖真实 LLM 或外部网络——通过 fake client 脚本化模型响应,驱动
handle_delegate 走完整链路。覆盖:
  - session 隔离(两 child 互不污染 cwd,不落 default)
  - backend 清理(成功 / max_iter / 异常 三条路径)
  - blocked tools(schema 不出现 + toolsets 不能绕过)
  - 结构化返回(completed / tool_error / model_error / invalid_args / max_iter)
  - 真实 terminal/file 工具分发(测试 cwd 真的隔离)

用法:python delegate_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from hermes import backends as bm
from hermes.config import MAX_CHILD_ITERATIONS
from hermes.tools import register_all, registry
from hermes.tools import delegate as dlg
from hermes.tools.terminal import run_terminal  # noqa: F401  (确保模块导入)


# ---------------------------------------------------------------------------
# Fake OpenAI client —— 脚本化 chat.completions.create 的返回
# ---------------------------------------------------------------------------

class _ToolCall:
    def __init__(self, name: str, arguments: str, call_id: str = "tc1"):
        self.id = call_id
        self.function = type("F", (), {"name": name, "arguments": arguments})()


class _Message:
    def __init__(self, content: str | None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or None


class _Choice:
    def __init__(self, message, finish_reason="stop"):
        self.message = message
        self.finish_reason = finish_reason


class _Response:
    def __init__(self, message, finish_reason="stop"):
        self.choices = [_Choice(message, finish_reason)]


class FakeClient:
    """client.chat.completions.create(...) 按 script 顺序返回。"""

    def __init__(self, script):
        # script: 每项要么是 _Response,要么是 Exception 实例(抛出)
        self._script = list(script)
        self.calls: list[dict] = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._script:
            raise RuntimeError("fake client script exhausted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _install_fake_client(fake: FakeClient):
    """把 delegate 模块的 client 替换为 fake。返回原 client 用于还原。"""
    original = dlg.client
    dlg.client = fake
    return original


def _restore_client(original):
    dlg.client = original


# ---------------------------------------------------------------------------
# terminal 调用 spy:记录每次 child 调 terminal 时传入的 session_key
# ---------------------------------------------------------------------------

_seen_session_keys: list[str | None] = []
_original_terminal_handler = None


def _install_terminal_spy() -> None:
    global _original_terminal_handler
    register_all()
    _original_terminal_handler = registry._tools["terminal"].handler

    def spy(args, **kwargs):
        _seen_session_keys.append(kwargs.get("session_key"))
        return _original_terminal_handler(args, **kwargs)

    registry._tools["terminal"].handler = spy


def _uninstall_terminal_spy() -> None:
    global _original_terminal_handler
    if _original_terminal_handler is not None:
        registry._tools["terminal"].handler = _original_terminal_handler
        _original_terminal_handler = None


def _reset_terminal_spy() -> None:
    _seen_session_keys.clear()


# ---------------------------------------------------------------------------
# utils
# ---------------------------------------------------------------------------

def _call(args: dict) -> dict:
    return json.loads(dlg.handle_delegate(args))


def _reset_backends() -> None:
    bm.cleanup_all_backends()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    register_all()
    _install_terminal_spy()
    try:
        _run_blocked_tools_suite()
        _run_structured_return_suite()
        _run_cleanup_suite()
        _run_isolation_suite()
        print("\nAll delegate tests passed.")
    finally:
        _uninstall_terminal_spy()
        _reset_backends()


# ---------------------------------------------------------------------------
# Test C: blocked tools
# ---------------------------------------------------------------------------

def _run_blocked_tools_suite() -> None:
    # 白名单 + blocked 集合
    assert dlg._ALLOWED_CHILD_TOOLSETS == {"terminal", "file", "skill"}, dlg._ALLOWED_CHILD_TOOLSETS
    for blocked in ("delegate_task", "memory", "skill_manage", "cron"):
        assert blocked in dlg.DELEGATE_BLOCKED_TOOLS, dlg.DELEGATE_BLOCKED_TOOLS
    print("  blocked set defined .... OK")

    # _filter_definitions 返回的工具里不应出现 blocked 工具
    defs = dlg._filter_definitions(["terminal", "file", "skill"])
    names = {d["function"]["name"] for d in defs}
    assert "delegate_task" not in names, names
    assert "memory" not in names, names
    assert "skill_manage" not in names, names
    assert "cron" not in names, names
    # skill_view / skills_list 仍可用
    assert "skill_view" in names and "skills_list" in names, names
    print("  schema excludes blocked . OK")

    # toolsets 参数不能绕过限制
    # 用户传 ["cron"] → 安全区为 [] → handle_delegate 返回 invalid_args
    d = _call({"goal": "x", "toolsets": ["cron"]})
    assert d["ok"] is False and d["status"] == "invalid_args", d
    d = _call({"goal": "x", "toolsets": ["memory"]})
    assert d["ok"] is False and d["status"] == "invalid_args", d
    d = _call({"goal": "x", "toolsets": ["delegate"]})
    assert d["ok"] is False and d["status"] == "invalid_args", d
    print("  toolsets cannot bypass . OK")


# ---------------------------------------------------------------------------
# Test D: structured return
# ---------------------------------------------------------------------------

def _run_structured_return_suite() -> None:
    # 1. invalid_args
    d = _call({"goal": ""})
    assert d["ok"] is False and d["status"] == "invalid_args", d
    assert d["error"] and d["child_session_key"] == "", d
    d = _call({"goal": "x", "toolsets": "not_a_list"})
    assert d["ok"] is False and d["status"] == "invalid_args", d
    print("  invalid_args .......... OK")

    # 2. completed —— 模型只回内容不调工具
    fake = FakeClient([_Response(_Message("all done"))])
    orig = _install_fake_client(fake)
    try:
        d = _call({"goal": "say hi"})
        assert d["ok"] is True and d["status"] == "completed", d
        assert d["summary"] == "all done", d
        assert d["iterations"] == 1 and d["tools_used"] == [], d
        assert d["child_session_key"].startswith("child-"), d
        assert d["error"] is None, d
    finally:
        _restore_client(orig)
    print("  completed ............. OK")

    # 3. tool_error —— 模型发出无效 JSON 参数
    fake = FakeClient([_Response(_Message(None, [
        _ToolCall("terminal", "not valid json {"),
    ]))])
    orig = _install_fake_client(fake)
    try:
        d = _call({"goal": "do thing"})
        assert d["ok"] is False and d["status"] == "tool_error", d
        assert "invalid JSON" in d["error"], d
    finally:
        _restore_client(orig)
    print("  tool_error (json) ..... OK")

    # 4. tool_error —— 工具 dispatch 抛异常(用一个不存在的工具名模拟)
    #    实际场景:_run_child 内 dispatch 异常被 catch。这里手工塞一个
    #    非法工具名走双层防御分支。
    fake = FakeClient([_Response(_Message(None, [
        _ToolCall("memory", "{}"),  # 在 DELEGATE_BLOCKED_TOOLS 内
    ]))])
    orig = _install_fake_client(fake)
    try:
        d = _call({"goal": "do thing"})
        assert d["ok"] is False and d["status"] == "tool_error", d
        assert "blocked tool" in d["error"], d
    finally:
        _restore_client(orig)
    print("  tool_error (blocked) .. OK")

    # 5. model_error —— API 调用抛异常
    fake = FakeClient([RuntimeError("upstream 500")])
    orig = _install_fake_client(fake)
    try:
        d = _call({"goal": "do thing"})
        assert d["ok"] is False and d["status"] == "model_error", d
        assert "upstream 500" in d["error"], d
    finally:
        _restore_client(orig)
    print("  model_error ........... OK")

    # 6. max_iterations —— 模型反复调工具,循环到底
    #    构造 MAX_CHILD_ITERATIONS + 1 条响应,每次都是 tool_call
    eternal = [
        _Response(_Message(None, [_ToolCall("terminal", '{"command": "true"}', str(i))]))
        for i in range(MAX_CHILD_ITERATIONS)
    ]
    fake = FakeClient(eternal)
    orig = _install_fake_client(fake)
    try:
        d = _call({"goal": "loop forever"})
        assert d["ok"] is False and d["status"] == "max_iterations", d
        assert d["iterations"] == MAX_CHILD_ITERATIONS, d
        assert "terminal" in d["tools_used"], d
    finally:
        _restore_client(orig)
    print("  max_iterations ........ OK")

    # 7. 所有返回都是合法 JSON(已隐式验证),再显式校验字段集合
    required = {"ok", "status", "summary", "iterations", "tools_used",
                "child_session_key", "error"}
    for args in [{"goal": "x"}, {"goal": ""}, {"goal": "x", "toolsets": ["cron"]}]:
        raw = dlg.handle_delegate(args)
        parsed = json.loads(raw)  # 不能炸
        assert required.issubset(parsed.keys()), parsed
    print("  json fields complete .. OK")


# ---------------------------------------------------------------------------
# Test B: backend 清理
# ---------------------------------------------------------------------------

def _run_cleanup_suite() -> None:
    # 成功路径
    fake = FakeClient([_Response(_Message("done"))])
    orig = _install_fake_client(fake)
    try:
        d = _call({"goal": "x"})
        assert d["ok"] is True, d
        assert d["child_session_key"] not in bm._backends, \
            f"backend leaked after completed: {d['child_session_key']}"
    finally:
        _restore_client(orig)
    print("  cleanup completed ..... OK")

    # max_iterations 路径(也实际跑 terminal,验证 backend 真的被创建又被清)
    _reset_terminal_spy()
    eternal = [
        _Response(_Message(None, [_ToolCall("terminal", '{"command": "echo x"}', str(i))]))
        for i in range(MAX_CHILD_ITERATIONS)
    ]
    fake = FakeClient(eternal)
    orig = _install_fake_client(fake)
    try:
        d = _call({"goal": "loop"})
        assert d["status"] == "max_iterations", d
        ck = d["child_session_key"]
        assert ck not in bm._backends, f"backend leaked after max_iter: {ck}"
        # terminal 真的被调过
        assert ck in _seen_session_keys, _seen_session_keys
    finally:
        _restore_client(orig)
    _reset_terminal_spy()
    print("  cleanup max_iter ...... OK")

    # 异常路径:model_error
    fake = FakeClient([RuntimeError("boom")])
    orig = _install_fake_client(fake)
    try:
        d = _call({"goal": "x"})
        assert d["status"] == "model_error", d
        assert d["child_session_key"] not in bm._backends
    finally:
        _restore_client(orig)
    print("  cleanup model_error ... OK")


# ---------------------------------------------------------------------------
# Test A: session 隔离 + 不落 default
# ---------------------------------------------------------------------------

def _run_isolation_suite() -> None:
    _reset_backends()
    _reset_terminal_spy()

    # 准备两个独立临时目录
    dir_a = Path(tempfile.mkdtemp(prefix="delegate-iso-A-"))
    dir_b = Path(tempfile.mkdtemp(prefix="delegate-iso-B-"))

    # LocalBackend 在 Windows 下跑 Git Bash,需要 MSYS 形式路径
    backend_tmp = bm.get_backend(session_key="iso-probe")
    shell_a = backend_tmp._cwd_to_shell(str(dir_a))
    shell_b = backend_tmp._cwd_to_shell(str(dir_b))
    bm.cleanup_backend("iso-probe")

    try:
        # child A:cd 到 dir_a,创建 a.txt
        fake_a = FakeClient([
            _Response(_Message(None, [
                _ToolCall("terminal", json.dumps({
                    "command": f"cd {shell_a} && echo a_marker > a.txt && pwd"
                }), "a1"),
            ])),
            _Response(_Message("done A")),
        ])
        orig = _install_fake_client(fake_a)
        try:
            d_a = _call({"goal": "create a.txt in dir A", "toolsets": ["terminal"]})
            assert d_a["ok"] is True and d_a["status"] == "completed", d_a
        finally:
            _restore_client(orig)

        # child B:cd 到 dir_b,创建 b.txt
        fake_b = FakeClient([
            _Response(_Message(None, [
                _ToolCall("terminal", json.dumps({
                    "command": f"cd {shell_b} && echo b_marker > b.txt && pwd"
                }), "b1"),
            ])),
            _Response(_Message("done B")),
        ])
        orig = _install_fake_client(fake_b)
        try:
            d_b = _call({"goal": "create b.txt in dir B", "toolsets": ["terminal"]})
            assert d_b["ok"] is True and d_b["status"] == "completed", d_b
        finally:
            _restore_client(orig)

        ck_a = d_a["child_session_key"]
        ck_b = d_b["child_session_key"]
        assert ck_a != ck_b, f"child session keys must differ: {ck_a}"

        # 文件落在各自目录里
        assert (dir_a / "a.txt").exists() and "a_marker" in (dir_a / "a.txt").read_text()
        assert (dir_b / "b.txt").exists() and "b_marker" in (dir_b / "b.txt").read_text()
        assert not (dir_a / "b.txt").exists(), "A must not contain b.txt"
        assert not (dir_b / "a.txt").exists(), "B must not contain a.txt"

        # backend 隔离:两个 child_session_key 都已被清理
        assert ck_a not in bm._backends and ck_b not in bm._backends

        # 两个 child 调 terminal 时都用了各自的 child_session_key
        assert ck_a in _seen_session_keys and ck_b in _seen_session_keys, _seen_session_keys
        # 任何 child 都没用 default
        assert "default" not in _seen_session_keys, \
            f"child must not fall back to default session; saw {_seen_session_keys}"

        print("  isolation (dirs) ...... OK")
        print("  isolation (cleanup) ... OK")
        print("  isolation (no default). OK")
    finally:
        shutil.rmtree(dir_a, ignore_errors=True)
        shutil.rmtree(dir_b, ignore_errors=True)
        _reset_terminal_spy()


if __name__ == "__main__":
    main()
