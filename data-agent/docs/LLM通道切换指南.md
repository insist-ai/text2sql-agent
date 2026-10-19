# LLM 通道切换指南（免费网关 ⇄ DeepSeek 官方）

> 用途：测试期用免费公益网关；**答辩当天一键切回 DeepSeek 官方**。
> 全程只改 2 个文件、共 4 行，**零代码改动**。改完重启后端即可。

---

## 一、两个通道对照

| | 通道A · 公益网关（测试用） | 通道B · DeepSeek 官方（答辩用） |
|---|---|---|
| base_url | `https://api.hcnsec.cn/v1` | `https://api.deepseek.com/v1` |
| model_name | `sensenova-6.8-flash-lite` | `deepseek-flash` |
| model_name_code | `sensenova-6.8-flash-lite` | `deepseek-flash` |
| api_key（.env） | `LLM_API_KEY` | `LLM_API_KEY`（换成官方的值） |
| 费用 | 免费 | 需充值（几元足够答辩） |
| 稳定性 | 约 60~75%，复杂查询会挂死 | 高，几乎不挂 |
| 速度 | 单次 2~6s | 单次 3~9s |

> 备注：DeepSeek 官方现可用的模型只有 `deepseek-flash` 与 `deepseek-v4-pro`（`deepseek-chat` 已下线）。
> 免费网关多数模型 504/挂起，实测只有 `sensenova-6.8-flash-lite`（最快最稳）、`longcat-2.5`、`step-5-preview` 可用；
> **`auto` 不要用**——路由目标会漂移，评测结果不可复现。

---

## 二️、切回 DeepSeek 官方（答辩前照做）

### 第 1 步：改 `data-agent/.env`

把当前生效的 `LLM_API_KEY` 换成官方的 key（文件里已存为 `DEEPSEEK_LLM_API_KEY`）：

```diff
- LLM_API_KEY=<免费网关 key>
+ LLM_API_KEY=<DeepSeek 官方 key>
```

> ⚠️ 官方 key 若已轮换，请用最新那把。

### 第 2 步：改 `data-agent/conf/app_config.yaml` 的 `llm:` 段

```diff
- model_name: sensenova-6.8-flash-lite
- model_name_code: sensenova-6.8-flash-lite
- base_url: https://api.hcnsec.cn/v1
+ model_name: deepseek-flash
+ model_name_code: deepseek-flash
+ base_url: https://api.deepseek.com/v1
```

同时把超时/重试调回官方档（可选，但推荐）：

```diff
- request_timeout: 30
- max_retries: 3
+ request_timeout: 60
+ max_retries: 2
```

### 第 3 步：重启后端

```bash
# 停掉旧进程（8000）
PID=$(netstat -ano | grep ":8000" | grep LISTEN | awk '{print $NF}' | head -1)
taskkill //F //PID $PID

# 启动新进程
cd data-agent
.venv/Scripts/python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

### 第 4 步：验证

```bash
# 1) 确认配置生效
.venv/Scripts/python.exe -c "from app.conf.app_config import app_config as c; print(c.llm.base_url, c.llm.model_name, c.llm.request_timeout, c.llm.max_retries)"

# 2) 直连官方验证 key 有效
curl -s https://api.deepseek.com/v1/models -H "Authorization: Bearer $KEY"
```

预期：`https://api.deepseek.com/v1 deepseek-flash 60 2`，且接口能列出模型。

---

## 三、切回免费网关（测试期，或官方 key 出问题时）

反向改同样 4 行：

```diff
# .env
- LLM_API_KEY=<官方key>
+ LLM_API_KEY=<免费网关 key>

# conf/app_config.yaml
- model_name: deepseek-flash
- model_name_code: deepseek-flash
- base_url: https://api.deepseek.com/v1
+ model_name: sensenova-6.8-flash-lite
+ model_name_code: sensenova-6.8-flash-lite
+ base_url: https://api.hcnsec.cn/v1
```

超时/重试改回 `30` / `3`，重启后端即可。

---

## 四、为什么会"挂死"、超时重试起什么作用

- 公益网关会间歇性**不返回、不报错、直接悬住**，且与模型无关（三个可用模型都会随机挂）。
- `ChatOpenAI` 默认**既不超时也不重试**，所以一次挂死会让整个问数请求**永久卡住**。
- 已在 `app/agent/llm.py` 统一接线：`request_timeout` 到点即断、`max_retries` 自动重试。
  效果：把"无限挂起"变成"最坏 3×30s 后干净报错"，服务不再被拖死。
- 该设置在**切回官方后同样是纯收益**（官方极少挂，但并非不会），建议保留。

---

## 五、相关文件清单

| 文件 | 作用 |
|---|---|
| `data-agent/.env` | 存两把 key（生效 + 备用） |
| `data-agent/conf/app_config.yaml` | `llm:` 段：base_url / model_name / request_timeout / max_retries |
| `data-agent/app/agent/llm.py` | 读取配置构建 `llm` / `llm_code` / `llm_sc` 三个实例 |
| `data-agent/app/conf/app_config.py` | `LLMConfig` 数据类定义 |

> 两个 key 均可直接填入；`.env` 已在 `.gitignore` 中，不会被提交。
