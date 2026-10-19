"""SQL 生成上下文增强：指标口径结构化注入 + Few-shot 相似示例检索 + 长度预算裁剪。

- 相似度：中文友好的字符二元组(bigram) Dice 系数 + 关键词命中率，零额外依赖、无网络开销；
- 长度控制：三段独立字符预算 + 总预算二级裁剪，优先级 指标口径 > few-shot > 原始召回明细；
- 降级：示例库缺失/为空、开关关闭均返回占位文本，绝不阻断主流程；
- 可追溯：build_enhanced_context 返回 trace，调用方写日志（注入了哪些示例与口径）。
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml
from omegaconf import OmegaConf

from app.core.log import logger

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TRUNC_SUFFIX = "\n…（内容过长，已按优先级截断）"


# ---------------- Few-shot 示例库加载 ----------------

@lru_cache(maxsize=4)
def load_few_shot_examples(examples_file: str) -> tuple[dict, ...]:
    """加载示例库；文件缺失/解析失败/为空均返回空元组（不影响主流程）。lru_cache 缓存解析结果。"""
    path = Path(examples_file)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    try:
        if not path.exists():
            logger.warning(f"Few-shot 示例库不存在，跳过示例注入: {path}")
            return ()
        data = OmegaConf.to_container(OmegaConf.load(path), resolve=True) or {}
        examples = data.get("examples") or []
        examples = [e for e in examples if e.get("question") and e.get("sql")]
        logger.info(f"加载 Few-shot 示例库: {path}，有效示例 {len(examples)} 条")
        return tuple(examples)
    except Exception as e:
        logger.warning(f"Few-shot 示例库加载失败，跳过示例注入: {type(e).__name__}: {e}")
        return ()


# ---------------- 相似度检索（字符 bigram Dice + 关键词命中） ----------------

def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text.lower())
    return {compact[i:i + 2] for i in range(max(0, len(compact) - 1))}


def _dice(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return 2.0 * len(a & b) / (len(a) + len(b))


def example_score(query: str, keywords: list[str], example_question: str) -> float:
    """问题相似度：bigram Dice 为主，关键词命中率加权（范围约 0~1.3）。"""
    dice = _dice(_bigrams(query), _bigrams(example_question))
    hit = 0.0
    if keywords:
        hit = sum(1 for kw in keywords if kw and kw in example_question) / len(keywords)
    return dice + 0.3 * hit


def metric_relevance(metric: dict, query: str, keywords: list[str]) -> float:
    """指标相关性（复用 example_score 思路）：对 name/每个别名/description 分别计算
    bigram Dice + 关键词命中，取最大值；全部为空时返回 0。"""
    candidates = [metric.get("name", "")]
    candidates += [a for a in (metric.get("alias") or []) if a]
    if metric.get("description"):
        candidates.append(metric["description"])
    candidates = [t for t in candidates if t]
    if not candidates:
        return 0.0
    return max(example_score(query, keywords or [], text) for text in candidates)


def select_few_shot(query: str, keywords: list[str], examples: tuple[dict, ...],
                    top_k: int, min_score: float = 0.0) -> tuple[list[dict], list[str]]:
    """按相似度挑选 top_k 示例。返回 (入选示例, 被过滤条目描述)。

    过滤规则：min_score>0 时要求 s >= min_score；min_score=0 退化为原行为（s > 0，
    完全无重合的不注入）。
    """
    scored = [(example_score(query, keywords or [], ex["question"]), ex) for ex in examples]
    floor = min_score if min_score and min_score > 0 else 0.0
    kept = [(s, ex) for s, ex in scored if s > 0 and s >= floor]
    dropped = [f"{ex['question']}({s:.2f})" for s, ex in scored if not (s > 0 and s >= floor)]
    kept.sort(key=lambda x: x[0], reverse=True)
    return [ex for _, ex in kept[:top_k]], dropped


# ---------------- 文本构造与长度预算 ----------------

def hard_cap(text: str, cap: int, suffix: str = _TRUNC_SUFFIX) -> str:
    """硬封顶：截断后缀长度计入预算，保证结果严格不超过 cap。"""
    if cap <= 0 or not text:
        return ""
    if len(text) <= cap:
        return text
    budget = max(0, cap - len(suffix))
    return text[:budget] + suffix


def format_few_shot(selected: list[dict], max_chars: int) -> str:
    """逐条装配示例并按字符预算累加（不会截断到半条）；预算耗尽即停止。"""
    if not selected:
        return "（无相似示例）"
    blocks: list[str] = []
    for i, ex in enumerate(selected, start=1):
        block = (f"示例{i}\n问题：{ex['question']}\n"
                 + (f"说明：{ex['note']}\n" if ex.get("note") else "")
                 + f"SQL：\n{str(ex['sql']).strip()}")
        candidate = "\n\n".join(blocks + [block])
        if len(candidate) > max_chars:
            if not blocks:  # 单条就超预算：硬截断保证不超限
                return hard_cap(block, max_chars)
            break
        blocks.append(block)
    return "\n\n".join(blocks)


def dump_table_infos(table_infos: list, cap: int) -> str:
    """按整张表条目边界序列化召回明细并应用字符预算：

    超预算时丢弃排在后面（低相关）的整表，绝不产生半截 YAML；
    末尾标注省略表数（note 长度计入预算，保证结果严格不超过 cap）。
    入参假定已按相关性排序（RRF 融合排序）。
    """
    blocks: list[str] = []
    total = len(table_infos or [])
    for idx, tb in enumerate(table_infos or []):
        block = yaml.dump(tb, allow_unicode=True, sort_keys=False).strip()
        will_drop = total - idx - 1  # 放入当前块后仍将剩余（被省略）的表数
        note = f"\n…（已省略 {will_drop} 张低相关数据表）" if will_drop > 0 else ""
        if len("\n".join(blocks + [block])) + len(note) > cap:
            break
        blocks.append(block)
    dropped = total - len(blocks)
    if dropped > 0:
        note = f"\n…（已省略 {dropped} 张低相关数据表）"
        body = "\n".join(blocks)
        if not body:  # 预算极小（连一张表都放不下）：至少给出不超限的提示
            return hard_cap(note, cap)
        return body + note  # 选块时已为 note 预留预算，总长严格 <= cap
    return "\n".join(blocks)


def format_metric_specs(metric_infos: list, max_chars: int) -> str:
    """把召回命中的指标结构化为口径文本（只陈述元数据中的事实，不臆造计算公式）。"""
    if not metric_infos:
        return "（本次问题未命中指标定义，基于问题语义与通用业务常识处理）"
    blocks: list[str] = []
    for m in metric_infos:
        aliases = "、".join(m.get("alias") or [])
        cols = "、".join(m.get("relevant_columns") or [])
        block = (f"【{m.get('name', '')}】" + (f"（别名：{aliases}）" if aliases else "") + "\n"
                 f"- 口径定义：{m.get('description', '')}\n"
                 f"- 关联字段：{cols or '（无）'}\n"
                 f"- 计算要求：必须严格按上述口径定义、在所列关联字段上计算，"
                 f"不得自行拆解口径、替换字段或引入其他度量。")
        candidate = "\n\n".join(blocks + [block])
        if len(candidate) > max_chars:
            if not blocks:
                return hard_cap(block, max_chars)
            break
        blocks.append(block)
    return "\n\n".join(blocks)


# ---------------- 总装 ----------------

def build_enhanced_context(
    *,
    query: str,
    keywords: list[str],
    table_infos: list,
    metric_infos_yaml: str,
    metric_infos: list,
    config,
) -> dict:
    """生成提示词三段增强文本与注入追溯信息。

    返回 {metric_specs, few_shot_examples, table_infos, trace}。
    任一增强关闭或数据缺失时对应段落降级为原始/占位文本，主流程不受影响。
    """
    # 1) 指标口径（优先级最高）：开启→结构化口径；关闭→保留原 yaml 文本（消融对照公平）
    # metric_spec_min_score>0 时按 metric_relevance 过滤低相关指标；=0 时不过滤（原行为）
    dropped_metrics: list[str] = []
    dropped_examples: list[str] = []
    if config.metric_spec.enable:
        metric_infos_effective = list(metric_infos or [])
        if config.metric_spec_min_score and config.metric_spec_min_score > 0:
            scored = [(metric_relevance(m, query, keywords), m) for m in metric_infos_effective]
            metric_infos_effective = [m for s, m in scored if s >= config.metric_spec_min_score]
            dropped_metrics = [f"{m.get('name')}({s:.2f})"
                               for s, m in scored if s < config.metric_spec_min_score]
        metric_text = format_metric_specs(metric_infos_effective, config.metric_spec.max_chars)
        trace_metrics = [m.get("name") for m in metric_infos_effective]
    else:
        metric_text = metric_infos_yaml
        trace_metrics = []

    # 2) Few-shot（优先级次之）：开启且示例库非空→相似度检索；否则占位
    trace_examples: list[str] = []
    if config.few_shot.enable:
        examples = load_few_shot_examples(config.few_shot.examples_file)
        selected, dropped_examples = select_few_shot(
            query, keywords, examples, config.few_shot.top_k, config.few_shot_min_score)
        few_shot_text = format_few_shot(selected, config.few_shot.max_chars)
        trace_examples = [ex["question"] for ex in selected]
    else:
        few_shot_text = "（未启用）"

    # 3) 原始召回明细（优先级最低）：按整张表条目边界裁剪，超预算丢弃尾部低相关表
    table_cap = config.table_infos_max_chars
    table_text = dump_table_infos(table_infos, table_cap)

    # 4) 总预算二级裁剪：先砍召回明细（整表丢弃，严格不超剩余预算），再砍 few-shot，指标口径不动
    table_budget = config.total_max_chars - len(metric_text) - len(few_shot_text)
    if len(table_text) > table_budget:
        table_text = dump_table_infos(table_infos, max(0, table_budget))
    overflow = len(metric_text) + len(few_shot_text) + len(table_text) - config.total_max_chars
    if overflow > 0 and config.few_shot.enable and few_shot_text not in ("（无相似示例）", "（未启用）"):
        few_shot_text = hard_cap(few_shot_text, max(0, len(few_shot_text) - overflow))

    trace = {
        "metric_spec_enabled": config.metric_spec.enable,
        "few_shot_enabled": config.few_shot.enable,
        "injected_metrics": trace_metrics,
        "injected_few_shot": trace_examples,
        "dropped_metrics": dropped_metrics,
        "dropped_examples": dropped_examples,
        "chars": {"metric_specs": len(metric_text),
                  "few_shot": len(few_shot_text),
                  "table_infos": len(table_text)},
    }
    logger.info(f"SQL上下文增强注入: 指标口径={trace_metrics}（过滤：{dropped_metrics}），"
                f"few-shot={trace_examples}（过滤：{dropped_examples}），"
                f"字符(口径/示例/明细)={trace['chars']}")
    return {
        "metric_specs": metric_text,
        "few_shot_examples": few_shot_text,
        "table_infos": table_text,
        "trace": trace,
    }
