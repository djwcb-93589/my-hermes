"""Gateway runtime lease 约束下的异步 Cron 调度器。"""

from __future__ import annotations

import asyncio
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from hermes.cron.executor import CronExecutor
from hermes.cron.job import CronJob, CronRun
from hermes.cron.parser import next_schedule_fire
from hermes.db import (
    advance_due_cron_job_without_run,
    claim_manual_cron_run,
    claim_due_cron_job_run,
    create_cron_retry_run,
    list_due_cron_jobs,
    list_unclaimed_manual_cron_runs,
    transition_cron_run,
)
from hermes.gateway.persistence import GatewayPersistence


@dataclass
class _RunningCron:
    """当前 Gateway 已领取且尚未结束的一次 Cron 执行。"""

    run_id: str
    task: asyncio.Task
    cancel_event: threading.Event = field(repr=False)


class GatewayCronScheduler:
    """只在本 Gateway runtime lease 有效时领取和执行 Cron 任务。"""

    def __init__(
        self,
        persistence: GatewayPersistence,
        db_path: str,
        *,
        llm_semaphore: asyncio.Semaphore,
        lease_fence_provider: Callable[[], dict | None],
        lease_is_valid: Callable[[], bool],
        poll_seconds: float = 5.0,
        max_concurrent: int = 1,
        misfire_grace_seconds: float = 60.0,
        artifact_root: str | None = None,
        execution_finished: Callable[[CronJob, CronRun, object, dict], Awaitable[None]] | None = None,
    ):
        if poll_seconds <= 0:
            raise ValueError("Gateway Cron poll_seconds must be positive")
        if max_concurrent <= 0:
            raise ValueError("Gateway Cron max_concurrent must be positive")
        if misfire_grace_seconds < 0:
            raise ValueError("Gateway Cron misfire_grace_seconds must be non-negative")
        self._persistence = persistence
        self._db_path = db_path
        self._llm_semaphore = llm_semaphore
        self._lease_fence_provider = lease_fence_provider
        self._lease_is_valid = lease_is_valid
        self._poll_seconds = float(poll_seconds)
        self._misfire_grace_seconds = float(misfire_grace_seconds)
        self._max_concurrent = int(max_concurrent)
        self._artifact_root = artifact_root
        self._execution_finished = execution_finished
        self._cron_semaphore = asyncio.Semaphore(self._max_concurrent)
        self._dispatch_task: asyncio.Task | None = None
        self._running: dict[str, _RunningCron] = {}
        self._tick_lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self._accepting = False

    async def _schedule_retry_if_eligible(
        self,
        job: CronJob,
        run: CronRun,
        result: object,
    ) -> None:
        """仅为明确的暂时性 Agent 故障追加重试运行，不改变正常计划窗口。"""
        if str(getattr(result, "status", "")) != "failed":
            return
        if not bool(getattr(result, "retryable", False)):
            return
        policy = dict(job.retry_policy or {})
        max_attempts = int(policy.get("max_attempts", 1))
        error_type = str(getattr(result, "error_type", "") or "")
        allowed = set(policy.get("retryable_error_types", ()))
        transient = {
            "model_service_unavailable", "model_timeout", "infrastructure_error",
            "model_error", "network_or_timeout", "rate_limit", "server_error",
        }
        if (max_attempts <= int(run.attempt_number)
                or error_type not in transient
                or error_type not in allowed):
            return
        base = max(0.0, float(policy.get("base_delay_seconds", 5.0)))
        maximum = max(base, float(policy.get("max_delay_seconds", 300.0)))
        jitter_ratio = min(1.0, max(0.0, float(policy.get("jitter_ratio", 0.2))))
        delay = min(maximum, base * (2 ** max(0, int(run.attempt_number) - 1)))
        delay += random.uniform(-delay * jitter_ratio, delay * jitter_ratio)
        fence = self._lease_fence_provider()
        if fence is None or not self._lease_is_valid():
            return
        try:
            await self._persistence.call(
                create_cron_retry_run,
                run.run_id,
                uuid.uuid4().hex,
                time.time() + max(0.0, delay),
                **fence,
            )
            self._wakeup.set()
        except Exception as exc:
            print("  [gateway:cron] retry scheduling failed: " f"{type(exc).__name__}")

    def start(self) -> None:
        """lease 有效且 Gateway 已进入 running 后启动唯一调度循环。"""
        if not self._lease_is_valid():
            return
        self._accepting = True
        if self._dispatch_task is None or self._dispatch_task.done():
            self._dispatch_task = asyncio.create_task(
                self._dispatch_loop(),
                name="gateway-cron-dispatcher",
            )
        self._wakeup.set()

    def revoke(self) -> None:
        """立即停止新 claim，并向当前执行传播协作式取消。"""
        self._accepting = False
        self._wakeup.set()
        for running in tuple(self._running.values()):
            running.cancel_event.set()

    async def stop(self) -> None:
        """统一关闭调度循环并等待已领取运行安全收敛。"""
        self.revoke()
        dispatcher = self._dispatch_task
        if dispatcher is not None and not dispatcher.done():
            await asyncio.gather(dispatcher, return_exceptions=True)
        self._dispatch_task = None
        tasks = [item.task for item in tuple(self._running.values())]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._running.clear()

    async def tick(self) -> None:
        """供调度循环和手工 tick 共用的一次无重入扫描。"""
        if not self._accepting or not self._lease_is_valid():
            return
        if self._tick_lock.locked():
            return
        async with self._tick_lock:
            if not self._accepting or not self._lease_is_valid():
                return
            fence = self._lease_fence_provider()
            if fence is None:
                return
            manual_runs = await self._persistence.call(
                list_unclaimed_manual_cron_runs,
                limit=max(1, self._max_concurrent - len(self._running)),
            )
            for pending_run in manual_runs:
                if not self._accepting or not self._lease_is_valid():
                    return
                if len(self._running) >= self._max_concurrent:
                    return
                await self._claim_manual(pending_run, fence)
            due_jobs = await self._persistence.call(list_due_cron_jobs)
            for record in due_jobs:
                if not self._accepting or not self._lease_is_valid():
                    return
                if len(self._running) >= self._max_concurrent:
                    return
                await self._claim_one(record, fence)

    async def _claim_manual(self, pending_run: dict, fence: dict) -> None:
        """让手工请求走与到期任务相同的 lease、并发和执行器边界。"""
        claimed = await self._persistence.call(
            claim_manual_cron_run,
            str(pending_run["run_id"]),
            f"gateway-cron-manual-{uuid.uuid4().hex}",
            **fence,
        )
        if claimed.get("outcome") != "claimed":
            return
        job = CronJob.from_record(claimed["job"])
        run = CronRun.from_record(claimed["run"])
        cancel_event = threading.Event()
        if not self._accepting or not self._lease_is_valid():
            cancel_event.set()
        task = asyncio.create_task(
            self._execute_claimed(job, run, cancel_event, fence),
            name=f"gateway-cron-{run.run_id}",
        )
        self._running[run.run_id] = _RunningCron(run.run_id, task, cancel_event)
        task.add_done_callback(
            lambda completed, run_id=run.run_id: self._running.pop(run_id, None)
        )

    def _next_schedule_state(
        self,
        job: CronJob,
        *,
        now: float,
    ) -> tuple[float | None, bool]:
        """按错过策略计算本次领取后任务应保存的下一计划窗口。"""
        if job.one_shot:
            return None, True
        after = job.next_fire if job.misfire_policy == "catch_up" else now
        next_fire = next_schedule_fire(
            job.schedule,
            float(after),
            timezone_name=job.timezone,
        )
        return next_fire, False

    async def _claim_one(self, record: dict, fence: dict) -> None:
        """以数据库 claim 原子决定执行、排队或跳过，不在内存预占资格。"""
        job = CronJob.from_record(record)
        if job.next_fire is None:
            return
        now = time.time()
        next_fire, pause_after_claim = self._next_schedule_state(job, now=now)
        missed = now - float(job.next_fire) > self._misfire_grace_seconds
        if missed and job.misfire_policy == "skip":
            await self._persistence.call(
                advance_due_cron_job_without_run,
                job.job_id,
                job.next_fire,
                next_fire,
                pause_after_advance=pause_after_claim,
                **fence,
            )
            return
        run_id = uuid.uuid4().hex
        execution_instance_id = f"gateway-cron-{uuid.uuid4().hex}"
        claimed = await self._persistence.call(
            claim_due_cron_job_run,
            job.job_id,
            job.next_fire,
            run_id,
            execution_instance_id,
            next_fire,
            pause_after_claim=pause_after_claim,
            **fence,
        )
        if claimed.get("outcome") != "claimed":
            return
        claimed_job = CronJob.from_record(claimed["job"])
        claimed_run = CronRun.from_record(claimed["run"])
        cancel_event = threading.Event()
        if not self._accepting or not self._lease_is_valid():
            cancel_event.set()
        task = asyncio.create_task(
            self._execute_claimed(claimed_job, claimed_run, cancel_event, fence),
            name=f"gateway-cron-{claimed_run.run_id}",
        )
        self._running[claimed_run.run_id] = _RunningCron(
            claimed_run.run_id,
            task,
            cancel_event,
        )
        task.add_done_callback(
            lambda completed, run_id=claimed_run.run_id: self._running.pop(
                run_id,
                None,
            )
        )

    async def _execute_claimed(
        self,
        job: CronJob,
        run: CronRun,
        cancel_event: threading.Event,
        fence: dict,
    ) -> None:
        """在 Cron 和 Gateway 全局模型两个并发边界内执行已领取运行。"""
        try:
            async with self._cron_semaphore:
                async with self._llm_semaphore:
                    result = await asyncio.to_thread(
                        CronExecutor(
                            self._db_path,
                            cancel_checker=cancel_event.is_set,
                            artifact_root=self._artifact_root,
                            **fence,
                        ).execute,
                        job,
                        run,
                    )
                if (
                    self._execution_finished is not None
                    and self._lease_is_valid()
                ):
                    try:
                        await self._execution_finished(job, run, result, fence)
                    except Exception as exc:
                        print(
                            "  [gateway:cron] delivery preparation failed: "
                            f"{type(exc).__name__}"
                        )
                await self._schedule_retry_if_eligible(job, run, result)
        except asyncio.CancelledError:
            cancel_event.set()
            raise
        except Exception as exc:
            if not self._lease_is_valid():
                return
            try:
                await self._persistence.call(
                    transition_cron_run,
                    run.run_id,
                    "failed",
                    error_type="cron_executor_error",
                    result_summary=f"Cron executor failed: {type(exc).__name__}",
                    **fence,
                )
            except Exception:
                # lease 已失效或状态已被其它合法执行者推进时，不覆盖事实。
                return

    async def _dispatch_loop(self) -> None:
        """周期扫描；每轮只执行一次 tick，异常不终止 Gateway 主生命周期。"""
        try:
            while self._accepting and self._lease_is_valid():
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(
                        "  [gateway:cron] scheduler tick failed: "
                        f"{type(exc).__name__}"
                    )
                if not self._accepting or not self._lease_is_valid():
                    return
                self._wakeup.clear()
                try:
                    await asyncio.wait_for(
                        self._wakeup.wait(),
                        timeout=self._poll_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
