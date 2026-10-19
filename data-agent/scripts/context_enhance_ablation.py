"""上下文增强（指标口径 + Few-shot）开/关消融实验 —— 论文素材。

同一问题在两种状态下各生成一次 SQL：
  OFF：context_enhance.metric_spec/few_shot 全关（基线：仅原始 yaml 召回明细）；
  ON ：两开关全开（结构化指标口径 + 相似示例 top3 + 预算裁剪）。
输出两态注入内容、生成 SQL 与规则化差异检查。

用法：
    d:/Work-Data-Agent/data-agent/.venv/Scripts/python.exe scripts/context_enhance_ablation.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.graph import END, START, StateGraph

from app.agent.context import DataAgentContext
from app.agent.context_enhancer import build_enhanced_context
from app.agent.nodes.generate_sql import generate_sql
from app.agent.state import DataAgentState
from app.clients.mysql_client_manager import dw_mysql_client_manager
from app.conf.app_config import app_config
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository

QUERIES = [
    # (问题, 关键词, 规则检查)
    ("2024年每个季度的GMV是多少", ["GMV", "季度", "2024年"],
     lambda s: {
        "过滤年份 2024": "2024" in s,
        "按季度分组(quarter+GROUP BY)": "QUARTER" in s and "GROUP BY" in s,
        "金额口径 SUM(order_amount)": "SUM" in s and "ORDER_AMOUNT" in s,
        "关联时间维表 dim_date": "DIM_DATE" in s,
        "未误用销量字段 order_quantity": "ORDER_QUANTITY" not in s}),
    # 口径易混题：销量(order_quantity) vs 销售额(order_amount)，few-shot 有显式反例
    ("统计每个商品品类的商品销量", ["销量", "品类"],
     lambda s: {
        "销量用 order_quantity": "ORDER_QUANTITY" in s,
        "未误用金额字段 order_amount": "ORDER_AMOUNT" not in s,
        "按品类分组(category+GROUP BY)": "CATEGORY" in s and "GROUP BY" in s,
        "关联商品维表 dim_product": "DIM_PRODUCT" in s}),
    # 区分度案例（口径歧义类）：GMV 口径 vs 直接 SUM(订单金额)；实测若两态等价将如实报告
    ("统计销售额", ["销售额", "统计"],
     lambda s: {
        "金额口径 SUM(order_amount)": "SUM" in s and "ORDER_AMOUNT" in s,
        "未误用销量字段 order_quantity": "ORDER_QUANTITY" not in s}),
]
MAX_ROUNDS = 3

# 模拟真实召回：5 张表全量字段（带噪声）+ 命中指标 GMV
TABLE_INFOS = [
    {"name": "fact_order", "role": "fact", "description": "订单事实表，记录订单数量和金额等核心指标。",
     "columns": [
         {"name": "order_id", "type": "bigint", "role": "primary_key", "examples": [], "description": "订单唯一标识。", "alias": ["订单ID"]},
         {"name": "customer_id", "type": "bigint", "role": "foreign_key", "examples": [], "description": "关联客户维度的外键。", "alias": ["客户ID"]},
         {"name": "product_id", "type": "bigint", "role": "foreign_key", "examples": [], "description": "关联商品维度的外键。", "alias": ["商品ID"]},
         {"name": "date_id", "type": "bigint", "role": "foreign_key", "examples": [], "description": "关联时间维度的外键。", "alias": ["日期", "下单日期"]},
         {"name": "region_id", "type": "bigint", "role": "foreign_key", "examples": [], "description": "关联地区维度的外键。", "alias": ["地区ID"]},
         {"name": "order_quantity", "type": "int", "role": "measure", "examples": [], "description": "订单中商品的购买数量。", "alias": ["销量", "购买数量", "件数"]},
         {"name": "order_amount", "type": "decimal", "role": "measure", "examples": [], "description": "订单金额。", "alias": ["销售额", "订单金额", "收入"]},
     ]},
    {"name": "dim_date", "role": "dim", "description": "时间维度表，用于多时间粒度分析。",
     "columns": [
         {"name": "date_id", "type": "bigint", "role": "primary_key", "examples": [], "description": "日期唯一标识，格式 yyyyMMdd。", "alias": ["日期ID", "日期"]},
         {"name": "year", "type": "int", "role": "dimension", "examples": [], "description": "年份。", "alias": ["年", "年份"]},
         {"name": "quarter", "type": "varchar", "role": "dimension", "examples": [], "description": "季度。", "alias": ["季度"]},
         {"name": "month", "type": "int", "role": "dimension", "examples": [], "description": "月份。", "alias": ["月", "月份"]},
     ]},
    {"name": "dim_region", "role": "dim", "description": "地区维度表，用于描述订单发生的地理区域信息。",
     "columns": [
         {"name": "region_id", "type": "bigint", "role": "primary_key", "examples": [], "description": "地区唯一标识。", "alias": ["地区ID"]},
         {"name": "region_name", "type": "varchar", "role": "dimension", "examples": [], "description": "订单所属的大区名称。", "alias": ["地区", "大区"]},
         {"name": "province", "type": "varchar", "role": "dimension", "examples": [], "description": "订单所属的省份名称。", "alias": ["省份"]},
     ]},
    {"name": "dim_customer", "role": "dim", "description": "客户维度表，描述下单客户的基本属性。",
     "columns": [
         {"name": "customer_id", "type": "bigint", "role": "primary_key", "examples": [], "description": "客户唯一标识。", "alias": ["客户ID"]},
         {"name": "customer_name", "type": "varchar", "role": "dimension", "examples": [], "description": "客户名称。", "alias": ["客户名称"]},
         {"name": "gender", "type": "varchar", "role": "dimension", "examples": [], "description": "客户性别。", "alias": ["性别"]},
     ]},
    {"name": "dim_product", "role": "dim", "description": "商品维度表，描述商品的基本属性信息。",
     "columns": [
         {"name": "product_id", "type": "bigint", "role": "primary_key", "examples": [], "description": "商品唯一标识。", "alias": ["商品ID"]},
         {"name": "product_name", "type": "varchar", "role": "dimension", "examples": [], "description": "商品名称。", "alias": ["商品名称"]},
         {"name": "category", "type": "varchar", "role": "dimension", "examples": [], "description": "商品所属品类。", "alias": ["品类"]},
     ]},
]
METRIC_INFOS = [
    {"name": "GMV", "description": "全称Gross Merchandise Value，表示所有订单的成交金额总和。",
     "relevant_columns": ["fact_order.order_amount"], "alias": ["成交总额", "订单总额", "销售额", "销售总额"]},
]
DATE_INFO = {"date": "2026-10-12", "weekday": "星期一", "quarter": "Q4"}


def build_graph():
    builder = StateGraph(state_schema=DataAgentState, context_schema=DataAgentContext)
    builder.add_node("generate_sql", generate_sql)
    builder.add_edge(START, "generate_sql")
    builder.add_edge("generate_sql", END)
    return builder.compile()


def set_switches(metric_on: bool, fewshot_on: bool):
    app_config.context_enhance.metric_spec.enable = metric_on
    app_config.context_enhance.few_shot.enable = fewshot_on
    # 消融场景：5 张召回表全量明细（2902 字符）+ 3 条示例均需完整可见，放宽预算
    app_config.context_enhance.table_infos_max_chars = 3000
    app_config.context_enhance.total_max_chars = 5200


async def generate_once(context: dict, db_info: dict, query: str, keywords: list) -> tuple[str, int]:
    state = {
        "query": query, "rewritten_query": query, "keywords": keywords,
        "table_infos": TABLE_INFOS, "metric_infos": METRIC_INFOS,
        "date_info": DATE_INFO, "db_info": db_info, "history": [],
    }
    final_state, t0 = None, time.monotonic()
    async for mode, payload in build_graph().astream(
            input=state, context=context, stream_mode=["custom", "values"]):
        if mode == "values":
            final_state = payload
    return final_state["sql"], int((time.monotonic() - t0) * 1000)


def preview(query: str, keywords: list) -> dict:
    metric_yaml = yaml.dump(METRIC_INFOS, allow_unicode=True, sort_keys=False)
    return build_enhanced_context(
        query=query, keywords=keywords, table_infos=TABLE_INFOS,
        metric_infos_yaml=metric_yaml, metric_infos=METRIC_INFOS,
        config=app_config.context_enhance)


async def main() -> int:
    print("== 上下文增强开/关消融实验（指标口径 + Few-shot，3 个问题，含相关性门槛） ==")

    dw_mysql_client_manager.init()
    results = []  # (query, off_sql, on_sql, off_ms, on_ms, off_preview, on_preview)
    try:
        async with dw_mysql_client_manager.session_factory() as dw_session:
            db_info = await DWMySQLRepository(dw_session).get_db_info()
            context = DataAgentContext(dw_mysql_repository=DWMySQLRepository(dw_session))

            for qi, (query, keywords, _checks) in enumerate(QUERIES, start=1):
                set_switches(False, False)
                off_pv = preview(query, keywords)
                set_switches(True, True)
                on_pv = preview(query, keywords)

                print(f"\n########## 问题{qi}: {query} ##########")
                set_switches(False, False)
                off_sql, off_ms = None, None
                for attempt in range(1, MAX_ROUNDS + 1):
                    print(f"---- [OFF 基线] 第 {attempt}/{MAX_ROUNDS} 轮 ----")
                    try:
                        off_sql, off_ms = await generate_once(context, db_info, query, keywords)
                        break
                    except Exception as exc:
                        print(f"[RETRY] {type(exc).__name__}: {exc}")
                if off_sql is None:
                    print("[FAIL] OFF 连续失败(LLM 网关故障)，请稍后重试。")
                    return 1

                set_switches(True, True)
                on_sql, on_ms = None, None
                for attempt in range(1, MAX_ROUNDS + 1):
                    print(f"---- [ON 增强] 第 {attempt}/{MAX_ROUNDS} 轮 ----")
                    try:
                        on_sql, on_ms = await generate_once(context, db_info, query, keywords)
                        break
                    except Exception as exc:
                        print(f"[RETRY] {type(exc).__name__}: {exc}")
                if on_sql is None:
                    print("[FAIL] ON 连续失败(LLM 网关故障)，请稍后重试。")
                    return 1
                results.append((query, off_sql, on_sql, off_ms, on_ms, off_pv, on_pv))
    finally:
        set_switches(True, True)
        await dw_mysql_client_manager.close()

    # ---------- 呈现 ----------
    for qi, (query, off_sql, on_sql, off_ms, on_ms, off_pv, on_pv) in enumerate(results, start=1):
        print("\n" + "#" * 24 + f" 问题{qi}: {query} " + "#" * 24)
        print(f"[OFF] 字符 {off_pv['trace']['chars']}，示例区块: {off_pv['few_shot_examples']}")
        print(f"[ON ] 注入指标口径: {on_pv['trace']['injected_metrics']}")
        print(f"[ON ] 注入 few-shot: {on_pv['trace']['injected_few_shot']}")
        print(f"[ON ] 被过滤指标: {on_pv['trace']['dropped_metrics']}，"
              f"被过滤示例: {on_pv['trace']['dropped_examples']}")
        tr = on_pv["trace"]
        m_cand = len(tr["injected_metrics"]) + len(tr["dropped_metrics"])
        e_cand = len(tr["injected_few_shot"]) + len(tr["dropped_examples"])
        print(f"[ON ] 注入相关性达标率: 指标 {len(tr['injected_metrics'])}/{m_cand}，"
              f"few-shot {len(tr['injected_few_shot'])}/{e_cand}")
        print(f"[ON ] 字符(口径/示例/明细): {on_pv['trace']['chars']}")
        print(f"[OFF SQL]（{off_ms} ms）\n{off_sql}")
        print(f"[ON  SQL]（{on_ms} ms）\n{on_sql}")

        checks = QUERIES[qi - 1][2]
        off_c, on_c = checks(off_sql.upper()), checks(on_sql.upper())
        print("-" * 60)
        print(f"{'检查项':<34s}{'OFF':>6s}{'ON':>6s}")
        off_pass = on_pass = 0
        for k in off_c:
            print(f"{k:<34s}{'√' if off_c[k] else '×':>6s}{'√' if on_c[k] else '×':>6s}")
            off_pass += int(off_c[k])
            on_pass += int(on_c[k])
        print(f"规则命中: OFF {off_pass}/{len(off_c)}，ON {on_pass}/{len(on_c)}")

    print("\n" + "=" * 20 + " 汇总 " + "=" * 20)
    print("注：增强为本地相似度检索，不产生额外 LLM 调用；单次 case 仅作定性对比，准确率结论需以全量评测集统计。")

    # ---------- 验收判定 ----------
    print("\n" + "=" * 20 + " 验收判定 " + "=" * 20)
    # 断言1：相关性门槛生效——问"销量"时不得注入 GMV 口径
    q2_trace = results[1][6]["trace"]
    gmv_leak = "GMV" in (q2_trace["injected_metrics"] or [])
    print(f"{'[PASS]' if not gmv_leak else '[FAIL]'} 问题2(销量) 注入指标口径不含 GMV"
          f"（实际注入: {q2_trace['injected_metrics']}，被过滤: {q2_trace['dropped_metrics']}）")
    # 断言2：GMV 补同义词后——问"统计销售额"应能选中 GMV 口径（消除假阴性）
    q3_trace = results[2][6]["trace"]
    gmv_hit = "GMV" in (q3_trace["injected_metrics"] or [])
    print(f"{'[PASS]' if gmv_hit else '[FAIL]'} 问题3(统计销售额) GMV 口径被选中注入"
          f"（实际注入: {q3_trace['injected_metrics']}，被过滤: {q3_trace['dropped_metrics']}）")
    print("注：问题3 为区分度案例（口径歧义），OFF/ON SQL 等价与否均如实报告。")
    return 0 if (not gmv_leak and gmv_hit) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
