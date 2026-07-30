"""Scheduled tasks subsystem: database-backed Cron execution and management."""

import sys


def _metadata_registration_import_active() -> bool:
    """仅在工具目录装配窗口读取已加载的上下文，避免 Cron 包反向导入工具层。"""
    tools_module = sys.modules.get("hermes.tools")
    checker = getattr(
        tools_module,
        "_metadata_registration_import_active",
        None,
    )
    return bool(checker()) if callable(checker) else False


__hermes_metadata_only__ = _metadata_registration_import_active()


if not __hermes_metadata_only__:
    from hermes.cron.store import (
        JobStore,
        get_job_store,
        set_job_store,
    )
    from hermes.cron.job import CronJob, CronRun
    from hermes.cron.executor import (
        CronExecutionContext,
        CronExecutionResult,
        CronExecutor,
    )
    from hermes.cron.parser import parse_schedule

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
else:
    # 能力目录只需加载 cron.tool 的声明，不能由包导入初始化执行器。
    __all__: list[str] = []
