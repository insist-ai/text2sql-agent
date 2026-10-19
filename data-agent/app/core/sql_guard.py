"""SQL 安全网关（执行前最后一道防线）。

设计目标（便于单测与论文描述，纯标准库实现、无三方依赖）：
  1. 只读白名单：仅允许 SELECT / WITH ... SELECT；
     INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/CREATE/GRANT/REPLACE/CALL 等一律拒绝。
  2. 语句约束：禁止多语句（分号拼接）；禁止注释绕过（--、/* */、#）。
  3. 强制 LIMIT：无顶层 LIMIT 时自动追加 default_limit；已有 LIMIT 超过 max_rows 时下调。
  4. 危险函数 / 系统库黑名单可配置（information_schema 等默认拦截）。
  5. 资源限制（execution_timeout_ms / max_rows / max_result_bytes）在仓储层配合执行。

关键实现：所有关键字/黑名单扫描都在"字面量掩码"后的文本上进行——
字符串('...')与反引号标识符(`...`)整体替换为占位符，避免字面量内容绕过或误伤。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---- 默认黑名单（均可被配置覆盖） ----
DEFAULT_BLOCKED_KEYWORDS: list[str] = [
    # 任务明确要求
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
    "CREATE", "GRANT", "REPLACE", "CALL",
    # 其它写/管理类危险语句
    "MERGE", "REVOKE", "LOAD", "HANDLER", "RENAME", "SHUTDOWN",
    "KILL", "PREPARE", "EXECUTE", "FLUSH", "PURGE", "LOCK", "UNLOCK",
]

DEFAULT_BLOCKED_FUNCTIONS: list[str] = [
    "SLEEP", "BENCHMARK", "LOAD_FILE", "GET_LOCK", "RELEASE_LOCK",
]

DEFAULT_BLOCKED_SCHEMAS: list[str] = [
    "information_schema", "mysql", "performance_schema", "sys",
]

# 字符串字面量（单/双引号，支持 '' 转义与反斜杠转义）
_STRING_RE = re.compile(r"'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"")
# 反引号标识符
_BACKTICK_RE = re.compile(r"`(?:``|[^`])*`")
# 顶层 LIMIT 子句（LIMIT n 或 LIMIT m,n 或 LIMIT n OFFSET m）
_LIMIT_RE = re.compile(
    r"\bLIMIT\s+(\d+)\s*(?:(,)\s*(\d+)|OFFSET\s+(\d+))?",
    re.IGNORECASE,
)
_KEYWORD_RE_TMPL = r"(?:^|[^A-Za-z0-9_])({keyword})(?:$|[^A-Za-z0-9_])"


class SqlGuardError(Exception):
    """安全策略拒绝异常。message 为可直接展示给用户的安全文案（不含内部结构）。"""

    def __init__(self, message: str, code: str = "policy_rejected"):
        super().__init__(message)
        self.code = code


@dataclass
class SqlGuardPolicy:
    default_limit: int = 1000
    max_rows: int = 1000
    max_result_bytes: int = 5 * 1024 * 1024
    execution_timeout_ms: int = 10_000
    blocked_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_KEYWORDS))
    blocked_functions: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_FUNCTIONS))
    blocked_schemas: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_SCHEMAS))

    @classmethod
    def from_config(cls, cfg) -> "SqlGuardPolicy":
        """从 app_config.sql_guard(OmegaConf/dataclass) 构建，缺项用默认值。"""
        return cls(
            default_limit=getattr(cfg, "default_limit", cls.default_limit),
            max_rows=getattr(cfg, "max_rows", cls.max_rows),
            max_result_bytes=getattr(cfg, "max_result_bytes", cls.max_result_bytes),
            execution_timeout_ms=getattr(cfg, "execution_timeout_ms", cls.execution_timeout_ms),
            blocked_keywords=list(getattr(cfg, "blocked_keywords", DEFAULT_BLOCKED_KEYWORDS)),
            blocked_functions=list(getattr(cfg, "blocked_functions", DEFAULT_BLOCKED_FUNCTIONS)),
            blocked_schemas=list(getattr(cfg, "blocked_schemas", DEFAULT_BLOCKED_SCHEMAS)),
        )

    @property
    def execution_timeout(self) -> float:
        return self.execution_timeout_ms / 1000.0


def _mask_literals(sql: str) -> str:
    """把字符串字面量与反引号标识符替换为占位符，防止内容参与规则扫描。"""
    return _BACKTICK_RE.sub("``", _STRING_RE.sub("''", sql))


def _contains_comment(sql: str) -> bool:
    """在跳过字符串/反引号的前提下检测注释序列（--、/*、#）。"""
    i, n = 0, len(sql)
    quote = None
    while i < n:
        ch = sql[i]
        if quote:
            # 字符串内部：处理转义/结束引号
            if ch == "\\" and quote != "`":
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and sql[i + 1] == ch and quote in ("'", '"', "`"):
                    i += 2
                    continue  # 双写转义 '' / "" / ``
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        if ch == "#":
            return True  # MySQL 行注释
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            return True  # SQL 行注释
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            return True  # 块注释（含 /*+ */ 优化器提示，统一禁止）
        i += 1
    return False


