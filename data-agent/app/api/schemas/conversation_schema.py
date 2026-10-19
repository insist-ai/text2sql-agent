from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreateSchema(BaseModel):
    title: str | None = Field(
        default=None,
        max_length=255,
        description="会话标题(可选,留空则首轮自动取问题前20字)",
    )


class ConversationRenameSchema(BaseModel):
    title: str = Field(..., min_length=1, max_length=255, description="新标题")


class ConversationSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    title: str | None
    created_at: datetime
    updated_at: datetime


class MessageSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    role: str
    content: str
    sql_text: str | None
    result_json: dict | list | None
    created_at: datetime
