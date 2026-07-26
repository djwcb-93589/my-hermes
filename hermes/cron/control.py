"""不依赖 Web 的 Cron 生命周期控制编排。"""

from __future__ import annotations

import sqlite3
from typing import Any

from hermes.cron.parser import parse_schedule
from hermes.persistence.cron import (
    create_manual_cron_run,
    get_cron_job,
    get_cron_run,
    list_cron_runs,
    resume_cron_job,
    set_cron_job_paused,
)
from hermes.persistence.database import DBError


class CronControlNotFound(Exception):
    """请求的任务不存在或已删除。"""


class CronControlConflict(Exception):
    """当前任务状态不允许创建新的手动运行。"""


class CronControlDataUnavailable(Exception):
    """任务记录无法安全解释为可控制的调度状态。"""


def _job_or_not_found(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    job = get_cron_job(conn, job_id)
    if job is None or job.get("deleted_at") is not None:
        raise CronControlNotFound()
    return job


def pause_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    """暂停未来调度；已暂停任务不写入数据库。"""
    job = _job_or_not_found(conn, job_id)
    if job.get("paused") is True:
        return job
    return set_cron_job_paused(conn, job_id, True)


def resume_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """恢复未来调度，不补建暂停期间错过的运行记录。"""
    job = _job_or_not_found(conn, job_id)
    if job.get("paused") is False:
        return job
    if job.get("paused") is not True:
        raise CronControlDataUnavailable()
    try:
        schedule_expr = str(job["schedule_expr"])
        timezone_name = str(job["timezone"])
        schedule_type = job["schedule_type"]
        if schedule_type not in {"one_shot", "interval", "cron"}:
            raise ValueError("schedule type is invalid")
        next_run_at, parsed_one_shot = parse_schedule(
            schedule_expr,
            timezone_name=timezone_name,
            now=now,
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise CronControlDataUnavailable() from exc
    if (schedule_type == "one_shot") != parsed_one_shot:
        raise CronControlDataUnavailable()
    return resume_cron_job(conn, job_id, next_run_at)


def request_manual_run(
    conn: sqlite3.Connection,
    job_id: str,
    run_id: str,
) -> dict[str, Any]:
    """持久化手动运行请求，不领取或执行任务。"""
    job = _job_or_not_found(conn, job_id)
    existing = get_cron_run(conn, run_id)
    if existing is not None:
        if existing.get("job_id") != job_id:
            raise CronControlConflict()
        return existing
    if job.get("overlap_policy") == "skip" and any(
        item.get("status") in {"claimed", "running"}
        for item in list_cron_runs(conn, job_id)
    ):
        raise CronControlConflict()
    try:
        return create_manual_cron_run(conn, job_id, run_id)
    except DBError as exc:
        existing = get_cron_run(conn, run_id)
        if existing is not None:
            if existing.get("job_id") != job_id:
                raise CronControlConflict() from exc
            return existing
        if job.get("overlap_policy") == "skip" and any(
            item.get("status") in {"claimed", "running"}
            for item in list_cron_runs(conn, job_id)
        ):
            raise CronControlConflict() from exc
        raise CronControlDataUnavailable() from exc
