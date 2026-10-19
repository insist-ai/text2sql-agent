# ==========================================================
# 后端镜像
#   Python 3.12 + uv（严格按 uv.lock 复现依赖）+ uvicorn
# ==========================================================
FROM python:3.12-slim

# uv 由官方镜像直接拷贝，无需 pip 安装
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 镜像源可通过构建参数覆盖：
#   国内默认用清华镜像（Debian 官方源在部分网络下 502，PyPI 直连也较慢）；
#   在海外构建时传 --build-arg APT_MIRROR=deb.debian.org --build-arg UV_INDEX_URL=https://pypi.org/simple
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
ARG UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# 中文与时区：slim 镜像默认无 tzdata，需显式安装，否则容器内时间为 UTC。
# Debian 13(trixie) 的源在 /etc/apt/sources.list.d/debian.sources，老版本在 /etc/apt/sources.list，两者都兼容。
RUN (sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true) \
    && (sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list 2>/dev/null || true) \
    && apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# 先只拷贝依赖清单：利用镜像层缓存，改业务代码不会触发重装依赖
COPY pyproject.toml uv.lock ./

# --frozen 严格按 uv.lock 安装，禁止解析出新版本，保证可复现
# --no-install-project 先装第三方依赖（项目代码此刻还没拷进来）
ENV UV_INDEX_URL=${UV_INDEX_URL}
RUN uv sync --frozen --no-install-project --no-dev

COPY . .

# 再装项目自身（以源码运行）
RUN uv sync --frozen --no-dev

# 让 uvicorn 等 CLI 直接可用
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8000/docs || exit 1

# workers=1 的原因：
#   1) SSE 流式响应与进程内缓存降级均依赖单进程状态；
#   2) 限流/缓存的进程内兜底在多 worker 下各实例独立，计数会不一致。
#   如需横向扩容，请起多个容器实例并统一走 Redis（而非加大 workers）。
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
