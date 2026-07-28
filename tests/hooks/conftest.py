"""hook 系统测试共享 fixture。"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from hermes.hooks import HookContext, HookEvent


@pytest.fixture
def ctx():
    return HookContext(invocation_id="inv-1", payload={"key": "value"})


@pytest.fixture
def event():
    def _make(name="post_tool_call", payload=None):
        return HookEvent(
            name=name,
            context=HookContext(invocation_id="inv-x", payload=payload or {}),
        )
    return _make


# ===================== event loop 线程辅助（bridge 测试用）=====================

@pytest.fixture
def loop_thread():
    """启动一个持续运行的 event loop 线程，返回 (loop, stop_fn)。"""
    box: dict = {}
    ready = threading.Event()

    def runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        box["loop"] = loop
        ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    ready.wait(timeout=2.0)
    loop = box["loop"]

    def stop():
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2.0)

    return loop, stop


def run_on_loop(loop, coro):
    """在指定 loop 上跑协程并同步等结果。"""
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=2.0)


# ===================== plugin 临时目录辅助 =====================

@pytest.fixture
def plugin_root(tmp_path) -> Path:
    """临时 plugin 搜索根目录。"""
    root = tmp_path / "plugins"
    root.mkdir()
    return root


@pytest.fixture
def make_plugin(plugin_root):
    """在 plugin_root 下创建一个最小可用 plugin 目录。

    register_body 为 register 函数体（多行文本，需自行保证缩进正确）。
    返回 plugin 目录路径。
    """
    def _make(name="demo-plugin", *, register_body="pass", version="1.0.0", description=None):
        d = plugin_root / name
        d.mkdir()
        manifest = f"name: {name}\nversion: {version}\n"
        if description is not None:
            manifest += f"description: {description}\n"
        (d / "plugin.yaml").write_text(manifest, encoding="utf-8")
        (d / "__init__.py").write_text(
            "def register(context):\n"
            f"{register_body}\n",
            encoding="utf-8",
        )
        return d
    return _make
