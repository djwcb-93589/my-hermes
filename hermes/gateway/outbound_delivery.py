"""统一的出站文件验证与持久投递创建入口。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from hermes.config import PATH_ACCESS_POLICY, SENSITIVE_FILE_PATTERNS
from hermes.db import (
    create_cron_file_delivery,
    create_cron_run_artifact,
    create_gateway_file_delivery,
    init_db,
)
from hermes.outbound_file import (
    capture_outbound_file_snapshot,
    normalize_display_name,
)


class OutboundDeliveryService:
    """复用同一快照、数据库任务和 runtime fencing 边界的出站服务。"""

    def __init__(self, db_path: str, file_transfer_config: Mapping[str, object]):
        self.db_path = str(db_path)
        self.file_transfer_config = dict(file_transfer_config)

    def capture_file(self, path: object, display_name: object = None) -> dict:
        """每次投递前重新验证普通文件、路径策略、大小与稳定快照。"""
        snapshot = capture_outbound_file_snapshot(
            path,
            path_policy=PATH_ACCESS_POLICY,
            allowed_roots=self.file_transfer_config.get("outbound_allowed_roots"),
            max_file_bytes=self.file_transfer_config.get("max_outbound_file_bytes"),
            database_path=self.db_path,
            sensitive_patterns=SENSITIVE_FILE_PATTERNS,
        )
        snapshot["display_name"] = normalize_display_name(
            display_name,
            fallback=os.path.basename(snapshot["abs_path"]),
        )
        return snapshot

    def create_gateway_file_delivery(
        self,
        delivery: dict,
        *,
        runtime_fence: Mapping[str, object],
    ) -> dict:
        """创建当前 Gateway 会话审批绑定的文件投递。"""
        conn = init_db(self.db_path)
        try:
            return create_gateway_file_delivery(
                conn,
                delivery,
                **dict(runtime_fence),
            )
        finally:
            conn.close()

    def create_cron_artifact_delivery(
        self,
        *,
        artifact: dict,
        delivery: dict,
        runtime_fence: Mapping[str, object],
    ) -> dict:
        """记录 Cron 产物与待上传文件，不依赖 Gateway 入站消息。"""
        conn = init_db(self.db_path)
        try:
            created = create_cron_file_delivery(
                conn,
                delivery,
                **dict(runtime_fence),
            )
            create_cron_run_artifact(conn, {
                **artifact,
                "delivery_id": created["id"],
                "delivery_status": created["status"],
            })
            return created
        finally:
            conn.close()

    @staticmethod
    def require_artifact_path(path: str, artifact_dir: str) -> None:
        """默认只允许投递本次 Cron 运行隔离目录内生成的文件。"""
        candidate = Path(path).resolve()
        root = Path(artifact_dir).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("Cron artifact path is outside this run artifact directory") from exc
