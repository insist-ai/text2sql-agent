from typing import Optional

from langchain_core.embeddings import Embeddings
from huggingface_hub import InferenceClient, AsyncInferenceClient


class CustomEmbeddings(Embeddings):
    def __init__(self, api_url: str, model_name: str):
        self.client = InferenceClient(model=api_url)
        self.async_client = AsyncInferenceClient(model=api_url)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.client.feature_extraction(texts)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self.async_client.feature_extraction(texts)

    def embed_query(self, text: str) -> list[float]:
        return self.client.feature_extraction([text])[0]

    async def aembed_query(self, text: str) -> list[float]:
        result = await self.async_client.feature_extraction([text])
        return result[0]


from app.conf.app_config import EmbeddingConfig, app_config


class EmbeddingClientManager:
    def __init__(self, config: EmbeddingConfig):
        self.client: Optional[CustomEmbeddings] = None
        self.config: EmbeddingConfig = config

    def _get_url(self):
        return f"http://{self.config.host}:{self.config.port}"

    def init(self):
        self.client = CustomEmbeddings(
            api_url=self._get_url(),
            model_name=self.config.model
        )


embedding_client_manager = EmbeddingClientManager(app_config.embedding)
