"""全局错误码集中定义（禁止在业务代码中散落魔法数字）。

约定：
- 0 表示成功，非 0 表示失败；
- 错误码表达「业务语义」，与 HTTP 状态码解耦（HTTP 状态表达传输语义）；
- 4xxxx 段对应客户端错误，5xxxx 段对应服务端错误。

错误码表：
| code  | 名称                | 典型 HTTP | 含义                              |
|-------|---------------------|-----------|-----------------------------------|
| 0     | SUCCESS             | 200       | 成功                              |
| 40000 | PARAM_ERROR         | 400       | 请求参数校验失败                  |
| 40100 | UNAUTHENTICATED     | 401       | 缺少/格式错误的认证令牌           |
| 40101 | TOKEN_INVALID       | 401       | 令牌无效/过期/类型不匹配          |
| 40102 | INVALID_CREDENTIALS | 401       | 用户名或密码错误(防枚举,文案统一) |
| 40300 | ACCOUNT_DISABLED    | 401/403   | 账号已被禁用                      |
| 40301 | FORBIDDEN           | 403       | 权限不足/无权访问他人资源         |
| 40400 | RESOURCE_NOT_FOUND  | 404       | 资源不存在                        |
| 40900 | BUSINESS_CONFLICT   | 409       | 业务冲突(通用)                    |
| 40901 | USERNAME_EXISTS     | 409       | 用户名已存在                      |
| 42900 | TOO_MANY_REQUESTS   | 429       | 请求过于频繁(触发限流)            |
| 50000 | INTERNAL_ERROR      | 500       | 服务内部异常                      |
"""
from __future__ import annotations

from enum import IntEnum


class ErrorCode(IntEnum):
    """业务错误码枚举。"""

    SUCCESS = 0

    # ---- 400 参数错误 ----
    PARAM_ERROR = 40000

    # ---- 401 未认证 ----
    UNAUTHENTICATED = 40100
    TOKEN_INVALID = 40101
    INVALID_CREDENTIALS = 40102

    # ---- 403 无权限 ----
    ACCOUNT_DISABLED = 40300
    FORBIDDEN = 40301

    # ---- 404 资源不存在 ----
    RESOURCE_NOT_FOUND = 40400

    # ---- 409 业务冲突 ----
    BUSINESS_CONFLICT = 40900
    USERNAME_EXISTS = 40901

    # ---- 429 请求过于频繁 ----
    TOO_MANY_REQUESTS = 42900

    # ---- 500 服务异常 ----
    INTERNAL_ERROR = 50000
