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
    client,
)
from hermes.conversation import ConversationAgentLoop
from hermes.cron.job import CronJob, CronRun
from hermes.db import (
    DBError,
    add_messages,
    ensure_session,
    get_cron_run,
    init_db,
    transition_cron_run,
)
from hermes.prompt import build_system_prompt
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
    error_type: str | None = None
    error: str | None = None
    timed_out: bool = False
    cancelled: bool = False


def _cron_session_id(job_id: str, run_id: str) -> str:
    """生成与单次运行一一对应的稳定会话标识。"""
    return f"cron:{job_id}:{run_id}"


def _result_artifacts(messages: list[dict], artifact_dir: str) -> list[dict]:
    """从已有工具结果提取轻量产物描述，不执行投递或复制文件。"""
    artifacts: list[dict] = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            payload = json.loads(str(message.get("content", "")))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("path"), str):
            continue
        try:
            Path(payload["path"]).resolve().relative_to(Path(artifact_dir).resolve())
        except (OSError, ValueError):
            continue
        artifacts.append({
            "kind": "tool_file_reference",
            "tool_call_id": str(message.get("tool_call_id", "")),
            "path": payload["path"],
            "description": "A tool result referenced this file; delivery was not requested.",
        })
    return artifacts


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
        artifact_root: str | Path | None = None,
    ):
        self._db_path = db_path or DB_PATH
        self._external_cancel_checker = cancel_checker
        self._artifact_root = Path(artifact_root or Path.cwd() / "cache" / "files")
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
    ) -> CronExecutionContext:
        """规范化受控运行上下文，拒绝不存在的工作目录。"""
        workdir = Path(job.workdir or os.getcwd()).expanduser().resolve()
        if not workdir.is_dir():
            raise ValueError("Cron workdir does not exist or is not a directory")
        artifact_dir = (
            self._artifact_root / "cron-artifacts" / job.job_id / run.run_id
        ).resolve()
        artifact_dir.mkdir(parents=True, exist_ok=True)

        raw_grant = job.capability_grant or {}
        raw_risk = raw_grant.get("max_risk_level", ToolRiskLevel.HIGH.value)
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
            file_access_scope=(str(workdir), str(artifact_dir)),
            max_risk_level=max_risk_level,
            timeout_seconds=float(job.execution_timeout_seconds),
            delivery_target=MappingProxyType(dict(job.delivery_config)),
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
    ) -> None:
        """将执行结论原子写回独立运行事实。"""
        transition_cron_run(
            conn,
            run_id,
            status,
            error_type=error_type,
            result_summary=summary[:8000],
            artifacts=artifacts or [],
            **self._lease_fence,
        )

    def execute(self, job: CronJob, run: CronRun) -> CronExecutionResult:
        """执行已领取运行；调用方负责领取，执行器负责运行到终态。"""
        session_id = _cron_session_id(job.job_id, run.run_id)
        conn = init_db(self._db_path)
        backend_created = False
        try:
            stored_run = get_cron_run(conn, run.run_id)
            if stored_run is None or stored_run["job_id"] != job.job_id:
                raise DBError("Cron run does not belong to the supplied job")
            if stored_run["status"] != "claimed":
                raise DBError("Cron executor requires a claimed run")
            if self._lease_fence and (
                stored_run["claim_lease_name"] != self._lease_fence["lease_name"]
                or stored_run["claim_instance_id"] != self._lease_fence["instance_id"]
                or stored_run["claim_epoch"] != self._lease_fence["lease_epoch"]
            ):
                raise DBError("Cron run claim does not match executor lease")

            if job.approval_status not in {"not_required", "granted"}:
                summary = "Cron capability approval is not granted."
                self._write_terminal_result(
                    conn,
                    run.run_id,
                    status="blocked",
                    summary=summary,
                    error_type="approval_not_granted",
                )
                return CronExecutionResult(
                    job.job_id, run.run_id, session_id, "blocked", summary,
                    0, (), (), "approval_not_granted", summary,
                )

            self._write_terminal_result(
                conn,
                run.run_id,
                status="running",
                summary="",
                error_type=None,
            )
            deadline = time.monotonic() + float(job.execution_timeout_seconds)
            try:
                context = self._build_context(job, run, deadline=deadline)
            except ValueError as exc:
                summary = str(exc)
                self._write_terminal_result(
                    conn,
                    run.run_id,
                    status="blocked",
                    summary=summary,
                    error_type="invalid_execution_context",
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
            context = replace(
                context,
                toolsets=tuple(sorted(resolution.toolsets)),
            )
            backend = get_backend(session_id)
            backend_created = True
            backend.cwd = context.workdir

            ensure_session(conn, session_id, source="cron")
            add_messages(conn, session_id, [{"role": "user", "content": job.prompt}])
            loop = ConversationAgentLoop(
                model=MODEL,
                max_iterations=job.max_agent_iterations,
                tools=list(resolution.definitions),
                system_prompt=build_system_prompt(
                    context.workdir,
                    enabled_toolsets=sorted(resolution.toolsets),
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
                model_kwargs=None,
                cancel_checker=context.cancel_checker,
                allowed_tool_names=set(resolution.allowed_tool_names),
                tool_context={
                    "cron_execution_context": context,
                    "interactive_approval": False,
                },
            )
            run_prompt = (
                f"{job.prompt}\n\n"
                "For files intended as Cron artifacts, write them only to: "
                f"{context.artifact_dir}"
            )
            loop_result = loop.run(run_prompt)
            timed_out = bool(time.monotonic() >= deadline)
            cancelled = bool(context.cancel_checker and context.cancel_checker())
            artifacts = _result_artifacts(loop_result.messages, context.artifact_dir)
            if loop_result.ok:
                status = "completed"
                error_type = None
            elif timed_out:
                status = "cancelled"
                error_type = "timeout"
            elif loop_result.status == "cancelled" or cancelled:
                status = "cancelled"
                error_type = loop_result.error_type or "cancelled"
            else:
                status = "failed"
                error_type = loop_result.error_type or loop_result.status
            summary = loop_result.summary or loop_result.error or ""
            self._write_terminal_result(
                conn,
                run.run_id,
                status=status,
                summary=summary,
                error_type=error_type,
                artifacts=artifacts,
            )
            return CronExecutionResult(
                job_id=job.job_id,
                run_id=run.run_id,
                session_id=session_id,
                status=status,
                final_response=loop_result.summary,
                iterations=loop_result.iterations,
                tools_used=tuple(loop_result.tools_used),
                artifacts=tuple(artifacts),
                error_type=error_type,
                error=loop_result.error,
                timed_out=timed_out,
                cancelled=cancelled,
            )
        finally:
            if backend_created:
                cleanup_backend(session_id)
            conn.close()
