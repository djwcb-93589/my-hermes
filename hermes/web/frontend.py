"""Dashboard 前端构建产物的同源静态资源适配层。"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


_FRONTEND_DIST_DIRECTORY = Path(__file__).resolve().parent / "frontend_dist"
_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
_INDEX_CACHE_CONTROL = "no-cache"
_FRONTEND_ROUTE_PREFIXES = ("overview", "config")


class _ImmutableStaticFiles(StaticFiles):
    """为 Vite 带内容摘要的资源补充长期不可变缓存。"""

    def file_response(
        self,
        full_path: os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(
            full_path,
            stat_result,
            scope,
            status_code,
        )
        response.headers["Cache-Control"] = _IMMUTABLE_CACHE_CONTROL
        return response


def install_dashboard_frontend(application: FastAPI) -> None:
    """在业务路由之后挂载已构建前端，不要求 Node 运行时存在。"""
    index_path = _FRONTEND_DIST_DIRECTORY / "index.html"
    assets_path = _FRONTEND_DIST_DIRECTORY / "assets"

    if assets_path.is_dir():
        application.mount(
            "/assets",
            _ImmutableStaticFiles(directory=assets_path, check_dir=False),
            name="dashboard-assets",
        )

    async def serve_index() -> Response:
        return _index_response(index_path)

    async def serve_frontend_route(frontend_path: str) -> Response:
        if not _is_frontend_route(frontend_path):
            return PlainTextResponse("Not Found", status_code=404)
        return _index_response(index_path)

    application.add_api_route(
        "/",
        serve_index,
        methods=["GET"],
        include_in_schema=False,
        name="dashboard-frontend-index",
    )
    application.add_api_route(
        "/{frontend_path:path}",
        serve_frontend_route,
        methods=["GET"],
        include_in_schema=False,
        name="dashboard-frontend-fallback",
    )


def _index_response(index_path: Path) -> Response:
    """缺少构建产物时保持 API 可启动并返回稳定的未构建响应。"""
    if not index_path.is_file():
        return PlainTextResponse(
            "Dashboard frontend is not built.",
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )
    return FileResponse(
        index_path,
        media_type="text/html",
        headers={"Cache-Control": _INDEX_CACHE_CONTROL},
    )


def _is_frontend_route(path: str) -> bool:
    """仅允许明确的前端路由，避免 SPA fallback 接管服务端端点。"""
    normalized = path.strip("/")
    return any(
        normalized == prefix or normalized.startswith(f"{prefix}/")
        for prefix in _FRONTEND_ROUTE_PREFIXES
    )


__all__ = ["install_dashboard_frontend"]
