from pydantic import BaseModel, Field


class RegisterSchema(BaseModel):
    username: str = Field(
        ...,
        min_length=3,
        max_length=64,
        pattern=r"^[A-Za-z0-9_]+$",
        description="用户名(字母/数字/下划线,3-64位)",
    )
    password: str = Field(
        ...,
        min_length=6,
        max_length=128,
        description="密码(6-128位)",
    )


class LoginSchema(BaseModel):
    username: str = Field(
        ...,
        min_length=3,
        max_length=64,
        pattern=r"^[A-Za-z0-9_]+$",
        description="用户名(字母/数字/下划线,3-64位)",
    )
    password: str = Field(..., min_length=6, max_length=128, description="密码(6-128位)")


class TokenSchema(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserSchema(BaseModel):
    id: int
    username: str
    role: str


class RefreshTokenSchema(BaseModel):
    refresh_token: str


class AccessTokenSchema(BaseModel):
    access_token: str
    token_type: str = "bearer"
