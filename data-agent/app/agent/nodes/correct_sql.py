import yaml
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.llm import llm_code
from app.agent.state import DataAgentState
from app.conf.app_config import app_config
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt


async def correct_sql(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    query = state.get("rewritten_query") or state["query"]
    sql = state["sql"]
    error = state.get("error")

    max_retries = state.get("max_retries") or app_config.agent.max_retries
    attempt_count = state.get("attempt_count") or 0
    error_history = list(state.get("error_history") or [])
    # 当前这次失败已计入 attempt_count；纠错次数 = 失败次数
    retry_no = len(error_history)
    writer = runtime.stream_writer

    if retry_no > max_retries:
        # 已达最大重试次数：给出用户可读的失败原因，交路由到 END
        reason = f"SQL 执行失败，已自动纠错重试 {max_retries} 次仍无法成功。最后错误：{error}"
        writer({"type": "progress", "step": "SQL纠错", "status": "error", "attempt": retry_no})
        logger.error(f"SQL 自省闭环终止：{reason}")
        return {"error": reason}

    writer({"type": "progress", "step": "SQL纠错", "status": "running", "attempt": retry_no})

    table_infos = state["table_infos"]
    metric_infos = state["metric_infos"]
    date_info = state["date_info"]
    db_info = state["db_info"]

    try:
        prompt = PromptTemplate(template=load_prompt("correct_sql"), input_variables=["query", "table_infos", "date_info", "db_info", "metric_infos", "sql", "error", "error_history"])
        output_parser = StrOutputParser()

        chain = prompt | llm_code | output_parser

        result = await chain.ainvoke(
            {"query": query,
             "table_infos": yaml.dump(table_infos, allow_unicode=True, sort_keys=False),
             "metric_infos": yaml.dump(metric_infos, allow_unicode=True, sort_keys=False),
             "date_info": yaml.dump(date_info, allow_unicode=True, sort_keys=False),
             "db_info": yaml.dump(db_info, allow_unicode=True, sort_keys=False),
             "sql": sql,
             "error": error,
             "error_history": yaml.dump(error_history, allow_unicode=True, sort_keys=False) if error_history else "（无历史错误）"
             })
        writer({"type": "progress", "step": "SQL纠错", "status": "success", "attempt": retry_no})
        logger.info(f"第 {retry_no} 次纠错后的SQL: {result}")
        # 清空当前错误，进入下一轮校验/执行
        return {"sql": result, "error": None}
    except Exception as e:
        err_msg = str(e)
        writer({"type": "progress", "step": "SQL纠错", "status": "error", "attempt": retry_no})
        logger.error(f"SQL纠错失败(第 {retry_no} 次): {err_msg}")
        # 纠错本身失败：记录并交路由（避免死循环/无限抛出）
        sql_attempts = list(state.get("sql_attempts") or [])
        error_history.append(f"纠错失败: {err_msg}")
        sql_attempts.append({"attempt": attempt_count + 1, "sql": sql, "stage": "correct", "error": err_msg})
        return {"error": f"SQL 自动纠错失败：{err_msg}", "sql_attempts": sql_attempts,
                "error_history": error_history, "attempt_count": attempt_count + 1}
