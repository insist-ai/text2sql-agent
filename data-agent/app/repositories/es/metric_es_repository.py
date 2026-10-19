from dataclasses import asdict

from elasticsearch import AsyncElasticsearch

from app.core.log import logger
from app.entities.metric_info import MetricInfo


class MetricESRepository:
    """指标全文检索仓储：为召回融合提供指标"关键词路"召回结果。

    _source 存储 MetricInfo 全量字段(便于还原实体)，仅对 name/alias/description
    建立中文分词(ik)的全文索引；按 name > alias > description 加权匹配。
    """
    index_name = 'data-agent-metric'
    index_mappings = {
        "dynamic": False,
        "properties": {
            "id": {"type": "keyword"},
            "name": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_max_word"},
            "alias": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_max_word"},
            "description": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_max_word"},
        }
    }

    def __init__(self, client: AsyncElasticsearch):
        self.client = client

    async def ensure_index(self):
        if not await self.client.indices.exists(index=self.index_name):
            await self.client.indices.create(index=self.index_name, mappings=self.index_mappings)

    async def index(self, metric_infos: list[MetricInfo], batch_size=20):
        for i in range(0, len(metric_infos), batch_size):
            batch = metric_infos[i:i + batch_size]
            operations = []
            for metric_info in batch:
                operations.append(
                    {
                        "index": {"_index": self.index_name,
                                  "_id": metric_info.id}
                    }
                )
                operations.append(asdict(metric_info))
            await self.client.bulk(operations=operations)

    async def search(self, keyword: str, limit: int = 20,
                     min_score: float | None = None) -> list[MetricInfo]:
        # 仅当显式传入 min_score 时才下发相关性过滤（None 时与改造前默认行为完全一致）
        extra = {"min_score": min_score} if min_score is not None else {}
        resp = await self.client.search(
            index=self.index_name,
            query={
                "multi_match": {
                    "query": keyword,
                    "fields": ["name^3", "alias^2", "description"]
                }
            },
            size=limit,
            **extra,
        )
        hits = resp["hits"]["hits"]
        if min_score is not None and hits:
            logger.debug(f"ES[{self.index_name}] '{keyword}' min_score={min_score} "
                         f"保留 {len(hits)} 条，分数 {hits[0]['_score']:.2f}~{hits[-1]['_score']:.2f}")
        return [MetricInfo(**hit["_source"]) for hit in hits]
