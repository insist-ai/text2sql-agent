"""Self-Consistency 开/关消融实验 —— 论文素材。

对同一问题分别在 self_consistency.enable=false / true 下各跑一次 generate_sql 节点：
  - OFF：temperature=0 单次生成（基线）；
  - ON ：并行采样 N 份候选 → 归一化多数投票 → 执行结果一致性 → t0 回退。
输出每份候选 SQL、投票结果、最终选择与耗时对比。

用法：
    d:/Work-Data-Agent/data-agent/.venv/Scripts/python.exe scripts/self_consistency_ablation.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.graph import END, START, StateGraph

from app.agent.context import DataAgentContext
from app.agent.nodes.generate_sql import generate_sql
from app.agent.state import DataAgentState
from app.clients.mysql_client_manager import dw_mysql_client_manager
from app.conf.app_config import app_config
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository

QUERY = "统计华东地区的销售总额"
MAX_ROUNDS = 3  # LLM 网关间歇故障，每侧最多整轮重试次数

TABLE_INFOS = [
    {
        "name": "fact_order",
        "role": "fact",
        "description": "订单事实表，记录订单数量和金额等核心指标。",
        "columns": [
            {"name": "order_id", "type": "bigint", "role": "primary_key", "examples": [1],
             "description": "订单唯一标识。", "alias": ["订单ID"]},
            {"name": "order_amount", "type": "decimal", "role": "measure", "examples": [199.5],
             "description": "订单金额。", "alias": ["销售额", "订单金额", "收入"]},
            {"name": "region_id", "type": "bigint", "role": "foreign_key", "examples": [1],
             "description": "关联地区维度的外键。", "alias": ["地区ID"]},
        ],
    },
    {
        "name": "dim_region",
        "role": "dim",
        "description": "地区维度表，用于描述订单发生的地理区域信息。",
        "columns": [
            {"name": "region_id", "type": "bigint", "role": "primary_key", "examples": [1],
             "description": "地区唯一标识。", "alias": ["地区ID"]},
            {"name": "region_name", "type": "varchar", "role": "dimension", "examples": ["华东"],
             "description": "订单所属的大区名称，如华东、华南等。", "alias": ["地区", "大区"]},
        ],
    },
]
METRIC_INFOS = [
    {"name": "GMV", "description": "所有订单的成交金额总和。",
     "relevant_columns": ["fact_order.order_amount"], "alias": ["成交总额", "订单总额"]},
]
DATE_INFO = {"date": "2026-10-11", "weekday": "星期日", "quarter": "Q4"}


def build_generate_graph():
    builder = StateGraph(state_schema=DataAgentState, context_schema=DataAgentContext)
    builder.add_node("generate_sql", generate_sql)
    builder.add_edge(START, "generate_sql")
    builder.add_edge("generate_sql", END)
    return builder.compile()


async def run_once(context: dict, db_info: dict) -> tuple[dict, dict, int]:
    """跑一次生成节点，返回 (final_state, sc_event, elapsed_ms)。"""
    state = {
        "query": QUERY, "rewritten_query": QUERY,
        "table_infos": TABLE_INFOS, "metric_infos": METRIC_INFOS,
        "date_info": DATE_INFO, "db_info": db_info, "history": [],
    }
    final_state, sc_event, t0 = None, None, time.monotonic()
    async for mode, payload in build_generate_graph().astream(
            input=state, context=context, stream_mode=["custom", "values"]):
        if mode == "custom" and isinstance(payload, dict) and payload.get("type") == "self_consistency":
            sc_event = payload
        elif mode == "values":
            final_state = payload
    return final_state, sc_event, int((time.monotonic() - t0) * 1000)


async def main() -> int:
    print(f"== Self-Consistency 消融实验  query={QUERY!r} ==")
    print(f"配置: n_samples={app_config.self_consistency.n_samples}, "
          f"temperature={app_config.self_consistency.temperature}, "
          f"max_concurrency={app_config.self_consistency.max_concurrency}")

    dw_mysql_client_manager.init()
    try:
        async with dw_mysql_client_manager.session_factory() as dw_session:
            db_info = await DWMySQLRepository(dw_session).get_db_info()
            context = DataAgentContext(dw_mysql_repository=DWMySQLRepository(dw_session))

            # ---------- OFF：基线单次生成 ----------
            app_config.self_consistency.enable = False
            off_state, off_elapsed = None, None
            for attempt in range(1, MAX_ROUNDS + 1):
                print(f"\n---- [OFF 单次生成] 第 {attempt}/{MAX_ROUNDS} 轮 ----")
                try:
                    off_state, _, off_elapsed = await run_once(context, db_info)
                    break
                except Exception as exc:
                    print(f"[RETRY] {type(exc).__name__}: {exc}")
            if off_state is None:
                print("[FAIL] OFF 基线连续失败(LLM 网关故障)，请稍后重试。")
                return 1

            # ---------- ON：Self-Consistency ----------
            app_config.self_consistency.enable = True
            on_state, on_event, on_elapsed = None, None, None
            for attempt in range(1, MAX_ROUNDS + 1):
                print(f"\n---- [ON Self-Consistency] 第 {attempt}/{MAX_ROUNDS} 轮 ----")
                try:
                    on_state, on_event, on_elapsed = await run_once(context, db_info)
                    break
                except Exception as exc:
                    print(f"[RETRY] {type(exc).__name__}: {exc}")
            if on_state is None:
                print("[FAIL] ON 自洽生成连续失败(LLM 网关故障)，请稍后重试。")
                return 1
    finally:
        app_config.self_consistency.enable = False
        await dw_mysql_client_manager.close()

    # ---------- 结果呈现 ----------
    print("\n" + "=" * 25 + " OFF（enable=false，基线） " + "=" * 25)
    print(f"耗时: {off_elapsed} ms")
    print(f"SQL : {off_state.get('sql')}")

    print("\n" + "=" * 25 + " ON（enable=true，自洽投票） " + "=" * 25)
    samples = on_event.get("samples") if on_event else (on_state.get("sc_samples") or [])
    strategy = on_event.get("strategy") if on_event else (on_state.get("sc_vote_detail") or {}).get("strategy")
    detail = on_event.get("vote_detail") if on_event else (on_state.get("sc_vote_detail") or {}).get("vote_detail")
    print(f"候选 {len(samples)}/{app_config.self_consistency.n_samples} 份:")
    for i, s in enumerate(samples, start=1):
        print(f"  候选{i}: {s}")
    print("\n投票/归一化结果:")
    for item in detail or []:
        if "votes" in item:
            print(f"  {item['votes']} 票: {item['normalized_sql']}")
        elif "cluster_size" in item and "result_preview" in item and "members" not in item:
            print(f"  执行结果等价类 size={item['cluster_size']}, 预览={item['result_preview']}")
        elif "skipped_unexecutable" in item:
            for sk in item["skipped_unexecutable"]:
                print(f"  不可执行跳过: {sk}")
        elif "sc_metrics" in item:
            print(f"  一致性指标: {item['sc_metrics']}")

    # 文本分歧 vs 语义分歧（两级指标）
    text_unique = on_event.get("text_unique_count") if on_event else None
    sem_count = on_event.get("semantic_cluster_count") if on_event else None
    sem_rate = on_event.get("semantic_agreement_rate") if on_event else None
    cluster_mode = on_event.get("cluster_mode") if on_event else None
    sem_clusters = on_event.get("semantic_clusters") if on_event else None
    print("\n文本分歧 vs 语义分歧:")
    print(f"  text_unique_count(归一化文本唯一数)     : {text_unique}")
    print(f"  semantic_cluster_count(语义簇数)        : {sem_count}"
          f"（口径 cluster_mode={cluster_mode}：execution=真实执行结果 / alias_stripped=去别名兜底）")
    print(f"  semantic_agreement_rate(最大语义簇占比) : {sem_rate}")
    if sem_clusters:
        print("  语义聚类明细:")
        for ci, cl in enumerate(sem_clusters, start=1):
            preview = cl.get("result_values_preview", cl.get("normalized_sql_no_alias", ""))
            print(f"    簇{ci} size={cl['cluster_size']} 行值预览/文本键={str(preview)[:80]}")
            for mem in cl.get("members", []):
                print(f"      - {mem}")
    if text_unique is not None and sem_count is not None:
        if text_unique == 1 and sem_count == 1:
            print("  → 文本唯一数=1、语义簇数=1：3 份候选完全一致，本轮无任何分歧。")
        elif text_unique == sem_count:
            print(f"  → 文本唯一数={text_unique} 与 语义簇数={sem_count} 相等(且>1)：本轮分歧为语义级（口径/维度不同）。")
        elif sem_count < text_unique:
            print(f"  → 语义簇数({sem_count}) < 文本唯一数({text_unique})："
                  f"存在 {text_unique - sem_count} 处仅文本/别名差异（语义等价）。")
    print(f"\n选择策略: {strategy}")
    print(f"最终选择: {on_event.get('selected') if on_event else on_state.get('sql')}")
    print(f"节点内自洽流程耗时: {on_event.get('elapsed_ms') if on_event else '-'} ms；"
          f"含图调度总耗时: {on_elapsed} ms")

    # 候选一致性指标（Step 13 消融报告用）
    valid_cnt = (on_event.get("valid_sample_count") if on_event else None) or 0
    unique_cnt = (on_event.get("candidate_unique_count") if on_event else None) or 0
    agree_rate = (on_event.get("candidate_agreement_rate") if on_event else None) or 0.0
    max_votes = next((item["votes"] for item in (detail or []) if "votes" in item), 0)
    print(f"\n候选一致率(文本级): {agree_rate:.2f}（{max_votes}/{valid_cnt} 相同）")
    print(f"唯一候选数(文本级): {unique_cnt}")
    print(f"语义簇数: {sem_count}；语义等价簇占比: {sem_rate}（口径={cluster_mode}）")

    print("\n" + "=" * 25 + " 消融对比表 " + "=" * 25)
    ratio = on_elapsed / max(off_elapsed, 1)
    print(f"{'指标':<18}{'OFF（基线）':<20}{'ON（自洽）'}")
    print(f"{'总耗时':<18}{f'{off_elapsed} ms':<20}{f'{on_elapsed} ms'}")
    print(f"{'ON/OFF 耗时比':<18}{'-':<20}{f'{ratio:.2f}x'}（并发采样，期望显著小于 N 倍）")
    print(f"{'候选一致率(文本)':<18}{'-':<20}{f'{agree_rate:.2f}（{max_votes}/{valid_cnt} 相同）'}")
    print(f"{'文本唯一数':<18}{'-':<20}{unique_cnt}")
    print(f"{'语义簇数':<18}{'-':<20}{f'{sem_count}（{cluster_mode}）'}")
    print(f"{'语义等价簇占比':<18}{'-':<20}{sem_rate}")

    checks = [
        ("OFF 产出 SQL", bool(off_state.get("sql"))),
        ("ON 产出候选", len(samples) >= 1),
        ("ON 产出最终 SQL", bool(on_event and on_event.get("selected"))),
        ("ON 记录了选择策略", strategy in ("majority_vote", "equal_execution_result", "fallback_temperature_zero")),
        ("ON 输出文本/语义两级指标", isinstance(text_unique, int) and isinstance(sem_count, int)),
    ]
    print("\n========== 验收判定 ==========")
    ok = True
    for name, passed in checks:
        print(f"{'[PASS]' if passed else '[FAIL]'} {name}")
        ok = ok and passed
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
