# 不写 # syntax=docker/dockerfile:1：该指令会让构建器先去 Docker Hub 拉
# 构建前端镜像（docker/dockerfile:1）。服务器无法直连 Docker Hub，构建会卡在
# 0B 传输上十几分钟。本项目只用基础语法与 COPY --from，内置解析器完全够用。

# ---------- builder：只负责把依赖装进独立 venv ----------
FROM python:3.12-slim AS builder

# uv 官方镜像里的静态二进制，比 pip 装 uv 快且不污染运行时层
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

# 先只拷依赖清单：源码变动时这一层仍可命中缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# ---------- runtime：只装 venv + 源码，不带编译工具链 ----------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# curl 用于 HEALTHCHECK（slim 镜像不自带）
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行：容器逃逸时的第一道防线，也是常见的安全审计项
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser alembic ./alembic
COPY --chown=appuser:appuser alembic.ini ./

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# 生产不用 --reload；单机 2C 起 2 个 worker（uvicorn 多进程，各自持有号段）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
