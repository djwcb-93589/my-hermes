"""Web 管理 API 的集中只读适配层。"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version

from hermes.web.schemas import (
    CronJobDetailResponse,
    CronJobListResponse,
    SessionDetailResponse,
    SessionListResponse,
    SkillListResponse,
    SkillSummary,
    StatusResponse,
    ToolsetListResponse,
    ToolsetSummary,
)


class ReadDataUnavailable(Exception):
    """现有公开接口不足以保证无副作用读取时使用的受控异常。"""


class ResourceNotFound(Exception):
    """为以后可安全启用的只读资源保留的受控未找到异常。"""


class ReadService:
    """把现有公开读取能力转换为 Web schema，不保存业务状态。"""

    def get_status(self) -> StatusResponse:
        """返回不触发 Agent、Gateway 或调度器的进程状态。"""
        try:
            project_version: str | None = version("my-hermes")
        except PackageNotFoundError:
            project_version = None

        # 现有数据库接口只提供可能建库或迁移的 init_db，不能用于只读探测。
        # Gateway 的可读运行状态也没有不竞争 lease 的公开接口。
        return StatusResponse(
            application_name="MyHermes",
            project_version=project_version,
            web_status="running",
            gateway_status="unavailable",
            database_status="unavailable",
            current_time=datetime.now(UTC),
        )

    def list_sessions(self, *, limit: int, offset: int) -> SessionListResponse:
        """暂不读取会话，避免通过会写入的数据库初始化入口绕过边界。"""
        del limit, offset
        # list_cli_sessions、session_exists 和 get_session_messages 都要求 sqlite3
        # 连接；当前项目没有只读连接的公开入口，因此不能安全暴露此数据。
        raise ReadDataUnavailable(
            "会话数据缺少无副作用的公开读取连接，暂不可用。"
        )

    def get_session(self, conversation_id: str) -> SessionDetailResponse:
        """暂不读取会话详情，原因与会话列表一致。"""
        del conversation_id
        raise ReadDataUnavailable(
            "会话数据缺少无副作用的公开读取连接，暂不可用。"
        )

    def list_cron_jobs(self) -> CronJobListResponse:
        """暂不读取 Cron，避免建库、迁移或旧 jobs.json 导入。"""
        # Cron 的公开查询函数同样需要数据库连接；JobStore 初始化还会迁移旧
        # jobs.json。两者都不符合本阶段的严格只读边界。
        raise ReadDataUnavailable(
            "Cron 数据缺少无副作用的公开读取连接，暂不可用。"
        )

    def get_cron_job(self, job_id: str) -> CronJobDetailResponse:
        """暂不读取 Cron 任务详情，原因与任务列表一致。"""
        del job_id
        raise ReadDataUnavailable(
            "Cron 数据缺少无副作用的公开读取连接，暂不可用。"
        )

    def list_skills(self) -> SkillListResponse:
        """复用现有 Skill 发现逻辑，只映射非敏感摘要。"""
        try:
            from hermes.tools.skill import discover_skills

            discovered = discover_skills()
        except Exception as exc:
            raise ReadDataUnavailable("Skill 目录当前不可读取。") from exc

        items = [
            SkillSummary(
                name=str(item.get("name", "")),
                description=self._optional_text(item.get("description")),
                version=self._optional_text(item.get("version")),
                available="error" not in item,
            )
            for item in discovered
            if item.get("name")
        ]
        return SkillListResponse(items=items)

    def list_toolsets(self) -> ToolsetListResponse:
        """复用注册表公开查询能力，不调用任何工具处理器。"""
        try:
            from hermes.tools import ExecutionEnvironment, register_all, registry

            # 只装配现有工具声明，以读取目录；不会创建新工具或调用 handler。
            register_all()
            environments_by_toolset: dict[str, set[str]] = {}
            for environment in ExecutionEnvironment:
                for toolset in registry.toolsets_for_environment(environment):
                    environments_by_toolset.setdefault(toolset, set()).add(
                        environment.value
                    )
        except Exception as exc:
            raise ReadDataUnavailable("工具集目录当前不可读取。") from exc

        return ToolsetListResponse(
            items=[
                ToolsetSummary(
                    name=name,
                    available=True,
                    environments=sorted(environments),
                )
                for name, environments in sorted(environments_by_toolset.items())
            ],
            # ToolRegistry 没有公开的逐工具元数据枚举接口；不读取私有字段。
            tool_details_available=False,
        )

    @staticmethod
    def _optional_text(value: object) -> str | None:
        """避免把内部对象的表示直接带入 HTTP 响应。"""
        return value if isinstance(value, str) and value else None
