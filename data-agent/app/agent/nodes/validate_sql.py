from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState
from app.conf.app_config import app_config
from app.core.log import logger
from app.core.rejection import REJECT_USER_MESSAGE, is_rejection
from app.core.sql_guard import SqlGuardError, SqlGuardPolicy, check_sql

async def validate_sql(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    writer = runtime.stream_writer
    writer({"type": "progress", "step": "验证SQL", "status": "running"})

    dw_mysql_repository = runtime.context["dw_mysql_repository"]

    sql = state["sql"]
    sql_attempts = list(state.get("sql_attempts") or [])
    error_history = list(state.get("error_history") or [])
    attempt_count = state.get("attempt_count") or 0

    # ---- 兜底：若上游漏判、SQL 仍是拒答标记，则在此拦截，不再走安全校验与纠错闭环 ----
    if is_rejection(sql):
        writer({"type": "progress", "step": "验证SQL", "status": "success"})
        writer({"type": "rejected", "reason": "out_of_scope", "message": REJECT_USER_MESSAGE})
        logger.warning("SQL 为拒答标记，跳过校验与执行")
        return {"rejected": True, "reject_reason": "out_of_scope", "error": None, "sql": ""}

    # ---- 安全网关校验（执行前第一道防线：只读/单语句/无注释/强制 LIMIT） ----
    guard_policy = SqlGuardPolicy.from_config(app_config.sql_guard)
    try:
        guarded_sql = check_sql(sql, guard_policy)
    except SqlGuardError as e:
        # 用户可读的安全拒绝事件（不暴露内部表结构），并进入自省闭环交由纠错节点改写
        attempt_count += 1
        sql_attempts.append({"attempt": attempt_count, "sql": sql, "stage": "guard", "error": str(e)})
        error_history.append(str(e))
        writer({"type": "progress", "step": "安全校验", "status": "error"})
        writer({"type": "security", "status": "rejected", "reason": str(e)})
        logger.warning(f"SQL 被安全策略拒绝: {str(e)} | 原始SQL: {sql}")
        return {"error": str(e), "sql_attempts": sql_attempts,
                "error_history": error_history, "attempt_count": attempt_count}

    # ---- 静态校验（EXPLAIN，捕获语法/字段不存在等数据库侧错误） ----
    try:
        await dw_mysql_repository.validate_sql(guarded_sql)
        writer({"type": "progress", "step": "验证SQL", "status": "success"})
        logger.info(f"SQL验证成功: {guarded_sql}")
        # 校验通过：以后续执行使用改写后的 SQL（已强制 LIMIT）
        attempt_count += 1
        sql_attempts.append({"attempt": attempt_count, "sql": guarded_sql, "stage": "validate", "error": None})
        return {"sql": guarded_sql, "error": None, "sql_attempts": sql_attempts,
                "error_history": error_history, "attempt_count": attempt_count}
    except Exception as e:
        err_msg = str(e)
        writer({"type": "progress", "step": "验证SQL", "status": "error"})
        logger.error(f"SQL验证失败: {guarded_sql} 错误: {err_msg}")
        # 校验失败：记录本次尝试与错误
        attempt_count += 1
        sql_attempts.append({"attempt": attempt_count, "sql": guarded_sql, "stage": "validate", "error": err_msg})
        error_history.append(err_msg)
        return {"error": err_msg, "sql_attempts": sql_attempts, "error_history": error_history,
                "attempt_count": attempt_count}
