# ShortLink 高并发短链服务

> 目标：业务极简、技术纵深足够的短链服务。核心只有两个接口，其余精力全部投入"怎么让它扛更高并发"。
>
> **技术栈**：Python 3.12 · FastAPI · MySQL 8 · Redis 7 · SQLAlchemy 2.0(async) · Alembic · Celery · Docker Compose

## 状态

- [x] Week 0：环境与工程骨架
- [ ] Week 1-2：基础功能跑通（生成 + 302 跳转，部署上线）
- [ ] Week 3-4：Redis 缓存与三种缓存异常治理
- [ ] Week 5：压测与优化（Locust + py-spy，产出 QPS/P99 数据表）
- [ ] Week 6：Celery 异步统计、批量聚合落库
- [ ] Week 7：Prometheus + Grafana 可观测性
- [ ] Week 8：测试、CI、README 终稿

## 快速开始

```bash
# 前置：本地装好 Docker（WSL 内 Docker Engine）与 uv
docker compose up -d          # 起 MySQL + Redis + app
uv sync                       # 安装依赖
uv run alembic upgrade head   # 建表
uv run uvicorn app.main:app --reload   # 开发服务器
```

## API

| 接口 | 说明 |
|---|---|
| `POST /api/shorten` | 长链换短码 |
| `GET /{code}` | 短码 302 跳转 + 计数 |

## 设计决策记录（为什么这么选）

<!-- 每做一个选型，就往这里补一条：问题 → 备选方案 → 选择理由。面试官最爱问这个。 -->

| # | 决策 | 理由 |
|---|---|---|
| 1 | 短码生成：号段式自增 ID + Base62 | 发号器不依赖数据库自增锁；全局唯一且递增，利于 InnoDB 聚簇索引顺序写入 |
| 2 | ORM：SQLAlchemy 2.0 async + Alembic | 迁移先行，避免 Week 3 被表结构变更卡住（规划中的避坑建议） |
| 3 | 依赖管理：uv | 比pip快一个数量级，锁文件保证可复现 |
| 3 | 部署形态：Docker Compose | Week 1-2 的目标是"一键起来"，不做 K8s 过度设计 |

## 压测数据表（Week 5 填）

| 优化步骤 | QPS | P99 (ms) | 备注 |
|---|---|---|---|
| 基线（无缓存） | - | - | |
| + Redis Cache Aside | - | - | |
| + 本地 LRU | - | - | |
| + uvloop | - | - | |

## 项目结构

```
shortlink/
├── app/
│   ├── api/        # 路由
│   ├── core/       # 配置、依赖注入
│   ├── models/     # SQLAlchemy 模型
│   ├── schemas/    # Pydantic 模型
│   ├── services/   # 业务逻辑
│   └── main.py     # 入口
├── alembic/        # 数据库迁移
├── tests/          # pytest
├── docker-compose.yml
└── pyproject.toml
```

## 本地环境备忘

- WSL Ubuntu 24.04 内运行 Docker Engine（虚拟磁盘在 E 盘，不占 C 盘）
- uv 已配置清华 PyPI 镜像、缓存目录 `D:\uv\cache`
- 云服务器（待购）：2C2G，部署用
