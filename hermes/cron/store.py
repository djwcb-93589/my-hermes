"""Cron 任务的 SQLite 仓储与旧 jobs.json 兼容导入。"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from hermes.config import DB_PATH, HERMES_HOME
from hermes.cron.job import CronJob
from hermes.cron.parser import parse_schedule
from hermes.db import (
    create_cron_job,
    delete_cron_job,
    get_cron_job,
    init_db,
    list_cron_jobs,
    list_due_cron_jobs,
    migrate_legacy_cron_jobs_json,
    pause_cron_one_shot_job,
    set_cron_job_paused,
    update_cron_job_definition,
    update_cron_job_schedule_state,
)


class JobStore:
    """面向旧调用点的 SQLite Cron 任务仓储。"""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        legacy_path: str | Path | None = None,
    ):
        """打开正式数据库，并在首次使用时幂等导入旧 jobs.json。"""
        requested_path = Path(db_path) if db_path is not None else None
        if requested_path is not None and requested_path.suffix.lower() == ".json":
            # 兼容旧调用 ``JobStore(path_to_jobs_json)``：正式状态放在同目录
            # 的 SQLite 文件，传入路径仅作为待导入的旧数据来源。
            if legacy_path is None:
                legacy_path = requested_path
            requested_path = requested_path.with_suffix(".db")
        self._db_path = requested_path or Path(DB_PATH)
        self._legacy_path = (
            Path(legacy_path)
            if legacy_path is not None
            else HERMES_HOME / "jobs.json"
        )
        self._lock = threading.RLock()
        self._migrate_legacy_jobs()

    def _open(self):
        """每次操作使用独立连接，避免 scheduler 线程跨线程复用连接。"""
        return init_db(str(self._db_path))

    def _migrate_legacy_jobs(self) -> None:
        """导入失败直接向启动方报告，不能把损坏旧任务静默丢弃。"""
        with self._lock:
            conn = self._open()
            try:
                migrate_legacy_cron_jobs_json(conn, self._legacy_path)
            finally:
                conn.close()

    def add(self, job: CronJob) -> CronJob:
        """创建任务定义；相同 job ID 不会覆盖既有状态。"""
        with self._lock:
            conn = self._open()
            try:
                record = create_cron_job(conn, job.to_record())
            finally:
                conn.close()
        return CronJob.from_record(record)

    def get(self, job_id: str) -> CronJob | None:
        """按 ID 读取任务定义。"""
        with self._lock:
            conn = self._open()
            try:
                record = get_cron_job(conn, job_id)
            finally:
                conn.close()
        return CronJob.from_record(record) if record is not None else None

    def remove(self, job_id: str) -> bool:
        """删除无运行历史的任务；有历史时数据库层明确拒绝。"""
        with self._lock:
            conn = self._open()
            try:
                return delete_cron_job(conn, job_id)
            finally:
                conn.close()

    def list_all(self) -> list[CronJob]:
        """读取全部任务定义，包括暂停任务。"""
        with self._lock:
            conn = self._open()
            try:
                records = list_cron_jobs(conn)
            finally:
                conn.close()
        return [CronJob.from_record(record) for record in records]

    def get_due(self) -> list[CronJob]:
        """读取当前到期且未暂停的任务定义。"""
        with self._lock:
            conn = self._open()
            try:
                records = list_due_cron_jobs(conn)
            finally:
                conn.close()
        return [CronJob.from_record(record) for record in records]

    def update(self, job_id: str, changes: dict) -> CronJob:
        """更新任务定义并由数据库递增版本。"""
        with self._lock:
            conn = self._open()
            try:
                record = update_cron_job_definition(conn, job_id, changes)
            finally:
                conn.close()
        return CronJob.from_record(record)

    def set_paused(self, job_id: str, paused: bool) -> CronJob:
        """切换任务是否参与后续调度。"""
        with self._lock:
            conn = self._open()
            try:
                record = set_cron_job_paused(conn, job_id, paused)
            finally:
                conn.close()
        return CronJob.from_record(record)

    def advance(self, job: CronJob) -> CronJob:
        """保留旧 scheduler 接口，但一次性任务只暂停而不删除。"""
        if job.one_shot:
            with self._lock:
                conn = self._open()
                try:
                    record = pause_cron_one_shot_job(conn, job.job_id)
                finally:
                    conn.close()
            return CronJob.from_record(record)

        next_fire, _ = parse_schedule(job.schedule)
        with self._lock:
            conn = self._open()
            try:
                record = update_cron_job_schedule_state(
                    conn,
                    job.job_id,
                    next_run_at=next_fire,
                )
            finally:
                conn.close()
        return CronJob.from_record(record)


_job_store: Optional[JobStore] = None


def get_job_store() -> JobStore:
    """返回进程内共享的 SQLite 仓储。"""
    global _job_store
    if _job_store is None:
        _job_store = JobStore()
    return _job_store


def set_job_store(store: Optional[JobStore]) -> None:
    """保留外部调用替换仓储实例的兼容入口。"""
    global _job_store
    _job_store = store
