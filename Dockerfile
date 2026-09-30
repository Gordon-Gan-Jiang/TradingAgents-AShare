# Stage 1: Build Frontend (始终在原生架构运行以提速)
FROM --platform=$BUILDPLATFORM node:25-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
# 开启缓存挂载，加速 npm 安装
RUN --mount=type=cache,target=/root/.npm npm install
COPY frontend/ ./
RUN npm run build

# Stage 2: Final Image (多架构目标镜像)
# Python 底线 3.11：akshare 自 1.18.90 起 requires_python>=3.11，而它是本项目
# A 股数据的主源之一。基础镜像随之从 3.10 提到 3.12（仍满足 >=3.11，且 3.10 已于
# 2026-10 EOL）。若这里改回 3.10，`uv sync --frozen` 会因 requires-python 不匹配
# 而直接失败——这是有意的：宁可 build 阶段报错，也不要装出一个数据源静默不可用的镜像。
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS runtime
WORKDIR /app

# 安装基础系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 利用 uv 同步依赖（uv 在多架构环境下依然非常快）
# 先只装第三方依赖（利用 Docker 层缓存）
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# 拷贝后端源码
COPY api/ ./api/
COPY tradingagents/ ./tradingagents/

# 安装项目本身，避免 uv run 启动时重复安装
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# 拷贝在 Stage 1 中快速构建好的前端产物
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

# 暴露端口
EXPOSE 8000

# 版本号：通过 --build-arg VERSION=v0.2.0 注入（通常取 git tag）
ARG VERSION=dev
ENV APP_VERSION=${VERSION}

# 环境变量设置
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# 启动命令
CMD ["uv", "run", "--no-sync", "tradingagents-api"]
