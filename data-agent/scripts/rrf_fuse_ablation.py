"""RRF 召回融合消融实验 —— 论文调参素材。

对同一问题运行真实召回节点（extract_keywords + recall_column/metric/value 双路召回），
然后：
  1. 打印"融合前后"召回列表对比（融合前=两路全量并集(旧行为)，融合后=RRF Top-N）；
  2. 对同一批召回结果分别用 k=60 与 k=10 计算 RRF，对比排序差异。

用法：
    d:/Work-Data-Agent/data-agent/.venv/Scripts/python.exe scripts/rrf_fuse_ablation.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.graph import END, START, StateGraph

from app.agent.context import DataAgentContext
from app.agent.nodes.extract_keywords import extract_keywords
from app.agent.nodes.fuse_recall import rrf_fuse
from app.agent.nodes.recall_column import recall_column
from app.agent.nodes.recall_metric import recall_metric
from app.agent.nodes.recall_value import recall_value
from app.agent.state import DataAgentState
from app.clients.embedding_client_manager import embedding_client_manager
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import dw_mysql_client_manager, meta_mysql_client_manager
from app.clients.qdrant_client_manager import qdrant_client_manager
from app.conf.app_config import app_config
from app.repositories.es.column_es_repository import ColumnESRepository
from app.repositories.es.metric_es_repository import MetricESRepository
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository
from app.repositories.qdrant.value_qdrant_repository import ValueQdrantRepository

QUERY = "统计华东地区的销售总额"
K_LIST = [60, 10]
SWEEP_K_LIST = [1, 10, 20, 60, 100]  # k 扫描模式：与 k=60 基准对比排序差异
MAX_ROUNDS = 3  # LLM 网关偶发 500，整轮重试上限


def fmt_column(c) -> str:
    return f"{c.table_id}.{c.name}"


def fmt_metric(m) -> str:
    return m.name


def fmt_value(v) -> str:
    return f"{v.value}({v.column_id})"


def print_list(title: str, items, fmt) -> None:
    print(f"  {title} ({len(items)} 条):")
    for i, item in enumerate(items, start=1):
        print(f"    {i}. {fmt(item)}")
    if not items:
        print("    (空)")


def union_dedup(list_a: list, list_b: list, key_func) -> list:
    """融合前(旧行为)：两路全量拼接并按 key 去重，保持出现顺序。"""
    seen: dict = {}
    for item in list_a + list_b:
        seen.setdefault(key_func(item), item)
    return list(seen.values())


def rrf_scores(ranked_lists: list[list], key_func, k: int) -> dict:
    """计算每条的 RRF 分数（不截断），用于观察 k 值对分数分布的影响。"""
    scores: dict = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            key = key_func(item)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return scores


async def run_recall(context: DataAgentContext) -> dict:
    """用真实节点构建子图跑召回阶段，返回各路召回结果。"""
    builder = StateGraph(state_schema=DataAgentState, context_schema=DataAgentContext)
    builder.add_node("extract_keywords", extract_keywords)
    builder.add_node("recall_column", recall_column)
    builder.add_node("recall_metric", recall_metric)
    builder.add_node("recall_value", recall_value)
    builder.add_edge(START, "extract_keywords")
    builder.add_edge("extract_keywords", "recall_column")
    builder.add_edge("extract_keywords", "recall_metric")
    builder.add_edge("extract_keywords", "recall_value")
    builder.add_edge("recall_column", END)
    builder.add_edge("recall_metric", END)
    builder.add_edge("recall_value", END)
    mini_graph = builder.compile()

    final_state: dict | None = None
    async for mode, payload in mini_graph.astream(
            input={"query": QUERY, "rewritten_query": QUERY},
            context=context,
            stream_mode=["custom", "values"]):
        if mode == "values":
            final_state = payload
    return final_state


async def main() -> int:
    print(f"== RRF 召回融合消融实验  query={QUERY!r} ==")
    print(f"配置: rrf_k={app_config.agent.rrf_k}, recall_top_k={app_config.agent.recall_top_k}, "
          f"fuse_top_n={app_config.agent.fuse_top_n}")

    qdrant_client_manager.init()
    embedding_client_manager.init()
    es_client_manager.init()
    meta_mysql_client_manager.init()
    dw_mysql_client_manager.init()

    async with (meta_mysql_client_manager.session_factory() as meta_session,
                dw_mysql_client_manager.session_factory() as dw_session):
        context = DataAgentContext(
            embedding_client=embedding_client_manager.client,
            column_qdrant_repository=ColumnQdrantRepository(qdrant_client_manager.client),
            metric_qdrant_repository=MetricQdrantRepository(qdrant_client_manager.client),
            value_qdrant_repository=ValueQdrantRepository(qdrant_client_manager.client),
            column_es_repository=ColumnESRepository(es_client_manager.client),
            metric_es_repository=MetricESRepository(es_client_manager.client),
            value_es_repository=ValueESRepository(es_client_manager.client),
            meta_mysql_repository=MetaMySQLRepository(meta_session),
            dw_mysql_repository=DWMySQLRepository(dw_session),
        )

        state = None
        for attempt in range(1, MAX_ROUNDS + 1):
            print(f"\n---- 召回执行 第 {attempt}/{MAX_ROUNDS} 轮 ----")
            try:
                state = await run_recall(context)
                break
            except Exception as exc:
                print(f"[RETRY] 本轮失败({exc})")
        if state is None:
            print("[FAIL] 连续多轮失败(多为 LLM 网关 500)，请稍后重试。")
            return 1

    vc, kc = state.get("vector_columns") or [], state.get("keyword_columns") or []
    vm, km = state.get("vector_metrics") or [], state.get("keyword_metrics") or []
    vv, kv = state.get("vector_values") or [], state.get("keyword_values") or []
    top_n = app_config.agent.fuse_top_n

    print("\n========== 每路召回结果(顺序即排名) ==========")
    print_list("字段-向量路(Qdrant)", vc, fmt_column)
    print_list("字段-关键词路(ES)", kc, fmt_column)
    print_list("指标-向量路(Qdrant)", vm, fmt_metric)
    print_list("指标-关键词路(ES)", km, fmt_metric)
    print_list("取值-向量路(Qdrant)", vv, fmt_value)
    print_list("取值-关键词路(ES)", kv, fmt_value)
    print(f"\n  候选规模汇总(recall_top_k={app_config.agent.recall_top_k}): "
          f"字段 向量{len(vc)}/关键词{len(kc)} | 指标 向量{len(vm)}/关键词{len(km)} | "
          f"取值 向量{len(vv)}/关键词{len(kv)}")

    print("\n========== 融合前后对比 ==========")
    for name, la, lb, key, fmt in [
        ("字段", vc, kc, lambda c: c.id, fmt_column),
        ("指标", vm, km, lambda m: m.id, fmt_metric),
        ("取值", vv, kv, lambda v: v.id, fmt_value),
    ]:
        before = union_dedup(la, lb, key)
        after = rrf_fuse([la, lb], key, app_config.agent.rrf_k, top_n)
        print(f"\n[{name}] 融合前(两路并集): {len(before)} 条 -> 融合后(RRF k={app_config.agent.rrf_k}, Top-{top_n}): {len(after)} 条")
        print_list("融合后列表", after, fmt)

    print("\n========== k 值调参对比 (同一批召回，k=60 vs k=10) ==========")
    for name, la, lb, key, fmt in [
        ("字段", vc, kc, lambda c: c.id, fmt_column),
        ("指标", vm, km, lambda m: m.id, fmt_metric),
        ("取值", vv, kv, lambda v: v.id, fmt_value),
    ]:
        results = {k: rrf_fuse([la, lb], key, k, top_n) for k in K_LIST}
        for k in K_LIST:
            print(f"\n[{name}] k={k}:")
            print_list(f"RRF Top-{top_n}", results[k], fmt)
        keys = {k: [key(it) for it in results[k]] for k in K_LIST}
        common = [x for x in keys[K_LIST[0]] if x in keys[K_LIST[1]]]
        only60 = [x for x in keys[K_LIST[0]] if x not in keys[K_LIST[1]]]
        only10 = [x for x in keys[K_LIST[1]] if x not in keys[K_LIST[0]]]
        moved = sum(1 for i, x in enumerate(common) if x != keys[K_LIST[1]][i])
        print(f"\n[{name}] 差异统计: 两 k 值共有 {len(common)} 条; 仅 k=60 入选 {len(only60)} 条; "
              f"仅 k=10 入选 {len(only10)} 条; 共有条目中顺序变化 {moved} 条")

    print("\n========== RRF 分数明细(字段) ==========")
    print("  说明：k 越小头部名次权重越大(接近'最优名次'导向)，k 越大越接近'名次之和'平均导向。")
    sc60 = rrf_scores([vc, kc], lambda c: c.id, 60)
    sc10 = rrf_scores([vc, kc], lambda c: c.id, 10)
    print(f"  {'条目':<32s} {'k=60 分数':>12s} {'k=10 分数':>12s} {'路命中':>8s}")
    for key in sorted(sc60, key=sc60.get, reverse=True):
        item = next(c for c in vc + kc if c.id == key)
        in_vec, in_kw = any(c.id == key for c in vc), any(c.id == key for c in kc)
        paths = "双路" if in_vec and in_kw else ("向量" if in_vec else "关键词")
        print(f"  {fmt_column(item):<32s} {sc60[key]:12.5f} {sc10[key]:12.5f} {paths:>8s}")

    # ---------- k 扫描模式（只做加法：不改变上方既有输出与融合算法） ----------
    # 对同一批双路结果用不同 k 重新融合，观察 k 对 Top-N 排序的影响，并导出 CSV 供论文画图。
    print(f"\n========== k 扫描 (字段双路融合, k ∈ {SWEEP_K_LIST}, Top-{top_n}) ==========")
    ckey = lambda c: c.id
    base_order = [ckey(it) for it in rrf_fuse([vc, kc], ckey, 60, top_n)]
    csv_path = Path(__file__).resolve().parents[1] / "docs" / "eval" / "rrf_k_sweep.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_lines = ["k,rank,entity,score"]
    for k in SWEEP_K_LIST:
        fused = rrf_fuse([vc, kc], ckey, k, top_n)
        order = [ckey(it) for it in fused]
        score_map = rrf_scores([vc, kc], ckey, k)
        diff = sum(1 for a, b in zip(order, base_order) if a != b) + abs(len(order) - len(base_order))
        first_same = "是" if order and base_order and order[0] == base_order[0] else "否"
        seq = " > ".join(fmt_column(next(c for c in vc + kc if c.id == x)) for x in order) or "(空)"
        print(f"\n  k={k:<4} 首位相同: {first_same} | 与 k=60 顺序差异条目数: {diff}")
        print(f"        Top-{top_n} 顺序: {seq}")
        for rank, it in enumerate(fused, start=1):
            csv_lines.append(f"{k},{rank},{ckey(it)},{score_map.get(ckey(it), 0.0):.5f}")
    csv_path.write_text("\n".join(csv_lines) + "\n", encoding="utf-8")
    print(f"\n  CSV 已导出: {csv_path} (列: k,rank,entity,score; 内容=字段双路融合 Top-{top_n})")

    await qdrant_client_manager.close()
    await es_client_manager.close()
    await meta_mysql_client_manager.close()
    await dw_mysql_client_manager.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
