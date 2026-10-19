import asyncio

from app.agent.context import DataAgentContext
from app.agent.nodes.correct_sql import correct_sql
from app.agent.nodes.add_extra_context import add_extra_context
from app.agent.nodes.extract_keywords import extract_keywords
from app.agent.nodes.filter_metric import filter_metric
from app.agent.nodes.filter_table import filter_table
from app.agent.nodes.fuse_recall import fuse_recall
from app.agent.nodes.generate_sql import generate_sql
from app.agent.nodes.merge_retrieved_info import merge_retrieved_info
from app.agent.nodes.recall_column import recall_column
from app.agent.nodes.recall_metric import recall_metric
from app.agent.nodes.recall_value import recall_value
from app.agent.nodes.rewrite_query import rewrite_query
from app.agent.nodes.run_sql import run_sql
from app.agent.nodes.validate_sql import validate_sql
from app.agent.state import DataAgentState
from langgraph.graph import StateGraph
from langgraph.graph import START, END

from app.clients.embedding_client_manager import embedding_client_manager
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import meta_mysql_client_manager, dw_mysql_client_manager
from app.clients.qdrant_client_manager import qdrant_client_manager
from app.repositories.es.column_es_repository import ColumnESRepository
from app.repositories.es.metric_es_repository import MetricESRepository
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository
from app.repositories.qdrant.value_qdrant_repository import ValueQdrantRepository

graph_builder = StateGraph(state_schema=DataAgentState,context_schema=DataAgentContext)
graph_builder.add_node("rewrite_query", rewrite_query)
graph_builder.add_node("extract_keywords", extract_keywords)
graph_builder.add_node("recall_column", recall_column)
graph_builder.add_node("recall_value", recall_value)
graph_builder.add_node("recall_metric", recall_metric)
graph_builder.add_node("fuse_recall", fuse_recall)
graph_builder.add_node("merge_retrieved_info", merge_retrieved_info)
graph_builder.add_node("filter_metric", filter_metric)
graph_builder.add_node("filter_table", filter_table)
graph_builder.add_node("add_extra_context", add_extra_context)
graph_builder.add_node("generate_sql", generate_sql)
graph_builder.add_node("validate_sql", validate_sql)
graph_builder.add_node("correct_sql", correct_sql)
graph_builder.add_node("run_sql", run_sql)

graph_builder.add_edge(START, "rewrite_query")
graph_builder.add_edge("rewrite_query", "extract_keywords")
graph_builder.add_edge("extract_keywords", "recall_column")
graph_builder.add_edge("extract_keywords", "recall_value")
graph_builder.add_edge("extract_keywords", "recall_metric")
graph_builder.add_edge("recall_column", "fuse_recall")
graph_builder.add_edge("recall_value", "fuse_recall")
graph_builder.add_edge("recall_metric", "fuse_recall")
graph_builder.add_edge("fuse_recall", "merge_retrieved_info")
graph_builder.add_edge("merge_retrieved_info", "filter_table")
graph_builder.add_edge("merge_retrieved_info", "filter_metric")
graph_builder.add_edge("filter_table", "add_extra_context")
graph_builder.add_edge("filter_metric", "add_extra_context")
graph_builder.add_edge("add_extra_context", "generate_sql")

# ===== SQL 自省闭环路由 =====
def route_after_generate(state: DataAgentState):
    """拒答→直接结束；否则进入 SQL 校验。"""
    return END if state.get("rejected") else "validate_sql"


def route_after_validate(state: DataAgentState):
    """拒答→结束；校验通过→执行；校验失败→纠错。"""
    if state.get("rejected"):
        return END
    return "run_sql" if state.get("error") is None else "correct_sql"


def route_after_run(state: DataAgentState):
    """执行成功→结束；执行失败→纠错（是否超限由 correct_sql 内部判定）。"""
    return END if state.get("error") is None else "correct_sql"


def route_after_correct(state: DataAgentState):
    """拒答→结束；纠错成功(error=None)→重新校验；纠错放弃/失败(error 非空)→结束。"""
    if state.get("rejected"):
        return END
    return "validate_sql" if state.get("error") is None else END


graph_builder.add_conditional_edges(source="generate_sql",
                                    path=route_after_generate,
                                    path_map={END: END, "validate_sql": "validate_sql"})
graph_builder.add_conditional_edges(source="validate_sql",
                                    path=route_after_validate,
                                    path_map={"run_sql": "run_sql", "correct_sql": "correct_sql", END: END})
graph_builder.add_conditional_edges(source="correct_sql",
                                    path=route_after_correct,
                                    path_map={"validate_sql": "validate_sql", END: END})
graph_builder.add_conditional_edges(source="run_sql",
                                    path=route_after_run,
                                    path_map={END: END, "correct_sql": "correct_sql"})

graph = graph_builder.compile()

# print(graph.get_graph().draw_mermaid())
if __name__ == "__main__":
    async def text():
        qdrant_client_manager.init()
        embedding_client_manager.init()
        es_client_manager.init()
        meta_mysql_client_manager.init()
        dw_mysql_client_manager.init()
        async with (meta_mysql_client_manager.session_factory() as meta_session,
                    dw_mysql_client_manager.session_factory() as dw_session):
            meta_mysql_repository = MetaMySQLRepository(meta_session)
            dw_mysql_repository = DWMySQLRepository(dw_session)
            value_es_repository = ValueESRepository(es_client_manager.client)
            column_es_repository = ColumnESRepository(es_client_manager.client)
            metric_es_repository = MetricESRepository(es_client_manager.client)
            column_qdrant_repository = ColumnQdrantRepository(qdrant_client_manager.client)
            metric_qdrant_repository = MetricQdrantRepository(qdrant_client_manager.client)
            value_qdrant_repository = ValueQdrantRepository(qdrant_client_manager.client)
            context = DataAgentContext(column_qdrant_repository=column_qdrant_repository,
                                       metric_qdrant_repository=metric_qdrant_repository,
                                       value_qdrant_repository=value_qdrant_repository,
                                       embedding_client=embedding_client_manager.client,
                                       value_es_repository=value_es_repository,
                                       column_es_repository=column_es_repository,
                                       metric_es_repository=metric_es_repository,
                                       meta_mysql_repository=meta_mysql_repository,
                                       dw_mysql_repository=dw_mysql_repository)

            state = DataAgentState(query="统计华北地区的销售总额")
            async for state in graph.astream(input=state, context=context, stream_mode="custom"):
                print(state)
        await qdrant_client_manager.close()
        await es_client_manager.close()
        await meta_mysql_client_manager.close()
        await dw_mysql_client_manager.close()


    asyncio.run(text())
