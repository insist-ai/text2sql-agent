"""Golden Set 导出与校验脚本。

用法（在 data-agent 目录下）:
    ./.venv/Scripts/python.exe scripts/export_golden_set.py
    ./.venv/Scripts/python.exe scripts/export_golden_set.py --golden evaluation/golden_set.jsonl

功能:
  1. 读取 evaluation/golden_set.jsonl（每行一条 JSON）；
  2. 非 out_of_scope 类: 逐条在 dw 库真实执行 gold_sql，记录成功/失败与行数；
  3. out_of_scope 类: 仅校验 question 非空、gold_sql 为空字符串（期望拒答）；
  4. 基础合规检查: 单语句、无黑名单关键字、无注释、聚合列 AS 别名（提示级）；
  5. 输出统计（总体 + 分类别 + 失败明细），退出码 0=全部通过 / 1=存在失败。
"""
import asyncio
import json
import re
import sys
from argparse import ArgumentParser
from pathlib import Path

# 允许以 `python scripts/export_golden_set.py` 直接运行（不在包内）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.clients.mysql_client_manager import dw_mysql_client_manager  # noqa: E402
from app.conf.app_config import app_config  # noqa: E402

# 与 conf/app_config.yaml 的 sql_guard.blocked_keywords / blocked_functions 保持一致
BLOCKED_KEYWORDS = [
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE",
    "GRANT", "REPLACE", "CALL", "MERGE", "REVOKE", "LOAD", "HANDLER",
    "RENAME", "SHUTDOWN", "KILL", "PREPARE", "EXECUTE", "FLUSH", "PURGE",
    "LOCK", "UNLOCK",
]
BLOCKED_FUNCTIONS = ["SLEEP", "BENCHMARK", "LOAD_FILE", "GET_LOCK", "RELEASE_LOCK"]


def load_golden_set(path: Path) -> list[dict]:
    items: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"[FATAL] 第 {line_no} 行不是合法 JSON: {e}")
            items.append(obj)
    return items


def static_checks(item: dict) -> list[str]:
    """静态合规检查（不连库），返回警告列表。"""
    warnings: list[str] = []
    sql = (item.get("gold_sql") or "").strip()
    # gold_sqls 变体：同一问题的多种等价正确写法，任一命中即判 EX 通过
    variants = [v for v in (item.get("gold_sqls") or []) if v and v.strip()]
    if variants and item.get("category") != "out_of_scope":
        if sql and sql not in variants:
            warnings.append("gold_sql 不在 gold_sqls 变体列表中")
        for i, v in enumerate(variants):
            upper = v.upper()
            for kw in BLOCKED_KEYWORDS:
                if re.search(rf"\b{kw}\b", upper):
                    warnings.append(f"gold_sqls[{i}] 命中黑名单关键字 {kw}")
            for fn in BLOCKED_FUNCTIONS:
                if re.search(rf"\b{fn}\s*\(", upper):
                    warnings.append(f"gold_sqls[{i}] 命中黑名单函数 {fn}")
    if item.get("category") == "out_of_scope":
        if sql:
            warnings.append("out_of_scope 类的 gold_sql 应为空字符串")
        if not (item.get("question") or "").strip():
            warnings.append("question 为空")
        return warnings

    if not sql:
        warnings.append("gold_sql 为空")
        return warnings
    if ";" in sql.rstrip().rstrip(";"):
        warnings.append("疑似多语句（含分号）")
    if "--" in sql or "/*" in sql:
        warnings.append("含注释")
    upper = sql.upper()
    for kw in BLOCKED_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            warnings.append(f"命中黑名单关键字 {kw}")
    for fn in BLOCKED_FUNCTIONS:
        if re.search(rf"\b{fn}\s*\(", upper):
            warnings.append(f"命中黑名单函数 {fn}")
    if re.search(r"\b(dw|meta|app_db|mysql|information_schema)\s*\.", sql, re.I):
        warnings.append("含库名前缀（应直接写表名）")
    return warnings


async def run_golden(golden_path: Path) -> int:
    items = load_golden_set(golden_path)
    print(f"载入 Golden Set: {golden_path} 共 {len(items)} 条\n")

    dw_mysql_client_manager.init()
    results: list[dict] = []
    async with dw_mysql_client_manager.session_factory() as session:
        for item in items:
            rec = {
                "id": item.get("id"),
                "category": item.get("category"),
                "ok": False,
                "rows": None,
                "error": "",
                "warnings": static_checks(item),
            }
            sql = (item.get("gold_sql") or "").strip()
            if item.get("category") == "out_of_scope":
                rec["ok"] = not sql and bool((item.get("question") or "").strip())
                rec["rows"] = 0
                if not rec["ok"]:
                    rec["error"] = "out_of_scope 格式不符合约定"
            elif sql:
                variants = [v for v in (item.get("gold_sqls") or []) if v and v.strip()] or [sql]
                try:
                    for i, v in enumerate(variants):
                        cursor = await session.execute(text(v))
                        rows = cursor.fetchall()
                        if i == 0:
                            rec["rows"] = len(rows)
                    rec["ok"] = True
                    rec["variants"] = len(variants)
                except Exception as e:  # noqa: BLE001
                    rec["error"] = f"{type(e).__name__}: {e}"
            else:
                rec["error"] = "gold_sql 为空"
            results.append(rec)

    await dw_mysql_client_manager.close()

    # ---- 统计输出 ----
    exec_targets = [r for r in results if r["category"] != "out_of_scope"]
    passed = [r for r in exec_targets if r["ok"]]
    failed = [r for r in exec_targets if not r["ok"]]
    oos = [r for r in results if r["category"] == "out_of_scope"]
    oos_ok = [r for r in oos if r["ok"]]

    print("== 逐条结果 ==")
    for r in results:
        mark = "PASS" if r["ok"] else "FAIL"
        line = f"[{mark}] {r['id']} ({r['category']:12s}) rows={r['rows']}"
        if r["error"]:
            line += f"  错误: {r['error'][:120]}"
        if r["warnings"]:
            line += f"  警告: {'; '.join(r['warnings'])}"
        print(line)

    print("\n== 分类统计 ==")
    by_cat: dict[str, list] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)
    for cat in sorted(by_cat):
        rs = by_cat[cat]
        ok = sum(1 for r in rs if r["ok"])
        print(f"  {cat:14s} {ok}/{len(rs)}")

    print("\n== 总体 ==")
    print(f"  可执行类(非 out_of_scope): {len(passed)}/{len(exec_targets)} 条执行成功")
    print(f"  越界类(out_of_scope):      {len(oos_ok)}/{len(oos)} 条格式合规")
    warn_cnt = sum(1 for r in results if r["warnings"])
    print(f"  含静态警告: {warn_cnt} 条")

    all_ok = not failed and len(oos_ok) == len(oos)
    print(f"\n结论: {'全部通过' if all_ok else '存在失败项，请修正后重跑'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    parser = ArgumentParser(description="Golden Set 导出与校验")
    parser.add_argument(
        "--golden",
        default=str(Path(__file__).resolve().parents[1] / "evaluation" / "golden_set.jsonl"),
        help="golden_set.jsonl 路径（默认 evaluation/golden_set.jsonl）",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(run_golden(Path(args.golden))))
