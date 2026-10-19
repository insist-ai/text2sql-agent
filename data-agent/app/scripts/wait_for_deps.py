"""启动前等待依赖服务就绪（容器化部署用）。

背景：docker compose 的 depends_on 只能保证「容器已启动」，不能保证「服务可连接」——
尤其是 qdrant 镜像内没有 curl/wget，无法用 healthcheck 探测。
本脚本直接对配置里的 host/port 做 TCP 探活，全部可达后才放行后续的建表与索引构建。

只等待**强依赖**：MySQL（业务库/数仓）、Qdrant（向量召回）、ES（关键词召回）、
Embedding（向量化）。Redis 属于可降级组件（不可用时自动降级为进程内缓存），
若在这里阻塞等待，会导致 Redis 未部署时永远起不来。

用法：python -m app.scripts.wait_for_deps [超时秒数，默认 180]
"""

import socket
import sys
import time

from app.conf.app_config import app_config

# 强依赖：(显示名, host, port)
TARGETS: list[tuple[str, str, int]] = [
    ("MySQL", app_config.db_app.host, app_config.db_app.port),
    ("Qdrant", app_config.qdrant.host, app_config.qdrant.port),
    ("Elasticsearch", app_config.es.host, app_config.es.port),
    ("Embedding", app_config.embedding.host, app_config.embedding.port),
]


def _reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_all(timeout: int = 180, interval: float = 2.0) -> bool:
    deadline = time.time() + timeout
    pending = list(TARGETS)

    while pending and time.time() < deadline:
        still_pending = []
        for name, host, port in pending:
            if _reachable(host, port):
                print(f"  [就绪] {name}  {host}:{port}", flush=True)
            else:
                still_pending.append((name, host, port))
        pending = still_pending
        if pending:
            time.sleep(interval)

    if pending:
        for name, host, port in pending:
            print(f"  [超时] {name}  {host}:{port} 在 {timeout}s 内未就绪", flush=True)
        return False
    return True


def main() -> int:
    timeout = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    print(f"等待强依赖服务就绪（最多 {timeout}s）...", flush=True)
    ok = wait_all(timeout)
    print("依赖就绪，继续初始化。" if ok else "依赖未就绪，初始化中止。", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
