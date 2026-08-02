"""Claude Code 受管启动与基础生命周期公共接口。"""

from __future__ import annotations

import threading
from collections.abc import Callable

from hermes.claude_code.contracts import (
    ClaudeCodeProcessLog,
    ClaudeCodeProcessPort,
    ClaudeCodeProcessSnapshot,
    ClaudeCodeReadResult,
    ClaudeCodeRuntimeError,
    ClaudeCodeSessionRef,
)
from hermes.claude_code.runtime import ClaudeCodeRuntime


_DEFAULT_RUNTIME_LOCK = threading.Lock()
_default_runtime: ClaudeCodeRuntime | None = None


def create_claude_code_runtime(
    *,
    process_manager=None,
    backend_provider: Callable[[str], object] | None = None,
    executable: str = "claude",
) -> ClaudeCodeRuntime:
    """为显式隔离或依赖注入创建 runtime，不作为逐任务生产入口。"""

    if process_manager is None:
        from hermes.processes import process_manager as default_process_manager

        process_manager = default_process_manager
    from hermes.claude_code.process_port import ProcessManagerClaudeCodePort

    port_kwargs = {}
    if backend_provider is not None:
        port_kwargs["backend_provider"] = backend_provider
    port = ProcessManagerClaudeCodePort(process_manager, **port_kwargs)
    return ClaudeCodeRuntime(port, executable=executable)


def get_claude_code_runtime() -> ClaudeCodeRuntime:
    """惰性返回绑定全局 ProcessManager 的进程级默认 runtime。"""

    global _default_runtime
    with _DEFAULT_RUNTIME_LOCK:
        if _default_runtime is None:
            _default_runtime = create_claude_code_runtime()
        return _default_runtime


__all__ = [
    "ClaudeCodeProcessLog",
    "ClaudeCodeProcessPort",
    "ClaudeCodeProcessSnapshot",
    "ClaudeCodeReadResult",
    "ClaudeCodeRuntime",
    "ClaudeCodeRuntimeError",
    "ClaudeCodeSessionRef",
    "create_claude_code_runtime",
    "get_claude_code_runtime",
]
