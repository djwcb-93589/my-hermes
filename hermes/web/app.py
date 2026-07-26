"""独立 FastAPI 应用工厂。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from hermes.web.read_service import ReadDataUnavailable, ReadService, ResourceNotFound
from hermes.web.routes import catalog, cron, sessions, status
from hermes.web.schemas import ErrorResponse


def create_app(read_service: ReadService | None = None) -> FastAPI:
    """创建没有后台任务、没有认证副作用的只读应用。"""
    application = FastAPI(title="MyHermes Dashboard API")
    application.state.read_service = read_service or ReadService()

    # 未来认证应添加在此处的应用级 middleware，而不是分散到每条路由。
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["Accept", "Content-Type"],
    )

    @application.exception_handler(ReadDataUnavailable)
    async def handle_unavailable(
        request: Request,
        exc: ReadDataUnavailable,
    ) -> JSONResponse:
        del request, exc
        body = ErrorResponse(
            code="data_unavailable",
            message="请求的数据当前不可通过只读公开接口获得。",
        )
        return JSONResponse(status_code=503, content=jsonable_encoder(body))

    @application.exception_handler(ResourceNotFound)
    async def handle_not_found(
        request: Request,
        exc: ResourceNotFound,
    ) -> JSONResponse:
        del request, exc
        body = ErrorResponse(code="not_found", message="请求的资源不存在。")
        return JSONResponse(status_code=404, content=jsonable_encoder(body))

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
    return application


app = create_app()
