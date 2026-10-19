from typing import TypedDict

from app.entities.column_info import ColumnInfo
from app.entities.metric_info import MetricInfo
from app.entities.value_info import ValueInfo


class MetricInfoState(TypedDict):
    name: str
    description: str
    relevant_columns: list[str]
    alias: list[str]
class ColumnInfoState(TypedDict):
    name: str
    type: str
    role: str
    examples: list
    description: str
    alias: list[str]
class TableInfoState(TypedDict):
    name: str
    role: str
    description: str
    columns: list[ColumnInfoState]

class DateInfoState(TypedDict):
    date: str
    weekday: str
    quarter: str

class DBInfoState:
    dialect: str
    version: str


class SqlAttemptState(TypedDict):
    attempt: int
    sql: str
    stage: str       # generate | validate | execute
    error: str | None

class DataAgentState(TypedDict):
    query: str
    # 查询改写后的完整问题(结合历史消解指代与省略)；无历史时等于 query，下游节点优先使用
    rewritten_query: str
    keywords: list[str]
    retrieved_columns: list[ColumnInfo]
    retrieved_metrics: list[MetricInfo]
    retrieved_values: list[ValueInfo]

    # ===== 双路召回原始结果（列表顺序即原始排名，供 fuse_recall 计算 RRF） =====
    vector_columns: list[ColumnInfo]
    keyword_columns: list[ColumnInfo]
    vector_metrics: list[MetricInfo]
    keyword_metrics: list[MetricInfo]
    vector_values: list[ValueInfo]
    keyword_values: list[ValueInfo]

    table_infos: list[TableInfoState]
    metric_infos: list[MetricInfoState]

    date_info:DateInfoState
    db_info:DBInfoState

    sql: str

    # ===== SQL 自省闭环（生成→校验→执行→纠错 可循环） =====
    error: str                   # 当前错误信息；为 None 表示无错误
    sql_attempts: list[SqlAttemptState]  # 每次尝试记录(供落库/评测)
    error_history: list[str]     # 历次错误信息(供纠错参考)
    attempt_count: int           # 已尝试次数
    max_retries: int             # 最大纠错重试次数(默认取 app_config.agent.max_retries)
    final_sql: str               # 最终成功执行的 SQL（未成功则为空）

    # ===== Self-Consistency 记录（供论文统计/评测；关闭开关时不写入） =====
    sc_samples: list             # 并行采样的候选 SQL 列表
    sc_selected: str             # 投票/回退后最终选中的 SQL
    sc_vote_detail: dict         # {strategy, vote_detail, elapsed_ms, n_samples}

    # 会话上下文：最近若干轮历史消息（[{"role":...,"content":...}, ...]），供 LLM 参考
    history: list
    # 当前会话 ID（自动新建或前端传入，已校验归属）
    conversation_id: int | None

    # ===== 数据范围拒答（问题超出数仓数据范围时置位） =====
    rejected: bool
    reject_reason: str | None
