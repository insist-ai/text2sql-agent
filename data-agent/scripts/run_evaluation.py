"""NL2SQL 智能问数系统 · Golden Set 离线评测脚本。

直接调用 LangGraph 图（app/agent/graph.py 的模块级 graph），**不走 HTTP 接口**：
/api/query 挂有限流（10 次/分）与查询缓存（同问题二次命中回放旧结果），都会污染评测。

用法（在 data-agent 目录下）:
    ./.venv/Scripts/python.exe scripts/run_evaluation.py
    ./.venv/Scripts/python.exe scripts/run_evaluation.py --limit 5 --category filter
    ./.venv/Scripts/python.exe scripts/run_evaluation.py --concurrency 2 --output evaluation

指标:
    EX  执行准确率（预测 SQL 与 gold_sql 的执行结果等价：列数相同、行数相同、
        排序无关逐行比对、数值容差 1e-6）
    EM  精确匹配率（归一化后字符串完全一致）
    有效率      预测 SQL 能成功执行的占比
    平均尝试    state.sql_attempts 平均长度（自省闭环重试观测）
    平均耗时    单条端到端毫秒
    拒答准确率  out_of_scope 类中正确拒答的比例（本项目特色能力）
    误拒答率    非 out_of_scope 类中被错误拒答的比例
    token       口径说明：项目 LLM 实例未暴露 usage 回调，退化为
                「(问题+改写后问题) 输入字符 + 生成 SQL 输出字符」估算，非真实 token。

可复现:
    - 本脚本直连 graph，天然绕过查询缓存；不写任何缓存；
    - temperature 已在 app/agent/llm.py 硬编码为 0，请勿修改；
    - --concurrency 默认 1（LLM 网关并发能力弱，串行最稳）；
    - 正式论文数据必须使用 DeepSeek 官方通道跑（免费网关会挂死/漂移）。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from argparse import ArgumentParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.agent.context import DataAgentContext  # noqa: E402
from app.agent.graph import graph  # noqa: E402
from app.agent.state import DataAgentState  # noqa: E402
from app.clients.embedding_client_manager import embedding_client_manager  # noqa: E402
from app.clients.es_client_manager import es_client_manager  # noqa: E402
from app.clients.mysql_client_manager import (  # noqa: E402
    dw_mysql_client_manager,
    meta_mysql_client_manager,
)
from app.clients.qdrant_client_manager import qdrant_client_manager  # noqa: E402
from app.conf.app_config import app_config  # noqa: E402
from app.core.log import logger  # noqa: E402
from app.repositories.es.column_es_repository import ColumnESRepository  # noqa: E402
from app.repositories.es.metric_es_repository import MetricESRepository  # noqa: E402
from app.repositories.es.value_es_repository import ValueESRepository  # noqa: E402
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository  # noqa: E402
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository  # noqa: E402
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository  # noqa: E402
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository  # noqa: E402
from app.repositories.qdrant.value_qdrant_repository import ValueQdrantRepository  # noqa: E402

import re

# 单条问题整体重试上限（LLM 网关偶发超时/5xx，非问题本身缺陷）
MAX_ROUNDS_PER_ITEM = 2
NUM_TOLERANCE = 1e-6
# 召回层 Recall@K 的 K：与 agent.fuse_top_n 对齐（融合后每类保留条数）
RECALL_TOP_K = 10

# SQL 关键字/函数，解析 gold 实体时排除，避免把关键字误判为列名
_SQL_STOPWORDS = {
    "select", "from", "where", "group", "by", "order", "having", "limit", "join",
    "on", "and", "or", "not", "in", "as", "asc", "desc", "case", "when", "then",
    "else", "end", "distinct", "between", "like", "is", "null", "inner", "left",
    "right", "outer", "count", "sum", "avg", "max", "min", "round", "abs", "if",
    "ifnull", "coalesce", "cast", "date", "year", "month", "day", "quarter",
}


# ---------------------------------------------------------------- 基础工具
def load_golden_set(path: Path, category: str | None, limit: int | None) -> list[dict]:
    items: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if category and obj.get("category") != category:
                continue
            items.append(obj)
    return items[:limit] if limit else items


def normalize_sql(sql: str) -> str:
    """EM 归一化：统一小写、压缩空白、去末尾分号、
    剥掉安全网关自动追加的尾部 LIMIT n（否则系统行为会导致 EM 恒为 0）。"""
    s = " ".join((sql or "").lower().split()).rstrip(";")
    return re.sub(r"\s+limit\s+\d+\s*$", "", s).strip()


def _cell_equal(a, b) -> bool:
    """单元格等价：数值走容差，其余按字符串。"""
    try:
        fa, fb = float(a), float(b)
        return abs(fa - fb) <= NUM_TOLERANCE * max(1.0, abs(fa), abs(fb))
    except (TypeError, ValueError):
        return str(a) == str(b)


def parse_gold_entities(sql: str) -> tuple[set[str], set[str]]:
    """从 gold_sql 解析出 gold 表集合与 gold 字段集合（字段为 `表名.列名` 形式）。

    用于召回层 Recall@K：召回是"实体级"的，逐实体验证比逐样本验证分辨率高一个量级，
    且 RRF / 融合策略的作用点正在召回层，端到端 EX 在小样本下分辨不出差异时，
    召回命中率仍能量出增益。
    """
    if not sql:
        return set(), set()
    # 表别名 → 表名（FROM/JOIN xxx [AS] alias）
    alias_map: dict[str, str] = {}
    tables: set[str] = set()
    for m in re.finditer(r"\b(?:from|join)\s+([a-z_]\w*)(?:\s+(?:as\s+)?([a-z]\w*))?", sql, re.I):
        table, alias = m.group(1), m.group(2)
        tables.add(table)
        if alias and alias.lower() not in _SQL_STOPWORDS:
            alias_map[alias.lower()] = table
        alias_map.setdefault(table.lower(), table)

    # 先剥掉 "AS 别名"（别名常被误判为列名，如 AS GMV / AS order_count）
    sql_body = re.sub(r"\bAS\s+[a-z_]\w*", "", sql, flags=re.I)
    table_names = {t.lower() for t in tables}

    columns: set[str] = set()
    # 限定列：alias.column / table.column
    for m in re.finditer(r"\b([a-z]\w*)\.([a-z_]\w*)", sql_body, re.I):
        qualifier, col = m.group(1).lower(), m.group(2)
        if qualifier in _SQL_STOPWORDS or col.lower() in _SQL_STOPWORDS:
            continue
        if col.isupper():  # 全大写多为指标/常量别名（如 GMV），不是真实列
            continue
        table = alias_map.get(qualifier)
        if table:
            columns.add(f"{table}.{col}")
    # 非限定列：单表查询时可确定归属；多表时跳过（避免误判）
    if len(tables) == 1:
        only_table = next(iter(tables))
        for m in re.finditer(r"(?<![\w.])([a-z_]\w*)\b(?!\s*\()", sql_body, re.I):
            token = m.group(1)
            if (token.lower() in _SQL_STOPWORDS or token.lower() in table_names
                    or token.isupper()):
                continue
            columns.add(f"{only_table}.{token}")
    return tables, columns


def recall_at_k(gold: set[str], retrieved: list[str], k: int = RECALL_TOP_K) -> float:
    """召回命中率：gold 实体中出现在召回 Top-K 里的比例；gold 为空时返回 None（不计入均值）。"""
    if not gold:
        return None  # type: ignore[return-value]
    hit = len(gold & set(retrieved[:k]))
    return round(hit / len(gold), 3)


# ------------------------------------------------- 组件级指标（Component-level）
# 动机：端到端 EX 要求结果集逐列完全一致，会把"输出列多寡"这类无语义差异的写法判为失败
# （如 gold 返回 订单号+客户号+数量，模型只返回 订单号，逻辑全对仍判错）。本评测集 40 条业务题中
# 有 3 条属此类，直接压低 EX 上限并掩盖组间差异。组件级 F1 逐子句比对，每条样本贡献 6~7 个判定点，
# 分辨率比 EX 高一个量级，且能定位每个优化项的作用环节（召回→JOIN/WHERE，口径→聚合，增强→SELECT）。
_AGG_RE = re.compile(r"\b(sum|avg|count|max|min)\s*\(\s*(distinct\s+)?([^()]*?)\s*\)", re.I)
_COMP_KEYS = ("select", "agg", "where", "group", "order", "join", "limit")
SAFETY_LIMIT = "1000"   # sql_guard 自动追加的安全上限，gold 无 LIMIT 时视为等价


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """按分隔符切分，跳过括号内的分隔符（避免 COUNT(...) 内部被切开）。"""
    parts, buf, depth = [], [], 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _build_alias_map(sql: str) -> tuple[dict[str, str], set[str]]:
    alias_map: dict[str, str] = {}
    tables: set[str] = set()
    for m in re.finditer(r"\b(?:from|join)\s+([a-z_]\w*)(?:\s+(?:as\s+)?([a-z]\w*))?", sql, re.I):
        table, alias = m.group(1), m.group(2)
        tables.add(table)
        if alias and alias.lower() not in _SQL_STOPWORDS:
            alias_map[alias.lower()] = table
        alias_map.setdefault(table.lower(), table)
    return alias_map, tables


def parse_sql_components(sql: str) -> dict[str, set[str]]:
    """把 SQL 拆成语义组件集合，供组件级 F1 比对。

    归一化口径：表名限定还原（别名→真表名）、大小写统一、引号统一、
    `COUNT(col)` 归一为 `count(*)`（行数统计与取哪一列无关），`COUNT(DISTINCT col)` 保留列。
    """
    out = {k: set() for k in _COMP_KEYS}
    if not sql:
        return out
    s = re.sub(r"\bAS\s+[a-z_]\w*", "", sql, flags=re.I)   # 剥别名
    alias_map, tables = _build_alias_map(s)
    table_names = {t.lower() for t in tables}
    single = next(iter(tables)) if len(tables) == 1 else None

    def resolve(tok: str) -> str:
        tok = " ".join(tok.split()).lower().rstrip(",")
        tok = re.sub(r"[`'\"]", "", tok)
        if "." in tok:
            q, c = tok.split(".", 1)
            t = alias_map.get(q, q)
            return f"{t}.{c}"
        if single and tok not in _SQL_STOPWORDS and tok not in table_names:
            return f"{single}.{tok}"
        return tok

    def norm_expr(expr: str) -> str:
        """把表达式里的聚合与列做统一归一化（别名还原为真实表名）。"""
        e = expr
        def _agg(m: "re.Match[str]") -> str:
            fn = m.group(1).lower()
            distinct = bool(m.group(2))
            arg = " ".join(m.group(3).split()).lower().strip()
            arg = re.sub(r"[`'\"]", "", arg)
            if fn == "count" and not distinct:
                return "count(*)"          # COUNT(任意列) 等价 COUNT(*)
            if arg.startswith("*"):
                return f"{fn}(*)"
            return f"{fn}({resolve(arg)})" if arg else f"{fn}(*)"
        e = _AGG_RE.sub(_agg, e)
        # 限定列 alias.col → 真实表名.col（别名不同但语义相同，必须还原否则误判）
        e = re.sub(r"\b([a-z]\w*)\.([a-z_]\w*)\b",
                   lambda m: resolve(f"{m.group(1)}.{m.group(2)}"), e)
        # 剩余裸列（非聚合、无限定符）
        e = re.sub(r"(?<![\w.])([a-z_]\w*)\b(?!\s*\()", lambda m: resolve(m.group(0)), e)
        return " ".join(e.split())

    # ---- SELECT 列表 ----
    m_sel = re.search(r"\bselect\b(.*?)\bfrom\b", s, re.I | re.S)
    if m_sel:
        for it in _split_top_level(m_sel.group(1)):
            out["select"].add(norm_expr(it))
        for m in _AGG_RE.finditer(m_sel.group(1)):
            out["agg"].add(norm_expr(m.group(0)))

    # ---- WHERE 条件 ----
    m_where = re.search(
        r"\bwhere\b(.*?)(?=\bgroup\s+by\b|\border\s+by\b|\blimit\b|$)", s, re.I | re.S)
    if m_where:
        for cond in re.split(r"\s+and\s+", m_where.group(1), flags=re.I):
            cond = " ".join(cond.split()).strip()
            if cond and not cond.endswith("("):
                out["where"].add(norm_expr(cond))

    # ---- GROUP BY ----
    m_grp = re.search(
        r"\bgroup\s+by\b(.*?)(?=\bhaving\b|\border\s+by\b|\blimit\b|$)", s, re.I | re.S)
    if m_grp:
        for it in _split_top_level(m_grp.group(1)):
            out["group"].add(norm_expr(it))

    # ---- ORDER BY（含方向）----
    m_ord = re.search(r"\border\s+by\b(.*?)(?=\blimit\b|$)", s, re.I | re.S)
    if m_ord:
        for it in _split_top_level(m_ord.group(1)):
            parts = it.split()
            direction = "asc"
            if parts and parts[-1].lower() in ("asc", "desc"):
                direction = parts.pop().lower()
            col = norm_expr(" ".join(parts)) if parts else ""
            if col:
                out["order"].add(f"{col} {direction}")

    # ---- JOIN 关联条件（表对）----
    for m in re.finditer(r"\bjoin\s+([a-z_]\w*)(?:\s+(?:as\s+)?([a-z]\w*))?\s+on\s+(.*?)(?=\b(?:join|where|group|order|limit)\b|$)",
                         s, re.I | re.S):
        right = m.group(1)
        cond = " ".join(m.group(3).split())
        if "=" in cond:
            left_expr, right_expr = cond.split("=", 1)
            pair = tuple(sorted([norm_expr(left_expr), norm_expr(right_expr)]))
        else:
            pair = (norm_expr(cond), right)
        out["join"].add("|".join(pair))

    # ---- LIMIT ----
    m_lim = re.search(r"\blimit\s+(\d+)", s, re.I)
    if m_lim:
        out["limit"].add(m_lim.group(1))

    return out


def component_f1(gold: dict[str, set[str]], pred: dict[str, set[str]]) -> dict[str, float]:
    """逐组件集合 F1（limit 为精确匹配）。

    两边皆空记 1.0（该组件不存在，无需比对）；一边空一边非空记 0.0。
    """
    scores: dict[str, float] = {}
    for k in _COMP_KEYS:
        g, p = gold.get(k, set()), pred.get(k, set())
        if k == "limit":
            # sql_guard 会给每条 SQL 自动追加安全上限 LIMIT 1000，gold 无 LIMIT 时视为等价
            g_eff = set() if g == {SAFETY_LIMIT} else g
            p_eff = set() if p == {SAFETY_LIMIT} else p
            scores[k] = 1.0 if g_eff == p_eff else 0.0
            continue
        if not g and not p:
            scores[k] = 1.0
            continue
        if not g or not p:
            scores[k] = 0.0
            continue
        inter = len(g & p)
        if inter == 0:
            scores[k] = 0.0
            continue
        precision, recall = inter / len(p), inter / len(g)
        scores[k] = round(2 * precision * recall / (precision + recall), 3)
    return scores


def rows_equal(gold_rows: list, pred_rows: list) -> bool:
    """EX 等价判定：列数相同、行数相同、排序无关逐行比对。"""
    if len(gold_rows) != len(pred_rows):
        return False
    if not gold_rows:
        return True
    if any(len(g) != len(p) for g, p in zip(gold_rows, pred_rows)):
        return False
    key = lambda r: tuple(str(c) for c in r)  # noqa: E731
    for g, p in zip(sorted(gold_rows, key=key), sorted(pred_rows, key=key)):
        if not all(_cell_equal(a, b) for a, b in zip(g, p)):
            return False
    return True


# ---------------------------------------------------------------- 单条评测
async def run_one(item: dict, make_context) -> dict:
    """对一条 Golden 样本跑完整问数图，返回评测记录。

    make_context: () -> (context, meta_session, dw_session)——每条问题独立 session，
    避免并发时共享 AsyncSession。
    """
    question = item["question"]
    # gold 变体：同一问题可能存在多种等价正确写法（主要是输出列多寡）。
    # 仅用单一 gold 会把"逻辑全对、投影列不同"判为失败，人为压低 EX 上限。
    gold_sqls = [g for g in (item.get("gold_sqls") or [item.get("gold_sql", "")]) if g]
    record: dict = {
        "id": item["id"],
        "category": item["category"],
        "level": item.get("level"),
        "question": question,
        "gold_sql": gold_sqls[0] if gold_sqls else "",
        "pred_sql": None,
        "rejected": False,
        "reject_reason": None,
        "executed": False,      # 预测 SQL 是否成功执行
        "ex": False,
        "em": False,
        "attempts": 0,
        "elapsed_ms": 0,
        "error": None,
        "est_chars_in": 0,
        "est_chars_out": 0,
        # 召回层指标（Recall@K，实体级；out_of_scope 及无召回时为 None，不计入均值）
        "recall_table": None,
        "recall_column": None,
        "recall_metric": None,
        "gold_tables": [],
        "gold_columns": [],
        "missed_columns": [],
        # 组件级 F1（逐子句比对；分辨率高于 EX，用于定位优化项作用环节）
        "comp": {k: 0.0 for k in _COMP_KEYS},
        "comp_avg": 0.0,
        "gold_variant_hit": None,   # 命中的 gold 变体序号（-1=均未命中）
    }

    final_state: dict = {}
    result_event: dict | None = None
    last_err: Exception | None = None

    for round_no in range(1, MAX_ROUNDS_PER_ITEM + 1):
        final_state = {}
        result_event = None
        last_err = None
        context, meta_session, dw_session = make_context()
        t0 = time.perf_counter()
        try:
            state = DataAgentState(
                query=question, history=item.get("history") or [], conversation_id=None,
                sql_attempts=[], error_history=[], attempt_count=0,
                max_retries=app_config.agent.max_retries, final_sql=None,
            )
            async for mode, payload in graph.astream(
                input=state, context=context, stream_mode=["custom", "values"],
            ):
                if mode == "custom":
                    if isinstance(payload, dict) and payload.get("type") == "result":
                        result_event = payload
                elif mode == "values":
                    final_state = payload
            record["elapsed_ms"] = round((time.perf_counter() - t0) * 1000)
            break  # 跑通即出
        except Exception as e:  # noqa: BLE001 —— 单条失败不中断整轮
            last_err = e
            record["elapsed_ms"] = round((time.perf_counter() - t0) * 1000)
            logger.warning(f"评测 {item['id']} 第 {round_no} 轮异常: {type(e).__name__}: {e}")
        finally:
            # session 生命周期与单条问题绑定
            await meta_session.close()
            await dw_session.close()

    if last_err is not None and not final_state:
        record["error"] = f"{type(last_err).__name__}: {last_err}"
        return record

    # 图跑到一半抛异常时 final_state 只有部分字段，异常信息会被吞掉，
    # 导致只看到「无预测 SQL」而查不到真实原因（网关 5xx / 超时 / 解析失败）。此处显式保留。
    if last_err is not None:
        record["error"] = f"链路异常（未完成）: {type(last_err).__name__}: {last_err}"

    # ---- 从最终 state 提取预测结果 ----
    rejected = bool(final_state.get("rejected"))
    pred_sql = final_state.get("final_sql") or final_state.get("sql") or ""
    record["rejected"] = rejected
    record["reject_reason"] = final_state.get("reject_reason")
    record["pred_sql"] = pred_sql or None
    record["attempts"] = len(final_state.get("sql_attempts") or [])
    record["est_chars_in"] = len(question) + len(final_state.get("rewritten_query") or "")
    record["est_chars_out"] = len(pred_sql or "")

    # ---- 召回层指标：表 / 字段 / 指标 Recall@K（实体级，对 RRF 等融合策略最敏感）----
    if item["category"] != "out_of_scope":
        gold_tables, gold_columns = parse_gold_entities(item.get("gold_sql", ""))
        gold_metrics = set(item.get("gold_metrics") or [])
        ret_cols = [getattr(c, "id", "") for c in (final_state.get("retrieved_columns") or [])]
        ret_metrics = [getattr(m, "id", "") for m in (final_state.get("retrieved_metrics") or [])]
        ret_tables = sorted({c.split(".")[0] for c in ret_cols if "." in c})
        record["gold_tables"] = sorted(gold_tables)
        record["gold_columns"] = sorted(gold_columns)
        record["recall_table"] = recall_at_k(gold_tables, ret_tables, RECALL_TOP_K)
        record["recall_column"] = recall_at_k(gold_columns, ret_cols, RECALL_TOP_K)
        record["recall_metric"] = recall_at_k(gold_metrics, ret_metrics, RECALL_TOP_K)
        record["missed_columns"] = sorted(gold_columns - set(ret_cols[:RECALL_TOP_K]))

    # ---- out_of_scope：只判拒答，不比 SQL ----
    if item["category"] == "out_of_scope":
        return record

    if rejected:
        # 误拒答：有 gold_sql 却被拒 → EX/EM 均失败
        return record

    if not pred_sql:
        record["error"] = record["error"] or "无预测 SQL 且未拒答"
        return record

    # ---- EM：命中任一 gold 变体即算精确匹配 ----
    record["em"] = any(
        normalize_sql(pred_sql) == normalize_sql(g) for g in gold_sqls)

    # ---- 组件级 F1：对所有 gold 变体取最优 ----
    pred_comp = parse_sql_components(pred_sql)
    best: dict[str, float] = {k: 0.0 for k in _COMP_KEYS}
    best_avg = 0.0
    for g in gold_sqls:
        sc = component_f1(parse_sql_components(g), pred_comp)
        avg = round(sum(sc.values()) / len(sc), 3)
        if avg > best_avg:
            best, best_avg = sc, avg
    record["comp"], record["comp_avg"] = best, best_avg

    # ---- 执行预测 SQL（不落任何缓存）----
    try:
        _, m_session, dw_session = make_context()
        try:
            cursor = await dw_session.execute(text(pred_sql))
            pred_rows = [list(r) for r in cursor.fetchall()]
        finally:
            await m_session.close()
            await dw_session.close()
        record["executed"] = True

        # 逐一执行 gold 变体，任一变体结果集等价即判 EX 通过
        hit_idx = -1
        last_gold_rows: list = []
        for idx, g in enumerate(gold_sqls):
            _, gm_session, g_session = make_context()
            try:
                gcur = await g_session.execute(text(g))
                gold_rows = [list(r) for r in gcur.fetchall()]
            finally:
                await gm_session.close()
                await g_session.close()
            last_gold_rows = gold_rows
            if rows_equal(gold_rows, pred_rows):
                hit_idx = idx
                break

        record["gold_variant_hit"] = hit_idx
        record["ex"] = hit_idx >= 0
        if hit_idx < 0:
            record["error"] = record["error"] or (
                f"结果不等价: gold {len(last_gold_rows)} 行 / pred {len(pred_rows)} 行")
    except Exception as e:  # noqa: BLE001
        record["error"] = record["error"] or f"执行预测 SQL 失败 {type(e).__name__}: {e}"

    return record


# ---------------------------------------------------------------- 汇总
def summarize(records: list[dict]) -> dict:
    oos = [r for r in records if r["category"] == "out_of_scope"]
    biz = [r for r in records if r["category"] != "out_of_scope"]

    def _pct(n: int, d: int) -> float:
        return round(n / d * 100, 1) if d else 0.0

    metrics = {
        "total": len(records),
        "business_total": len(biz),
        "oos_total": len(oos),
        "EX": _pct(sum(1 for r in biz if r["ex"]), len(biz)),
        "EM": _pct(sum(1 for r in biz if r["em"]), len(biz)),
        "sql_valid_rate": _pct(sum(1 for r in biz if r["executed"]), len(biz)),
        "avg_attempts": round(sum(r["attempts"] for r in biz) / len(biz), 2) if biz else 0,
        "avg_elapsed_ms": round(sum(r["elapsed_ms"] for r in records) / len(records)) if records else 0,
        "reject_accuracy": _pct(sum(1 for r in oos if r["rejected"]), len(oos)),
        "false_reject_rate": _pct(sum(1 for r in biz if r["rejected"]), len(biz)),
        "est_chars_in_avg": round(sum(r["est_chars_in"] for r in records) / len(records)) if records else 0,
        "est_chars_out_avg": round(sum(r["est_chars_out"] for r in records) / len(records)) if records else 0,
    }

    # 召回层 Recall@K（只在有该字段的业务样本上取均值；out_of_scope 不计入）
    def _avg_recall(key: str) -> float | None:
        vals = [r[key] for r in biz if r.get(key) is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    metrics["recall_table"] = _avg_recall("recall_table")
    metrics["recall_column"] = _avg_recall("recall_column")
    metrics["recall_metric"] = _avg_recall("recall_metric")

    # 组件级 F1 均值（仅统计有预测 SQL 的业务样本；无 SQL/拒答时组件全 0 会拉低均值，
    # 但这类样本本就是失败样本，计入才不会高估）
    def _avg_comp(key: str) -> float:
        vals = [r["comp"][key] for r in biz if r.get("comp")]
        return round(sum(vals) / len(vals) * 100, 1) if vals else 0.0

    metrics["comp"] = {k: _avg_comp(k) for k in _COMP_KEYS}
    metrics["comp_avg"] = round(
        sum(r.get("comp_avg", 0.0) for r in biz) / len(biz) * 100, 1) if biz else 0.0

    by_cat: dict[str, dict] = {}
    for cat in sorted({r["category"] for r in records}):
        rs = [r for r in records if r["category"] == cat]
        c_biz = [r for r in rs if r["category"] != "out_of_scope"]
        entry = {"total": len(rs)}
        if c_biz:
            entry.update({
                "EX": _pct(sum(1 for r in c_biz if r["ex"]), len(c_biz)),
                "EM": _pct(sum(1 for r in c_biz if r["em"]), len(c_biz)),
                "valid": _pct(sum(1 for r in c_biz if r["executed"]), len(c_biz)),
                "avg_attempts": round(sum(r["attempts"] for r in c_biz) / len(c_biz), 2),
            })
        else:
            entry.update({"reject_accuracy": _pct(sum(1 for r in rs if r["rejected"]), len(rs))})
        by_cat[cat] = entry
    metrics["by_category"] = by_cat
    return metrics


def _fmt_pct(v: float | None) -> str:
    """0.857 → '85.7%'；None → '-'（该组无此维度样本）。"""
    return "-" if v is None else f"{round(v * 100, 1)}%"


def _fmt_comp(comp: dict | None) -> str:
    if not comp:
        return "-"
    return " ".join(f"{k}={comp.get(k, 0)}" for k in _COMP_KEYS)


def write_reports(records: list[dict], metrics: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    # report.json
    with open(out_dir / "report.json", "w", encoding="utf-8") as f:
        json.dump({"metrics": metrics, "records": records}, f, ensure_ascii=False, indent=2)

    # report.md
    lines: list[str] = ["# NL2SQL 智能问数系统 · Golden Set 评测报告", ""]
    lines += [
        "| 指标 | 值 |", "|---|---|",
        f"| 样本总数 | {metrics['total']}（业务类 {metrics['business_total']} / 越界类 {metrics['oos_total']}） |",
        f"| EX 执行准确率 | {metrics['EX']}% |",
        f"| EM 精确匹配率 | {metrics['EM']}% |",
        f"| SQL 有效率 | {metrics['sql_valid_rate']}% |",
        f"| 平均尝试次数（自省闭环） | {metrics['avg_attempts']} |",
        f"| 平均耗时 | {metrics['avg_elapsed_ms']} ms |",
        f"| 拒答准确率（out_of_scope） | {metrics['reject_accuracy']}% |",
        f"| 误拒答率（业务类被拒） | {metrics['false_reject_rate']}% |",
        f"| 平均输入字符（估算口径） | {metrics['est_chars_in_avg']} |",
        f"| 平均输出字符（估算口径） | {metrics['est_chars_out_avg']} |",
        f"| 表召回率 Recall@{RECALL_TOP_K} | {_fmt_pct(metrics.get('recall_table'))} |",
        f"| 字段召回率 Recall@{RECALL_TOP_K} | {_fmt_pct(metrics.get('recall_column'))} |",
        f"| 指标召回率 Recall@{RECALL_TOP_K} | {_fmt_pct(metrics.get('recall_metric'))} |",
        f"| 组件级综合 F1 | {metrics.get('comp_avg', 0)} |",
        "",
        "> token 口径说明：LLM 实例未暴露 usage 回调，采用字符数估算（输入=问题+改写后问题，输出=生成 SQL），非真实 token。",
        "> EM 归一化说明：统一小写/压缩空白/去末尾分号，并剥掉安全网关自动追加的尾部 LIMIT n。",
        "> 平均尝试说明：sql_attempts 含首次生成，故最小值为 1（生成即成功且直接执行为 2：生成+执行各记一条）。",
        "> EX 多参考答案说明：部分问题存在多种等价正确写法（主要是输出列多寡），gold 以 `gold_sqls` 列举变体，"
        "任一变体执行结果等价即判 EX 通过，避免把「逻辑全对、投影列不同」误判为失败。",
        "> 组件级 F1 说明：把 SQL 拆为 SELECT / 聚合 / WHERE / GROUP BY / ORDER BY / JOIN / LIMIT 七个子句，"
        "逐子句做集合 F1（两边皆空记满分），用于定位错误发生在哪个环节，分辨率高于端到端 EX。",
        "",
        "## 组件级指标（子句 F1，%）", "",
        "| 组件 | F1 | 说明 |",
        "|---|---|---|",
        f"| SELECT 投影 | {metrics['comp']['select']} | 输出列选择 |",
        f"| 聚合函数 | {metrics['comp']['agg']} | SUM/AVG/COUNT/MIN/MAX 及参数列 |",
        f"| WHERE 过滤 | {metrics['comp']['where']} | 过滤条件（列+操作符+值） |",
        f"| GROUP BY | {metrics['comp']['group']} | 分组维度 |",
        f"| ORDER BY | {metrics['comp']['order']} | 排序字段与方向 |",
        f"| JOIN 关联 | {metrics['comp']['join']} | 表关联条件 |",
        f"| LIMIT | {metrics['comp']['limit']} | 返回条数 |",
        "",
        "## 分类别指标", "",
        "| 类别 | 样本 | EX | EM | 有效率 | 平均尝试 | 拒答准确率 |",
        "|---|---|---|---|---|---|---|",
    ]
    for cat, m in metrics["by_category"].items():
        lines.append(
            f"| {cat} | {m['total']} | {m.get('EX', '-')} | {m.get('EM', '-')} | "
            f"{m.get('valid', '-')} | {m.get('avg_attempts', '-')} | {m.get('reject_accuracy', '-')} |"
        )

    lines += ["", "## 失败样例（bad case 候选）", ""]
    failures = [r for r in records
                if (r["category"] == "out_of_scope" and not r["rejected"])
                or (r["category"] != "out_of_scope"
                    and (r["rejected"] or not r["ex"] or r["error"]))]
    if not failures:
        lines.append("（无失败样例）")
    for r in failures:
        lines += [
            f"### {r['id']}（{r['category']}）",
            f"- 问题：{r['question']}",
            f"- 拒答：{r['rejected']}" + (f"（reason={r['reject_reason']}）" if r["reject_reason"] else ""),
            f"- gold_sql：`{r['gold_sql'] or '(期望拒答)'}`",
            f"- pred_sql：`{r['pred_sql'] or '(无)'}`",
            f"- EX={r['ex']} EM={r['em']} 尝试={r['attempts']} 耗时={r['elapsed_ms']}ms",
            f"- 组件 F1：{_fmt_comp(r.get('comp'))}",
            f"- 错误：{r['error'] or '（无）'}",
            "",
        ]

    with open(out_dir / "report.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------- 主流程
async def main() -> int:
    parser = ArgumentParser(description="NL2SQL 智能问数系统 Golden Set 离线评测")
    parser.add_argument("--golden", default=str(Path(__file__).resolve().parents[1] / "evaluation" / "golden_set.jsonl"))
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条")
    parser.add_argument("--category", default=None, help="只跑指定类别")
    parser.add_argument("--concurrency", type=int, default=1, help="并发数（默认 1，网关弱并发时最稳）")
    parser.add_argument("--output", default=str(Path(__file__).resolve().parents[1] / "evaluation"))
    args = parser.parse_args()

    items = load_golden_set(Path(args.golden), args.category, args.limit)
    if not items:
        print("无待评测样本"); return 1
    print(f"载入 {len(items)} 条样本，concurrency={args.concurrency}")
    print(f"LLM 通道: base_url={app_config.llm.base_url} model={app_config.llm.model_name}\n")

    # ---- 客户端初始化（与项目现有脚本一致） ----
    embedding_client_manager.init()
    qdrant_client_manager.init()
    es_client_manager.init()
    meta_mysql_client_manager.init()
    dw_mysql_client_manager.init()

    def make_context():
        """每条问题独立的 context + meta/dw session（AsyncSession 不可并发共享）。"""
        meta_session = meta_mysql_client_manager.session_factory()
        dw_session = dw_mysql_client_manager.session_factory()
        context = DataAgentContext(
            embedding_client=embedding_client_manager.client,
            column_qdrant_repository=ColumnQdrantRepository(qdrant_client_manager.client),
            value_es_repository=ValueESRepository(es_client_manager.client),
            metric_qdrant_repository=MetricQdrantRepository(qdrant_client_manager.client),
            value_qdrant_repository=ValueQdrantRepository(qdrant_client_manager.client),
            column_es_repository=ColumnESRepository(es_client_manager.client),
            metric_es_repository=MetricESRepository(es_client_manager.client),
            meta_mysql_repository=MetaMySQLRepository(meta_session),
            dw_mysql_repository=DWMySQLRepository(dw_session),
        )
        return context, meta_session, dw_session

    records: list[dict] = []
    sem = asyncio.Semaphore(args.concurrency)

    async def worker(item: dict) -> dict:
        async with sem:
            rec = await run_one(item, make_context)
            mark = "PASS" if ((rec["category"] == "out_of_scope" and rec["rejected"])
                              or (rec["category"] != "out_of_scope" and rec["ex"])) else "FAIL"
            print(f"[{mark}] {rec['id']} ({rec['category']:12s}) "
                  f"rejected={rec['rejected']} EX={rec['ex']} EM={rec['em']} "
                  f"尝试={rec['attempts']} {rec['elapsed_ms']}ms"
                  + (f" err={str(rec['error'])[:80]}" if rec["error"] else ""))
            return rec

    tasks = [asyncio.create_task(worker(it)) for it in items]
    records = await asyncio.gather(*tasks)

    # ---- 汇总与输出 ----
    metrics = summarize(list(records))
    write_reports(list(records), metrics, Path(args.output))

    print("\n========== 汇总 ==========")
    for k in ("total", "EX", "EM", "sql_valid_rate", "avg_attempts", "avg_elapsed_ms",
              "reject_accuracy", "false_reject_rate"):
        print(f"  {k}: {metrics[k]}")

    await qdrant_client_manager.close()
    await es_client_manager.close()
    await meta_mysql_client_manager.close()
    await dw_mysql_client_manager.close()
    print(f"\n报告已写入: {args.output}/report.md 与 report.json")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
