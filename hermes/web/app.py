"""Dashboard FastAPI 应用装配与应用级认证边界。"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from hermes.config_environment import ConfigEnvironment
from hermes.config_values import hermes_home
from hermes.configuration import (
    DEFAULT_CONFIG_FIELD_REGISTRY,
    ConfigManagementError,
    ConfigReadService,
    ConfigWriteService,
)
from hermes.configuration.yaml_repository import YamlConfigRepository
from hermes.observability.contracts import (
    CapabilityDescriptor,
    ToolsetDescriptor,
)
from hermes.persistence.database_diagnostics import (
    SQLiteDatabaseDiagnosticsRepository,
)
from hermes.persistence.monitoring import (
    SQLiteObservationReadRepository,
    SQLiteToolExecutionReadRepository,
)
from hermes.persistence.monitoring_aggregation import (
    SQLiteMonitoringAggregationRepository,
)
from hermes.persistence.runtime import SQLiteRuntimeStatusReadRepository
from hermes.tool_declarations.catalog import build_toolset_catalog_snapshot
from hermes.web.config import DashboardConfig, validate_dashboard_config
from hermes.web.control_service import CronControlService
from hermes.web.database_diagnostics_service import (
    DatabaseDiagnosticsService,
)
from hermes.web.monitoring_aggregation_service import (
    MonitoringAggregationService,
)
from hermes.web.monitoring_service import MonitoringReadService
from hermes.web.read_context import DashboardReadContext, ReadInvalidRequest
from hermes.web.read_service import (
    CatalogReadService,
    CronReadService,
    HealthReadService,
    ReadDataUnavailable,
    ReadService,
    ResourceNotFound,
    SessionReadService,
)
from hermes.web.redaction import DashboardRedactor
from hermes.web.runtime_status_service import RuntimeStatusReadService
from hermes.web.routes import (
    catalog,
    config as config_routes,
    cron,
    database_diagnostics,
    monitoring,
    monitoring_aggregation,
    runtime,
    sessions,
    status,
)
from hermes.web.schemas import ErrorResponse
from hermes.web.security import (
    TOKEN_HEADER,
    ControlAuthenticator,
    ControlBadRequest,
    ControlConflict,
    ControlForbidden,
    ControlNotFound,
    ControlUnavailable,
    DashboardAccessPolicy,
    DashboardPermission,
    cors_origin_regex,
)


_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_CONFIG_ERROR_STATUS = {
    "config_invalid": 400,
    "config_field_unknown": 400,
    "config_field_read_only": 400,
    "config_value_invalid": 400,
    "config_not_found": 404,
    "config_conflict": 409,
    "config_shadowed": 409,
    "config_unavailable": 503,
    "config_write_failed": 503,
}
_CONFIG_ERROR_MESSAGES = {
    "config_invalid": "配置文件未通过安全校验。",
    "config_field_unknown": "配置字段不受支持。",
    "config_field_read_only": "配置字段不允许修改。",
    "config_value_invalid": "配置修改请求无效。",
    "config_not_found": "配置文件不存在。",
    "config_conflict": "配置已经变更，请重新读取后再提交。",
    "config_shadowed": "配置字段当前由环境覆盖，不能通过文件修改。",
    "config_unavailable": "配置当前不可用。",
    "config_write_failed": "配置无法安全写入。",
}


def build_dashboard_app(config: DashboardConfig) -> FastAPI:
    """使用已校验配置装配唯一的正式 Dashboard 应用实例。"""
    validate_dashboard_config(config)
    authenticator = ControlAuthenticator.from_digest(config.control_token_digest)
    access_policy = DashboardAccessPolicy(
        authenticator,
        read_auth_required=config.auth_required,
        bound_host=config.host,
    )
    read_context = DashboardReadContext(config.db_path)
    redactor = DashboardRedactor()
    capabilities, toolsets = _build_toolset_catalog_snapshot()
    health_read_service = HealthReadService(read_context, redactor)
    session_read_service = SessionReadService(read_context, redactor)
    cron_read_service = CronReadService(read_context, redactor)
    catalog_read_service = CatalogReadService(
        read_context,
        redactor,
        capabilities=capabilities,
        toolsets=toolsets,
    )
    monitoring_read_service = (
        MonitoringReadService(
            SQLiteObservationReadRepository(config.db_path),
            SQLiteToolExecutionReadRepository(config.db_path),
        )
        if config.db_path is not None
        else None
    )
    monitoring_aggregation_service = (
        MonitoringAggregationService(
            SQLiteMonitoringAggregationRepository(config.db_path),
        )
        if config.db_path is not None
        else None
    )
    database_diagnostics_service = (
        DatabaseDiagnosticsService(
            SQLiteDatabaseDiagnosticsRepository(config.db_path),
        )
        if config.db_path is not None
        else None
    )
    runtime_status_read_service = (
        RuntimeStatusReadService(
            SQLiteRuntimeStatusReadRepository(config.db_path),
        )
        if config.db_path is not None
        else None
    )
    config_path = config.config_path or str(hermes_home() / "config.yaml")
    config_environment = config.config_environment
    if config_environment is None:
        # 兼容直接构造 DashboardConfig 的调用方；正式入口会注入完整快照。
        config_environment = ConfigEnvironment.from_sources(
            allowed_keys=(
                DEFAULT_CONFIG_FIELD_REGISTRY.environment_override_keys
            ),
            process_environment=os.environ,
            profile_environment={},
        )
    # Repository 构造不读取或写入配置文件，故障隔离到具体请求。
    config_repository = YamlConfigRepository(
        config_path,
        DEFAULT_CONFIG_FIELD_REGISTRY,
        config_environment,
    )
    config_read_service = ConfigReadService(
        config_repository,
        DEFAULT_CONFIG_FIELD_REGISTRY,
    )
    config_write_service = ConfigWriteService(
        config_repository,
        DEFAULT_CONFIG_FIELD_REGISTRY,
    )
    read_service = ReadService(
        context=read_context,
        redactor=redactor,
        health_read_service=health_read_service,
        session_read_service=session_read_service,
        cron_read_service=cron_read_service,
        catalog_read_service=catalog_read_service,
    )
    return create_app(
        read_service=read_service,
        health_read_service=health_read_service,
        session_read_service=session_read_service,
        cron_read_service=cron_read_service,
        catalog_read_service=catalog_read_service,
        monitoring_read_service=monitoring_read_service,
        monitoring_aggregation_service=monitoring_aggregation_service,
        database_diagnostics_service=database_diagnostics_service,
        runtime_status_read_service=runtime_status_read_service,
        config_read_service=config_read_service,
        config_write_service=config_write_service,
        control_service=CronControlService(config.db_path),
        control_authenticator=authenticator,
        access_policy=access_policy,
    )


def _build_toolset_catalog_snapshot() -> tuple[
    tuple[CapabilityDescriptor, ...] | None,
    tuple[ToolsetDescriptor, ...] | None,
]:
    """在应用装配期构建一次轻量声明快照，不导入工具运行时模块。"""
    snapshot = build_toolset_catalog_snapshot()
    return snapshot.capabilities, snapshot.toolsets


def create_app(
    read_service: ReadService | None = None,
    control_service: CronControlService | None = None,
    control_authenticator: ControlAuthenticator | None = None,
    *,
    access_policy: DashboardAccessPolicy | None = None,
    health_read_service: HealthReadService | None = None,
    session_read_service: SessionReadService | None = None,
    cron_read_service: CronReadService | None = None,
    catalog_read_service: CatalogReadService | None = None,
    monitoring_read_service: MonitoringReadService | None = None,
    monitoring_aggregation_service: MonitoringAggregationService | None = None,
    database_diagnostics_service: DatabaseDiagnosticsService | None = None,
    runtime_status_read_service: RuntimeStatusReadService | None = None,
    config_read_service: ConfigReadService | None = None,
    config_write_service: ConfigWriteService | None = None,
) -> FastAPI:
    """创建不启动运行时组件的应用；正式启动应使用 build_dashboard_app。"""
    authenticator = control_authenticator or ControlAuthenticator()
    policy = access_policy or DashboardAccessPolicy(
        authenticator,
        read_auth_required=False,
        bound_host="127.0.0.1",
    )
    compatibility_read_service = read_service or ReadService(
        health_read_service=health_read_service,
        session_read_service=session_read_service,
        cron_read_service=cron_read_service,
        catalog_read_service=catalog_read_service,
    )
    resolved_health_service = (
        health_read_service or compatibility_read_service.health
    )
    resolved_session_service = (
        session_read_service or compatibility_read_service.sessions
    )
    resolved_cron_service = cron_read_service or compatibility_read_service.cron
    resolved_catalog_service = (
        catalog_read_service or compatibility_read_service.catalog
    )

    application = FastAPI(title="MyHermes Dashboard API")
    # 保留兼容门面，但新路由必须使用各自领域服务。
    application.state.read_service = compatibility_read_service
    application.state.health_read_service = resolved_health_service
    application.state.session_read_service = resolved_session_service
    application.state.cron_read_service = resolved_cron_service
    application.state.catalog_read_service = resolved_catalog_service
    application.state.monitoring_read_service = monitoring_read_service
    application.state.monitoring_aggregation_service = (
        monitoring_aggregation_service
    )
    application.state.database_diagnostics_service = (
        database_diagnostics_service
    )
    application.state.runtime_status_read_service = runtime_status_read_service
    application.state.config_read_service = config_read_service
    application.state.config_write_service = config_write_service
    application.state.control_service = control_service
    application.state.control_authenticator = authenticator
    application.state.dashboard_access_policy = policy

    # 不允许任意 Origin；通配绑定没有可安全推导的远程浏览器 Origin。
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_origin_regex=cors_origin_regex(policy.bound_host),
        allow_credentials=False,
        allow_methods=["GET", "HEAD", "POST", "PATCH", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Content-Type",
            TOKEN_HEADER,
            "Idempotency-Key",
        ],
    )

    @application.middleware("http")
    async def authorize_dashboard_api(
        request: Request,
        call_next,
    ) -> JSONResponse:
        # CORS 预检和最小存活探针不包含业务数据，也不需要 Token。
        if request.method == "OPTIONS" or request.url.path == "/healthz":
            return await call_next(request)
        if not request.url.path.startswith("/api/"):
            return await call_next(request)

        permission = (
            DashboardPermission.READ
            if request.method in _READ_METHODS
            else DashboardPermission.CONTROL
        )
        decision = policy.authorize(
            permission,
            token=request.headers.get(TOKEN_HEADER),
            origin=request.headers.get("Origin"),
        )
        if decision.allowed:
            return await call_next(request)
        return _authorization_error_response(decision.status_code, decision.error_code)

    @application.exception_handler(ReadDataUnavailable)
    async def handle_unavailable(
        request: Request,
        exc: ReadDataUnavailable,
    ) -> JSONResponse:
        del request
        body = ErrorResponse(
            code=exc.reason_code,
            message="请求的数据当前不可通过只读公开接口获得。",
        )
        return JSONResponse(status_code=503, content=jsonable_encoder(body))

    @application.exception_handler(ResourceNotFound)
    async def handle_not_found(
        request: Request,
        exc: ResourceNotFound,
    ) -> JSONResponse:
        del request
        body = ErrorResponse(
            code=exc.reason_code,
            message="请求的资源不存在。",
        )
        return JSONResponse(status_code=404, content=jsonable_encoder(body))

    @application.exception_handler(ReadInvalidRequest)
    async def handle_invalid_read_request(
        request: Request,
        exc: ReadInvalidRequest,
    ) -> JSONResponse:
        del request, exc
        body = ErrorResponse(
            code="invalid_request",
            message="读取请求参数无效。",
        )
        return JSONResponse(status_code=400, content=jsonable_encoder(body))

    @application.exception_handler(ConfigManagementError)
    async def handle_config_error(
        request: Request,
        exc: ConfigManagementError,
    ) -> JSONResponse:
        """只按稳定原因码映射配置错误，不传播底层异常信息。"""
        del request
        reason_code = getattr(exc, "reason_code", "config_unavailable")
        if reason_code not in _CONFIG_ERROR_STATUS:
            reason_code = "config_unavailable"
        body = ErrorResponse(
            code=reason_code,
            message=_CONFIG_ERROR_MESSAGES[reason_code],
        )
        return JSONResponse(
            status_code=_CONFIG_ERROR_STATUS[reason_code],
            content=jsonable_encoder(body),
        )

    @application.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        """配置 PATCH 校验失败时不回显请求值，其余路由保持原行为。"""
        if (
            request.method == "PATCH"
            and request.url.path == "/api/config"
        ):
            body = ErrorResponse(
                code="config_value_invalid",
                message=_CONFIG_ERROR_MESSAGES["config_value_invalid"],
            )
            return JSONResponse(
                status_code=400,
                content=jsonable_encoder(body),
            )
        return await request_validation_exception_handler(request, exc)

    @application.exception_handler(ControlUnavailable)
    async def handle_control_unavailable(
        request: Request,
        exc: ControlUnavailable,
    ) -> JSONResponse:
        del request, exc
        body = ErrorResponse(code="control_unavailable", message="控制接口当前不可用。")
        return JSONResponse(status_code=503, content=jsonable_encoder(body))

    @application.exception_handler(ControlNotFound)
    async def handle_control_not_found(
        request: Request,
        exc: ControlNotFound,
    ) -> JSONResponse:
        del request, exc
        body = ErrorResponse(code="not_found", message="请求的资源不存在。")
        return JSONResponse(status_code=404, content=jsonable_encoder(body))

    @application.exception_handler(ControlForbidden)
    async def handle_control_forbidden(
        request: Request,
        exc: ControlForbidden,
    ) -> JSONResponse:
        del request, exc
        body = ErrorResponse(code="control_forbidden", message="控制请求未获授权。")
        return JSONResponse(status_code=403, content=jsonable_encoder(body))

    @application.exception_handler(ControlConflict)
    async def handle_control_conflict(
        request: Request,
        exc: ControlConflict,
    ) -> JSONResponse:
        del request, exc
        body = ErrorResponse(
            code="control_conflict",
            message="当前任务状态不允许创建新的手动运行。",
        )
        return JSONResponse(status_code=409, content=jsonable_encoder(body))

    @application.exception_handler(ControlBadRequest)
    async def handle_control_bad_request(
        request: Request,
        exc: ControlBadRequest,
    ) -> JSONResponse:
        del request, exc
        body = ErrorResponse(code="invalid_request", message="控制请求参数无效。")
        return JSONResponse(status_code=400, content=jsonable_encoder(body))

    @application.exception_handler(Exception)
    async def handle_unexpected_error(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        del request, exc
        body = ErrorResponse(code="internal_error", message="服务暂时不可用。")
        return JSONResponse(status_code=500, content=jsonable_encoder(body))

    application.include_router(status.router)
    application.include_router(sessions.router)
    application.include_router(cron.router)
    application.include_router(catalog.router)
    application.include_router(monitoring.router)
    application.include_router(monitoring_aggregation.router)
    application.include_router(database_diagnostics.router)
    application.include_router(runtime.router)
    application.include_router(config_routes.router)
    return application


def _authorization_error_response(
    status_code: int | None,
    error_code: str | None,
) -> JSONResponse:
    """生成不泄漏 Token 配置或请求详情的统一认证错误。"""
    resolved_status = status_code or 401
    if error_code == "control_unavailable":
        message = "控制接口当前不可用。"
    elif resolved_status == 403:
        message = "请求未获授权。"
    else:
        message = "需要有效认证。"
    body = ErrorResponse(code=error_code or "authentication_required", message=message)
    return JSONResponse(status_code=resolved_status, content=jsonable_encoder(body))


# 导入兼容对象：不注入数据库、控制服务或正式认证配置，不能作为生产启动路径。
app = create_app()
