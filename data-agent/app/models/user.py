from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.app_base import AppBase


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


class UserMySQL(AppBase):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        comment="用户ID"
    )
    username: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        comment="用户名"
    )
    password_hash: Mapped[str] = mapped_column(
        String(255),
        comment="密码哈希"
    )
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        default=UserRole.USER,
        server_default=UserRole.USER.value,
        comment="角色(user/admin)"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("1"),
        comment="是否启用"
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        comment="最近登录时间"
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
