from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parents[2] / ".env")

# 日志配置
from dataclasses import dataclass

from omegaconf import OmegaConf

@dataclass
class File:
    enable: bool
    level: str
    path: str
    rotation: str
    retention: str



@dataclass
class Console:
    enable: bool
    level: str


@dataclass
class LoggingConfig:
    file: File
    console: Console

# 数据库配置

@dataclass
class DBConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


@dataclass
class QdrantConfig:
    host: str
    port: int
    embedding_size: int


@dataclass
class EmbeddingConfig:
    host: str
    port: int
    model: str


@dataclass
class ESConfig:
    host: str
    port: int
    index_name: str


@dataclass
class AgentConfig:
    rrf_enable: bool    # 召回融合开关：false 时退化为两路并集去重后截断 Top-N（消融 baseline）
    rrf_k: int          # RRF 常数 k：score = Σ 1/(k + rank)，越大各名次分数差异越平缓
    recall_top_k: int   # 每路召回(向量/关键词)参与 RRF 融合的 Top-K 截断
    es_min_score: float # 字段/指标关键词路 ES 相关性下限；0 表示不过滤
    qdrant_min_score: float # 向量路(字段/指标/取值) Qdrant 相似度下限；0 表示不过滤
    fuse_top_n: int     # RRF 融合后每类(字段/指标/取值)保留的 Top-N
    max_retries: int    # SQL 自省闭环：最大纠错重试次数


@dataclass
class SqlGuardConfig:
    default_limit: int           # 无 LIMIT 时自动追加的行数
    max_rows: int                # 返回行数硬上限
    max_result_bytes: int        # 结果集大小硬上限(字节)
    execution_timeout_ms: int    # 单条 SQL 执行超时(毫秒)
    blocked_keywords: list[str]  # 写操作/管理类禁止关键字黑名单
    blocked_functions: list[str] # 危险函数黑名单
    blocked_schemas: list[str]   # 系统/敏感库黑名单


@dataclass
class SelfConsistencyConfig:
    enable: bool           # 总开关：false 时生成节点与原单次(temperature=0)行为完全一致
    n_samples: int         # 同一问题并行采样的候选 SQL 份数
    temperature: float     # 采样温度（适当调高以产生多样化候选）
    max_concurrency: int   # 并发 LLM 调用上限（asyncio 信号量）


@dataclass
class FewShotConfig:
    enable: bool        # few-shot 示例注入开关
    top_k: int          # 按相似度挑选的示例条数
    max_chars: int      # few-shot 区块字符预算
    examples_file: str  # 示例库文件（相对项目根 conf/）


@dataclass
class MetricSpecConfig:
    enable: bool       # 指标口径结构化注入开关
    max_chars: int     # 指标口径区块字符预算


@dataclass
class ContextEnhanceConfig:
    few_shot: FewShotConfig
    metric_spec: MetricSpecConfig
    metric_spec_min_score: float  # 指标口径注入的相关性门槛（0 表示不过滤）
    few_shot_min_score: float     # few-shot 相似度门槛（0 退化为原先的 s>0 行为）
    table_infos_max_chars: int  # 原始召回明细(table_infos)字符预算（优先级最低，最先被裁剪）
    total_max_chars: int        # 口径+few-shot+召回明细 总字符预算（超长按 口径>few-shot>明细 裁剪）



@dataclass
class LLMConfig:
    model_name: str
    model_name_code: str
    api_key: str
    base_url: str
    request_timeout: int = 60  # 单次 LLM 请求超时(秒)。第三方网关可能间歇性挂死，必须设超时；设为 0/负数表示不限制
    max_retries: int = 2       # 超时/连接错误/5xx 时的自动重试次数（由 OpenAI SDK 执行）


@dataclass
class JWTConfig:
    secret_key: str
    algorithm: str
    access_token_expire_minutes: int
    refresh_token_expire_minutes: int


@dataclass
class AuthRateLimitConfig:
    """认证入口（登录/注册）专用额度。

    与业务接口的区别：登录/注册时尚无 token，无法按 user_id 计数，
    故改按「客户端 IP（登录再拼用户名）」维度限流，且额度比业务接口更严。
    复用父级 rate_limit.window_seconds 作为窗口长度。
    """

    login_max_requests: int     # 登录：单窗口内单 IP(+用户名) 最大尝试次数
    register_max_requests: int  # 注册：单窗口内单 IP 最大注册次数


@dataclass
class RateLimitConfig:
    enable: bool          # 总开关：false 时完全放行（便于本地调试）
    window_seconds: int   # 滑动窗口长度(秒)
    max_requests: int     # 单窗口内单用户对单接口的最大请求数
    auth: AuthRateLimitConfig  # 认证入口（登录/注册）分段额度


@dataclass
class RedisConfig:
    host: str
    port: int
    db: int
    password: str
    socket_timeout: float  # 连接/读写超时(秒)，避免 Redis 挂死拖垮请求


@dataclass
class QueryCacheConfig:
    enable: bool          # 总开关：false 时完全关闭缓存（不走缓存读写）
    ttl_seconds: int      # 缓存有效期(秒)
    key_prefix: str       # 键前缀，便于按前缀清除/区分命名空间
    max_entries: int      # 内存降级实现的最大条目数（防止内存无限增长）


@dataclass
class AppConfig:
    logging: LoggingConfig
    db_meta: DBConfig
    db_dw: DBConfig
    db_app: DBConfig
    qdrant: QdrantConfig
    embedding: EmbeddingConfig
    es: ESConfig
    agent: AgentConfig
    sql_guard: SqlGuardConfig
    self_consistency: SelfConsistencyConfig
    context_enhance: ContextEnhanceConfig
    llm: LLMConfig
    jwt: JWTConfig
    rate_limit: RateLimitConfig
    redis: RedisConfig
    query_cache: QueryCacheConfig


config_file = Path(__file__).parents[2] / 'conf' / 'app_config.yaml'
context = OmegaConf.load(config_file)
schema = OmegaConf.structured(AppConfig)
app_config: AppConfig = OmegaConf.to_object(OmegaConf.merge(schema, context))
