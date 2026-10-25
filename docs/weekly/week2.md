## Week 2 (2026-10-25)

- **计划完成**：Dockerfile + compose 生产编排、服务器部署、Nginx 反代、**公网可访问**
- **实际完成**：全部完成。公网地址 **http://101.200.80.105**
- **卡住的地方**：见下面"五个真实故障"，这周几乎是排查周
- **下周调整**：无。Week 3 的 Redis 缓存按计划开始
- **学到的关键认知（一句话）**：
  **"本地能跑"的容器化版本和真容器是两套环境——只要依赖靠运气装上了，
  你测的就一直不是你的依赖声明。这周两个 bug（cryptography 缺失、大小写排序规则）
  都源于此，而且第二个还是正确性问题，不是性能问题。**

## 本周产出清单

| 产出 | 位置 |
|---|---|
| 生产编排（app + MySQL + Redis + Nginx） | `docker-compose.prod.yml` |
| 多阶段 Dockerfile，非 root，内置健康检查 | `Dockerfile` |
| Nginx 反代（keepalive、gzip、XFF 透传） | `deploy/nginx.conf` |
| 幂等部署脚本 | `scripts/deploy.sh` |
| 号段初始化迁移 | `alembic/versions/b7f3a91c2d40_*.py` |
| 短码排序规则迁移 | `alembic/versions/c4e8d2a6f910_*.py` |
| 大小写敏感回归测试 | `tests/test_api.py::TestCodeCaseSensitivity` |
| 35 个测试全绿 / ruff / black / mypy 全过 | CI |

## 本周五个真实故障（面试素材，按发现顺序）

### 1. 容器里所有写接口 500：依赖声明静默失效

现象：镜像起来 `/health` 正常，`POST /api/shorten` 一律 500，日志只有
`RuntimeError: 'cryptography' package is required for sha256_password...`。

根因：`pyproject.toml` 写的是 `aiomysql[cryptography]`，但 aiomysql 0.3 把 extra
改名成了 `rsa`，`cryptography` 这个 extra **不存在**——pip/uv 不会报错，只是装上
一个没有额外依赖的 aiomysql。本地 venv 里恰好有个残留下来的 `cryptography` 包，
所以本地一直好的。

修法：在真正的驱动上声明 `pymysql[rsa]`。教训是**extra 名写错是静默失败**，
依赖声明要看 lock 文件确认真的解析进去了。

### 2. 新库第一次写短链必 500：迁移只建表没种数据

`segment_alloc` 表建好了但没有任何行，发号器 `SELECT ... .one()` 直接
`NoResultFound`。本地开发是手工 INSERT 的，迁移里没有。

修法：把种子数据写进迁移。**"初始化数据也是 schema 的一部分"**——否则
`alembic upgrade head` 得到的是一个不可用的库。

### 3. 并发下大量主键冲突：短码列大小写不敏感

现象：40 个并发请求里出现 `Duplicate entry '1012W' for key 'PRIMARY'`，
而发号器是严格递增的，不可能发重号。

根因：`url_map.code` 建表时沿用了库默认 `utf8mb4_unicode_ci`（大小写不敏感），
但 Base62 的字符集是 `0-9A-Za-z`，`w` 和 `W` 是**两个不同字符**。先发的 `1012w`
和后发的 `1012W` 在 MySQL 眼里是同一个主键。

更严重的是读路径：`WHERE code = '1012w'` 会命中 `1012W` 的记录——
**访问者输错大小写会被重定向到别人的链接**。这不是性能问题，是正确性问题。

修法：迁移把列改成 `utf8mb4_bin`。模型侧用 `with_variant` 而不是直接写
`collation=`，因为后者会把 MySQL 专有排序规则带进单测的 SQLite，
`CREATE TABLE` 直接报 `no such collation sequence`。

### 4. 500 时日志里什么都查不到：异常被吞

写库的 `except Exception` 只做了 `raise HTTPException(500)`，原始异常没进日志。
上面第 3 个问题的排查因此多花了不少时间——只能靠猜。

修法：`logger.exception(...)` 带上 code 与截断后的 url。

### 5. 构建卡死十几分钟：`# syntax` 指令要连 Docker Hub

Dockerfile 首行 `# syntax=docker/dockerfile:1` 会让构建器先去 Docker Hub 拉
构建前端镜像。这台服务器只配了镜像加速、直连 Hub 不通，于是卡在 `0B / 14.28MB`。
去掉该指令（本项目只用基础语法和 `COPY --from`，内置解析器够用）后正常。

## 部署形态（面试可讲的三点）

1. **只暴露 80**：MySQL/Redis/app 全在 compose 内部网络，`ports` 只给 Nginx。
   本地开发的 compose 才映射 3306/6379，两套编排职责分明。
2. **迁移与启动解耦**：容器 entrypoint 不跑 `alembic upgrade`，由 `deploy.sh`
   在一次性容器里执行。回滚时不会出现"代码退回旧版本但表已经改了"。
3. **健康依赖链**：Nginx 等 app healthy、app 等 MySQL/Redis healthy，
   避免启动期的连环 502。

## 下周预告（Week 3-4：Redis 缓存）

- Cache Aside：先更库再删缓存，讲清"为什么不是先删缓存"
- 穿透：布隆过滤器，同时回答"短链场景真的需要吗"
- 击穿：SETNX 互斥 vs 逻辑过期，两条路都实现并压测对比
- 雪崩：TTL + 随机抖动，讨论多级缓存的必要性
- 连接池与 uvloop
- **交付**：缓存模块 + 一篇把三种异常讲清楚的技术笔记
