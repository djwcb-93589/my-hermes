"""Cron 已领取运行的独立 Agent 执行器。"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from hermes.backends import cleanup_backend, get_backend
from hermes.config import (
    COMPRESSION_THRESHOLD,
    DB_PATH,
    MAX_CONTINUATIONS,
    MAX_RETRIES,
    MODEL,
    FALLBACK_MAX_OUTPUT_TOKENS,
    MODEL_MAX_OUTPUT_TOKENS,
    client,
)
from hermes.conversation import ConversationAgentLoop
from hermes.cron.job import CronJob, CronRun
from hermes.cron.artifacts import cron_run_artifact_dir
from hermes.cron.capability import (
    CronCapabilityGuard,
    _normalise_path,
    validate_cron_capability_grant,
)
from hermes.db import (
    DBError,
    add_messages,
    ensure_session,
    get_cron_run,
    get_active_cron_capability_grant,
    list_cron_incomplete_tool_executions,
    init_db,
    transition_cron_run,
)
from hermes.path_utils import git_bash_to_windows_path
from hermes.prompt import build_system_prompt
from hermes.durable_tool_dispatcher import (
    DurableToolDispatcher,
    DurableToolExecutionContext,
)
from hermes.tool_execution_recovery import ToolExecutionRecoveryService
from hermes.tools.skill import load_skill_body
from hermes.tools import (
    ExecutionEnvironment,
    ToolPolicy,
    ToolRiskLevel,
    register_all,
    registry,
)


@dataclass(frozen=True)
class CronExecutionContext:
    """只由执行器构造并传给工具 handler 的可信 Cron 运行上下文。"""

    job_id: str
    run_id: str
    execution_kind: str
    workdir: str
    artifact_dir: str
    toolsets: tuple[str, ...]
    file_access_scope: tuple[str, ...]
    max_risk_level: ToolRiskLevel
    timeout_seconds: float
    delivery_target: Mapping[str, object]
    trusted_source: Mapping[str, str]
    cancel_checker: Callable[[], bool] | None = None


@dataclass(frozen=True)
class CronExecutionResult:
    """一次 Cron AgentLoop 的结构化执行结果。"""

    job_id: str
    run_id: str
    session_id: str
    status: str
    final_response: str
    iterations: int
    tools_used: tuple[str, ...]
    artifacts: tuple[dict, ...]
    tool_batches: int = 0
    tool_call_count: int = 0
    error_type: str | None = None
    error: str | None = None
    timed_out: bool = False
    cancelled: bool = False
    retryable: bool = False


def _cron_session_id(job_id: str, run_id: str) -> str:
    """生成与单次运行一一对应的稳定会话标识。"""
    return f"cron:{job_id}:{run_id}"


def _result_artifacts(messages: list[dict], artifact_dir: str) -> list[dict]:
    """从已有工具结果提取轻量产物描述，不执行投递或复制文件。"""
    artifacts: list[dict] = []
    artifact_root = Path(artifact_dir).resolve()
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            payload = json.loads(str(message.get("content", "")))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        # 优先用 abs_path（file 工具返回的 Windows 绝对路径）；没有再退回 path
        # 并用 git_bash_to_windows_path 把 /d/... 转成 D:\... 否则 Windows 上
        # Path("/d/...").resolve() 会解析到当前盘符的子目录，relative_to 失败。
        raw_path = payload.get("abs_path") or payload.get("path")
        if not isinstance(raw_path, str):
            continue
        normalized = git_bash_to_windows_path(raw_path) if os.name == "nt" else raw_path
        try:
            candidate = Path(normalized).resolve()
            candidate.relative_to(artifact_root)
        except (OSError, ValueError):
            continue
        # 排除 artifact 目录本身：子代理常 list/stat 这个目录，tool result 的
        # path 会等于 artifact_root，relative_to 通过但它是目录不是文件，投递
        # 阶段 capture_file 会失败。只收严格在 artifact_root 之下的路径。
        if candidate == artifact_root:
            continue
        artifacts.append({
            "kind": "tool_file_reference",
            "tool_call_id": str(message.get("tool_call_id", "")),
            "path": str(candidate),
            "description": "A tool result referenced this file; delivery was not requested.",
        })
    return artifacts


def _artifact_limits_exceeded(artifact_dir: str, grant: dict) -> bool:
    """在运行结束前按当前文件状态检查授权中的单文件和总产物上限。"""
    scope = dict(grant.get("scope") or {})
    try:
        per_file_limit = int(scope["max_artifact_file_bytes"])
        total_limit = int(scope["max_artifact_total_bytes"])
    except (KeyError, TypeError, ValueError):
        return True
    total = 0
    try:
        for candidate in Path(artifact_dir).rglob("*"):
            if not candidate.is_file():
                continue
            size = candidate.stat().st_size
            if size > per_file_limit:
                return True
            total += size
            if total > total_limit:
                return True
    except OSError:
        return True
    return False


def _load_cron_skills(job: CronJob) -> str:
    """在启动循环前验证并装载任务明确允许的 Skill，不依赖模型临时发现。"""
    if not job.skills:
        return ""
    sections: list[str] = []
    for name in job.skills:
        payload = load_skill_body(name)
        if not payload.get("ok") or not isinstance(payload.get("body"), str):
            raise ValueError("Cron skill is unavailable")
        sections.append(f"## Preloaded Skill: {payload.get('name', name)}\n{payload['body']}")
    return "\n\n".join(sections)


def delivery_preparation_status(job: CronJob, status: str) -> str:
    """判断执行终态是否需要由 Gateway 后续持久准备投递。"""
    policy = str(dict(job.delivery_config or {}).get("policy", "text")).strip().lower()
    if policy in {"silent", "none"}:
        return "not_requested"
    if policy == "failure_only" and status == "completed":
        return "not_requested"
    return "preparation_pending"


def _terminal_outcome(loop_result, *, timed_out: bool, cancelled: bool, guard: CronCapabilityGuard, artifact_limit: bool) -> tuple[str, str | None, str, str | None]:
    """单点决定 Cron 的用户可见终态，非完成状态绝不采用模型自述。"""
    if artifact_limit:
        return (
            "blocked", "cron_artifact_limit_exceeded",
            "Cron artifact limits were exceeded. Update the task or request authorization again.",
            "cron_artifact_limit_exceeded",
        )
    if timed_out:
        return "cancelled", "timeout", "Cron task timed out before completion.", "timeout"
    if cancelled or loop_result.status == "cancelled":
        return "cancelled", "cancelled", "Cron task was cancelled before completion.", "cancelled"
    if loop_result.ok:
        return "completed", None, str(loop_result.summary or ""), None
    # capability 拒绝允许模型换用已授权方案恢复；只有任务最终未完成时，
    # 才把运行期间记录的拒绝作为阻塞原因。
    if guard.violation is not None:
        return (
            "blocked", "cron_capability_denied",
            "Cron capability authorization does not permit a requested operation. Update the task or request authorization again.",
            "cron_capability_denied",
        )
    return (
        "failed",
        str(loop_result.error_type or loop_result.status or "cron_execution_failed"),
        "Cron task failed before completion. Review the run history and retry the task if appropriate.",
        str(loop_result.error_type or loop_result.status or "cron_execution_failed"),
    )


class CronExecutor:
    """把一条已领取的 CronRun 交给现有 ConversationAgentLoop 执行。"""

    def __init__(
        self,
        db_path: str | None = None,
        *,
        cancel_checker: Callable[[], bool] | None = None,
        lease_name: str | None = None,
        instance_id: str | None = None,
        lease_epoch: int | None = None,
    ):
        self._db_path = db_path or DB_PATH
        self._external_cancel_checker = cancel_checker
        fence_values = (lease_name, instance_id, lease_epoch)
        if any(value is not None for value in fence_values):
            if any(value is None for value in fence_values):
                raise ValueError("Cron executor lease fencing identity is incomplete")
            self._lease_fence = {
                "lease_name": str(lease_name),
                "instance_id": str(instance_id),
                "lease_epoch": lease_epoch,
            }
        else:
            self._lease_fence = {}

    def _build_context(
        self,
        job: CronJob,
        run: CronRun,
        *,
        deadline: float,
        grant: dict,
    ) -> CronExecutionContext:
        """规范化受控运行上下文，拒绝不存在的工作目录。"""
        # 复用 capability._normalise_path 的 Git Bash -> Windows 路径转换，
        # 让 LLM 传的 /e/双周报 能被正确解析成 E:\双周报，与 terminal 行为一致。
        workdir = _normalise_path(job.workdir or os.getcwd())
        if not Path(workdir).is_dir():
            raise ValueError("Cron workdir does not exist or is not a directory")
        artifact_dir = cron_run_artifact_dir(job.job_id, run.run_id).resolve()
        artifact_dir.mkdir(parents=True, exist_ok=True)

        scope = dict(grant.get("scope") or {})
        raw_risk = scope.get("terminal_risk_max", ToolRiskLevel.HIGH.value)
        try:
            max_risk_level = ToolRiskLevel(str(raw_risk).lower())
        except (TypeError, ValueError) as exc:
            raise ValueError("Cron capability_grant max_risk_level is invalid") from exc

        def cancelled() -> bool:
            if time.monotonic() >= deadline:
                return True
            return bool(
                self._external_cancel_checker is not None
                and self._external_cancel_checker()
            )

        return CronExecutionContext(
            job_id=job.job_id,
            run_id=run.run_id,
            execution_kind="cron",
            workdir=str(workdir),
            artifact_dir=str(artifact_dir),
            toolsets=tuple(job.toolsets),
            file_access_scope=tuple(str(value) for value in scope.get("allowed_roots", [])),
            max_risk_level=max_risk_level,
            timeout_seconds=float(job.execution_timeout_seconds),
            delivery_target=MappingProxyType(dict(scope.get("delivery_target") or {})),
            trusted_source=MappingProxyType({
                "created_source": job.created_source,
                "creator_id": job.creator_id,
            }),
            cancel_checker=cancelled,
        )

    def _write_terminal_result(
        self,
        conn,
        run_id: str,
        *,
        status: str,
        summary: str,
        error_type: str | None,
        artifacts: list[dict] | None = None,
        delivery_status: str | None = None,
    ) -> None:
        """将执行结论原子写回独立运行事实。"""
        transition_cron_run(
            conn,
            run_id,
            status,
            error_type=error_type,
            result_summary=summary[:8000],
            artifacts=artifacts or [],
            delivery_status=delivery_status,
            **self._lease_fence,
        )

    def execute(
        self,
        job: CronJob,
        run: CronRun,
        *,
        recovery_only: bool = False,
    ) -> CronExecutionResult:
        """执行已领取运行；调用方负责领取，执行器负责运行到终态。"""
        session_id = _cron_session_id(job.job_id, run.run_id)
        conn = init_db(self._db_path)
        backend_created = False
        try:
            stored_run = get_cron_run(conn, run.run_id)
            if stored_run is None or stored_run["job_id"] != job.job_id:
                raise DBError("Cron run does not belong to the supplied job")
            if stored_run["status"] != "claimed" and not (
                recovery_only and stored_run["status"] == "running"
            ):
                raise DBError("Cron executor requires a claimed run")
            if self._lease_fence and (
                stored_run["claim_lease_name"] != self._lease_fence["lease_name"]
                or stored_run["claim_instance_id"] != self._lease_fence["instance_id"]
                or stored_run["claim_epoch"] != self._lease_fence["lease_epoch"]
            ):
                raise DBError("Cron run claim does not match executor lease")

            if job.approval_status != "granted":
                summary = "Cron capability authorization is not active. Update the task or request authorization again."
                self._write_terminal_result(
                    conn,
                    run.run_id,
                    status="blocked",
                    summary=summary,
                    error_type="cron_capability_grant_invalid",
                    delivery_status=delivery_preparation_status(job, "blocked"),
                )
                return CronExecutionResult(
                    job.job_id, run.run_id, session_id, "blocked", summary,
                    0, (), (), "cron_capability_grant_invalid", summary,
                )
            deadline = time.monotonic() + float(job.execution_timeout_seconds)
            grant = get_active_cron_capability_grant(conn, job.job_id)
            try:
                context = self._build_context(job, run, deadline=deadline, grant=grant or {})
            except ValueError as exc:
                summary = "Cron execution context is invalid. Update the task configuration and try again."
                self._write_terminal_result(
                    conn,
                    run.run_id,
                    status="blocked",
                    summary=summary,
                    error_type="invalid_execution_context",
                    delivery_status=delivery_preparation_status(job, "blocked"),
                )
                return CronExecutionResult(
                    job.job_id, run.run_id, session_id, "blocked", summary,
                    0, (), (), "invalid_execution_context", summary,
                )

            register_all()
            policy = ToolPolicy(
                ExecutionEnvironment.CRON,
                enabled_toolsets=(
                    frozenset(context.toolsets) if context.toolsets else None
                ),
                unattended=True,
                trusted_context=frozenset({"cron_execution"}),
                max_risk_level=context.max_risk_level,
            )
            resolution = registry.resolve(policy)
            validation_error = validate_cron_capability_grant(
                job,
                grant,
                resolved_tool_names=set(resolution.allowed_tool_names),
                context=context,
            )
            if validation_error is not None:
                summary = "Cron capability authorization is no longer valid. Update the task or request authorization again."
                self._write_terminal_result(
                    conn,
                    run.run_id,
                    status="blocked",
                    summary=summary,
                    error_type="cron_capability_grant_invalid",
                    delivery_status=delivery_preparation_status(job, "blocked"),
                )
                return CronExecutionResult(
                    job.job_id, run.run_id, session_id, "blocked", summary,
                    0, (), (), "cron_capability_grant_invalid", "cron_capability_grant_invalid",
                )
            allowed_tool_names = set(grant["allowed_tool_names"])
            guarded_definitions = tuple(
                definition for definition in resolution.definitions
                if definition.get("function", {}).get("name") in allowed_tool_names
            )
            guard = CronCapabilityGuard(grant)
            context = replace(
                context,
                toolsets=tuple(sorted(resolution.toolsets)),
            )
            backend = get_backend(session_id)
            backend_created = True
            backend.cwd = context.workdir
            if self._lease_fence:
                recovery_context = DurableToolExecutionContext(
                    environment="cron",
                    session_id=session_id,
                    cron_run_id=run.run_id,
                    connection=conn,
                )
                records = list_cron_incomplete_tool_executions(
                    conn,
                    run.run_id,
                    **self._lease_fence,
                )
                ToolExecutionRecoveryService(
                    registry,
                    DurableToolDispatcher(registry, recovery_context),
                    before_recover=lambda: list_cron_incomplete_tool_executions(
                        conn,
                        run.run_id,
                        limit=1,
                        **self._lease_fence,
                    ),
                    cron_execution_context=context,
                    cron_capability_guard=guard,
                    interactive_approval=False,
                ).recover(records)
            if recovery_only:
                summary = (
                    "Cron task was interrupted. Its incomplete tool calls were "
                    "recovered according to their registered safety policy."
                )
                self._write_terminal_result(
                    conn,
                    run.run_id,
                    status="failed",
                    summary=summary,
                    error_type="execution_interrupted",
                    delivery_status=delivery_preparation_status(job, "failed"),
                )
                return CronExecutionResult(
                    job.job_id, run.run_id, session_id, "failed", summary,
                    0, (), (), "execution_interrupted", summary,
                )
            self._write_terminal_result(
                conn,
                run.run_id,
                status="running",
                summary="",
                error_type=None,
            )

            ensure_session(conn, session_id, source="cron")
            add_messages(conn, session_id, [{"role": "user", "content": job.prompt}])
            try:
                preloaded_skills = _load_cron_skills(job)
            except ValueError:
                summary = "A configured Cron skill is unavailable. Update the task or choose an available skill."
                self._write_terminal_result(
                    conn,
                    run.run_id,
                    status="blocked",
                    summary=summary,
                    error_type="cron_skill_unavailable",
                    delivery_status=delivery_preparation_status(job, "blocked"),
                )
                return CronExecutionResult(
                    job.job_id, run.run_id, session_id, "blocked", summary,
                    0, (), (), "cron_skill_unavailable", "cron_skill_unavailable",
                )
            loop = ConversationAgentLoop(
                model=MODEL,
                max_iterations=job.max_agent_iterations,
                tools=list(guarded_definitions),
                system_prompt=build_system_prompt(
                    context.workdir,
                    enabled_toolsets=sorted(
                        toolset for toolset in resolution.toolsets
                        if toolset != "skill_read"
                    ),
                ) + (
                    "\n\n# Cron Execution Context\n"
                    "This is an unattended task. Follow the trusted execution context; "
                    "do not create, modify, or delete Cron tasks.\n"
                    f"Artifact directory: {context.artifact_dir}\n"
                    "If this task must deliver files to the conversation, write them "
                    "under the artifact directory; Gateway will send them as "
                    "attachments when the run finishes (requires "
                    "delivery_policy=\"text_and_files\").\n\n"
                    + ("# Preloaded Skills\n" + preloaded_skills if preloaded_skills else "")
                ),
                registry=registry,
                client=client,
                session_key=session_id,
                conn=conn,
                db_session_id=session_id,
                existing_messages=[],
                max_retries=MAX_RETRIES,
                max_continuations=MAX_CONTINUATIONS,
                compression_threshold=COMPRESSION_THRESHOLD,
                model_kwargs={"max_tokens": MODEL_MAX_OUTPUT_TOKENS},
                fallback_model_kwargs={
                    "max_tokens": FALLBACK_MAX_OUTPUT_TOKENS,
                },
                cancel_checker=context.cancel_checker,
                allowed_tool_names=allowed_tool_names,
                tool_context={
                    "cron_execution_context": context,
                    "cron_capability_guard": guard,
                    "interactive_approval": False,
                    "durable_tool_execution": {
                        "environment": "cron",
                        "session_id": session_id,
                        "cron_run_id": run.run_id,
                        "connection": conn,
                    },
                },
            )
            run_prompt = (
                f"{job.prompt}\n\n"
                "Files to deliver to the conversation must be written under: "
                f"{context.artifact_dir}"
            )
            loop_result = loop.run(run_prompt)
            timed_out = bool(time.monotonic() >= deadline)
            cancelled = bool(
                not timed_out
                and context.cancel_checker
                and context.cancel_checker()
            )
            artifacts = _result_artifacts(loop_result.messages, context.artifact_dir)
            status, error_type, summary, final_error = _terminal_outcome(
                loop_result,
                timed_out=timed_out,
                cancelled=cancelled,
                guard=guard,
                artifact_limit=_artifact_limits_exceeded(context.artifact_dir, grant),
            )
            self._write_terminal_result(
                conn,
                run.run_id,
                status=status,
                summary=summary,
                error_type=error_type,
                artifacts=artifacts,
                delivery_status=delivery_preparation_status(job, status),
            )
            return CronExecutionResult(
                job_id=job.job_id,
                run_id=run.run_id,
                session_id=session_id,
                status=status,
                final_response=summary,
                iterations=loop_result.iterations,
                tools_used=tuple(loop_result.tools_used),
                artifacts=tuple(artifacts),
                tool_batches=loop_result.tool_batches,
                tool_call_count=loop_result.tool_call_count,
                error_type=error_type,
                error=final_error,
                timed_out=error_type == "timeout",
                cancelled=error_type == "cancelled",
                retryable=bool(loop_result.retryable and status == "failed"),
            )
        finally:
            if backend_created:
                cleanup_backend(session_id)
            conn.close()
