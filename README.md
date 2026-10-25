# ShortLink 高并发短链服务

**线上地址：http://101.200.80.105** · 直接可用：

```bash
# 生成短链
curl -X POST http://101.200.80.105/api/shorten \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://github.com/SaberAliceLOL/shortlink"}'
# → {"code":"abcd1","short_url":"http://101.200.80.105/abcd1","long_url":"..."}

# 访问 /abcd1 会 302 跳转回原链
```

> 目标：业务极简、技术纵深足够的短链服务。核心只有两个接口，其余精力全部投入"怎么让它扛更高并发"。
>
> **技术栈**：Python 3.12 · FastAPI · MySQL 8 · Redis 7 · SQLAlchemy 2.0(async) · Alembic · Celery · Docker Compose · Nginx

## 状态

- [x] Week 0：环境与工程骨架
- [x] Week 1-2：基础功能跑通（生成 + 302 跳转，**已部署上线**）
- [ ] Week 3-4：Redis 缓存与三种缓存异常治理
- [ ] Week 5：压测与优化（Locust + py-spy，产出 QPS/P99 数据表）
- [ ] Week 6：Celery 异步统计、批量聚合落库
- [ ] Week 7：Prometheus + Grafana 可观测性
- [ ] Week 8：测试、CI、README 终稿

## 快速开始

### 本地开发（Windows + WSL）

```bash
docker compose up -d                    # 起 MySQL + Redis
uv sync                                 # 安装依赖
uv run alembic upgrade head             # 建表 + 初始化发号器号段
uv run uvicorn app.main:app --reload    # 开发服务器，http://localhost:8000/docs
```

### 生产部署（服务器一键起）

```bash
cp .env.prod.example .env.prod   # 改密码与 PUBLIC_HOST
bash scripts/deploy.sh           # build → 迁移 → 启动 → 冒烟
```

`deploy.sh` 幂等，可重复执行。它做五件事：构建镜像、起 MySQL/Redis、等 healthy、
跑 `alembic upgrade head`、起 app 与 Nginx 并冒烟测试。

**日常发布**：本地 `git push` → 服务器 `git pull` → `SKIP_BUILD=1 bash scripts/deploy.sh`
（`SKIP_BUILD=1` 表示复用已加载的镜像；改了应用代码则先用
`docker save` + `scp` + `docker load` 传新镜像，见下方"镜像分发"）

**镜像分发（服务器拉不了 Docker Hub 时）**：

```bash
# 本地：构建并导出
docker compose --env-file .env.prod -f docker-compose.prod.yml build app
docker save shortlink-app:latest -o shortlink-app.tar
scp shortlink-app.tar shortlink-server:~/
# 服务器：加载
docker load -i ~/shortlink-app.tar
```

## API

| 接口 | 说明 |
|---|---|
| `POST /api/shorten` | 长链换短码，返回 `{code, short_url, long_url}` |
| `GET /{code}` | 短码 302 跳转 + 埋点（`code\|timestamp_ms` 进 Redis list） |
| `GET /health` | 健康检查（供容器探针用） |

## 设计决策记录（为什么这么选）

<!-- 每做一个选型，就往这里补一条：问题 → 备选方案 → 选择理由。面试官最爱问这个。 -->

| # | 决策 | 理由 |
|---|---|---|
| 1 | 短码生成：号段式自增 ID + Base62 | 发号器不依赖数据库自增锁；全局唯一且递增，利于 InnoDB 聚簇索引顺序写入 |
| 2 | ORM：SQLAlchemy 2.0 async + Alembic | 迁移先行，避免 Week 3 被表结构变更卡住 |
| 3 | 依赖管理：uv | 比 pip 快一个数量级，锁文件保证可复现 |
| 4 | 部署形态：Docker Compose | Week 1-2 的目标是"一键起来"，不做 K8s 过度设计 |
| 5 | 镜像分层：多阶段构建（uv 装依赖 → 干净运行时） | 运行镜像不带编译器与缓存，体积更小、攻击面更小；依赖层独立缓存，改源码不重装依赖 |
| 6 | 容器非 root（uid 10001） | 容器逃逸时的第一道防线，也是常见安全审计项 |
| 7 | 只暴露 Nginx 80，MySQL/Redis/app 全在内部网络 | 数据库端口永不暴露公网；本地开发才需要直连 3306 |
| 8 | 迁移不进容器启动流程，由部署脚本显式执行 | 启动与 schema 变更解耦；回滚时不会出现"代码退回但表已改" |
| 9 | Nginx 不开缓存 | 缓存策略属于应用层（Week 3），两处都缓存会让"缓存命中率"指标失去意义 |
| 10 | 短码列用 `utf8mb4_bin` | Base62 区分大小写，库默认 `utf8mb4_unicode_ci` 会让 `w` 与 `W` 撞主键、且跳转命中错误记录 |

## 部署踩坑记录（都是真实事故，面试可直接讲）

