import yaml
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.context_enhancer import build_enhanced_context
from app.agent.llm import llm, llm_sc
from app.agent.self_consistency import generate_self_consistent_sql
from app.agent.state import DataAgentState
from app.conf.app_config import app_config
from app.core.log import logger
from app.core.rejection import REJECT_USER_MESSAGE, is_rejection
from app.prompt.prompt_loader import load_prompt

async def generate_sql(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    writer = runtime.stream_writer
    writer({"type": "progress", "step": "生成SQL", "status": "running"})

    query = state.get("rewritten_query") or state["query"]
    table_infos = state["table_infos"]
    metric_infos = state["metric_infos"]
    date_info = state["date_info"]
    db_info = state["db_info"]
    history = state.get("history") or []

    try:
        prompt = PromptTemplate(template=load_prompt("generate_sql"),
                                input_variables=["query", "table_infos", "metric_specs",
                                                 "few_shot_examples", "date_info", "db_info", "history"])
        output_parser = StrOutputParser()

        metric_infos_yaml = yaml.dump(metric_infos, allow_unicode=True, sort_keys=False)
        # 上下文增强：指标口径结构化注入 + 相似 few-shot 检索 + 长度预算裁剪（均可配置开关）
        enhanced = build_enhanced_context(
            query=query,
            keywords=state.get("keywords") or [],
            table_infos=table_infos,
            metric_infos_yaml=metric_infos_yaml,
            metric_infos=metric_infos,
            config=app_config.context_enhance,
        )
        variables = {"query": query,
                     "table_infos": enhanced["table_infos"],
                     "metric_specs": enhanced["metric_specs"],
                     "few_shot_examples": enhanced["few_shot_examples"],
                     "date_info": yaml.dump(date_info, allow_unicode=True, sort_keys=False),
                     "db_info": yaml.dump(db_info, allow_unicode=True, sort_keys=False),
                     "history": yaml.dump(history, allow_unicode=True, sort_keys=False) if history else "（无历史对话）"
                     }

        sc_config = app_config.self_consistency
        if not sc_config.enable:
            # 开关关闭：与原实现完全一致的单次确定性生成（temperature=0）
            chain = prompt | llm | output_parser
            result = await chain.ainvoke(variables)
            writer({"type": "progress", "step": "生成SQL", "status": "success"})

            # 数据范围边界：LLM 判定问题超出数仓范围时输出 NO_ANSWER，不再生成 SQL
            if is_rejection(result):
                writer({"type": "rejected", "reason": "out_of_scope",
                        "message": REJECT_USER_MESSAGE})
                logger.warning(f"判定问题超出数据范围，拒绝生成SQL: {query}")
                return {"rejected": True, "reject_reason": "out_of_scope", "sql": ""}

            logger.info(f"生成的SQL: {result}")
            return {"sql": result}

        # 开关开启：Self-Consistency（并行采样 → 多数投票 → 执行结果一致 → t0 回退）
        writer({"type": "progress", "step": "生成SQL", "status": "running",
                "detail": f"self_consistency sampling x{sc_config.n_samples}"})
        sc_result = await generate_self_consistent_sql(
            prompt=prompt,
            variables=variables,
            llm_sample=llm_sc,
            llm_zero=llm,
            config=sc_config,
            dw_mysql_repository=runtime.context["dw_mysql_repository"],
        )

        # 数据范围边界：Self-Consistency 选出的候选若为拒答标记，同样短路（当前开关默认关闭，保持一致）
        if is_rejection(sc_result["sql"]):
            writer({"type": "progress", "step": "生成SQL", "status": "success"})
            writer({"type": "rejected", "reason": "out_of_scope",
                    "message": REJECT_USER_MESSAGE})
            logger.warning(f"判定问题超出数据范围，拒绝生成SQL: {query}")
            return {"rejected": True, "reject_reason": "out_of_scope", "sql": ""}

        writer({"type": "progress", "step": "生成SQL", "status": "success"})
        # 独立事件类型：候选/投票/耗时供前端展示与论文统计，不改变既有 progress/result 协议
        writer({"type": "self_consistency",
                "status": "success",
                "n_samples": sc_config.n_samples,
                "samples": sc_result["samples"],
                "selected": sc_result["sql"],
                "strategy": sc_result["strategy"],
                "vote_detail": sc_result["vote_detail"],
                "valid_sample_count": sc_result["valid_sample_count"],
                "candidate_unique_count": sc_result["candidate_unique_count"],
                "candidate_agreement_rate": sc_result["candidate_agreement_rate"],
                "text_unique_count": sc_result["text_unique_count"],
                "semantic_cluster_count": sc_result["semantic_cluster_count"],
                "semantic_agreement_rate": sc_result["semantic_agreement_rate"],
                "cluster_mode": sc_result["cluster_mode"],
                "semantic_clusters": sc_result["semantic_clusters"],
                "elapsed_ms": sc_result["elapsed_ms"]})
        logger.info(f"生成的SQL(self-consistency, {sc_result['strategy']}): {sc_result['sql']}")
        return {"sql": sc_result["sql"],
                "sc_samples": sc_result["samples"],
                "sc_selected": sc_result["sql"],
                "sc_vote_detail": {"strategy": sc_result["strategy"],
                                   "vote_detail": sc_result["vote_detail"],
                                   "elapsed_ms": sc_result["elapsed_ms"],
                                   "n_samples": sc_config.n_samples}}
    except Exception as e:
        writer({"type": "progress", "step": "生成SQL", "status": "error"})
        logger.error(f"生成SQL失败: {str(e)}")
        raise
