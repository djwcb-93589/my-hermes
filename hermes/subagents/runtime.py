"""运行调用方已准备完成的隔离 Agent，并统一释放其会话资源。"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from hermes.agent_loop import AgentLoop, AgentLoopResult, ParsedToolCall
from hermes.session_resources import cleanup_session_resources
from hermes.subagents.contracts import (
    IsolatedAgentRunResult,
    IsolatedAgentRunSpec,
    IsolatedAgentSessionInitializer,
)
from hermes.subagents.errors import IsolatedAgentSessionSetupError


if TYPE_CHECKING:
    from hermes.hooks import SyncHookRegistry
    from hermes.processes import ProcessManager
    from hermes.tools import ToolRegistry


logger = logging.getLogger(__name__)


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
        session_initializer: IsolatedAgentSessionInitializer | None = None,
    ) -> IsolatedAgentRunResult:
        """校验完整计划，并在接受有效 session 后独占最终清理。"""

        result: IsolatedAgentRunResult | None = None
        cleanup_session_key: str | None = None
        try:
            if not isinstance(spec, IsolatedAgentRunSpec):
                result = self._invalid_spec_result(
                    "spec must be an IsolatedAgentRunSpec"
                )
            elif (
                not isinstance(spec.session_key, str)
                or not spec.session_key.strip()
            ):
                result = self._invalid_spec_result(
                    "session_key must be a non-empty string"
                )
            else:
                # 仅在可信 session_key 建立后接管资源，之前的错误不触发清理。
                cleanup_session_key = spec.session_key
                validation_error = self._validate_spec_fields(spec)
                if validation_error is not None:
                    result = self._invalid_spec_result(validation_error)
                elif (
                    session_initializer is not None
                    and not callable(session_initializer)
                ):
                    result = self._invalid_spec_result(
                        "session_initializer must be callable"
                    )
                elif callable(cancel_checker) and bool(cancel_checker()):
                    result = self._cancelled_result()
                else:
                    plain_spec = spec.to_dict()
                    tools = plain_spec["tool_definitions"]
                    model_kwargs = plain_spec["model_kwargs"]
                    if not isinstance(tools, list) or any(
                        not isinstance(definition, dict)
                        for definition in tools
                    ):
                        raise TypeError(
                            "tool_definitions must resolve to a list of dicts"
                        )
                    if not isinstance(model_kwargs, dict):
                        raise TypeError(
                            "model_kwargs must resolve to a dict"
                        )
                    setup_result = self._initialize_session(
                        session_initializer,
                        session_key=spec.session_key,
                    )
                    if setup_result is not None:
                        result = setup_result
                    else:
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
            if cleanup_session_key is not None:
                try:
                    cleanup_session_resources(
                        cleanup_session_key,
                        process_manager=self._process_manager,
                    )
                except Exception as exc:
                    logger.exception(
                        (
                            "Isolated Agent session cleanup failed: "
                            "exception_type=%s"
                        ),
                        type(exc).__name__,
                    )
                    # 已形成的业务或校验结果不可被清理异常覆盖。
                    if result is None:
                        result = self._internal_error_result(exc)

        if result is None:
            return self._internal_error_result(
                RuntimeError("isolated execution produced no result")
            )
        return result

    @staticmethod
    def _validate_spec_fields(spec: IsolatedAgentRunSpec) -> str | None:
        """在 session 所有权建立后校验其余完整执行边界。"""

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
        if not isinstance(spec.model_kwargs, Mapping):
            return "model_kwargs must be a mapping"
        return None

    @staticmethod
    def _initialize_session(
        initializer: IsolatedAgentSessionInitializer | None,
        *,
        session_key: str,
    ) -> IsolatedAgentRunResult | None:
        """在 Executor 所有权内初始化 Session，并收敛安全错误。"""

        if initializer is None:
            return None
        try:
            initializer(session_key=session_key)
        except IsolatedAgentSessionSetupError as exc:
            return IsolatedAgentExecutor._session_setup_error_result(
                safe_message=exc.safe_message,
                error_type=exc.error_type,
                retryable=exc.retryable,
            )
        except Exception as exc:
            logger.error(
                "Isolated Agent session setup failed: exception_type=%s",
                type(exc).__name__,
            )
            return IsolatedAgentExecutor._session_setup_error_result(
                safe_message="isolated agent session setup failed",
                error_type="isolated_session_setup_failed",
                retryable=False,
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
    def _session_setup_error_result(
        *,
        safe_message: str,
        error_type: str,
        retryable: bool,
    ) -> IsolatedAgentRunResult:
        """构造不泄漏初始化参数的稳定 Session setup 失败。"""

        return IsolatedAgentRunResult(
            ok=False,
            status="error",
            summary="",
            messages=(),
            iterations=0,
            tools_used=(),
            error=safe_message,
            error_type=error_type,
            fatal=not retryable,
            retryable=retryable,
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
]
