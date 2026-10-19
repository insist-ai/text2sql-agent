from langchain.chat_models import init_chat_model
from langchain_openai import ChatOpenAI
from app.conf.app_config import app_config

# 公共连接参数：api_key / base_url / 超时 / 重试
# 第三方/公益网关可能间歇性“挂死”（不返回、不报错），必须设置超时并允许自动重试，
# 否则一次挂死会让整个问数请求永久卡住。
_common = {
    "api_key": app_config.llm.api_key,
    "base_url": app_config.llm.base_url,
    "max_retries": app_config.llm.max_retries,
}
# request_timeout <= 0 表示不限制（不传该参数，采用 SDK 默认）
if app_config.llm.request_timeout and app_config.llm.request_timeout > 0:
    _common["request_timeout"] = app_config.llm.request_timeout

llm = ChatOpenAI(
    model=app_config.llm.model_name,
    temperature=0,
    **_common,
)

# 代码/纠错类任务专用模型（SQL 纠错等场景使用，与通用对话模型解耦）
llm_code = ChatOpenAI(
    model=app_config.llm.model_name_code,
    temperature=0,
    **_common,
)

# Self-Consistency 采样专用实例：温度调高以产生多样化候选
llm_sc = ChatOpenAI(
    model=app_config.llm.model_name,
    temperature=app_config.self_consistency.temperature,
    **_common,
)
