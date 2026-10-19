from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLogMySQL, AuditStatus


class AuditLogRepository:
    """审计日志纯数据访问层。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self,
        *,
        user_id: int,
        request_id: str,
        conversation_id: int | None,
        question: str,
        sql_text: str | None,
        status: AuditStatus,
        error_message: str | None,
        duration_ms: int,
    ) -> AuditLogMySQL:
        record = AuditLogMySQL(
            user_id=user_id,
            request_id=request_id,
            conversation_id=conversation_id,
            question=question,
            sql_text=sql_text,
            status=status,
            error_message=error_message,
            duration_ms=duration_ms,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def list_paged(
        self,
        *,
        user_id: int | None = None,
        status: AuditStatus | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[int, list[AuditLogMySQL]]:
        """按条件分页查询，返回 (总数, 当前页记录)，按 id 倒序(最新在前)。"""
        conditions = []
        if user_id is not None:
            conditions.append(AuditLogMySQL.user_id == user_id)
        if status is not None:
            conditions.append(AuditLogMySQL.status == status)
        if start_time is not None:
            conditions.append(AuditLogMySQL.created_at >= start_time)
        if end_time is not None:
            conditions.append(AuditLogMySQL.created_at <= end_time)

        total = await self.session.scalar(
            select(func.count()).select_from(AuditLogMySQL).where(*conditions)
        )
        stmt = (
            select(AuditLogMySQL)
            .where(*conditions)
            .order_by(AuditLogMySQL.id.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return int(total or 0), list(result.scalars().all())
