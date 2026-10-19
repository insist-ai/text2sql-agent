"""Self-Consistency SQL 生成：并行采样 N 份候选 → 多数投票 → 执行结果一致性 → t0 回退。

选择策略（按优先级）：
  1. majority_vote          归一化后存在至少 2 份相同候选 → 取多数票候选；
  2. equal_execution_result 全部不同，但其中若干候选【真实执行结果一致】→ 取该等价类；
  3. fallback_temperature_zero 以上均无法选出 → 回退 temperature=0 的确定性单次生成。

设计要点：
  - LLM 采样用 asyncio.gather 并发，Semaphore 控制并发上限；
  - 单份采样失败只丢弃该候选，不拖垮整批（降低失败率）；
  - 归一化仅去首尾空白/末尾分号/折叠连续空白并统一大写，不做语义改写。
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate

from app.core.log import logger

_WS_RE = re.compile(r"\s+")

# 方案 B：提示词视角扰动（语义级）。
# 依据（2026-10-12 实测）：当前网关/模型对 temperature=0.7、top_p=0.95、presence_penalty=0.3
# 均不敏感（同 prompt 连续 5 次采样 sha1 完全一致），采样参数无法制造多样性；
# 改为在第 i 份候选的 query 末尾追加一条轻量"作答视角提示"（i % len 轮换）。
# 三条视角覆盖不同【语义】层面（时间组织 / 维度切分 / 聚合口径），刻意不引导别名、
# JOIN 顺序等写法格式，使候选差异尽量落在语义而非排版上。
PERSPECTIVE_HINTS: list[str] = [
    "请从时间维度组织查询（如按月/按趋势）。",
    "请从维度切分组织查询（如按地区/按品类）。",
    "请明确聚合口径（求和 / 均值 / 去重计数）后再作答。",
]


def _perturb_variables(variables: dict, index: int) -> dict:
    """给第 index 份候选追加一条视角提示（仅改 query 文案，浅拷贝，不影响其它候选）。"""
    if not PERSPECTIVE_HINTS:
        return variables
    hint = PERSPECTIVE_HINTS[index % len(PERSPECTIVE_HINTS)]
    perturbed = dict(variables)
    perturbed["query"] = f"{variables.get('query', '')}\n（作答视角提示：{hint}）"
    return perturbed


def normalize_sql(sql: str) -> str:
    """归一化候选 SQL 用于等价比较：去首尾空白、去末尾分号、折叠空白、统一大写。"""
    return _WS_RE.sub(" ", sql.strip().rstrip(";").strip()).upper()


# 显式列别名（AS xxx / AS `xxx` / AS "xxx"）；表别名与隐式别名不在此处理
_ALIAS_RE = re.compile(
    r"""\s+AS\s+(?:"[^"]*"|`[^`]*`|'[^']*'|[^\s,]+)""", re.IGNORECASE
)


def strip_aliases(sql: str) -> str:
    """归一化后去除显式列别名(AS …)，作为"语义簇"的兜底键。

    仅去 AS 别名，不处理 JOIN 顺序/表别名/条件顺序——它只够区分
    「仅输出别名不同」这类文本分歧，精度低于真实执行结果聚类。
    """
    return _ALIAS_RE.sub("", normalize_sql(sql))


def _group_by(samples: list[str], key_func) -> tuple[dict[str, list[str]], list[str]]:
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for sample in samples:
        key = key_func(sample)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(sample)
    return groups, order


async def _sample_once(chain, variables: dict, semaphore: asyncio.Semaphore, index: int) -> str | None:
    async with semaphore:
        try:
            result = await chain.ainvoke(_perturb_variables(variables, index))
        except Exception as e:  # 单份失败不影响其它候选
            logger.warning(f"Self-Consistency 第 {index + 1} 份候选采样失败: {e}")
            return None
    result = (result or "").strip()
    return result or None


async def sample_candidates(chain, variables: dict, n_samples: int, max_concurrency: int) -> list[str]:
    """并发生成 n_samples 份候选，返回成功采样的非空列表（保持采样顺序）。

    每份候选按索引轮换注入一条视角提示（PERSPECTIVE_HINTS），以在采样参数
    被网关忽略时仍能获得多样化候选。
    """
    semaphore = asyncio.Semaphore(max(1, max_concurrency))
    results = await asyncio.gather(
        *[_sample_once(chain, variables, semaphore, i) for i in range(n_samples)]
    )
    return [r for r in results if r]


def majority_vote(samples: list[str]) -> tuple[str | None, str, list[dict]]:
    """归一化多数投票。

    返回 (selected_sql 或 None, strategy, detail)；
    最高票簇票数 < 2 时 selected 为 None、strategy='all_distinct'。
    """
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for sample in samples:
        key = normalize_sql(sample)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(sample)

    ranked = sorted(order, key=lambda k: len(groups[k]), reverse=True)
    detail = [
        {"normalized_sql": groups[k][0], "votes": len(groups[k])}
        for k in ranked
    ]
    top = ranked[0]
    if len(groups[top]) >= 2:
        return groups[top][0], "majority_vote", detail
    return None, "all_distinct", detail


