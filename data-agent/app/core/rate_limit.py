"""基于内存的滑动窗口限流（单机实现）。

设计说明
--------
- 维度：以「用户 + 接口」为限流 key（``user_id:path``），同一用户对不同接口独立计数。
  认证入口（登录/注册）此时尚无 token，改以「客户端 IP（登录再拼用户名）」为 key，
  两者共用本模块的滑动窗口实现，仅额度不同（见 ``rate_limit.auth`` 配置）。
- 算法：滑动窗口。用双端队列(deque)保存窗口内每次请求的时间戳，每次校验先让
  已滑出窗口的旧时间戳出队，队列长度即窗口内的请求数。相比固定窗口算法，
  滑动窗口不会在窗口边界出现"计数清零后瞬时放行两倍请求"的突刺问题。
- 并发：每个 key 独享一把 ``asyncio.Lock``，保证"清理 + 判定 + 计数"这一步原子执行。
- 时钟：使用 ``time.monotonic()`` 单调时钟，避免系统时间回拨导致窗口计算异常。

单机内存的局限与生产替换
------------------------
- 当前计数保存在本进程内存中：多副本部署时各副本独立计数（实际额度≈副本数×配置值），
  进程重启后计数清零。
- 生产环境建议替换为 Redis 实现：例如 ``INCR + EXPIRE`` 的固定窗口，或
  ``ZSET + Lua`` 的滑动窗口。只要保持 ``is_allowed()`` 相同的返回语义
  （``(是否放行, 建议等待秒数)``）即可无缝替换，调用方无需改动。

与 SSE 的关系
-------------
- 限流在路由依赖注入阶段（返回 ``StreamingResponse`` 之前）执行，
  不介入、不缓冲流式响应体，因此不会影响 ``/api/query`` 的 SSE 流式返回。
- 限流触发时在流式响应开始前即返回统一 429（带 Retry-After），客户端据此退避重试。
"""
from __future__ import annotations

import math
import time
from asyncio import Lock
from collections import deque

from app.conf.app_config import app_config


class SlidingWindowRateLimiter:
    """单机内存滑动窗口限流器。

    Args:
        window_seconds: 窗口长度(秒)。
        max_requests: 窗口内允许的最大请求数。
    """

    def __init__(self, window_seconds: int, max_requests: int) -> None:
        if window_seconds <= 0 or max_requests <= 0:
            raise ValueError("window_seconds 与 max_requests 必须为正整数")
        self.window_seconds = window_seconds
        self.max_requests = max_requests
        # key -> 窗口内请求时间戳(单调时钟)
        self._hits: dict[str, deque[float]] = {}
        # key -> 该 key 的互斥锁
        self._locks: dict[str, Lock] = {}

    async def is_allowed(self, key: str) -> tuple[bool, int]:
        """判定 key 是否放行，并返回建议等待秒数。

        Returns:
            ``(allowed, retry_after)``：allowed=True 表示已计入本次请求、retry_after=0；
            allowed=False 表示已超限，retry_after 为最早一次请求滑出窗口所需秒数(至少 1)。
        """
        now = time.monotonic()
        lock = self._locks.setdefault(key, Lock())
        async with lock:
            hits = self._hits.setdefault(key, deque())
            boundary = now - self.window_seconds
            # 移除已滑出窗口的时间戳
            while hits and hits[0] <= boundary:
                hits.popleft()
            if len(hits) >= self.max_requests:
                # 最早一次请求滑出窗口后即可重试
                retry_after = max(1, math.ceil(self.window_seconds - (now - hits[0])))
                return False, retry_after
            hits.append(now)
            return True, 0


# 进程内限流器注册表：以 (窗口秒数, 最大请求数) 为键，同一配置复用同一实例。
# 业务接口与认证入口额度不同（见 conf/app_config.yaml 的 rate_limit.auth），
# 因此不能共用一个单例，改为按参数取用。
# 限流总开关关闭时 get_limiter() 返回 None，调用方直接放行。
_limiters: dict[tuple[int, int], SlidingWindowRateLimiter] = {}


def get_limiter(window_seconds: int, max_requests: int) -> SlidingWindowRateLimiter | None:
    """按 (窗口秒数, 最大请求数) 取限流器实例；限流关闭或参数非法时返回 None。

    相同参数返回同一实例（跨请求累计计数）；参数变化时自动按新键新建，
    旧实例保留但不影响新配置，便于运行期调整与测试。
    """
    cfg = app_config.rate_limit
    if not cfg.enable:
        return None
    # 额度配置为 0/负数视为该入口不限流（便于临时关停某一路）
    if window_seconds <= 0 or max_requests <= 0:
        return None
    key = (window_seconds, max_requests)
    limiter = _limiters.get(key)
    if limiter is None:
        limiter = SlidingWindowRateLimiter(window_seconds, max_requests)
        _limiters[key] = limiter
    return limiter


def get_rate_limiter() -> SlidingWindowRateLimiter | None:
    """业务接口限流器单例（按「用户 + 接口」维度），等价于 get_limiter(业务额度)。

    当 window_seconds / max_requests 配置发生变化时自动切换到新实例，便于运行期调整与测试。
    """
    cfg = app_config.rate_limit
    return get_limiter(cfg.window_seconds, cfg.max_requests)
