from sqlalchemy.orm import DeclarativeBase


class AppBase(DeclarativeBase):
    """应用库(user/conversation/message)的 ORM 基类，独立于 meta 库的 Base。"""
    pass
