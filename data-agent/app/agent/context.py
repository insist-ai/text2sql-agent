from typing import TypedDict

from app.clients.embedding_client_manager import CustomEmbeddings
from app.repositories.es.column_es_repository import ColumnESRepository
from app.repositories.es.metric_es_repository import MetricESRepository
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository
from app.repositories.qdrant.value_qdrant_repository import ValueQdrantRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository

class DataAgentContext(TypedDict):
    column_qdrant_repository: ColumnQdrantRepository
    embedding_client: CustomEmbeddings
    metric_qdrant_repository: MetricQdrantRepository
    value_qdrant_repository: ValueQdrantRepository
    column_es_repository: ColumnESRepository
    metric_es_repository: MetricESRepository
    value_es_repository: ValueESRepository
    meta_mysql_repository: MetaMySQLRepository
    dw_mysql_repository: DWMySQLRepository

