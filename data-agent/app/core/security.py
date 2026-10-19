"""安全工具：密码哈希(bcrypt) + JWT 双 token。

纯同步工具函数，不依赖异步。所有对外异常均不泄露内部细节。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import bcrypt
import jwt
from jwt import ExpiredSignatureError, InvalidTokenError

from app.conf.app_config import app_config

TokenType = Literal["access", "refresh"]

# 读取密钥的环境变量名
_SECRET_ENV = "APP_JWT_SECRET_KEY"


class SecurityError(Exception):
    """安全相关错误的统一基类（错误信息不泄露内部细节）。"""


class TokenError(SecurityError):
    """Token 校验失败统一异常。"""


def _get_secret_key() -> str:
    """优先从环境变量读取密钥，避免在配置文件中硬编码真实密钥。"""
    env_secret = os.getenv(_SECRET_ENV)
    if env_secret:
        return env_secret
    return app_config.jwt.secret_key


def _expire_delta(token_type: TokenType) -> timedelta:
    cfg = app_config.jwt
    minutes = (
        cfg.access_token_expire_minutes
        if token_type == "access"
        else cfg.refresh_token_expire_minutes
    )
    return timedelta(minutes=minutes)


def _build_payload(user_id: int | str, role: str, token_type: TokenType) -> dict[str, Any]:
    now = int(datetime.now(timezone.utc).timestamp())
    return {
        "sub": str(user_id),
        "role": role,
        "type": token_type,
        "iat": now,
        "exp": now + int(_expire_delta(token_type).total_seconds()),
        "jti": uuid.uuid4().hex,
    }


def hash_password(plain: str) -> str:
    """对明文密码做 bcrypt 哈希，返回可直接存储的字符串。"""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文密码与 bcrypt 哈希是否匹配；哈希格式异常等一律返回 False。"""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(user_id: int | str, role: str) -> str:
    """生成 access token（有效期 15 分钟，可配置）。"""
    payload = _build_payload(user_id, role, "access")
    return jwt.encode(payload, _get_secret_key(), algorithm=app_config.jwt.algorithm)


def create_refresh_token(user_id: int | str) -> str:
    """生成 refresh token（有效期 7 天，可配置）。"""
    payload = _build_payload(user_id, role="", token_type="refresh")
    return jwt.encode(payload, _get_secret_key(), algorithm=app_config.jwt.algorithm)


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """校验签名/过期/type；任一失败抛 TokenError（信息不泄露内部细节）。"""
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            _get_secret_key(),
            algorithms=[app_config.jwt.algorithm],
        )
    except ExpiredSignatureError:
        raise TokenError("令牌已过期")
    except InvalidTokenError:
        raise TokenError("无效的令牌")
    if payload.get("type") != expected_type:
        raise TokenError("令牌类型不匹配")
    return payload


def _self_test() -> None:
    """__main__ 自测：密码哈希/校验、token 生成/解码、篡改报错。"""
    # 自测阶段注入临时密钥，演示环境变量读取路径
    os.environ[_SECRET_ENV] = "self-test-secret-key-please-change"

    print("===== 1. 密码哈希与校验 =====")
    hashed = hash_password("p@ssw0rd")
    print("hash:", hashed)
    assert verify_password("p@ssw0rd", hashed) is True
    assert verify_password("wrong-password", hashed) is False
    print("OK: 正确密码验证通过 / 错误密码验证失败")

    print("\n===== 2. access token 生成与解码 =====")
    access = create_access_token(1, "admin")
    payload = decode_token(access, "access")
    print("payload:", payload)
    assert payload["sub"] == "1"
    assert payload["role"] == "admin"
    assert payload["type"] == "access"
    assert payload["jti"] and payload["exp"] > payload["iat"]
    print("OK: access token 解码正确(含 sub/role/type/exp/iat/jti)")

    print("\n===== 3. refresh token 生成与解码 =====")
    refresh = create_refresh_token(1)
    rpayload = decode_token(refresh, "refresh")
    print("payload:", rpayload)
    assert rpayload["sub"] == "1" and rpayload["type"] == "refresh"
    print("OK: refresh token 解码正确")

    print("\n===== 4. 令牌类型不匹配 =====")
    try:
        decode_token(access, "refresh")
    except TokenError as e:
        print("OK: 类型不匹配报错 ->", e)
    else:
        raise AssertionError("类型不匹配应报错")

    print("\n===== 5. 篡改 token =====")
    tampered = access[:-4] + ("aaaa" if access[-4:] != "aaaa" else "bbbb")
    try:
        decode_token(tampered, "access")
    except TokenError as e:
        print("OK: 篡改报错 ->", e)
    else:
        raise AssertionError("篡改 token 应报错")

    print("\n===== 全部自测通过 =====")


if __name__ == "__main__":
    _self_test()
