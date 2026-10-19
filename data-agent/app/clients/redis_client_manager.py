"""Redis 异步客户端管理器（查询缓存优先使用的后端）。

设计说明
--------
- 使用 ``redis.asyncio`` 创建异步连接池客户端，供 ``RedisQueryCache`` 复用。
- ``init()`` 会在启动时主动 ``PING`` 探测：Redis 不可用（未部署/连接失败/超时）时
  仅记录 warning 并把 ``client`` 置为 ``None``，由查询缓存的工厂自动降级为进程内实现，
  不会因缓存依赖缺失而阻断应用启动。
- ``decode_responses=True``：缓存值以 str 返回，交由上层 json 反序列化。
"""
from __future__ import annotations

from typing import Optional

from redis.asyncio import Redis

from app.conf.app_config import RedisConfig, app_config
from app.core.log import logger


class RedisClientManager:
    def __init__(self, redis_config: RedisConfig):
        self.redis_config = redis_config
        self.client: Optional[Redis] = None

    def _get_url(self) -> str:
        auth = f":{self.redis_config.password}@" if self.redis_config.password else ""
        return (
            f"redis://{auth}{self.redis_config.host}:{self.redis_config.port}"
            f"/{self.redis_config.db}"
        )

    async def init(self) -> None:
        """创建客户端并 PING 探测；不可用时 client 保持 None（触发降级）。"""
        client = Redis.from_url(
            self._get_url(),
            socket_timeout=self.redis_config.socket_timeout,
            decode_responses=True,
        )
        try:
            await client.ping()
        except Exception as e:
            logger.warning(f"Redis 不可用，查询缓存将降级为进程内实现: {e}")
            await client.aclose()
            self.client = None
            return
        self.client = client
        logger.info(f"Redis 连接就绪: {self.redis_config.host}:{self.redis_config.port}")

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None


redis_client_manager = RedisClientManager(app_config.redis)
