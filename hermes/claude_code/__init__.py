"""Claude Code 受管启动与基础生命周期公共接口。"""

from __future__ import annotations

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


def create_claude_code_runtime(
    *,
    process_manager=None,
    backend_provider: Callable[[str], object] | None = None,
    executable: str = "claude",
) -> ClaudeCodeRuntime:
    """显式组合一个 runtime；调用方负责复用实例和现有 session cleanup。"""

    if process_manager is None:
        from hermes.processes import process_manager as default_process_manager

        process_manager = default_process_manager
    from hermes.claude_code.process_port import ProcessManagerClaudeCodePort

    port_kwargs = {}
    if backend_provider is not None:
        port_kwargs["backend_provider"] = backend_provider
    port = ProcessManagerClaudeCodePort(process_manager, **port_kwargs)
    return ClaudeCodeRuntime(port, executable=executable)


__all__ = [
    "ClaudeCodeProcessLog",
    "ClaudeCodeProcessPort",
    "ClaudeCodeProcessSnapshot",
    "ClaudeCodeReadResult",
    "ClaudeCodeRuntime",
    "ClaudeCodeRuntimeError",
    "ClaudeCodeSessionRef",
    "create_claude_code_runtime",
]
