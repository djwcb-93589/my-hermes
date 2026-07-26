"""Web API 的隔离响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """对外稳定的错误结构，不包含内部异常信息。"""

    code: str
    message: str


class StatusResponse(BaseModel):
    """服务和可安全读取依赖的状态摘要。"""

    application_name: str
    project_version: str | None = None
    web_status: str
    gateway_status: str | None = None
    database_status: str
    current_time: datetime


class SessionSummary(BaseModel):
    """会话列表的一条脱离持久层的摘要。"""

    conversation_id: str
    preview: str | None = None
    source: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    message_count: int | None = Field(default=None, ge=0)


class SessionListResponse(BaseModel):
    """分页会话列表。"""

    items: list[SessionSummary]
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class MessageDetail(BaseModel):
    """会话中一条已持久化消息的安全表示。"""

    role: str
    content: str
    tool_calls: list[dict[str, object]] | None = None
    tool_call_id: str | None = None
    timestamp: datetime | None = None


class SessionDetailResponse(BaseModel):
    """会话与其消息历史。"""

    conversation_id: str
    source: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    messages: list[MessageDetail]


class CronRunSummary(BaseModel):
    """Cron 任务一次历史运行的非敏感摘要。"""

    run_id: str
    status: str
    scheduled_for: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result_summary: str | None = None


class CronJobSummary(BaseModel):
    """Cron 任务定义的只读摘要。"""

    job_id: str
    name: str
    prompt_preview: str | None = None
    schedule: str
    timezone: str | None = None
    enabled: bool | None = None
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None


class CronJobDetailResponse(CronJobSummary):
    """Cron 任务定义及其公开运行历史。"""

    runs: list[CronRunSummary]


class CronJobListResponse(BaseModel):
    """Cron 任务列表。"""

    items: list[CronJobSummary]


class SkillSummary(BaseModel):
    """Skill 发现结果的非敏感部分。"""

    name: str
    description: str | None = None
    version: str | None = None
    available: bool


class SkillListResponse(BaseModel):
    """Skill 目录。"""

    items: list[SkillSummary]


class ToolsetSummary(BaseModel):
    """ToolRegistry 声明的一个工具集。"""

    name: str
    available: bool
    environments: list[str]


class ToolsetListResponse(BaseModel):
    """工具集目录。"""

    items: list[ToolsetSummary]
    tool_details_available: bool


class CronControlResponse(BaseModel):
    """暂停或恢复 Cron 任务后的最小控制确认。"""

    job_id: str
    action: str
    status: str


class CronRunRequestResponse(BaseModel):
    """已持久化的手动运行请求，不代表任务已开始执行。"""

    job_id: str
    action: str
    status: str
    run_id: str
