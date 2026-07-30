"""Dashboard 列表读取的统一分页规则。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, TypeVar

from fastapi import Query


DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200

_Item = TypeVar("_Item")


@dataclass(frozen=True)
class PageParams:
    """已校验的 Dashboard 分页参数。"""

    limit: int = DEFAULT_PAGE_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise ValueError("page limit must be an integer")
        if self.limit < 1 or self.limit > MAX_PAGE_LIMIT:
            raise ValueError(
                f"page limit must be between 1 and {MAX_PAGE_LIMIT}"
            )
        if isinstance(self.offset, bool) or not isinstance(self.offset, int):
            raise ValueError("page offset must be an integer")
        if self.offset < 0:
            raise ValueError("page offset must be non-negative")

    @property
    def fetch_limit(self) -> int:
        """通过多读取一项判断是否还有下一页。"""
        return self.limit + 1


def page_params(
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> PageParams:
    """为所有列表路由提供同一套 FastAPI 参数校验。"""
    return PageParams(limit=limit, offset=offset)


def split_page(items: Sequence[_Item], page: PageParams) -> tuple[list[_Item], bool]:
    """裁剪至请求页，并根据额外一项返回稳定的 has_more。"""
    has_more = len(items) > page.limit
    return list(items[:page.limit]), has_more
