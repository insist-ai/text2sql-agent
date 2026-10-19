import yaml
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.llm import llm
from app.agent.state import DataAgentState
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt


async def rewrite_query(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """查询改写节点：结合历史对话消解当前问题中的指代与省略，产出语义完整、可独立理解的问题。

    - 无历史对话时直接返回原问题，不做无谓 LLM 调用；
    - 节点内部异常不中断整图，失败时回退为原问题并记日志。
    """
    writer = runtime.stream_writer
    writer({"type": "progress", "step": "查询改写", "status": "running"})

    query = state["query"]
    history = state.get("history") or []
    if not history:
        writer({"type": "progress", "step": "查询改写", "status": "success", "rewritten_query": query})
        logger.info(f"无历史对话，跳过查询改写，使用原问题: {query}")
        return {"rewritten_query": query}

    try:
        prompt = PromptTemplate(template=load_prompt("rewrite_query"), input_variables=["history", "query"])
        output_parser = StrOutputParser()

        chain = prompt | llm | output_parser

        result = await chain.ainvoke(
            {"history": yaml.dump(history, allow_unicode=True, sort_keys=False),
             "query": query})

        # 兜底：LLM 输出为空时回退为原问题
        rewritten = result.strip() if isinstance(result, str) else ""
        if not rewritten:
            rewritten = query

        writer({"type": "progress", "step": "查询改写", "status": "success", "rewritten_query": rewritten})
        logger.info(f"查询改写结果: {rewritten}")
        return {"rewritten_query": rewritten}
    except Exception as e:
        # 改写失败不中断整图：回退为原问题继续执行
        writer({"type": "progress", "step": "查询改写", "status": "error"})
        logger.error(f"查询改写失败，回退为原问题({query}): {str(e)}")
        return {"rewritten_query": query}