async def _cluster_by_execution(
    samples: list[str], dw_mysql_repository
) -> tuple[dict[str, list[str]], list[str], list[str], dict[str, dict], list[str]]:
    """真实执行候选并按结果集聚类（只执行一次，同时产出两种键）。

    返回 (clusters, order, skipped, value_groups, value_order)：
      clusters/order  key=带列名的完整结果 JSON —— 供"全异回退选择"使用，保持历史行为；
      skipped         未通过安全网关/语法校验/执行失败的候选说明；
      value_groups    key=仅行值(忽略列别名)、行序无关 —— 供语义指标，避免
                      "仅输出别名不同"被误判为语义分歧。结构 {key: {members, preview}}；
      value_order     值簇首次出现顺序。
    """
    clusters: dict[str, list[str]] = {}
    order: list[str] = []
    value_groups: dict[str, dict] = {}
    value_order: list[str] = []
    skipped: list[str] = []
    for sample in samples:
        try:
            # run_sql 内部含安全网关兜底校验（只读/强制 LIMIT），并施加超时与行数上限
            rows = await dw_mysql_repository.run_sql(sample)
        except Exception as e:
            skipped.append(f"{sample[:60]}… -> {type(e).__name__}")
            continue
        key = json.dumps(rows, ensure_ascii=False, default=str, sort_keys=True)
        if key not in clusters:
            clusters[key] = []
            order.append(key)
        clusters[key].append(sample)

        # 纯行值键：去掉列名(别名)，并对行排序消除无 ORDER BY 时的返回顺序差异
        value_key = json.dumps(
            sorted(json.dumps(list(row.values()), default=str) for row in rows),
            ensure_ascii=False,
        )
        if value_key not in value_groups:
            value_groups[value_key] = {
                "members": [],
                "preview": [list(row.values()) for row in rows[:1]],
            }
            value_order.append(value_key)
        value_groups[value_key]["members"].append(sample)
    return clusters, order, skipped, value_groups, value_order


def _build_execution_detail(
    clusters: dict[str, list[str]], order: list[str], skipped: list[str]
) -> list[dict]:
    detail = [
        {"result_preview": json.loads(k)[:1], "cluster_size": len(clusters[k])}
        for k in order
    ]
    if skipped:
        detail.append({"skipped_unexecutable": skipped})
    return detail


async def select_by_equal_execution(
    samples: list[str], dw_mysql_repository
) -> tuple[str | None, list[dict]]:
    """全部候选互异时的回退：真实执行候选，按【结果集】聚等价类。

    - 无法通过安全网关/语法校验/执行失败的候选直接跳过；
    - 结果集（JSON 规范化）相同的候选归为一类，取最大类中的首个候选；
    - 全部不可执行或结果两两不同时返回 (None, detail)。
    """
    clusters, order, skipped, _, _ = await _cluster_by_execution(samples, dw_mysql_repository)
    detail = _build_execution_detail(clusters, order, skipped)
    if not clusters:
        return None, detail
    best = sorted(order, key=lambda k: len(clusters[k]), reverse=True)[0]
    return clusters[best][0], detail


def semantic_metric_from_clusters(
    samples: list[str],
    value_groups: dict[str, dict],
    value_order: list[str],
    exec_skipped: list[str],
) -> tuple[int, str, list[dict]]:
    """由一次执行聚类的【纯行值簇】构造语义指标（不再次执行）。

    全部候选均可执行 → execution（按结果行值，忽略别名差异）；
    否则降级为去 AS 别名文本聚类。
    """
    if not samples:
        return 0, "alias_stripped", []
    if not exec_skipped and value_order:
        detail = [
            {"cluster_size": len(value_groups[k]["members"]),
             "members": list(value_groups[k]["members"]),
             "result_values_preview": value_groups[k]["preview"]}
            for k in value_order
        ]
        return len(value_order), "execution", detail
    # 兜底：去 AS 别名后的文本聚类（无法区分 JOIN 顺序等其它等价改写，仅作降级口径）
    stripped_groups, stripped_order = _group_by(samples, strip_aliases)
    detail = [
        {"cluster_size": len(stripped_groups[k]),
         "members": list(stripped_groups[k]),
         "normalized_sql_no_alias": stripped_groups[k][0]}
        for k in stripped_order
    ]
    return len(stripped_order), "alias_stripped", detail


