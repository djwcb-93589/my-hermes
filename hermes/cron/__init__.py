"""Scheduled tasks subsystem: database-backed Cron execution and management."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hermes.cron.executor import (
        CronExecutionContext,
        CronExecutionResult,
        CronExecutor,
    )
    from hermes.cron.job import CronJob, CronRun
    from hermes.cron.parser import parse_schedule
    from hermes.cron.store import JobStore, get_job_store, set_job_store


_EXPORT_MODULES = {
    "JobStore": "hermes.cron.store",
    "get_job_store": "hermes.cron.store",
    "set_job_store": "hermes.cron.store",
    "CronJob": "hermes.cron.job",
    "CronRun": "hermes.cron.job",
    "CronExecutionContext": "hermes.cron.executor",
    "CronExecutionResult": "hermes.cron.executor",
    "CronExecutor": "hermes.cron.executor",
    "parse_schedule": "hermes.cron.parser",
}


__all__ = [
    "JobStore",
    "get_job_store",
    "set_job_store",
    "CronJob",
    "CronRun",
    "CronExecutionContext",
    "CronExecutionResult",
    "CronExecutor",
    "parse_schedule",
]


def __getattr__(name: str) -> object:
    """仅在调用方明确使用公开接口时加载对应的 Cron 领域模块。"""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """让延迟导出的公开接口继续出现在标准模块检查结果中。"""
    return sorted(set(globals()).union(__all__))
