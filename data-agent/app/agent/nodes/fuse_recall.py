from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState
from app.conf.app_config import app_config
from app.core.log import logger


def rrf_fuse(ranked_lists: list[list], key_func, k: int, top_n: int) -> list:
    """Reciprocal Rank Fusion：score = Σ 1/(k + rank_i)。

    - 每路结果按列表顺序即原始排名(rank 从 1 开始)参与计算；
    - 同一条目出现在多路时分数累加，key_func 保证跨路去重；
    - 按分数降序取 Top-N，同分时保持首次出现顺序(sorted 稳定性)。
    """
    scores: dict = {}
    items: dict = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            key = key_func(item)
            if key not in items:
                items[key] = item
                scores[key] = 0.0
            scores[key] += 1.0 / (k + rank)
    ordered_keys = sorted(scores, key=scores.get, reverse=True)
    return [items[key] for key in ordered_keys[:top_n]]


def union_dedup(list_a: list, list_b: list, key_func, top_n: int | None = None) -> list:
    """RRF 关闭时的退化融合（消融 baseline）：两路全量拼接并按 key 去重，保持出现顺序，
    可选截断 Top-N。与 scripts/rrf_fuse_ablation.py 的"融合前(旧行为)"一致。"""
    seen: dict = {}
    for item in list_a + list_b:
        seen.setdefault(key_func(item), item)
    items = list(seen.values())
    return items if top_n is None else items[:top_n]


async def fuse_recall(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """召回融合节点：对字段/指标/取值三类的"向量+关键词"双路召回结果做 RRF 融合。

    融合结果写入原有的 retrieved_columns/retrieved_metrics/retrieved_values，
    下游 merge_retrieved_info 无需感知双路结构。
    agent.rrf_enable=False 时退化为"两路并集去重后截断 Top-N"（消融实验 baseline）。
    """
    writer = runtime.stream_writer
    writer({"type": "progress", "step": "召回融合", "status": "running"})

    try:
        rrf_enable = app_config.agent.rrf_enable
        k = app_config.agent.rrf_k
        top_n = app_config.agent.fuse_top_n

        vector_columns = state.get("vector_columns") or []
        keyword_columns = state.get("keyword_columns") or []
        vector_metrics = state.get("vector_metrics") or []
        keyword_metrics = state.get("keyword_metrics") or []
        vector_values = state.get("vector_values") or []
        keyword_values = state.get("keyword_values") or []

        if rrf_enable:
            # 去重键：字段按 表名.字段名(id)，指标按 指标名(id)，取值按 列.取值(id)
            fused_columns = rrf_fuse([vector_columns, keyword_columns], lambda c: c.id, k, top_n)
            fused_metrics = rrf_fuse([vector_metrics, keyword_metrics], lambda m: m.id, k, top_n)
            fused_values = rrf_fuse([vector_values, keyword_values], lambda v: v.id, k, top_n)
        else:
            fused_columns = union_dedup(vector_columns, keyword_columns, lambda c: c.id, top_n)
            fused_metrics = union_dedup(vector_metrics, keyword_metrics, lambda m: m.id, top_n)
            fused_values = union_dedup(vector_values, keyword_values, lambda v: v.id, top_n)

        writer({"type": "progress", "step": "召回融合", "status": "success"})
        if rrf_enable:
            logger.info(
                f"召回融合(RRF k={k}, top_n={top_n}): "
                f"字段 向量{len(vector_columns)}+关键词{len(keyword_columns)}→保留{len(fused_columns)} 条, "
                f"指标 向量{len(vector_metrics)}+关键词{len(keyword_metrics)}→保留{len(fused_metrics)} 条, "
                f"取值 向量{len(vector_values)}+关键词{len(keyword_values)}→保留{len(fused_values)} 条"
            )
        else:
            logger.info(
                f"召回融合(RRF 已关闭, 并集去重 Top-{top_n}): "
                f"字段 向量{len(vector_columns)}+关键词{len(keyword_columns)}→保留{len(fused_columns)} 条, "
                f"指标 向量{len(vector_metrics)}+关键词{len(keyword_metrics)}→保留{len(fused_metrics)} 条, "
                f"取值 向量{len(vector_values)}+关键词{len(keyword_values)}→保留{len(fused_values)} 条"
            )
        return {
            "retrieved_columns": fused_columns,
            "retrieved_metrics": fused_metrics,
            "retrieved_values": fused_values,
        }
    except Exception as e:
        writer({"type": "progress", "step": "召回融合", "status": "error"})
        logger.error(f"召回融合失败: {str(e)}")
        raise
