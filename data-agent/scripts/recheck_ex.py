"""离线重算 EX/EM：不调用 LLM，仅按 record 中已存的 pred_sql 重跑执行比对。

用途：修改 gold 标注（如新增 gold_sqls 变体）后，无需重跑 4 小时的 LLM 评测，
直接用已保存的 pred_sql 重新执行 SQL、重算 EX/EM 并更新报告。
EX/EM 都是确定性指标（pred_sql 已固定），离线重算结果与在线完全一致、可复现。

用法：
    python scripts/recheck_ex.py evaluation/ablation.json   # 消融结果
    python scripts/recheck_ex.py evaluation/report.json     # Step 22 基线结果
"""
import asyncio
import json
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "scripts"))

from sqlalchemy import text  # noqa: E402

from app.clients.mysql_client_manager import dw_mysql_client_manager  # noqa: E402
from run_evaluation import normalize_sql, rows_equal, summarize  # noqa: E402


def _gold_sqls_of(item: dict) -> list[str]:
    return [g for g in (item.get("gold_sqls") or [item.get("gold_sql", "")]) if g]


async def recheck_records(records: list[dict], golden_by_id: dict[str, dict],
                          dry_run: bool = False) -> tuple[int, int]:
    """重算一组记录的 EX/EM，返回 (修正数, 检查数)。"""
    session = dw_mysql_client_manager.session_factory()
    fixed = checked = 0
    try:
        for r in records:
            if r["category"] == "out_of_scope":
                continue
            item = golden_by_id.get(r["id"])
            if not item or not r.get("pred_sql"):
                continue
            checked += 1
            gold_sqls = _gold_sqls_of(item)
            pred_sql = r["pred_sql"]

            # EM：命中任一变体（离线，确定性）
            new_em = any(normalize_sql(pred_sql) == normalize_sql(g) for g in gold_sqls)

            # EX：若原已命中（ex=True）且变体没变，可跳过重执行省时间；
            # 但为保险起见统一重执行（数据可能更新）。
            new_ex = r["ex"]
            if r.get("executed"):
                try:
                    cur = await session.execute(text(pred_sql))
                    pred_rows = [list(row) for row in cur.fetchall()]
                    hit = -1
                    for idx, g in enumerate(gold_sqls):
                        gcur = await session.execute(text(g))
                        gold_rows = [list(row) for row in gcur.fetchall()]
                        if rows_equal(gold_rows, pred_rows):
                            hit = idx
                            break
                    new_ex = hit >= 0
                    r["gold_variant_hit"] = hit
                except Exception as e:  # noqa: BLE001
                    r["error"] = f"recheck 执行失败 {type(e).__name__}: {e}"
                    new_ex = False
            if new_ex != r["ex"] or new_em != r["em"]:
                old_ex, old_em = r["ex"], r["em"]
                if not dry_run:
                    r["ex"], r["em"] = new_ex, new_em
                    if new_ex and r.get("error") and "结果不等价" in str(r["error"]):
                        r["error"] = None
                print(f"  修正 {r['id']}: EX {old_ex}→{new_ex}  EM {old_em}→{new_em}")
                fixed += 1
    finally:
        await session.close()
    return fixed, checked


async def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "evaluation/ablation.json")
    golden_path = BASE / "evaluation" / "golden_set.jsonl"
    golden_by_id = {}
    for line in open(golden_path, encoding="utf-8"):
        line = line.strip()
        if line:
            obj = json.loads(line)
            golden_by_id[obj["id"]] = obj
    # 多轮集的 gold 也要支持（消融报告含多轮专项）
    mt_path = BASE / "evaluation" / "multi_turn_set.jsonl"
    if mt_path.exists():
        for line in open(mt_path, encoding="utf-8"):
            line = line.strip()
            if line:
                obj = json.loads(line)
                golden_by_id[obj["id"]] = obj

    dw_mysql_client_manager.init()

    data = json.load(open(target, encoding="utf-8"))
    shutil.copy(target, target.with_suffix(".json.bak"))

    total_fixed = 0
    if "groups" in data:  # 消融结果
        for name in data["groups"]:
            print(f"== {name} ==")
            recs = data["records"][name]
            fixed, checked = await recheck_records(recs, golden_by_id)
            total_fixed += fixed
            data["groups"][name]["metrics"] = summarize(recs)
        if data.get("multi_turn"):
            for arm in ("on", "off"):
                recs = data["multi_turn"][arm]["records"]
                print(f"== multi-{arm} ==")
                fixed, checked = await recheck_records(recs, golden_by_id)
                total_fixed += fixed
                data["multi_turn"][arm]["metrics"] = summarize(recs)
    else:  # Step 22 基线结果
        print("== report ==")
        fixed, checked = await recheck_records(data["records"], golden_by_id)
        total_fixed += fixed
        data["metrics"] = summarize(data["records"])

    with open(target, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    await dw_mysql_client_manager.close()
    print(f"\n共修正 {total_fixed} 条记录（备份在 {target.name}.bak）")

    # 重新渲染报告
    if "groups" in data:
        from run_ablation import draw_chart, write_report
        results = [(n, g["desc"], g.get("switches", {}), g["metrics"])
                   for n, g in data["groups"].items()]
        mt = data.get("multi_turn") or {}
        m_on = (mt.get("on") or {}).get("metrics")
        m_off = (mt.get("off") or {}).get("metrics")
        write_report(target.parent, results, m_on, m_off,
                     (mt.get("on") or {}).get("records") or [],
                     (mt.get("off") or {}).get("records") or [],
                     data["llm"]["note"], data.get("records") or {})
        draw_chart([n for n, _, _, _ in results], [m for _, _, _, m in results],
                   m_on, m_off, target.parent / "ablation_chart.png")
        print(f"已重渲染 {target.parent}/ablation.md + ablation_chart.png")
    else:
        from run_evaluation import write_reports
        write_reports(data["records"], data["metrics"], target.parent)
        print(f"已重渲染 {target.parent}/report.md")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
