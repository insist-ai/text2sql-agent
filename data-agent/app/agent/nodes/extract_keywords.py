import jieba.analyse

from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState
from langgraph.runtime import Runtime
from app.core.log import logger

async def extract_keywords(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    writer = runtime.stream_writer
    writer({"type": "progress", "step": "提取关键词", "status": "running"})
    query = state.get("rewritten_query") or state["query"]
    try:


        # 对查询进行分词，只提取指定词性的词
        allow_pos = (
            "n",  # 名词: 数据、服务器、表格
            "nr",  # 人名: 张三、李四
            "ns",  # 地名: 北京、上海
            "nt",  # 机构团体名: 政府、学校、某公司
            "nz",  # 其他专有名词: Unicode、哈希算法、诺贝尔奖
            "v",  # 动词: 运行、开发
            "vn",  # 名动词: 工作、研究
            "a",  # 形容词: 美丽、快速
            "an",  # 名形词: 难度、合法性、复杂度
            "eng",  # 英文
            "i",  # 成语
            "l",  # 常用固定短语
        )
        keywords = jieba.analyse.extract_tags(query, allowPOS=allow_pos)
        keywords = list(set(keywords + [query]))
        logger.info(f"提取到的关键词: {keywords}")
        writer({"type": "progress", "step": "提取关键词", "status": "success"})
        return {"keywords": keywords}
    except Exception as e:
        writer({"type": "progress", "step": "提取关键词", "status": "error"})
        logger.error(f"提取关键词失败: {str(e)}")
        raise