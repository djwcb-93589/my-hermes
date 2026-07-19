"""Cron 系统管理产物目录的唯一路径规则。"""

from __future__ import annotations

from pathlib import Path

from hermes.config import HERMES_HOME, _config


def cron_artifact_base_dir() -> Path:
    """返回由系统配置决定的产物基目录，不接受任务或模型提供的路径。"""
    gateway = _config.get("gateway", {})
    file_transfer = (
        gateway.get("file_transfer", {})
        if isinstance(gateway, dict)
        else {}
    )
    configured = (
        file_transfer.get("download_dir", "cache/files")
        if isinstance(file_transfer, dict)
        else "cache/files"
    )
    root = Path(str(configured)).expanduser()
    if not root.is_absolute():
        root = Path(HERMES_HOME) / root
    return root.resolve(strict=False) / "cron-artifacts"


def cron_job_artifact_root(job_id: str) -> Path:
    """返回一项任务专属的产物根目录。"""
    return cron_artifact_base_dir() / str(job_id)


def cron_run_artifact_dir(job_id: str, run_id: str) -> Path:
    """返回一次运行专属的产物目录。"""
    return cron_job_artifact_root(job_id) / str(run_id)
