# ShortLink 设计文档（Week 1 · 需求与 API 设计）

> 本文档是 Week 1-2 的施工图，也是"设计决策记录"的正文。
> 每个决策都写成：问题 → 备选 → 选择 → 理由。面试问"为什么"时直接翻这里。

## 1. 需求边界

### 做什么

| 接口 | 语义 | 幂等性 |
|---|---|---|
| `POST /api/shorten` | 长链 → 短码 | 否（同 URL 重复提交生成新码，Week 3 再考虑去重） |
| `GET /{code}` | 短码 → 302 跳转 + 异步计数 | 是（跳转天然幂等，计数允许最终一致） |

**明确不做**（防止业务复杂度吃掉技术深度）：
- 用户系统 / 登录（无 user 概念，全部匿名）
- 自定义短码（与发号器选型冲突，砍掉）
- 编辑 / 删除已生成的短链
- 短链过期（Week 6 做清理任务时再评估）

### 非功能指标（Week 5 压测的靶子）

- 单机目标：读 QPS 3000+（裸 FastAPI + MySQL 基线先测出来再定）
- 读延迟 P99 < 50ms
- 短码长度 ≤ 7 字符（62^7 ≈ 3.5 万亿，远超需求，留足冗余）

## 2. 核心设计决策

### D1：短码怎么生成 —— 号段式发号器 + Base62

**问题**：短码必须全局唯一、短、生成快。

**备选方案对比**：

| 方案 | 长度 | 唯一性 | 生成速度 | 缺点 |
|---|---|---|---|---|
| UUID（截断/全量） | 32+ | 概率唯一 | 快 | 不可读、截断有碰撞风险、无序 |
| 哈希（MD5 取前 N 位） | 可控 | 有碰撞，要处理 | 快 | 碰撞处理逻辑复杂 |
| 数据库自增 ID + Base62 | 6-7 | 严格唯一 | 受限于 DB 写 | 高并发下自增锁是瓶颈 |
| **号段式（本方案）** | 6-7 | 严格唯一 | 快（内存发号） | 实现稍复杂 |

**选择**：号段式（segment mode，美团 Leaf 的简化版）。
App 启动/耗尽时从 MySQL 一次取一段（如 [10000, 20000)），期间发号纯内存自增。
这样 DB 写频率 = QPS / 号段大小，本项目的量级下几乎为 0。

**理由**：
1. 严格唯一且递增 → Base62 后递增，对 InnoDB 聚簇索引是顺序写，不碎页
2. 发号零 DB 交互，Week 5 压测时它不会是瓶颈
3. 能讲清楚"为什么不用 UUID/哈希"——这是短链服务的经典面试题

**Base62 编码**：`0-9 A-Z a-z` 共 62 字符。ID 10000 → `2SG`（3 位），
本服务 ID 起点定为 62^4 = 14,776,336，保证短码 **≥5 位**（不至于短得反直觉）。

**多实例部署的一致性**（Week 2 部署时落实）：号段表 `segment_alloc` 加 `biz_tag`，
不同实例错开号段（step 乘以实例数），或直接步长翻倍。单机部署阶段先不做。

### D2：MySQL 表设计

```sql
CREATE TABLE url_map (
    code      CHAR(7)     NOT NULL COMMENT '短码，主键',
    url       VARCHAR(2048) NOT NULL COMMENT '原始长链',
    created_at DATETIME   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (code),
    KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

要点：
- **主键用 code 而非自增 id**：跳转路径 `WHERE code = ?` 主键点查，一次 B+ 树命中。
  发号器的数字 ID 只在写入侧使用，不落这张表（省一列，也省一次二级索引回表）。
- **url 前缀索引**：如果 Week 3 要做"同 URL 去重"，用 `url_crc` CRC32 列 + 前缀索引，
  不直接给 VARCHAR(2048) 建索引（索引太大）。现在先不做，写入性能优先。

### D3：写入路径（POST /api/shorten）

```
1. Pydantic 校验（必须是 http/https URL，长度 ≤ 2048）
2. 发号器.next_id()          ← 内存自增，号段耗尽才碰 DB
3. Base62(id) → code
4. INSERT url_map            ← 串行写，短链服务写少读多，够用
5. 返回 {code, short_url}
```

### D4：读取路径（GET /{code}）

```
1. code 非法（长度/字符集）→ 直接 404，不碰任何存储   ← 廉价防御
2. Week 1-2（无缓存版）：SELECT url FROM url_map WHERE code = ?
3. Week 3 加：Redis Cache Aside（读穿 + 先更库再删缓存）
4. 302 Location: url   （选 302 不选 301：需要每次都回源统计，且可变更目标）
```

**为什么 302 不 301**：301 是永久重定向，浏览器会缓存，后续请求不再打到服务端——
统计就漏了。302 每次都回源，代价是稍高延迟，换来可统计、可变更。
这是规划 Week 6 "302 vs 301" 的前置伏笔，理由先落在这里。

## 3. 统计（Week 6 展开，本周只埋点）

跳转时投递一条 `{code, ts, ip, ua}` 到 Redis list（LPUSH），
Worker 端聚合后批量落库。本周先写接口 + 单测，链路 Week 6 接。

## 4. 部署形态

Week 1-2 目标：**云服务器 Docker Compose 一键起**（app + MySQL + Redis），
Nginx 反代 80 → 8000，公网 IP 直接访问（域名+备案 Week 8 前再说）。

## 5. Week 1-2 任务拆解（每天的目标）

| 天 | 任务 | 完成标志 |
|---|---|---|
| W1-D1 | 本设计文档 + url_map/segment_alloc 迁移 + Note demo 下线 | 表建好，commit 推上去 |
| W1-D2 | 发号器（号段式）+ Base62 + 单测 | 单测覆盖：并发唯一、号段耗尽、编码边界 |
| W1-D3 | POST /api/shorten + 校验 + 错误处理 | curl 能生成短码 |
| W1-D4 | GET /{code} 302 跳转 + 埋点 | curl -I 能看到 Location |
| W1-D5 | pytest 补齐 + 集成测试（含 MySQL） | CI 全绿，覆盖率 ≥60% |
| W2-D1 | Dockerfile + compose 生产编排 | 服务器上一键起 |
| W2-D2 | Nginx 反代 + 服务器部署 | 公网 IP 可访问 |
| W2-D3 | 冒烟 + 修 bug + README 更新（贴公网地址） | 规划检查点：公网可访问 |
| W2-D4~5 | 缓冲 / 补算法与八股 | MySQL 索引主题过完 |

## 6. 面试问答预演（写代码时同步积累）

- Q: 为什么不用 Redis 自增 INCR 做发号器？
  A: 可用，但 Redis 持久化是异步的，宕机可能回吐已发号段 → 重复码；号段式
     落 MySQL，强一致，且 DB 压力被号段摊薄。
- Q: 短码会冲突吗？
  A: 号段式严格递增，不会。哈希方案才会，需要冲突重试。
- Q: 301 还是 302？
  A: 302，见 D4。可统计性 > 每次几毫秒的额外延迟。