def _find_top_level(masked: str, keyword: str) -> int:
    """返回关键字在括号深度 0 处首次出现的起始位置；不存在返回 -1。"""
    pattern = re.compile(rf"\b{keyword}\b", re.IGNORECASE)
    depth = 0
    for m in pattern.finditer(masked):
        depth += masked.count("(", 0, m.start()) - masked.count(")", 0, m.start())
        if depth == 0:
            return m.start()
    return -1


def _enforce_limit(masked: str, raw: str, policy: SqlGuardPolicy) -> str:
    """无顶层 LIMIT 则追加 default_limit；已有 LIMIT 的行数超过 max_rows 则下调。"""
    limit_pos = _find_top_level(masked, "LIMIT")
    if limit_pos < 0:
        return f"{raw.rstrip().rstrip(';')} LIMIT {policy.default_limit}"

    m = _LIMIT_RE.search(masked, limit_pos)
    if not m:
        return raw  # 形如 LIMIT ? 的非常见写法，保持原样交给数据库

    if m.group(2) == ",":  # LIMIT offset, count
        offset, count = int(m.group(1)), int(m.group(3))
        if count <= policy.max_rows:
            return raw
        return raw[:m.start(3)] + str(policy.max_rows) + raw[m.end(3):]

    count = int(m.group(1))  # LIMIT count [OFFSET offset]
    if count <= policy.max_rows:
        return raw
    return raw[:m.start(1)] + str(policy.max_rows) + raw[m.end(1):]


def check_sql(sql: str, policy: SqlGuardPolicy | None = None) -> str:
    """校验并改写 SQL。

    - 违反任一安全规则抛 SqlGuardError（message 为用户可读的安全文案）；
    - 校验通过返回可直接执行的 SQL（已强制 LIMIT）。
    """
    policy = policy or SqlGuardPolicy()

    if not isinstance(sql, str) or not sql.strip():
        raise SqlGuardError("该请求被安全策略拒绝：SQL 内容为空。")

    raw = sql.strip().rstrip(";").strip()

    # 规则 2a：注释绕过
    if _contains_comment(sql):
        raise SqlGuardError("该请求被安全策略拒绝：SQL 中不允许包含注释（--、/* */、#）。")

    # 规则 2b：多语句（允许末尾一个分号，其外再出现分号即判定拼接）
    masked = _mask_literals(raw)
    if ";" in masked:
        raise SqlGuardError("该请求被安全策略拒绝：仅允许执行单条查询语句，检测到分号拼接。")

    # 规则 1：只读白名单（首关键字必须是 SELECT / WITH，且 WITH 语句中必须包含 SELECT）
    first_kw = re.match(r"\s*([A-Za-z]+)", masked)
    first_kw = first_kw.group(1).upper() if first_kw else ""
    if first_kw not in ("SELECT", "WITH"):
        raise SqlGuardError("该请求被安全策略拒绝：仅允许只读查询（SELECT）。")
    if first_kw == "WITH" and not re.search(r"\bSELECT\b", masked, re.IGNORECASE):
        raise SqlGuardError("该请求被安全策略拒绝：仅允许只读查询（WITH 后必须为 SELECT）。")

    # 规则 1b：危险关键字（字面量/标识符已掩码，避免内容误伤）
    upper = masked.upper()
    for kw in policy.blocked_keywords:
        if re.search(_KEYWORD_RE_TMPL.format(keyword=re.escape(kw)), upper):
            raise SqlGuardError(
                f"该请求被安全策略拒绝：检测到禁止的写操作或管理关键字 {kw}，仅允许只读查询。"
            )

    # 规则 5b：INTO OUTFILE / DUMPFILE 导出
    if re.search(r"\bINTO\s+(OUTFILE|DUMPFILE)\b", upper):
        raise SqlGuardError("该请求被安全策略拒绝：禁止将查询结果导出到服务器文件。")

    # 规则 5a：危险函数
    for fn in policy.blocked_functions:
        if re.search(rf"\b{re.escape(fn)}\s*\(", upper):
            raise SqlGuardError(f"该请求被安全策略拒绝：检测到禁止的函数 {fn}。")

    # 规则 5c：系统库 / 敏感库
    for schema in policy.blocked_schemas:
        if re.search(rf"\b{re.escape(schema)}\s*\.", masked, re.IGNORECASE):
            raise SqlGuardError(f"该请求被安全策略拒绝：禁止访问系统/敏感数据库 {schema}。")

    # 规则 3：强制 LIMIT（同时把上限收敛到 max_rows）
    return _enforce_limit(masked, raw, policy)
