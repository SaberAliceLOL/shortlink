#!/usr/bin/env bash
# 服务器侧部署脚本：在服务器上执行，幂等（重复跑不会坏）
#
# 代码更新：git pull（服务器可访问 GitHub）
# 镜像：默认本地构建；若已用 docker load 预置了 shortlink-app:latest，
#       设 SKIP_BUILD=1 跳过构建——服务器拉基础镜像时通时不通，
#       紧急发布时“本地构建 + scp + docker load”是更可靠的路径。
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/shortlink}"
COMPOSE="docker compose --env-file .env.prod -f docker-compose.prod.yml"

cd "$APP_DIR"

if [[ ! -f .env.prod ]]; then
    echo "缺少 .env.prod，请先从 .env.prod.example 复制并改密码" >&2
    exit 1
fi

if [[ "${SKIP_BUILD:-0}" == "1" ]]; then
    echo "==> 1/5 跳过构建（使用已加载的 shortlink-app:latest）"
    docker image inspect shortlink-app:latest >/dev/null \
        || { echo "镜像不存在，无法跳过构建" >&2; exit 1; }
else
    echo "==> 1/5 构建镜像"
    $COMPOSE build
fi

echo "==> 2/5 启动依赖（mysql / redis）"
$COMPOSE up -d mysql redis

echo "==> 3/5 等待 MySQL healthy"
for i in $(seq 1 40); do
    if [[ "$(docker inspect -f '{{.State.Health.Status}}' shortlink-prod-mysql 2>/dev/null)" == "healthy" ]]; then
        echo "    MySQL ready after ${i}0s"
        break
    fi
    sleep 2
done

echo "==> 4/5 执行数据库迁移（在一次性容器内跑，不依赖宿主机 Python）"
docker run --rm \
    --network shortlink-prod_default \
    --env-file .env.prod \
    -e MYSQL_HOST=mysql \
    shortlink-app:latest \
    alembic upgrade head

echo "==> 5/5 启动应用与 Nginx"
$COMPOSE up -d app nginx

echo "==> 容器状态"
$COMPOSE ps

echo "==> 冒烟测试（走 Nginx 80 端口）"
sleep 3
curl -fsS http://127.0.0.1/health && echo
CODE=$(curl -fsS -X POST http://127.0.0.1/api/shorten \
    -H 'Content-Type: application/json' \
    -d '{"url":"https://example.com/deploy-smoke-test"}' \
    | sed -n 's/.*"code":"\([^"]*\)".*/\1/p')
echo "    generated code: ${CODE}"
curl -fsS -o /dev/null -w "    redirect status: %{http_code} -> %{redirect_url}\n" \
    "http://127.0.0.1/${CODE}"

echo "==> 部署完成"
