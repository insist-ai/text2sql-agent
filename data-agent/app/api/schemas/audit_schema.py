from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AuditLogSchema(BaseModel):
    """审计日志对外结构(仅暴露审计字段本身，不含敏感信息)。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    request_id: str
    conversation_id: int | None
    question: str
    sql_text: str | None
    status: str
    error_message: str | None
    duration_ms: int
    created_at: datetime
