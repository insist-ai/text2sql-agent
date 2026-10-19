"""管理端审计接口(仅 admin 角色)。"""
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_audit_service, require_role
from app.api.schemas.audit_schema import AuditLogSchema
from app.api.schemas.common import ApiResponse, PageData
from app.models.audit_log import AuditStatus
from app.models.user import UserMySQL
from app.services.audit_service import AuditService

audit_router = APIRouter(prefix="/api/admin", tags=["admin"])


@audit_router.get(
    "/audit-logs",
    response_model=ApiResponse[PageData[AuditLogSchema]],
    summary="查询审计日志(仅 admin，支持用户/时间范围/状态筛选与分页)",
)
async def list_audit_logs(
    current_user: Annotated[UserMySQL, Depends(require_role("admin"))],
    service: Annotated[AuditService, Depends(get_audit_service)],
    user_id: int | None = Query(None, gt=0, description="按用户ID筛选"),
    status: AuditStatus | None = Query(None, description="按状态筛选(success/error)"),
    start_time: datetime | None = Query(None, description="起始时间(含)"),
    end_time: datetime | None = Query(None, description="结束时间(含)"),
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页数量"),
):
    # require_role("admin") 已校验角色，非 admin 抛 403
    total, items = await service.query_logs(
        user_id=user_id,
        status=status,
        start_time=start_time,
        end_time=end_time,
        offset=offset,
        limit=limit,
    )
    return ApiResponse(
        data=PageData[AuditLogSchema](
            total=total,
            offset=offset,
            limit=limit,
            items=[AuditLogSchema.model_validate(i) for i in items],
        )
    )
