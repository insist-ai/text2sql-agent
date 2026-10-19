from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Text, func
from sqlalchemy.types import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.app_base import AppBase


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageMySQL(AppBase):
    __tablename__ = "message"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        comment="消息ID"
    )
    conversation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("conversation.id"),
        index=True,
        comment="会话ID"
    )
    role: Mapped[MessageRole] = mapped_column(
        Enum(MessageRole, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        comment="消息角色(user/assistant)"
    )
    content: Mapped[str] = mapped_column(
        Text,
        comment="消息内容"
    )
    sql_text: Mapped[str | None] = mapped_column(
        Text,
        comment="SQL语句"
    )
    result_json: Mapped[dict | list | None] = mapped_column(
        JSON,
        comment="查询结果"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        comment="更新时间"
    )
