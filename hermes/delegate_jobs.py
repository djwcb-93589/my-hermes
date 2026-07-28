"""
后台 delegate job 管理器(进程内,非持久化)。

提供 ``submit`` / ``get_status`` / ``get_result`` / ``cancel`` 接口,
后台 daemon worker 线程执行子任务。所有 job 状态用 ``threading.Lock``
保护;worker 无论成功 / 失败 / 取消 / 异常,都通过 runner 闭包内的
``finally`` 清理 child backend。

第一版:进程内内存存储,**不跨进程,不重启恢复**。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable


MAX_BACKGROUND_DELEGATE_JOBS = 3


@dataclass
class DelegateJob:
    """单个后台 delegate job 的状态记录。"""
    job_id: str
    parent_session_key: str | None
    child_session_key: str
    goal: str
    context: str
    toolsets: list[str]
    # job 生命周期状态:queued / running / completed / failed / cancelled
    status: str = "queued"
    # child AgentLoop 返回的原始状态:completed / max_iterations / tool_error /
    # model_error / invalid_args / cancelled 等。worker 自身异常时为 None
    # 或 "worker_error"。让调用方区分"job 失败因为 child 撞 max_iter"
    # 还是"job 失败因为 child tool_error"。
    child_status: str | None = None
    summary: str = ""
    iterations: int = 0
    tools_used: list[str] = field(default_factory=list)
    tool_batches: int = 0
    tool_call_count: int = 0
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel_requested: bool = False
    # 内部:worker 线程对象(不暴露给查询接口)
    _thread: threading.Thread | None = None
    # 内部:仅在本进程内交给子 Agent 的观察 Hook 依赖，不进入任何查询视图。
    _hook_registry: object | None = None
    _parent_run_id: str | None = None


# 终态集合:worker 跑完后 status 必落在这里之一
TERMINAL_STATUSES = ("completed", "failed", "cancelled")


class DelegateJobManager:
    """进程内后台 delegate job 调度器。线程安全。"""

    def __init__(self, max_jobs: int = MAX_BACKGROUND_DELEGATE_JOBS):
        self._jobs: dict[str, DelegateJob] = {}
        self._lock = threading.Lock()
        self.max_jobs = max_jobs

    # ---------- 查询:两种视图 ----------

    def status_view(self, job_id: str) -> dict | None:
        """轻量状态视图,用于 delegate_status 工具。

        不返 summary(可能很长);只返判断"还在跑 / 是否完成 / 是否失败"
        所需的最小字段集。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return {
                "ok": True,
                "job_id": job.job_id,
                "status": job.status,
                "child_status": job.child_status,
                "cancel_requested": job.cancel_requested,
                "created_at": job.created_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "iterations": job.iterations,
                "tools_used": list(job.tools_used),
                "tool_batches": job.tool_batches,
                "tool_call_count": job.tool_call_count,
                "error": job.error,
            }

    def result_view(self, job_id: str) -> dict | None:
        """结果视图,用于 delegate_result 工具。

        未终态(queued / running)时返 ``ok=False`` + ``error="Job is still running"``,
        ``summary=""``,不阻塞等待。
        终态时返完整 summary / iterations / tools_used / child_session_key / error。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status not in TERMINAL_STATUSES:
                return {
                    "ok": False,
                    "job_id": job.job_id,
                    "status": job.status,           # queued / running
                    "child_status": job.child_status,
                    "summary": "",
                    "error": "Job is still running",
                }
            # 终态:ok 仅当 job 真正 completed
            ok = (job.status == "completed")
            return {
                "ok": ok,
                "job_id": job.job_id,
                "status": job.status,
                "child_status": job.child_status,
                "summary": job.summary,
                "iterations": job.iterations,
                "tools_used": list(job.tools_used),
                "tool_batches": job.tool_batches,
                "tool_call_count": job.tool_call_count,
                "child_session_key": job.child_session_key,
                "error": job.error,
            }

    def is_cancel_requested(self, job_id: str) -> bool:
        """供 runner 闭包构造 cancel_checker 使用。"""
        with self._lock:
            job = self._jobs.get(job_id)
            return bool(job and job.cancel_requested)

    # ---------- 提交 / 取消 ----------

    def submit(
        self,
        *,
        runner_factory: Callable[[DelegateJob], Callable[[], dict]],
        goal: str,
        context: str,
        toolsets: list[str],
        parent_session_key: str | None,
        child_session_key: str,
        hook_registry: object | None = None,
        parent_run_id: str | None = None,
    ) -> dict:
        """提交后台 job。

        ``runner_factory`` 接收刚创建的 ``DelegateJob`` 对象,返回一个
        无参 ``runner()`` 闭包(内部跑 ``run_delegate_child``)。manager
        先创建 job 拿到 ``job_id``,再调 factory 让调用方据此构造
        cancel_checker(查 ``job.cancel_requested``),最后启动 worker。
        """
        with self._lock:
            active = sum(
                1 for j in self._jobs.values()
                if j.status in ("queued", "running")
            )
            if active >= self.max_jobs:
                return {
                    "ok": False,
                    "status": "rejected",
                    "error": "too many background delegate jobs",
                }

            job_id = f"delegate-job-{uuid.uuid4().hex[:12]}"
            job = DelegateJob(
                job_id=job_id,
                parent_session_key=parent_session_key,
                child_session_key=child_session_key,
                goal=goal,
                context=context,
                toolsets=list(toolsets),
                status="queued",
                _hook_registry=hook_registry,
                _parent_run_id=parent_run_id,
            )
            self._jobs[job_id] = job

        # 锁外构造 runner(避免 factory 内若有重操作会阻塞其它 submit)
        runner = runner_factory(job)

        thread = threading.Thread(
            target=self._worker,
            args=(job, runner),
            name=f"delegate-worker-{job_id}",
            daemon=True,
        )
        with self._lock:
            job._thread = thread
        thread.start()

        return {
            "ok": True,
            "status": "submitted",
            "job_id": job_id,
            "child_session_key": child_session_key,
        }

    def cancel(self, job_id: str) -> dict:
        """协作式取消:标记 cancel_requested,worker 在下一轮检查时退出。

        不会强行 kill 线程。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return {
                    "ok": False, "status": "not_found",
                    "error": f"unknown job_id: {job_id}",
                }
            if job.status in ("completed", "failed", "cancelled"):
                return {
                    "ok": True, "status": job.status, "job_id": job_id,
                    "message": "job already finished",
                }
            if job.cancel_requested:
                return {
                    "ok": True, "status": "cancel_requested", "job_id": job_id,
                    "message": "cancel already requested",
                }
            job.cancel_requested = True
            return {
                "ok": True, "status": "cancel_requested", "job_id": job_id,
                "message": "cancel flag set; worker will exit at next checkpoint",
            }

    # ---------- worker ----------

    def _worker(self, job: DelegateJob, runner: Callable[[], dict]) -> None:
        with self._lock:
            job.status = "running"
            job.started_at = time.time()
        try:
            result = runner() or {}
            with self._lock:
                job.summary = result.get("summary", "") or ""
                job.iterations = int(result.get("iterations", 0) or 0)
                job.tools_used = list(result.get("tools_used", []) or [])
                job.tool_batches = int(result.get("tool_batches", 0) or 0)
                job.tool_call_count = int(result.get("tool_call_count", 0) or 0)
                job.error = result.get("error")
                # 保留 child AgentLoop 的原始状态,不粗暴归并
                child_status = result.get("status")
                job.child_status = child_status
                if child_status == "completed":
                    job.status = "completed"
                elif child_status == "cancelled":
                    job.status = "cancelled"
                else:
                    # max_iterations / tool_error / model_error / invalid_args 等
                    # 都归到 job 失败,但 child_status 保留原始原因
                    job.status = "failed"
        except Exception as exc:
            with self._lock:
                job.status = "failed"
                job.child_status = "worker_error"
                job.error = f"worker exception: {exc!r}"
        finally:
            with self._lock:
                job.finished_at = time.time()
                # 任务终态后立即释放运行时 Hook 引用，查询视图从未暴露它们。
                job._hook_registry = None
                job._parent_run_id = None


# ---------------------------------------------------------------------------
# 模块级单例(进程内)
# ---------------------------------------------------------------------------

_manager: DelegateJobManager | None = None
_manager_lock = threading.Lock()


def get_delegate_job_manager() -> DelegateJobManager:
    """获取进程级单例。第一版用内存存储,所以单例即可。"""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = DelegateJobManager()
        return _manager
