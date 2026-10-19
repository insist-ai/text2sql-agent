"""SQL 安全网关 6 组验收用例（论文素材，可重跑）。

用例 1：正常 SELECT（含 LIMIT）→ 放行
用例 2：INSERT 写操作 → 拦截（节点模块 + 仓储层兜底各验一次）
用例 3：分号拼接多语句 → 拦截
用例 4：注释绕过（--、/* */、#）→ 拦截
用例 5：无 LIMIT（含已有 LIMIT 超上限下调）→ 自动追加/收敛 LIMIT
用例 6：超长执行 → 仓储层超时中止（SELECT SLEEP，超时阈值临时调小、临时放开 SLEEP 函数）

另附两个误伤防护对照：字符串字面量/反引号标识符内的关键字不应触发拦截。

用法：
    d:/Work-Data-Agent/data-agent/.venv/Scripts/python.exe scripts/sql_guard_test.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients.mysql_client_manager import dw_mysql_client_manager
from app.core.sql_guard import SqlGuardError, SqlGuardPolicy, check_sql
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository

NORMAL_SQL = "SELECT region_name, SUM(order_amount) AS amt FROM fact_order fo JOIN dim_region dr ON fo.region_id = dr.region_id GROUP BY region_name ORDER BY amt DESC LIMIT 10"

results: list[tuple[str, bool, str]] = []


def record(case: str, passed: bool, detail: str = ""):
    results.append((case, passed, detail))
    print(f"{'[PASS]' if passed else '[FAIL]'} {case}" + (f" — {detail}" if detail else ""))


def expect_reject(case: str, sql: str, policy: SqlGuardPolicy | None = None):
    try:
        out = check_sql(sql, policy)
        record(case, False, f"应拦截却放行，输出: {out}")
    except SqlGuardError as e:
        record(case, True, str(e))


def expect_pass(case: str, sql: str, policy: SqlGuardPolicy | None = None) -> str | None:
    try:
        out = check_sql(sql, policy)
        record(case, True, f"放行: {out}")
        return out
    except SqlGuardError as e:
        record(case, False, f"应放行却拦截: {e}")
        return None


async def main() -> int:
    print("=" * 30 + " 纯规则校验（用例 1-5） " + "=" * 30)

    # 用例 1：正常 SELECT（已带 LIMIT）
    out1 = expect_pass("用例1 正常 SELECT", NORMAL_SQL)
    if out1 is not None:
        record("用例1b 已有 LIMIT 不重复追加", out1.upper().count("LIMIT") == 1)

    # 用例 2：写操作拦截
    expect_reject("用例2 INSERT 拦截",
                  "INSERT INTO dim_region(region_name) VALUES ('华北')")
    expect_reject("用例2b UPDATE 拦截", "UPDATE dim_region SET region_name='x' WHERE region_id=1")
    expect_reject("用例2c DELETE 拦截", "DELETE FROM dim_region WHERE region_id=1")

    # 用例 3：多语句
    expect_reject("用例3 分号拼接多语句",
                  "SELECT * FROM dim_region; DROP TABLE dim_region")
    expect_reject("用例3b SELECT 后拼接 DELETE",
                  "SELECT * FROM dim_region; DELETE FROM dim_region WHERE 1=1")
    # 单个末尾分号是允许的
    expect_pass("用例3c 单个末尾分号容忍", "SELECT * FROM dim_region LIMIT 5;")

    # 用例 4：注释绕过
    expect_reject("用例4a 行注释 --", "SELECT * FROM dim_region -- 恶意注释\nLIMIT 5")
    expect_reject("用例4b 块注释 /* */", "SELECT /* x */ * FROM dim_region LIMIT 5")
    expect_reject("用例4c MySQL 井号注释", "SELECT * FROM dim_region # 恶意注释\nLIMIT 5")

    # 用例 5：强制 LIMIT
    sql_no_limit = "SELECT * FROM dim_region"
    out5 = expect_pass("用例5a 无 LIMIT 自动追加", sql_no_limit)
    if out5 is not None:
        record("用例5a-断言 追加 LIMIT 1000", out5.upper().endswith("LIMIT 1000"))
    # 子查询内 LIMIT 不影响顶层判定：顶层无 LIMIT 仍追加
    sql_subq = "SELECT * FROM (SELECT region_name FROM dim_region LIMIT 3) t"
    out5b = expect_pass("用例5b 顶层无 LIMIT(子查询有) 仍追加", sql_subq)
    if out5b is not None:
        record("用例5b-断言 顶层追加且不改动子查询",
               out5b == sql_subq + " LIMIT 1000")
    # 已有 LIMIT 超过 max_rows 时下调
    policy_cap = SqlGuardPolicy(default_limit=1000, max_rows=100)
    out5c = expect_pass("用例5c LIMIT 50000 下调到 100",
                        "SELECT * FROM dim_region LIMIT 50000", policy_cap)
    if out5c is not None:
        record("用例5c-断言", out5c.endswith("LIMIT 100"))

    # 误伤对照：字面量/反引号里的关键字不得触发
    expect_pass(
        "对照A 字符串内含 DELETE/UPDATE 不误伤",
        "SELECT 'DELETE FROM x' AS note FROM dim_region WHERE region_name = 'UPDATE时间' LIMIT 5")
    # dim_region 无 update_time 列，仅做规则层断言（不执行）
    try:
        out_bt_sql = check_sql("SELECT `update_time` FROM dim_region LIMIT 5")
        record("对照B 反引号标识符 update_time 不误伤", True, out_bt_sql)
    except SqlGuardError as e:
        record("对照B 反引号标识符 update_time 不误伤", False, str(e))

    print("\n" + "=" * 30 + " 仓储层真实执行（用例 2兜底/6） " + "=" * 30)
    dw_mysql_client_manager.init()
    async with dw_mysql_client_manager.session_factory() as dw_session:
        repo = DWMySQLRepository(dw_session)

        # 用例 2-兜底：绕过节点直接调仓储执行 INSERT → 仍被拦截
        try:
            await repo.run_sql("INSERT INTO dim_region(region_name) VALUES ('xx')")
            record("用例2d 仓储层兜底拦截 INSERT", False, "仓储未拦截")
        except SqlGuardError as e:
            record("用例2d 仓储层兜底拦截 INSERT", True, str(e))

        # 用例 1-执行：正常 SQL（无 LIMIT）经仓储执行成功且被自动追加 LIMIT
        try:
            rows = await repo.run_sql("SELECT region_name FROM dim_region")
            record("用例1c 仓储执行无 LIMIT 查询成功并返回结果",
                   isinstance(rows, list) and len(rows) >= 1, f"{len(rows)} 行")
        except SqlGuardError as e:
            record("用例1c 仓储执行无 LIMIT 查询成功并返回结果", False, str(e))

        # 用例 6：超长执行——临时放开 SLEEP 函数、超时阈值压到 0.5s
        timeout_policy = SqlGuardPolicy(
            execution_timeout_ms=500,
            blocked_functions=[],  # 仅本用例临时放开，专门验证超时中止能力
        )
        import time
        t0 = time.monotonic()
        try:
            await repo.run_sql("SELECT SLEEP(3)", timeout_policy)
            record("用例6 超长执行被超时中止", False, "未超时")
        except SqlGuardError as e:
            elapsed = time.monotonic() - t0
            record("用例6 超长执行被超时中止",
                   e.code == "timeout" and elapsed < 3,
                   f"{elapsed:.2f}s 中止 | {e}")
        except Exception as e:  # noqa: BLE001
            record("用例6 超长执行被超时中止", False, f"异常类型不符: {type(e).__name__}: {e}")

    await dw_mysql_client_manager.close()

    print("\n" + "=" * 30 + " 汇总 " + "=" * 30)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
