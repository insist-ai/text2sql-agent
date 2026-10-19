"""通用响应结构：统一响应体 ApiResponse 与分页数据结构 PageData。"""
from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一响应结构 {code, message, data}。

    code=0 表示成功；非 0 为业务错误码(见 app/core/error_code.py)。
    """

    code: int = Field(default=0, description="业务状态码，0 表示成功")
    message: str = Field(default="ok", description="提示信息")
    data: T | None = Field(default=None, description="业务数据")


class PageData(BaseModel, Generic[T]):
    """通用分页数据结构。"""

    total: int = Field(..., description="总记录数")
    offset: int = Field(..., description="偏移量")
    limit: int = Field(..., description="每页数量")
    items: list[T] = Field(default_factory=list, description="当前页数据")