| 现象 | 根因 | 修法 |
|---|---|---|
| 容器起来后所有写接口 500，日志只有 `'cryptography' package is required` | `aiomysql` 0.3 把 extra 从 `cryptography` 改名为 `rsa`，写 `aiomysql[cryptography]` 会**静默失效**；本地 venv 残留旧包掩盖了问题 | 在真正的驱动上声明 `pymysql[rsa]`，并让 `uv.lock` 显式包含 `cryptography` |
| 并发压测下大量 `Duplicate entry` / 500 | `code` 列沿用库默认 `utf8mb4_unicode_ci`，大小写不敏感；Base62 里 `1012w` 与 `1012W` 是两个不同短码，却撞同一个主键 | 迁移把列改成 `utf8mb4_bin`；模型用 `with_variant` 只在 MySQL 生效（SQLite 测试替身不认识该排序规则） |
| 读路径可能跳到别人的链接 | 同上：`WHERE code = '1012w'` 会命中 `1012W` 的记录 | 同上，改用二进制比较 |
| 500 时日志里什么都查不到 | 写库的 `except` 只 `raise HTTPException`，异常被吞掉 | 改 `logger.exception` 记录 code 与 url |
| 新库上第一次 `POST /api/shorten` 必 500 | `segment_alloc` 只建表、从不插初始号段；本地靠手工 INSERT 掩盖 | 加迁移 `b7f3a91c2d40` 初始化号段行，纳入 `upgrade head` |
| 构建卡在 `docker/dockerfile:1` 十几分钟 0B | Dockerfile 首行 `# syntax=` 指令强制从 Docker Hub 拉构建前端，服务器只配了镜像加速 | 删掉该指令，用内置解析器 |

## 压测数据表（Week 5 填）

| 优化步骤 | QPS | P99 (ms) | 备注 |
|---|---|---|---|
| 基线（无缓存） | - | - | |
| + Redis Cache Aside | - | - | |
| + 本地 LRU | - | - | |
| + uvloop | - | - | |

<!-- 这张表是简历上 "QPS xxxx" 那句话的唯一依据。没有数据不要写数字。 -->

## 项目结构

```
shortlink/
├── app/                          # 应用代码
│   ├── api/shortlink.py          #   两个核心接口：生成 + 302 跳转 + 埋点
│   ├── core/config.py            #   Pydantic Settings：环境变量 → 带类型对象
│   ├── core/deps.py              #   依赖注入：engine / session / redis / idgen 单例
│   ├── models/url_map.py         #   短码主表（code 用 utf8mb4_bin）
│   ├── models/segment_alloc.py   #   号段分配表
│   ├── schemas/shortlink.py      #   请求/响应模型（Pydantic 校验）
│   ├── services/base62.py        #   Base62 编解码
│   ├── services/idgen.py         #   号段式发号器（内存发号，耗尽才碰 DB）
│   └── main.py                   #   入口；/health 必须注册在 /{code} 之前
├── alembic/
│   ├── env.py                    #   迁移环境（同步 pymysql 驱动）
│   └── versions/                 #   建表 → 号段种子 → 短码排序规则
├── tests/                        # 35 个测试（单测 + 接口集成，SQLite/fakeredis 替身）
├── deploy/nginx.conf             # 反代、gzip、XFF 透传、keepalive
├── scripts/deploy.sh             # 服务器幂等部署（支持 SKIP_BUILD=1）
├── Dockerfile                    # 多阶段构建，非 root uid 10001，内置 HEALTHCHECK
├── docker-compose.yml            # 本地开发：MySQL + Redis（映射 3306/6379）
├── docker-compose.prod.yml       # 生产：app + MySQL + Redis + Nginx（只暴露 80）
├── .env.example                  # 本地开发环境变量模板
├── .env.prod.example             # 生产环境变量模板（真 .env.prod 不提交）
├── .dockerignore                 # 构建上下文排除项
├── .github/workflows/ci.yml      # CI：ruff + black + mypy + pytest
├── docs/design.md                # 设计文档 + 决策记录 + 面试问答预演
├── docs/weekly/                  # 每周周报（含卡点与面试素材）
└── pyproject.toml                # 依赖、ruff/black/mypy/pytest 配置
```

## 本地环境备忘

- WSL Ubuntu 24.04 内运行 Docker Engine（虚拟磁盘在 E 盘，不占 C 盘）
- uv 已配置清华 PyPI 镜像、缓存目录 `D:\uv\cache`
- 云服务器：阿里云轻量 2C1.6G/2G swap，Ubuntu 24.04，公网 `101.200.80.105`
  - SSH 端口 10022，密钥登录，ufw 只放行 10022/80/443
  - Docker 配了镜像加速（`docker.1ms.run` + daocloud），**直连 Docker Hub 不通**
  - 代码目录 `~/shortlink`，从 GitHub clone，更新走 `git pull`
