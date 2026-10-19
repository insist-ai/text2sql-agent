"""审计服务：管理端查询 + 查询完成后的异步审计写入。

- 查询(读)：使用注入的请求 session，与其他服务共享同一事务视图；
- 写入：使用**独立 session**(旁路落库)，避免与请求事务耦合成长事务；由 QueryService
  以 fire-and-forget 方式异步触发，写入失败仅记日志、绝不影响主流程。
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.mysql_client_manager import app_mysql_client_manager
from app.core.log import logger
from app.models.audit_log import AuditLogMySQL, AuditStatus
from app.repositories.mysql.app.audit_log_repository import AuditLogRepository


class AuditService:
    """管理端审计查询服务(读路径)。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit_log_repository = AuditLogRepository(session)

    async def query_logs(
        self,
        *,
        user_id: int | None = None,
        status: AuditStatus | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[int, list[AuditLogMySQL]]:
        """按用户/时间范围/状态筛选并分页，返回 (总数, 当前页记录)。"""
        return await self.audit_log_repository.list_paged(
            user_id=user_id,
            status=status,
            start_time=start_time,
            end_time=end_time,
            offset=offset,
            limit=limit,
        )


async def write_audit_log(
    *,
    user_id: int,
    request_id: str,
    conversation_id: int | None,
    question: str,
    sql_text: str | None,
    status: AuditStatus,
    error_message: str | None,
    duration_ms: int,
) -> None:
    """写入一条审计记录。

    使用独立 session(``session_factory`` 新建连接)：不与请求事务共享，避免长事务；
    捕获全部异常并仅记日志，保证审计写入失败不影响主流程。
    """
    session_factory = app_mysql_client_manager.session_factory
    if session_factory is None:      # 应用库未初始化(极早期/单测场景)
        return
    try:
        async with session_factory() as session:
            repository = AuditLogRepository(session)
            await repository.add(
                user_id=user_id,
                request_id=request_id,
                conversation_id=conversation_id,
                question=question,
                sql_text=sql_text,
                status=status,
                error_message=error_message,
                duration_ms=duration_ms,
            )
            await session.commit()
    except Exception:
        logger.exception("审计日志写入失败(不影响主流程)")


# 持有后台任务引用，避免任务被 GC 提前回收
_background_tasks: set[asyncio.Task] = set()


def schedule_audit_log(**kwargs) -> None:
    """异步(后台任务)触发审计写入，不阻塞主流程。

    审计写入失败由 ``write_audit_log`` 内部兜底；此处只负责调度。
    创建任务时会复制当前上下文，故日志中的 request_id 能正确带入审计链路。
    """
    task = asyncio.create_task(write_audit_log(**kwargs))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
