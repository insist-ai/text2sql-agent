"""数据范围边界：拒答标记的判定与用户可读话术。

LLM 在判定问题超出数仓数据范围时，输出固定标记 NO_ANSWER 而非 SQL；
本模块负责稳健识别该标记（容忍前后空白/换行/Markdown 代码块包裹），
并统一提供面向用户的拒答提示语，避免各处硬编码字符串。
"""
from __future__ import annotations

import re

REJECT_MARKER = "NO_ANSWER"

# 用户可读的拒答话术（前端直接展示，勿写入敏感信息）
REJECT_USER_MESSAGE = (
    "该问题超出了当前的数据范围，暂时无法回答。\n"
    "当前问数仅覆盖「电商销售」业务域（订单、客户、商品、地区、日期），"
    "例如：统计每个省份的销售总额 / 查询销售额最高的前5个客户。\n"
    "请换一个与销售数据相关的问题。"
)

_FENCE_RE = re.compile(r"```[a-zA-Z]*|```")


def is_rejection(text: str | None) -> bool:
    """判断 LLM 输出是否为拒答标记（容忍空白、换行、代码块包裹）。

    Args:
        text: LLM 原始输出（generate_sql 未做 strip，故此处需自行归一）。

    Returns:
        True 表示该输出为拒答标记而非 SQL。
    """
    if not text:
        return False
    cleaned = _FENCE_RE.sub("", text).strip()
    if not cleaned:
        return False
    upper = cleaned.upper()
    if upper == REJECT_MARKER:
        return True
    # 宽松兜底：首行为标记且整体不含 SELECT，避免误判含标记的正常 SQL
    head = cleaned.splitlines()[0].strip().upper()
    return head == REJECT_MARKER and "SELECT" not in upper
