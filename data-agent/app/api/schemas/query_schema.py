from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="用户自然语言查询(1-500字)",
    )
    conversation_id: int | None = Field(
        default=None,
        gt=0,
        description="会话ID(正整数,不传则自动新建会话);传入时必须属于当前用户",
    )
