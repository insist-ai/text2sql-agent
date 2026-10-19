from dataclasses import asdict

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct

from app.conf.app_config import app_config
from app.entities.value_info import ValueInfo


class ValueQdrantRepository:
    """字段取值向量检索仓储：为召回融合提供取值"向量路"召回结果。"""
    collection_name: str = 'data-agent-value'

    def __init__(self, client: AsyncQdrantClient):
        self.client = client

    async def ensure_collection(self):
        if not await self.client.collection_exists(self.collection_name):
            await self.client.create_collection(self.collection_name,
                                                vectors_config=VectorParams(size=app_config.qdrant.embedding_size,
                                                                            distance=Distance.COSINE))

    async def upsert(self, ids: list[str], embeddings: list[list[float]], payloads: list[ValueInfo],
                     batch_size: int = 20):
        zipped = list(zip(ids, embeddings, payloads))
        for i in range(0, len(zipped), batch_size):
            batch = zipped[i:i + batch_size]
            batch_points = [PointStruct(id=id, vector=embedding, payload=asdict(payload)) for id, embedding, payload in
                            batch]
            await self.client.upsert(collection_name=self.collection_name, points=batch_points)

    async def search(self, embedding: list[float], limit: int = 20,
                     threshold: float | None = None) -> list[ValueInfo]:
        # threshold=None 时不传 score_threshold（等价不过滤），保持与改造前默认行为可切换
        query_kwargs = dict(collection_name=self.collection_name, query=embedding, limit=limit)
        if threshold is not None:
            query_kwargs["score_threshold"] = threshold
        res = await self.client.query_points(**query_kwargs)
        return [ValueInfo(**point.payload) for point in res.points]
