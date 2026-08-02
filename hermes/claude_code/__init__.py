"""Claude Code 受管生命周期、输出观察与有界工作流公共接口。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from hermes.claude_code.contracts import (
    ClaudeCodeActionKind,
    ClaudeCodeActionRequired,
    ClaudeCodeEvent,
    ClaudeCodeEventType,
    ClaudeCodeProcessLog,
    ClaudeCodeProcessPort,
    ClaudeCodeProcessSnapshot,
    ClaudeCodeReadResult,
    ClaudeCodeRuntimeError,
    ClaudeCodeSessionRef,
    ClaudeCodeSnapshot,
    ClaudeCodeState,
)
from hermes.claude_code.controller import (
    ClaudeCodeController,
    ClaudeCodeControllerError,
    ClaudeCodeControllerOutcome,
    ClaudeCodeControllerResult,
)
from hermes.claude_code.controller_policy import (
    ClaudeCodeControllerPolicy,
)
from hermes.claude_code.detector import (
    ClaudeCodeOutputDetector,
    DetectionResult,
)
from hermes.claude_code.normalizer import (
    ClaudeCodeOutputNormalizer,
    NormalizedOutputDelta,
)
from hermes.claude_code.runtime import ClaudeCodeRuntime


_DEFAULT_RUNTIME_LOCK = threading.Lock()
_default_runtime: ClaudeCodeRuntime | None = None
_DEFAULT_CONTROLLER_LOCK = threading.Lock()
_default_controller: ClaudeCodeController | None = None


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


def create_claude_code_controller(
    *,
    runtime: ClaudeCodeRuntime | None = None,
    policy: ClaudeCodeControllerPolicy | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> ClaudeCodeController:
    """为显式隔离或依赖注入创建 Controller，不作为逐任务生产入口。"""

    selected_runtime = runtime
    if selected_runtime is None:
        selected_runtime = get_claude_code_runtime()
    return ClaudeCodeController(
        selected_runtime,
        policy=policy,
        clock=clock,
        sleeper=sleeper,
    )


def get_claude_code_controller() -> ClaudeCodeController:
    """惰性返回复用默认 Runtime 的进程级工作流 Controller。"""

    global _default_controller
    with _DEFAULT_CONTROLLER_LOCK:
        if _default_controller is None:
            _default_controller = create_claude_code_controller(
                runtime=get_claude_code_runtime(),
            )
        return _default_controller


__all__ = [
    "ClaudeCodeActionKind",
    "ClaudeCodeActionRequired",
    "ClaudeCodeController",
    "ClaudeCodeControllerError",
    "ClaudeCodeControllerOutcome",
    "ClaudeCodeControllerPolicy",
    "ClaudeCodeControllerResult",
    "ClaudeCodeEvent",
    "ClaudeCodeEventType",
    "ClaudeCodeOutputDetector",
    "ClaudeCodeOutputNormalizer",
    "ClaudeCodeProcessLog",
    "ClaudeCodeProcessPort",
    "ClaudeCodeProcessSnapshot",
    "ClaudeCodeReadResult",
    "ClaudeCodeRuntime",
    "ClaudeCodeRuntimeError",
    "ClaudeCodeSessionRef",
    "ClaudeCodeSnapshot",
    "ClaudeCodeState",
    "DetectionResult",
    "NormalizedOutputDelta",
    "create_claude_code_controller",
    "create_claude_code_runtime",
    "get_claude_code_controller",
    "get_claude_code_runtime",
]
