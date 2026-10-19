"""SQL 自省闭环（生成→校验→执行→纠错）消融实验 —— 论文素材。

场景 A（自动纠错成功）：构造一条表名错误的 SQL（fact_orders/dim_regions 均不存在），
    从 validate_sql 进入闭环，观察"校验失败→纠错→再校验→执行"跑若干轮后成功。
场景 B（优雅失败）：预填 3 条历史错误（模拟已连续失败 3 次），第 4 次失败触发
    max_retries 上限，观察系统优雅终止并给出用户可读原因，且不会死循环。

用法：
    d:/Work-Data-Agent/data-agent/.venv/Scripts/python.exe scripts/sql_self_reflection_ablation.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.graph import END, START, StateGraph

from app.agent.context import DataAgentContext
from app.agent.graph import route_after_correct, route_after_run, route_after_validate
from app.agent.nodes.correct_sql import correct_sql
from app.agent.nodes.run_sql import run_sql
from app.agent.nodes.validate_sql import validate_sql
from app.agent.state import DataAgentState
from app.clients.mysql_client_manager import dw_mysql_client_manager
from app.conf.app_config import app_config
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository

MAX_ROUNDS = 3  # LLM 网关偶发 500，整轮重试上限

# 可用 schema 上下文（真实存在的表与字段）
TABLE_INFOS = [
    {
        "name": "fact_order",
        "role": "fact",
        "description": "订单事实表，记录订单数量和金额等核心指标。",
        "columns": [
            {"name": "order_amount", "type": "decimal", "role": "measure",
             "examples": [199.5], "description": "订单金额。", "alias": ["销售额", "订单金额", "收入"]},
            {"name": "region_id", "type": "bigint", "role": "foreign_key",
             "examples": [1], "description": "关联地区维度的外键。", "alias": ["地区ID"]},
        ],
    },
    {
        "name": "dim_region",
        "role": "dim",
        "description": "地区维度表，用于描述订单发生的地理区域信息。",
        "columns": [
            {"name": "region_id", "type": "bigint", "role": "primary_key",
             "examples": [1], "description": "地区唯一标识。", "alias": ["地区ID"]},
            {"name": "region_name", "type": "varchar", "role": "dimension",
             "examples": ["华东"], "description": "订单所属的大区名称，如华东、华南等。", "alias": ["地区", "大区"]},
        ],
    },
]
DATE_INFO = {"date": "2026-10-11", "weekday": "星期日", "quarter": "Q4"}

QUERY = "统计华东地区的销售总额"
# 场景 A 的故障 SQL：两张表名都写错（正确为 fact_order / dim_region）
BROKEN_SQL = ("SELECT SUM(fo.order_amount) AS total_sales FROM fact_orders fo "
              "JOIN dim_regions dr ON fo.region_id = dr.region_id WHERE dr.region_name = '华东'")


def build_loop_graph():
    """构建 validate→run→correct 自省闭环子图（与主图该段拓扑一致）。"""
    builder = StateGraph(state_schema=DataAgentState, context_schema=DataAgentContext)
    builder.add_node("validate_sql", validate_sql)
    builder.add_node("run_sql", run_sql)
    builder.add_node("correct_sql", correct_sql)
    builder.add_edge(START, "validate_sql")
    builder.add_conditional_edges(source="validate_sql", path=route_after_validate,
                                  path_map={"run_sql": "run_sql", "correct_sql": "correct_sql"})
    builder.add_conditional_edges(source="correct_sql", path=route_after_correct,
                                  path_map={"validate_sql": "validate_sql", END: END})
    builder.add_conditional_edges(source="run_sql", path=route_after_run,
                                  path_map={END: END, "correct_sql": "correct_sql"})
    return builder.compile()


async def run_scenario(context, *, name: str, sql: str, error_history: list[str],
                       max_retries: int, db_info: dict) -> dict | None:
    print(f"\n{'=' * 20} {name} {'=' * 20}")
    print(f"初始 SQL: {sql}")
    print(f"预填错误历史: {len(error_history)} 条, max_retries={max_retries}")
    print("---- 流式进度事件 ----")
    state = {
        "query": QUERY, "rewritten_query": QUERY, "sql": sql,
        "table_infos": TABLE_INFOS, "metric_infos": [],
        "date_info": DATE_INFO, "db_info": db_info,
        "sql_attempts": [], "error_history": error_history,
        "attempt_count": 0, "max_retries": max_retries, "final_sql": None,
    }
    final_state = None
    async for mode, payload in graph_app.astream(input=state, context=context,
                                                 stream_mode=["custom", "values"]):
        if mode == "custom":
            # 精简打印 progress/error/result 帧
            if isinstance(payload, dict):
                t = payload.get("type")
                if t == "progress":
                    print(f"  [progress] {payload.get('step')} -> {payload.get('status')}"
                          + (f" (attempt={payload['attempt']})" if "attempt" in payload else ""))
                elif t == "result":
                    print(f"  [result] 查询成功，返回 {len(payload.get('data') or [])} 行")
                elif t == "error":
                    print(f"  [error] {payload.get('message')}")
        else:
            final_state = payload

    print("---- 尝试记录(sql_attempts) ----")
    for att in (final_state or {}).get("sql_attempts") or []:
        err = att["error"]
        err_brief = (err[:120] + "…") if err and len(err) > 120 else err
        print(f"  #{att['attempt']} [{att['stage']}] error={err_brief}")
        print(f"      SQL: {att['sql']}")
    print(f"final_sql: {(final_state or {}).get('final_sql')}")
    print(f"最终 error: {(final_state or {}).get('error')}")
    return final_state


async def main() -> int:
    global graph_app
    print(f"== SQL 自省闭环消融实验  max_retries(配置)={app_config.agent.max_retries} ==")

    dw_mysql_client_manager.init()
    async with dw_mysql_client_manager.session_factory() as dw_session:
        dw_repo = DWMySQLRepository(dw_session)
        db_info = await dw_repo.get_db_info()
        context = DataAgentContext(dw_mysql_repository=dw_repo)
        graph_app = build_loop_graph()

        # ---------- 场景 A：错表名 → 自动纠错成功 ----------
        fs_a = None
        for attempt in range(1, MAX_ROUNDS + 1):
            fs_a = await run_scenario(
                context, name="场景A: 错表名自动纠错",
                sql=BROKEN_SQL, error_history=[],
                max_retries=app_config.agent.max_retries, db_info=db_info)
            err = fs_a.get("error") or ""
            if fs_a.get("final_sql"):
                break
            # 纠错链路本身的 LLM 网关故障（非 SQL 不可修复）才整轮重试
            if "自动纠错失败" in err and attempt < MAX_ROUNDS:
                print(f"[RETRY] 场景A 第 {attempt}/{MAX_ROUNDS} 轮遇 LLM 网关故障，重试…")
                continue
            break
        if fs_a is None or not fs_a.get("final_sql"):
            print("[FAIL] 场景A 多次重试仍未成功(多为 LLM 网关持续故障)，请稍后重试。")
            return 1

        # ---------- 场景 B：超限优雅失败 ----------
        fs_b = await run_scenario(
            context, name="场景B: 达到 max_retries 上限优雅失败",
            sql=BROKEN_SQL, error_history=["历史错误1", "历史错误2", "历史错误3"],
            max_retries=app_config.agent.max_retries, db_info=db_info)

    # ---------- 验收判定 ----------
    attempts_a = fs_a.get("sql_attempts") or []
    checks = [
        ("场景A 发生至少 1 次纠错(尝试数>=2)", len(attempts_a) >= 2),
        ("场景A 最终执行成功(final_sql 非空)", bool(fs_a.get("final_sql"))),
        ("场景A 最终 error 为 None", fs_a.get("error") is None),
        ("场景B 优雅失败(final_sql 为空)", not fs_b.get("final_sql")),
        ("场景B 返回用户可读错误信息", isinstance(fs_b.get("error"), str) and "重试" in fs_b.get("error")),
    ]
    print("\n========== 验收判定 ==========")
    ok = True
    for name, passed in checks:
        print(f"{'[PASS]' if passed else '[FAIL]'} {name}")
        ok = ok and passed

    await dw_mysql_client_manager.close()
    return 0 if ok else 1


graph_app = None

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