async def compute_semantic_clusters(
    samples: list[str], dw_mysql_repository
) -> tuple[int, str, list[dict]]:
    """语义级聚类指标（不改投票，仅统计）；独立调用入口，内部执行一次候选。

    返回 (semantic_cluster_count, cluster_mode, detail)：
      · 全部候选均可执行 → mode="execution"，按真实结果【行值】聚类（忽略列别名）；
      · 存在不可执行候选 → mode="alias_stripped"，退化为 normalize_sql 去 AS 别名后分组；
      · 无候选 → (0, "alias_stripped", [])。
    """
    if not samples:
        return 0, "alias_stripped", []
    _, _, skipped, value_groups, value_order = await _cluster_by_execution(
        samples, dw_mysql_repository
    )
    return semantic_metric_from_clusters(samples, value_groups, value_order, skipped)


async def generate_self_consistent_sql(
    *,
    prompt: PromptTemplate,
    variables: dict,
    llm_sample,
    llm_zero,
    config,
    dw_mysql_repository,
) -> dict:
    """完整自洽流程。返回 {sql, samples, strategy, vote_detail, elapsed_ms,
    valid_sample_count, candidate_unique_count(=text_unique_count),
    candidate_agreement_rate, semantic_cluster_count, semantic_agreement_rate,
    cluster_mode, semantic_clusters}。"""
    started = time.monotonic()

    sample_chain = prompt | llm_sample | StrOutputParser()
    samples = await sample_candidates(
        sample_chain, variables, config.n_samples, config.max_concurrency
    )
    logger.info(f"Self-Consistency 采样到 {len(samples)}/{config.n_samples} 份候选: {samples}")

    # 文本级一致性指标（用已有 normalize_sql 归一化统计，供消融报告/论文使用）
    valid_sample_count = len(samples)
    candidate_unique_count = len({normalize_sql(s) for s in samples}) if samples else 0
    text_unique_count = candidate_unique_count  # 文本级唯一数（与历史字段同值，新增名便于与语义级对照）
    max_votes = 0

    # 语义级聚类指标：全部候选只真实执行这一轮；指标与下方"全异→执行回退"共用同一执行结果。
    # 选择路径仍用带列名的 exec_clusters（历史行为不变）；语义指标用纯行值簇（忽略别名差异）。
    exec_clusters, exec_order, exec_skipped, value_groups, value_order = (
        await _cluster_by_execution(samples, dw_mysql_repository)
        if samples else ({}, [], [], {}, [])
    )
    semantic_cluster_count, cluster_mode, semantic_clusters = semantic_metric_from_clusters(
        samples, value_groups, value_order, exec_skipped
    )
    semantic_max_votes = (
        max((c.get("cluster_size", 0) for c in semantic_clusters), default=0)
    )
    semantic_agreement_rate = (
        round(semantic_max_votes / valid_sample_count, 4) if valid_sample_count else 0.0
    )

    strategy = "fallback_temperature_zero"
    vote_detail: list[dict] = []
    selected: str | None = None

    if samples:
        selected, strategy, vote_detail = majority_vote(samples)
        max_votes = vote_detail[0]["votes"] if vote_detail else 0
        if selected is None:
            # 归一化全部不同 → 比较真实执行结果（复用上方聚类，选择行为与此前完全一致）
            exec_detail = _build_execution_detail(exec_clusters, exec_order, exec_skipped)
            if exec_order:
                best = sorted(exec_order, key=lambda k: len(exec_clusters[k]), reverse=True)[0]
                selected = exec_clusters[best][0]
                strategy = "equal_execution_result"
            vote_detail.extend(exec_detail)

    if selected is None:
        # 采样全失败 / 全不同且执行结果也不一致 → temperature=0 确定性单次（与基线等价，兜底成功率）
        zero_chain = prompt | llm_zero | StrOutputParser()
        selected = (await zero_chain.ainvoke(variables)).strip()
        strategy = "fallback_temperature_zero"
        logger.info("Self-Consistency 回退到 temperature=0 单次生成")

    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        f"Self-Consistency 最终选择策略={strategy}, 耗时={elapsed_ms}ms, 选中SQL={selected}"
    )
    metrics = {
        "valid_sample_count": valid_sample_count,
        "candidate_unique_count": candidate_unique_count,
        "candidate_agreement_rate": round(max_votes / valid_sample_count, 4) if valid_sample_count else 0.0,
        # 语义级新增指标（纯加法，老调用方不读取也不受影响）
        "text_unique_count": text_unique_count,
        "semantic_cluster_count": semantic_cluster_count,
        "semantic_agreement_rate": semantic_agreement_rate,
        "cluster_mode": cluster_mode,
        "semantic_clusters": semantic_clusters,
    }
    # 指标随投票明细一并下发，便于前端/日志单帧获取完整统计
    vote_detail.append({"sc_metrics": metrics})
    logger.info(f"Self-Consistency 一致性指标: {metrics}")
    return {
        "sql": selected,
        "samples": samples,
        "strategy": strategy,
        "vote_detail": vote_detail,
        "elapsed_ms": elapsed_ms,
        **metrics,
    }
