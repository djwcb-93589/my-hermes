"""运行调用方已准备完成的隔离 Agent，并统一释放其会话资源。"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import TYPE_CHECKING

from hermes.agent_loop import AgentLoop, AgentLoopResult, ParsedToolCall
from hermes.session_resources import cleanup_session_resources
from hermes.subagents.contracts import (
    IsolatedAgentRunResult,
    IsolatedAgentRunSpec,
)


if TYPE_CHECKING:
    from hermes.hooks import SyncHookRegistry
    from hermes.processes import ProcessManager
    from hermes.tools import ToolRegistry


logger = logging.getLogger(__name__)


def _mutable_copy(value: object) -> object:
    """把契约中的冻结容器还原为本次运行独享的可变副本。"""

    if isinstance(value, Mapping):
        return {
            deepcopy(key): _mutable_copy(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_mutable_copy(item) for item in value]
    if isinstance(value, frozenset):
        return {_mutable_copy(item) for item in value}
    return deepcopy(value)


def _definition_tool_name(
    definition: Mapping[str, object],
) -> str | None:
    """从 OpenAI function tool Schema 中读取工具名。"""

    function = definition.get("function")
    if not isinstance(function, Mapping):
        return None
    name = function.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    return name


class IsolatedAgentLoop(AgentLoop):
    """无持久化、压缩、fallback、retry 或 continuation 的隔离循环。"""

    def __init__(
        self,
        *,
        allowed_tool_names: frozenset[str],
        **kwargs,
    ) -> None:
        """固定 Schema 与 dispatch 的交集，拒绝模型伪造未暴露工具。"""

        tools = kwargs.get("tools", ())
        schema_tool_names = frozenset(
            name
            for definition in tools
            if isinstance(definition, Mapping)
            for name in (_definition_tool_name(definition),)
            if name is not None
        )
        self.allowed_tool_names = (
            frozenset(allowed_tool_names) & schema_tool_names
        )
        super().__init__(**kwargs)

    def handle_model_error(self, exc, messages) -> str:
        """隔离运行不做 fallback 或 retry，模型异常立即终止。"""

        return "abort"

    def dispatch_one(
        self,
        tool_call,
        parsed_call: ParsedToolCall | None = None,
    ) -> tuple[str, str | None, str | None]:
        """仅分发本次 Schema 与允许名称共同暴露的已验证调用。"""

        parsed = parsed_call or self._parse_tool_call(tool_call)
        tool_name = self._tool_call_name(tool_call)
        if (
            parsed.tool_name != tool_name
            or not parsed.is_verified_for(self.registry)
        ):
            return (
                "(error: tool call rejected)",
                "dispatch",
                "tool call was not internally validated",
            )
        if (
            parsed.tool_name not in self.allowed_tool_names
            or not parsed.is_dispatchable
        ):
            if parsed.tool_name not in self.allowed_tool_names:
                return (
                    json.dumps(
                        {
                            "ok": False,
                            "error_type": "tool_disabled",
                            "fatal": True,
                            "error": (
                                "Tool is not enabled in this session: "
                                f"{parsed.tool_name}"
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    "disabled",
                    f"disabled tool invoked: {parsed.tool_name!r}",
                )
            return (
                parsed.error_output or "(error: tool call rejected)",
                parsed.error_status,
                parsed.error_detail,
            )
        return super().dispatch_one(tool_call, parsed)


class IsolatedAgentExecutor:
    """使用构造阶段绑定的依赖执行一个隔离 Agent。"""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        client,
        process_manager: ProcessManager,
    ) -> None:
        """绑定工具注册表、模型客户端和唯一的 ProcessManager。"""

        if not callable(getattr(registry, "get_entry", None)) or not callable(
            getattr(registry, "dispatch", None)
        ):
            raise TypeError("registry must provide get_entry() and dispatch()")
        if client is None:
            raise TypeError("client is required")
        if not callable(getattr(process_manager, "cleanup_session", None)):
            raise TypeError("process_manager must provide cleanup_session()")
        self._registry = registry
        self._client = client
        self._process_manager = process_manager

    @property
    def registry(self) -> ToolRegistry:
        """返回构造阶段绑定的 ToolRegistry。"""

        return self._registry

    @property
    def process_manager(self) -> ProcessManager:
        """返回构造阶段绑定的应用级 ProcessManager。"""

        return self._process_manager

    def execute(
        self,
        spec: IsolatedAgentRunSpec,
        *,
        cancel_checker: Callable[[], bool] | None = None,
        tool_context: Mapping[str, object] | None = None,
        hook_registry: SyncHookRegistry | None = None,
        parent_run_id: str | None = None,
    ) -> IsolatedAgentRunResult:
        """执行完整计划；方法一旦进入便独占 child session 的最终清理。"""

        result: IsolatedAgentRunResult | None = None
        cleanup_error: Exception | None = None
        try:
            if callable(cancel_checker) and bool(cancel_checker()):
                result = self._cancelled_result()
            else:
                validation_error = self._validate_spec(spec)
                if validation_error is not None:
                    result = self._invalid_spec_result(validation_error)
                else:
                    tools = [
                        _mutable_copy(definition)
                        for definition in spec.tool_definitions
                    ]
                    model_kwargs = _mutable_copy(spec.model_kwargs)
                    if not isinstance(model_kwargs, dict):
                        raise TypeError("model_kwargs must resolve to a dict")
                    loop = IsolatedAgentLoop(
                        model=spec.model,
                        max_iterations=spec.max_iterations,
                        tools=tools,
                        system_prompt=spec.system_prompt,
                        registry=self._registry,
                        client=self._client,
                        session_key=spec.session_key,
                        allowed_tool_names=spec.allowed_tool_names,
                        model_kwargs=model_kwargs,
                        cancel_checker=cancel_checker,
                        tool_context=(
                            dict(tool_context)
                            if tool_context is not None
                            else None
                        ),
                        hook_registry=hook_registry,
                        parent_run_id=parent_run_id,
                    )
                    result = self._from_agent_loop_result(
                        loop.run(spec.goal)
                    )
        except Exception as exc:
            result = self._internal_error_result(exc)
        finally:
            try:
                cleanup_session_resources(
                    spec.session_key,
                    process_manager=self._process_manager,
                )
            except Exception as exc:
                cleanup_error = exc
                logger.exception(
                    "Isolated Agent session cleanup failed"
                )

        if cleanup_error is not None:
            return self._internal_error_result(cleanup_error)
        if result is None:
            return self._internal_error_result(
                RuntimeError("isolated execution produced no result")
            )
        return result

    @staticmethod
    def _validate_spec(spec: IsolatedAgentRunSpec) -> str | None:
        """验证 Runtime 启动所需的最小完整执行边界。"""

        if not isinstance(spec, IsolatedAgentRunSpec):
            return "spec must be an IsolatedAgentRunSpec"
        if not isinstance(spec.session_key, str) or not spec.session_key.strip():
            return "session_key must be a non-empty string"
        if not isinstance(spec.goal, str) or not spec.goal.strip():
            return "goal must be a non-empty string"
        if not isinstance(spec.system_prompt, str):
            return "system_prompt must be a string"
        if not isinstance(spec.model, str) or not spec.model.strip():
            return "model must be a non-empty string"
        if (
            isinstance(spec.max_iterations, bool)
            or not isinstance(spec.max_iterations, int)
            or spec.max_iterations <= 0
        ):
            return "max_iterations must be a positive integer"
        if not spec.tool_definitions:
            return "tool_definitions must contain at least one tool"

        definition_names: list[str] = []
        for definition in spec.tool_definitions:
            if not isinstance(definition, Mapping):
                return "each tool definition must be a mapping"
            name = _definition_tool_name(definition)
            if name is None:
                return "each tool definition must contain a function name"
            definition_names.append(name)
        if len(definition_names) != len(set(definition_names)):
            return "tool_definitions must not contain duplicate names"
        if (
            not spec.allowed_tool_names
            or any(
                not isinstance(name, str) or not name
                for name in spec.allowed_tool_names
            )
        ):
            return "allowed_tool_names must contain valid tool names"
        if frozenset(definition_names) != spec.allowed_tool_names:
            return (
                "tool_definitions and allowed_tool_names must describe "
                "the same boundary"
            )
        return None

    @staticmethod
    def _from_agent_loop_result(
        result: AgentLoopResult,
    ) -> IsolatedAgentRunResult:
        """完整映射 AgentLoopResult，避免向上层暴露内部结果类型。"""

        return IsolatedAgentRunResult(
            ok=result.ok,
            status=result.status,
            summary=result.summary,
            messages=tuple(result.messages),
            iterations=result.iterations,
            tools_used=tuple(result.tools_used),
            error=result.error,
            error_type=result.error_type,
            fatal=result.fatal,
            retryable=result.retryable,
            approval_request=result.approval_request,
            tool_batches=result.tool_batches,
            tool_call_count=result.tool_call_count,
        )

    @staticmethod
    def _cancelled_result() -> IsolatedAgentRunResult:
        """构造执行前已经取消的稳定结果。"""

        return IsolatedAgentRunResult(
            ok=False,
            status="cancelled",
            summary="",
            messages=(),
            iterations=0,
            tools_used=(),
            error="cancel requested",
            error_type="cancelled",
            fatal=True,
            retryable=False,
            approval_request=None,
            tool_batches=0,
            tool_call_count=0,
        )

    @staticmethod
    def _invalid_spec_result(error: str) -> IsolatedAgentRunResult:
        """构造无效完整计划的稳定结果。"""

        return IsolatedAgentRunResult(
            ok=False,
            status="invalid_args",
            summary="",
            messages=(),
            iterations=0,
            tools_used=(),
            error=error,
            error_type="invalid_args",
            fatal=True,
            retryable=False,
            approval_request=None,
            tool_batches=0,
            tool_call_count=0,
        )

    @staticmethod
    def _internal_error_result(
        error: BaseException,
    ) -> IsolatedAgentRunResult:
        """把未预期基础设施异常转换为稳定 internal_error。"""

        return IsolatedAgentRunResult(
            ok=False,
            status="error",
            summary="",
            messages=(),
            iterations=0,
            tools_used=(),
            error=repr(error),
            error_type="internal_error",
            fatal=True,
            retryable=False,
            approval_request=None,
            tool_batches=0,
            tool_call_count=0,
        )


__all__ = [
    "IsolatedAgentExecutor",
    "IsolatedAgentLoop",
]
