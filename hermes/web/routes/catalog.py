"""Skill 和 Toolset 目录只读路由。"""

from fastapi import APIRouter, Request

from hermes.web.read_service import ReadService
from hermes.web.schemas import SkillListResponse, ToolsetListResponse


router = APIRouter(tags=["catalog"])


def _service(request: Request) -> ReadService:
    return request.app.state.read_service


@router.get("/api/skills", response_model=SkillListResponse)
def list_skills(request: Request) -> SkillListResponse:
    return _service(request).list_skills()


@router.get("/api/tools/toolsets", response_model=ToolsetListResponse)
def list_toolsets(request: Request) -> ToolsetListResponse:
    return _service(request).list_toolsets()
