from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState
from app.core.log import logger

async def run_sql(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    writer = runtime.stream_writer
    writer({"type": "progress", "step": "执行SQL", "status": "running"})

    sql = state["sql"]

    dw_mysql_repository = runtime.context["dw_mysql_repository"]

    sql_attempts = list(state.get("sql_attempts") or [])
    error_history = list(state.get("error_history") or [])
    attempt_count = state.get("attempt_count") or 0

    try:
        result = await dw_mysql_repository.run_sql(sql)

        # 执行成功：记录最终 SQL 并下发结果
        attempt_count += 1
        sql_attempts.append({"attempt": attempt_count, "sql": sql, "stage": "execute", "error": None})
        writer({"type": "progress", "step": "执行SQL", "status": "success"})
        writer({"type": "result", "data": result})
        logger.info(f"执行SQL结果: {result}")
        return {"error": None, "final_sql": sql, "sql_attempts": sql_attempts,
                "error_history": error_history, "attempt_count": attempt_count}
    except Exception as e:
        # 执行失败：不抛出，记录错误交给图路由进入纠错或终止
        err_msg = str(e)
        attempt_count += 1
        sql_attempts.append({"attempt": attempt_count, "sql": sql, "stage": "execute", "error": err_msg})
        error_history.append(err_msg)
        writer({"type": "progress", "step": "执行SQL", "status": "error"})
        logger.error(f"执行SQL失败: {sql} 错误: {err_msg}")
        return {"error": err_msg, "sql_attempts": sql_attempts,
                "error_history": error_history, "attempt_count": attempt_count}
