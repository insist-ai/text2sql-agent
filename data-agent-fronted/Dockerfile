# ==========================================================
# NL2SQL 智能问数系统 · 前端镜像
#   多阶段：node 构建静态资源 → nginx 托管 + 反代 /api
# ==========================================================

# ---------- 阶段 1：构建 ----------
FROM node:22-alpine AS build

WORKDIR /app

# 依赖清单先拷贝，利用层缓存
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# 容器内前端与 nginx 同源，接口走相对路径 /api，由 nginx 反代到后端容器。
# 如需直连后端（例如本地单独跑前端），构建时传 --build-arg VITE_API_TARGET=http://host:8000
ARG VITE_API_TARGET=/api
ENV VITE_API_TARGET=$VITE_API_TARGET

RUN npm run build

# ---------- 阶段 2：运行 ----------
FROM nginx:1.27-alpine

# 中文文件名与响应头不乱码
ENV TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
