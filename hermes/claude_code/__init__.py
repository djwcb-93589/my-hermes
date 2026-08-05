"""Claude Code 受管生命周期、输出观察与有界工作流公共接口。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from hermes.claude_code.contracts import (
    ClaudeCodeActionKind,
    ClaudeCodeActionRequired,
    ClaudeCodeCurrentInteraction,
    ClaudeCodeEvent,
    ClaudeCodeEventType,
    ClaudeCodeInteractionResponse,
    ClaudeCodeProcessLog,
    ClaudeCodeProcessPort,
    ClaudeCodeProcessSnapshot,
    ClaudeCodeReadResult,
    ClaudeCodeRuntimeError,
    ClaudeCodeSessionRef,
    ClaudeCodeSnapshot,
    ClaudeCodeState,
    build_claude_code_action_id,
)
from hermes.claude_code.agent_adapter import (
    CLAUDE_CODE_GRANT_CONTEXT_KEY,
    CLAUDE_CODE_INVOCATION_PURPOSE,
    CLAUDE_CODE_REQUIRED_TRUSTED_CONTEXT,
    ClaudeCodeAgentAdapter,
    ClaudeCodeAgentAdapterError,
    ClaudeCodeInvocationGrant,
    ClaudeCodeOwner,
    create_cli_claude_code_grant,
    create_gateway_claude_code_grant,
)
from hermes.claude_code.request_detector import (
    ClaudeCodeExplicitRequest,
    ClaudeCodeExplicitRequestDetector,
    ClaudeCodeRequestOperation,
    detect_claude_code_request,
)
from hermes.claude_code.invocation_context import (
    ClaudeCodeInvocationContext,
    prepare_cli_claude_code_invocation,
    prepare_gateway_claude_code_invocation,
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
from hermes.claude_code.notification import (
    ClaudeCodeNotificationPort,
    ClaudeCodeNotificationReceipt,
    ClaudeCodeNotificationTarget,
    ClaudeCodeTerminalNotification,
    render_claude_code_terminal_notification,
)
from hermes.claude_code.runtime import ClaudeCodeRuntime
from hermes.claude_code.watcher import (
    ClaudeCodeCompletionWatch,
    ClaudeCodeCompletionWatcher,
    ClaudeCodeCompletionWatcherError,
    ClaudeCodeCompletionWatcherPolicy,
    ClaudeCodeCompletionWatchState,
)


_DEFAULT_RUNTIME_LOCK = threading.Lock()
_default_runtime: ClaudeCodeRuntime | None = None
_DEFAULT_CONTROLLER_LOCK = threading.Lock()
_default_controller: ClaudeCodeController | None = None
_DEFAULT_COMPLETION_WATCHER_LOCK = threading.Lock()
_default_completion_watcher: ClaudeCodeCompletionWatcher | None = None


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


def create_claude_code_completion_watcher(
    *,
    notification_port: ClaudeCodeNotificationPort,
    controller: ClaudeCodeController | None = None,
    policy: ClaudeCodeCompletionWatcherPolicy | None = None,
) -> ClaudeCodeCompletionWatcher:
    """为显式依赖注入创建 Watcher，不作为每次任务的生产入口。"""

    selected_controller = controller
    if selected_controller is None:
        selected_controller = get_claude_code_controller()
    return ClaudeCodeCompletionWatcher(
        selected_controller,
        notification_port,
        policy=policy,
    )


def get_claude_code_completion_watcher(
    *,
    notification_port: ClaudeCodeNotificationPort | None = None,
) -> ClaudeCodeCompletionWatcher:
    """惰性复用默认 Controller 的进程级 Watcher；首次调用需注入通知端口。"""

    global _default_completion_watcher
    with _DEFAULT_COMPLETION_WATCHER_LOCK:
        watcher = _default_completion_watcher
        if watcher is None or watcher.is_shutdown:
            if notification_port is None:
                raise ClaudeCodeCompletionWatcherError(
                    "notification_port_unavailable",
                    "Claude Code completion watcher requires a notification port",
                )
            watcher = create_claude_code_completion_watcher(
                notification_port=notification_port,
                controller=get_claude_code_controller(),
            )
            _default_completion_watcher = watcher
            return watcher
        if (
            notification_port is not None
            and not watcher.uses_notification_port(notification_port)
        ):
            raise ClaudeCodeCompletionWatcherError(
                "notification_port_unavailable",
                "Claude Code completion watcher already uses another notification port",
            )
        return watcher


__all__ = [
    "CLAUDE_CODE_GRANT_CONTEXT_KEY",
    "CLAUDE_CODE_INVOCATION_PURPOSE",
    "CLAUDE_CODE_REQUIRED_TRUSTED_CONTEXT",
    "ClaudeCodeActionKind",
    "ClaudeCodeActionRequired",
    "ClaudeCodeAgentAdapter",
    "ClaudeCodeAgentAdapterError",
    "ClaudeCodeCompletionWatch",
    "ClaudeCodeCompletionWatcher",
    "ClaudeCodeCompletionWatcherError",
    "ClaudeCodeCompletionWatcherPolicy",
    "ClaudeCodeCompletionWatchState",
    "ClaudeCodeCurrentInteraction",
    "ClaudeCodeController",
    "ClaudeCodeControllerError",
    "ClaudeCodeControllerOutcome",
    "ClaudeCodeControllerPolicy",
    "ClaudeCodeControllerResult",
    "ClaudeCodeEvent",
    "ClaudeCodeEventType",
    "ClaudeCodeExplicitRequest",
    "ClaudeCodeExplicitRequestDetector",
    "ClaudeCodeInteractionResponse",
    "ClaudeCodeInvocationContext",
    "ClaudeCodeInvocationGrant",
    "ClaudeCodeOutputDetector",
    "ClaudeCodeOutputNormalizer",
    "ClaudeCodeNotificationPort",
    "ClaudeCodeNotificationReceipt",
    "ClaudeCodeNotificationTarget",
    "ClaudeCodeProcessLog",
    "ClaudeCodeProcessPort",
    "ClaudeCodeProcessSnapshot",
    "ClaudeCodeReadResult",
    "ClaudeCodeRuntime",
    "ClaudeCodeRuntimeError",
    "ClaudeCodeRequestOperation",
    "ClaudeCodeSessionRef",
    "ClaudeCodeSnapshot",
    "ClaudeCodeState",
    "ClaudeCodeOwner",
    "ClaudeCodeTerminalNotification",
    "DetectionResult",
    "NormalizedOutputDelta",
    "create_claude_code_controller",
    "create_claude_code_completion_watcher",
    "create_claude_code_runtime",
    "create_cli_claude_code_grant",
    "create_gateway_claude_code_grant",
    "get_claude_code_controller",
    "get_claude_code_completion_watcher",
    "get_claude_code_runtime",
    "build_claude_code_action_id",
    "detect_claude_code_request",
    "prepare_cli_claude_code_invocation",
    "prepare_gateway_claude_code_invocation",
    "render_claude_code_terminal_notification",
]
