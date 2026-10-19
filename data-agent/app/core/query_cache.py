"""查询结果缓存：统一接口 QueryCache + 两种实现（Redis 优先，内存 TTL 降级）。

背景
----
``/api/query`` 会触发 LangGraph 全流程（多次 LLM 调用 + 多路召回 + SQL 执行），
耗时长、成本高。对「同一问题 + 同一数据范围」的重复查询，可直接回放首次结果，
跳过整条链路。

接口
----
``QueryCache`` 抽象出三个方法，Redis 与内存两种实现共用：
- ``get(key)``：取缓存值（dict），未命中或已过期返回 None；
- ``set(key, value, ttl=...)``：写入缓存；
- ``clear_prefix(prefix)``：按前缀清除（缓存失效），返回删除条目数。

实现
----
- ``RedisQueryCache``：生产首选。基于 ``redis.asyncio``，支持 TTL 与 SCAN 前缀清除。
  首次操作失败时（Redis 中途挂掉）自动**降级**到内置的内存实现，保证查询不受影响。
- ``InMemoryTTLQueryCache``：进程内单机实现（dict + 单调时钟过期），
  用于 Redis 不可用时的自动降级与本地调试。多副本部署时各副本独立、进程重启即清零。

缓存键
------
``build_cache_key(rewritten_query, data_scope)``：对「改写后的问题 + 数据范围标识」
做空白归一化后取 SHA256。数据范围为「当前用户可见数据范围」的标识（本项目暂以角色
作为代理，见 ``data_scope`` 注释）。

与多轮上下文的关系
------------------
仅对「无历史上下文」（单轮）的请求启用缓存：该场景下改写节点直接返回原问题，
结果只取决于 (问题, 数据范围)。带历史的问题依赖会话上下文，改写结果跨会话不可复用，
因此一律不缓存/不命中，避免串会话污染。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from abc import ABC, abstractmethod

from app.clients.redis_client_manager import redis_client_manager
from app.conf.app_config import app_config
from app.core.log import logger


def build_cache_key(rewritten_query: str, data_scope: str) -> str:
    """规范化「改写后问题 + 数据范围标识」后取 SHA256 作为缓存键。

    - 空白归一化：折叠连续空白并去除首尾空白，避免同一问题因空格差异产生不同键；
    - 数据范围以 NUL 分隔拼入，避免「问题尾部/范围头部」拼接歧义。
    """
    normalized = " ".join(rewritten_query.split())
    raw = f"{data_scope}\x00{normalized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class QueryCache(ABC):
    """查询缓存的统一接口。"""

    @abstractmethod
    async def get(self, key: str) -> dict | None:
        """取缓存值；未命中或已过期返回 None。"""

    @abstractmethod
    async def set(self, key: str, value: dict, *, ttl: int | None = None) -> None:
        """写入缓存；ttl 为空时使用配置的默认 TTL。"""

    @abstractmethod
    async def clear_prefix(self, prefix: str = "") -> int:
        """按前缀清除缓存（缓存失效），返回删除条目数。"""


class InMemoryTTLQueryCache(QueryCache):
    """进程内 TTL 缓存（单机、dict + 单调时钟）。

    - 使用 ``time.monotonic()`` 计时，避免系统时间回拨影响过期判定；
    - 容量超限时先清理已过期条目，仍超限则淘汰最早过期的条目，防止内存无限增长。
    """

    def __init__(self, *, ttl_seconds: int, key_prefix: str, max_entries: int) -> None:
        self._ttl = ttl_seconds
        self._prefix = key_prefix
        self._max_entries = max(1, max_entries)
        # 存储 key -> (过期时刻(单调时钟), 值)
        self._store: dict[str, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def get(self, key: str) -> dict | None:
        full = self._full_key(key)
        async with self._lock:
            item = self._store.get(full)
            if item is None:
                return None
            expire_at, value = item
            if expire_at <= time.monotonic():
                self._store.pop(full, None)
                return None
            return value

    async def set(self, key: str, value: dict, *, ttl: int | None = None) -> None:
        full = self._full_key(key)
        expire_at = time.monotonic() + (ttl if ttl is not None else self._ttl)
        async with self._lock:
            if full not in self._store and len(self._store) >= self._max_entries:
                self._evict()
            self._store[full] = (expire_at, value)

    async def clear_prefix(self, prefix: str = "") -> int:
        async with self._lock:
            full_prefix = self._full_key(prefix)
            keys = [k for k in self._store if k.startswith(full_prefix)]
            for k in keys:
                self._store.pop(k, None)
            return len(keys)

    def _evict(self) -> None:
        """优先清理过期条目；仍满则淘汰最早过期的条目。"""
        now = time.monotonic()
        expired = [k for k, (expire_at, _) in self._store.items() if expire_at <= now]
        for k in expired:
            self._store.pop(k, None)
        if len(self._store) >= self._max_entries and self._store:
            oldest = min(self._store, key=lambda k: self._store[k][0])
            self._store.pop(oldest, None)


class RedisQueryCache(QueryCache):
    """基于 ``redis.asyncio`` 的缓存实现（生产首选）。

    Redis 中途不可用时（首次操作抛异常）自动降级到内置的内存实现并记录 warning，
    此后走内存缓存，保证查询链路不因缓存后端故障而失败。
    """

    def __init__(
        self,
        redis_client,
        *,
        key_prefix: str,
        ttl_seconds: int,
        fallback: QueryCache,
    ) -> None:
        self._redis = redis_client
        self._prefix = key_prefix
        self._ttl = ttl_seconds
        self._fallback = fallback
        self._degraded = False

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def _degrade(self, exc: Exception) -> None:
        if not self._degraded:
            self._degraded = True
            logger.warning(f"Redis 缓存操作失败，降级为进程内缓存: {exc}")

    async def get(self, key: str) -> dict | None:
        if self._degraded:
            return await self._fallback.get(key)
        try:
            raw = await self._redis.get(self._full_key(key))
        except Exception as e:
            self._degrade(e)
            return await self._fallback.get(key)
        return json.loads(raw) if raw else None

    async def set(self, key: str, value: dict, *, ttl: int | None = None) -> None:
        ttl = ttl if ttl is not None else self._ttl
        if self._degraded:
            await self._fallback.set(key, value, ttl=ttl)
            return
        try:
            payload = json.dumps(value, ensure_ascii=False)
            await self._redis.set(self._full_key(key), payload, ex=ttl)
        except Exception as e:
            self._degrade(e)
            await self._fallback.set(key, value, ttl=ttl)

    async def clear_prefix(self, prefix: str = "") -> int:
        if self._degraded:
            return await self._fallback.clear_prefix(prefix)
        try:
            pattern = f"{self._full_key(prefix)}*"
            deleted = 0
            async for k in self._redis.scan_iter(match=pattern, count=200):
                await self._redis.delete(k)
                deleted += 1
            return deleted
        except Exception as e:
            self._degrade(e)
            return await self._fallback.clear_prefix(prefix)


# 进程内单例；查询缓存关闭时 get_query_cache() 返回 None。
_query_cache: QueryCache | None = None


def get_query_cache() -> QueryCache | None:
    """返回查询缓存单例；``query_cache.enable=false`` 时返回 None（完全关闭）。

    Redis 可用则用 ``RedisQueryCache``，否则自动降级为 ``InMemoryTTLQueryCache``。
    降级实现同时作为 Redis 中途故障时的备用后端。
    """
    global _query_cache
    cfg = app_config.query_cache
    if not cfg.enable:
        return None
    if _query_cache is None:
        fallback = InMemoryTTLQueryCache(
            ttl_seconds=cfg.ttl_seconds,
            key_prefix=cfg.key_prefix,
            max_entries=cfg.max_entries,
        )
        redis_client = redis_client_manager.client
        if redis_client is not None:
            _query_cache = RedisQueryCache(
                redis_client,
                key_prefix=cfg.key_prefix,
                ttl_seconds=cfg.ttl_seconds,
                fallback=fallback,
            )
        else:
            _query_cache = fallback
    return _query_cache


def reset_query_cache() -> None:
    """重置单例（应用关闭 / 配置变更 / 测试用）。"""
    global _query_cache
    _query_cache = None
