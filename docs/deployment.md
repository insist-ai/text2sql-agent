# Docker 部署与一键启动

> 交付物：两个 Dockerfile（后端 / 前端）+ 全栈编排 + 环境变量模板 + 本文档。
> 目标：在干净环境用**一条命令**起全栈，浏览器打开即可问数。

---

## 一、前置条件

| 项 | 要求 | 说明 |
|---|---|---|
| Docker | ≥ 24（本机实测 29.5.3） | 需带 Compose 插件 |
| 可用镜像加速 | 建议配置 | 拉 `python:3.12-slim` / `node:22-alpine` / `nginx` 等基础镜像 |
| 磁盘 | ≥ 10 GB | 镜像约 3~4 GB + 模型 1.3 GB + 数据卷 |
| 内存 | ≥ 8 GB | ES + Qdrant + TEI 同时运行较吃内存 |

### ⚠️ 必须先下载 Embedding 模型（仓库里没有）

`bge-large-zh-v1.5` 权重约 **1.3 GB**，已被 `.gitignore` 排除，**不在仓库内**。首次部署前必须单独下载到
`data-agent/docker/embedding/bge-large-zh-v1.5/`（该目录已存在则跳过）。

推荐用 modelscope（国内速度快，项目本身已依赖该库）：

```bash
cd data-agent
.venv/Scripts/python.exe -m modelscope.cli.cli download \
    --model BAAI/bge-large-zh-v1.5 \
    --local_dir docker/embedding/bge-large-zh-v1.5
```

或用 huggingface 镜像：

```bash
export HF_ENDPOINT=https://hf-mirror.com
huggingface-cli download BAAI/bge-large-zh-v1.5 \
    --local-dir docker/embedding/bge-large-zh-v1.5
```

下载完成后目录内应包含 `config.json`、`model.safetensors`、`tokenizer.json` 等文件。

---

## 二、一键启动

```bash
cd data-agent/docker
cp .env.example .env          # 首次：复制模板
# 编辑 .env，至少填好 MYSQL_ROOT_PASSWORD / MYSQL_PASSWORD / LLM_API_KEY / APP_JWT_SECRET_KEY

docker compose up -d --build  # 首次或代码变更后加 --build
```

首次启动会依次完成：

1. 拉起 6 个依赖服务（mysql / elasticsearch / kibana / qdrant / redis / embedding）
2. 构建 backend、frontend 镜像
3. **`backend-init` 一次性容器**按序执行：
   - `wait_for_deps`：TCP 探活，等 MySQL / ES / Qdrant / Embedding 全部可连接（最多 240s）
   - `init_app_db.py`：建立业务库表（用户 / 会话 / 消息 / 审计）
   - `build_meta_knowledge.py`：构建元数据知识库（meta 库 + ES 索引 + Qdrant 向量）
4. `backend` 在 init 成功完成后才启动，`frontend` 最后启动

> ⏱ **首次启动需要耐心**：Embedding 服务（TEI）在 CPU 模式下加载 1.3GB 的
> `bge-large-zh-v1.5` 并做 warmup，实测约 **5~6 分钟**才会进入 Ready 状态；
> 在此之前 `backend-init` 会一直等待（最长 600s）。这是 CPU 推理的固有耗时，不是卡死。
> 后续重启因镜像已缓存、模型已加载，会快很多。

查看进度：

```bash
docker compose logs -f embedding      # 看到 "Ready" 才表示 Embedding 可用
docker compose logs -f backend-init   # 看初始化（最关键的排查入口）
docker compose logs -f backend
docker compose ps
```

---

## 三、访问地址

| 服务 | 地址 |
|---|---|
| **前端（入口）** | http://localhost:8080 |
| 后端 API 文档 | http://localhost:8000/docs |
| Kibana | http://localhost:5601 |
| Qdrant 控制台 | http://localhost:6333/dashboard |

前端通过 nginx 同源部署：页面请求 `/api/*` 由 nginx 反代到 `backend:8000`，
因此**浏览器只需访问 8080**，无需单独处理跨域。

---

## 四、关键设计说明

### 1. 连接配置全部支持环境变量覆盖

`conf/app_config.yaml` 中数据库 / Qdrant / ES / Embedding / Redis 的 `host`、`port`
以及 LLM 的 `base_url`、`model_name` 均写成 `${oc.env:XXX,默认值}` 形式：

- **不设环境变量时**，取值与容器化改造前完全一致（localhost:3307 等），本地开发行为零变化；
- **容器内部**通过 compose 注入 `DB_HOST=mysql`、`DB_PORT=3306`、`EMBEDDING_PORT=80` 等按服务名访问。

