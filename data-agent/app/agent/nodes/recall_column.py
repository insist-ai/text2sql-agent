from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.llm import llm
from app.agent.state import DataAgentState
from app.conf.app_config import app_config
from app.core.log import logger
from app.entities.column_info import ColumnInfo
from app.prompt.prompt_loader import load_prompt


async def recall_column(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    writer = runtime.stream_writer
    writer({"type": "progress", "step": "召回字段", "status": "running"})

    query = state.get("rewritten_query") or state["query"]
    keywords = state["keywords"]
    recall_top_k = app_config.agent.recall_top_k

    embedding_client = runtime.context["embedding_client"]
    column_qdrant_repository = runtime.context["column_qdrant_repository"]
    column_es_repository = runtime.context["column_es_repository"]

    try:
        # 使用LLM扩展关键词
        prompt = PromptTemplate(
            template=load_prompt("extend_keywords_for_column_recall"),
            input_variables=["query"],
        )
        output_parser = JsonOutputParser()

        chain = prompt | llm | output_parser

        result = await chain.ainvoke({"query": query})

        all_keywords = list(set(keywords + result))
        logger.info(f"召回字段信息扩展关键词：{all_keywords}")

        # 向量路：Qdrant 语义召回（列表顺序即原始排名，供 RRF 使用）
        # qdrant_min_score 相似度下限过滤低相关噪声；0 视为不过滤（不向 query_points 传 score_threshold）
        qdrant_min_score = app_config.agent.qdrant_min_score or None
        vector_map: dict[str, ColumnInfo] = {}
        for keyword in all_keywords:
            embedding = await embedding_client.aembed_query(keyword)
            payloads: list[ColumnInfo] = await column_qdrant_repository.search(
                embedding, threshold=qdrant_min_score)
            for payload in payloads:
                vector_map.setdefault(payload.id, payload)
        vector_columns = list(vector_map.values())[:recall_top_k]

        # 关键词路：ES 全文召回（列表顺序即原始排名，供 RRF 使用）
        # es_min_score 相关性下限过滤低相关噪声；0 视为不过滤（退化为改造前默认行为）
        es_min_score = app_config.agent.es_min_score or None
        keyword_map: dict[str, ColumnInfo] = {}
        for keyword in all_keywords:
            payloads: list[ColumnInfo] = await column_es_repository.search(keyword, min_score=es_min_score)
            for payload in payloads:
                keyword_map.setdefault(payload.id, payload)
        keyword_filtered = list(keyword_map.values())
        keyword_columns = keyword_filtered[:recall_top_k]

        writer({"type": "progress", "step": "召回字段", "status": "success"})
        logger.info(
            f"召回字段：向量路 {len(vector_columns)} 条"
            f"(qdrant_min_score={app_config.agent.qdrant_min_score})，"
            f"关键词路 {len(keyword_columns)} 条 "
            f"(es_min_score={app_config.agent.es_min_score}, 过滤后 {len(keyword_filtered)} 条, recall_top_k={recall_top_k})")
        return {"vector_columns": vector_columns, "keyword_columns": keyword_columns}
    except Exception as e:
        writer({"type": "progress", "step": "召回字段", "status": "error"})
        logger.error(f"召回字段信息失败: {str(e)}")
        raise
