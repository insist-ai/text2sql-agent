from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, Enum, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.app_base import AppBase


class AuditStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


class AuditLogMySQL(AppBase):
    """问数审计日志。

    仅记录查询本身的可观测信息(问题/SQL/耗时/状态)，**不得写入敏感信息**(如 token、
    密码、连接串)，避免审计表成为敏感数据泄露面。写入使用独立 session，见 AuditService。
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        comment="审计ID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        index=True,
        comment="发起查询的用户ID"
    )
    request_id: Mapped[str] = mapped_column(
        String(64),
        index=True,
        comment="请求链路ID(与日志中的 request_id 一致)"
    )
    conversation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        index=True,
        comment="会话ID(自动新建或前端传入)"
    )
    question: Mapped[str] = mapped_column(
        Text,
        comment="用户原始问题(非改写后问题)"
    )
    sql_text: Mapped[str | None] = mapped_column(
        Text,
        comment="最终执行的SQL(未产出则为空)"
    )
    status: Mapped[AuditStatus] = mapped_column(
        Enum(AuditStatus, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        index=True,
        comment="查询状态(success/error)"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        comment="失败原因(成功为空)"
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer,
        comment="查询耗时(毫秒)"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        index=True,
        server_default=func.now(),
        comment="创建时间"
    )
