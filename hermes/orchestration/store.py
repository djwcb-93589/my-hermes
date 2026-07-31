"""编排领域使用的持久化端口。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from hermes.orchestration.models import (
    TaskClaim,
    TaskRecord,
    TaskRunRecord,
    TaskStatus,
    WorkflowCreateSpec,
    WorkflowRecord,
)


class OrchestrationStore(Protocol):
    """由持久化 Adapter 原子实现的任务状态操作集合。"""

    def create_workflow(self, spec: WorkflowCreateSpec) -> WorkflowRecord:
        """在一个事务中创建 Workflow、Task 与依赖边。"""

    def get_workflow(self, workflow_id: str) -> WorkflowRecord | None:
        """读取 Workflow；不存在时返回 None。"""

    def get_task(self, task_id: str) -> TaskRecord | None:
        """读取 Task；不存在时返回 None。"""

    def list_workflow_tasks(
        self,
        workflow_id: str,
        *,
        statuses: tuple[TaskStatus, ...] | None = None,
    ) -> tuple[TaskRecord, ...]:
        """稳定列出一个 Workflow 的 Task。"""

    def list_task_runs(
        self,
        task_id: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[TaskRunRecord, ...]:
        """按最新 attempt 优先稳定列出 Run。"""

    def claim_ready_tasks(
        self,
        *,
        worker_id: str,
        limit: int,
        lease_seconds: float,
    ) -> tuple[TaskClaim, ...]:
        """原子领取 ready Task，并为每个 Task 创建 Run。"""

    def renew_task_claim(
        self,
        *,
        task_id: str,
        claim_token: str,
        lease_seconds: float,
    ) -> TaskClaim:
        """续租当前 claim，并更新对应 Run 心跳。"""

    def mark_task_run_started(
        self,
        *,
        task_id: str,
        claim_token: str,
        session_key: str | None = None,
    ) -> TaskRunRecord:
        """把当前 Run 从 claimed 原子推进到 running。"""

    def complete_task(
        self,
        *,
        task_id: str,
        claim_token: str,
        result_summary: str | None,
        result_metadata: Mapping[str, object] | None,
    ) -> TaskRecord:
        """原子完成 Task、Run，并推进满足依赖的下游。"""

    def fail_task(
        self,
        *,
        task_id: str,
        claim_token: str,
        error_type: str,
        error_message: str,
        retryable: bool,
    ) -> TaskRecord:
        """原子记录失败，并决定 retry 或 Workflow 终态失败。"""

    def block_task(
        self,
        *,
        task_id: str,
        claim_token: str,
        blocked_reason: str,
    ) -> TaskRecord:
        """原子结束当前 Run，并把 Task 置为 blocked。"""

    def unblock_task(self, *, task_id: str) -> TaskRecord:
        """根据当前依赖状态把 blocked Task 恢复为 ready 或 todo。"""

    def cancel_task(self, *, task_id: str) -> TaskRecord:
        """取消 Task 及其尚未执行且无法继续的后代。"""

    def cancel_workflow(self, *, workflow_id: str) -> WorkflowRecord:
        """在一个事务中取消 Workflow 的全部非终态 Task 与活跃 Run。"""

    def recover_expired_claims(
        self,
        *,
        limit: int,
    ) -> tuple[TaskRecord, ...]:
        """原子回收过期 claim，并 retry 或执行终态失败规则。"""


__all__ = ["OrchestrationStore"]