> 注意容器内端口 ≠ 宿主映射端口：MySQL 容器内 `3306`（宿主 3307）、TEI 容器内 `80`（宿主 8081）。

### 2. 密钥不进镜像

`LLM_API_KEY`、`APP_JWT_SECRET_KEY`、`MYSQL_PASSWORD` 全部由 `.env` 在**运行时**注入容器，
镜像内只有占位串 `please-set-env-*`。经全量扫描确认仓库无明文密钥。

### 3. SSE 流式必须在 nginx 层放行

问数接口是 `text/event-stream` 流式响应。nginx 默认会缓冲响应体，导致前端看不到逐字输出。
`nginx.conf` 中专门配置了 `proxy_buffering off`、`gzip off`、`proxy_read_timeout 300s`。

### 4. 为什么 uvicorn `--workers 1`

SSE 流式与「Redis 不可用时降级为进程内缓存」都依赖单进程状态；多 worker 会让缓存与限流计数各实例独立而失真。
如需扩容应起多个**容器实例**并统一走 Redis，而不是加大 workers。

### 5. Qdrant 无 healthcheck 的处理

`qdrant/qdrant` 镜像内没有 curl/wget，无法做 HTTP 健康检查。
改为由 `backend-init` 的 `wait_for_deps` 脚本做 TCP 探活兜底，效果等价且不依赖镜像内工具。

---

## 五、常见问题

**Q1：`docker compose up` 卡在 backend-init？**
看 `docker compose logs backend-init`。按出现概率排序：
1. **Embedding 还在加载模型**（最常见）——`docker logs -f embedding` 看到 `Ready` 即可，CPU 模式约需 5~6 分钟；
2. 模型目录为空导致 TEI 起不来——检查 `docker/embedding/bge-large-zh-v1.5/` 是否有权重文件；
3. LLM 网关不可达——只影响实际问数，不影响初始化完成。

**Q2：端口被占用（8080 / 8000 / 3307 / 9200）？**
改 `docker-compose.yaml` 的宿主侧端口（左侧数字），如 `"8081:80"`。
若本机已用 `docker` 起过旧容器，先 `docker compose down`。

**Q3：构建很慢 / uv 拉依赖超时？**
在 `data-agent/Dockerfile` 所在目录构建时可指定国内 PyPI 镜像：
`docker compose build --build-arg UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple backend`

**Q4：想切 DeepSeek 官方通道跑正式数据？**
改 `.env`：`LLM_BASE_URL=https://api.deepseek.com/v1`、`LLM_MODEL_NAME=deepseek-chat`、
`LLM_API_KEY=<官方 key>`，然后 `docker compose up -d backend`（无需重建镜像）。

---

## 六、停止与清理

```bash
docker compose down        # 停止并移除容器（数据卷保留，下次启动数据还在）
docker compose down -v     # 连数据卷一起删（会清空 MySQL/ES/Qdrant 数据，慎用）
docker compose up -d       # 再次启动（已有镜像时秒起）
```

---

## 七、验收记录（本机实测）

- [x] `docker compose config` 语法校验通过（9 个服务）
- [x] 后端镜像构建成功（6m14s）、前端镜像构建成功（2m31s）
- [x] `docker compose up -d` 全栈拉起，8 个服务全部 Running / Healthy
- [x] `backend-init` 完成：业务表建立（user / conversation / message / audit_log）+ 元数据知识库构建
- [x] 经 nginx 入口 `http://localhost:8080` 注册 → 登录 → 问数，返回正确结果
      （"华东地区一共卖出了多少件商品" → `total_quantity: 235`，与数仓一致）
- [x] SSE 流式事件逐条推送未被 nginx 缓冲（progress 事件连续返回，非一次性攒发）

### 实测量化数据

| 项 | 实测 |
|---|---|
| 后端镜像构建 | 6m14s（含 uv 装 113+ 包） |
| 前端镜像构建 | 2m31s |
| Embedding(TEI) 首次 Ready | 约 6 分钟（CPU 模式加载 1.3GB 模型） |
| 初始化（建表 + 元数据知识库） | 约 60 秒 |
| 单次问数端到端 | 约 40~60 秒（取决于 LLM） |

### 遗留说明

- 免费网关模型会不定期下架（本次验收时 `longcat-2.5` 已被下架，改用 `DeepSeek-V4-Flash`）。
  因 LLM 通道走环境变量，**换模型只需改 `.env` 并重启容器，无需重建镜像**。
- 论文正式数据建议充值后切 DeepSeek 官方通道。
