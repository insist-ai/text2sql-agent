"""鉴权与 RBAC 示范接口（最小演示，可按需删除）。"""
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import get_current_user, require_role
from app.api.schemas.auth_schema import UserSchema
from app.api.schemas.common import ApiResponse
from app.core.query_cache import get_query_cache
from app.models.user import UserMySQL

demo_router = APIRouter(tags=["demo"])


@demo_router.get("/api/users/me", response_model=ApiResponse[UserSchema])
async def get_me(
    current_user: Annotated[UserMySQL, Depends(get_current_user)],
):
    # get_current_user 已完成 token 校验，此处直接拿用户对象
    return ApiResponse(
        data=UserSchema(
            id=current_user.id,
            username=current_user.username,
            role=str(current_user.role),
        )
    )


@demo_router.post("/api/admin/cache/clear", response_model=ApiResponse[dict])
async def clear_query_cache(
    current_user: Annotated[UserMySQL, Depends(require_role("admin"))],
    prefix: str = "",
):
    """查询缓存失效入口：按前缀清除（prefix 为空表示清空全部缓存）。

    返回被删除的条目数；缓存关闭时 deleted=0。生产环境仅管理员可调用。
    """
    cache = get_query_cache()
    if cache is None:
        return ApiResponse(data={"deleted": 0, "prefix": prefix, "message": "查询缓存未启用"})
    deleted = await cache.clear_prefix(prefix)
    return ApiResponse(data={"deleted": deleted, "prefix": prefix})
