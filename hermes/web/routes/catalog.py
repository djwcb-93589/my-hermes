"""Skill 和 Toolset 目录只读路由。"""

from fastapi import APIRouter, Depends, Request

from hermes.web.pagination import PageParams, page_params
from hermes.web.read_service import CatalogReadService
from hermes.web.schemas import SkillListResponse, ToolsetListResponse


router = APIRouter(tags=["catalog"])


def _service(request: Request) -> CatalogReadService:
    return request.app.state.catalog_read_service


@router.get("/api/skills", response_model=SkillListResponse)
def list_skills(
    request: Request,
    page: PageParams = Depends(page_params),
) -> SkillListResponse:
    return _service(request).list_skills(page=page)


@router.get("/api/tools/toolsets", response_model=ToolsetListResponse)
def list_toolsets(
    request: Request,
    page: PageParams = Depends(page_params),
) -> ToolsetListResponse:
    return _service(request).list_toolsets(page=page)
