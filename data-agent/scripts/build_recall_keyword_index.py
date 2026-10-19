"""为"向量+关键词"双路召回补齐关键词侧索引（幂等，可重复执行）。

- 字段文档 → ES 全文索引 data-agent-column（数据源：meta 库 column_info）
- 指标文档 → ES 全文索引 data-agent-metric（数据源：meta 库 metric_info）
- 字段取值 → Qdrant 向量集合 data-agent-value（数据源：ES 已有取值文档）

说明：完整知识库构建入口仍为 app/scripts/build_meta_knowledge.py（已同步接入新索引）；
本脚本用于在不动 Qdrant 既有向量集（重建会产生重复点）的前提下增量补齐新索引。

用法：
    d:/Work-Data-Agent/data-agent/.venv/Scripts/python.exe scripts/build_recall_keyword_index.py
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients.embedding_client_manager import embedding_client_manager
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import meta_mysql_client_manager
from app.clients.qdrant_client_manager import qdrant_client_manager
from app.entities.value_info import ValueInfo
from app.repositories.es.column_es_repository import ColumnESRepository
from app.repositories.es.metric_es_repository import MetricESRepository
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.value_qdrant_repository import ValueQdrantRepository

EMBED_BATCH_SIZE = 10


async def main() -> int:
    meta_mysql_client_manager.init()
    qdrant_client_manager.init()
    embedding_client_manager.init()
    es_client_manager.init()

    column_es_repository = ColumnESRepository(es_client_manager.client)
    metric_es_repository = MetricESRepository(es_client_manager.client)
    value_es_repository = ValueESRepository(es_client_manager.client)
    value_qdrant_repository = ValueQdrantRepository(qdrant_client_manager.client)

    # 1. 字段文档 → ES 全文索引
    async with meta_mysql_client_manager.session_factory() as meta_session:
        meta_mysql_repository = MetaMySQLRepository(meta_session)

        column_infos = await meta_mysql_repository.list_column_infos()
        await column_es_repository.ensure_index()
        await column_es_repository.index(column_infos)
        print(f"[ES] 字段全文索引 {column_es_repository.index_name}: {len(column_infos)} 条")

        # 2. 指标文档 → ES 全文索引
        metric_infos = await meta_mysql_repository.list_metric_infos()
        await metric_es_repository.ensure_index()
        await metric_es_repository.index(metric_infos)
        print(f"[ES] 指标全文索引 {metric_es_repository.index_name}: {len(metric_infos)} 条")

    # 3. 字段取值 → Qdrant 向量集合（数据源：ES 取值索引，uuid5 幂等）
    resp = await es_client_manager.client.search(
        index=value_es_repository.index_name,
        query={"match_all": {}},
        size=10000,
    )
    value_infos = [ValueInfo(**hit["_source"]) for hit in resp["hits"]["hits"]]
    await value_qdrant_repository.ensure_collection()
    ids = [uuid.uuid5(uuid.NAMESPACE_URL, value_info.id) for value_info in value_infos]
    texts = [value_info.value for value_info in value_infos]
    embeddings = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        embeddings.extend(
            await embedding_client_manager.client.aembed_documents(texts[i:i + EMBED_BATCH_SIZE])
        )
    await value_qdrant_repository.upsert(ids, embeddings, value_infos)
    print(f"[Qdrant] 取值向量集合 {value_qdrant_repository.collection_name}: {len(value_infos)} 条")

    await meta_mysql_client_manager.close()
    await qdrant_client_manager.close()
    await es_client_manager.close()
    print("关键词侧索引构建完成。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
