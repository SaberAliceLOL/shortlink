"""读缓存层：Cache Aside + 三种缓存异常的治理。

三种异常在这里各自的落点，以及为什么这么放：

  穿透 penetration  查询一个库里根本不存在的短码，每次都穿过缓存打 DB。
                    治理 = 负缓存（本模块）+ 前置过滤（services/prefilter.py）。
                    负缓存只能挡住"第二次"开始的重复访问，第一次必定回源；
                    前置过滤挡的是第一次。两者是不同防线，不能互相替代。

  击穿 breakdown    单个热点 key 失效的瞬间，成百请求同时回源。
                    治理 = 互斥锁重建（mutex）或逻辑过期（logical_expire）。
                    两种都实现，压测对比见 docs/cache.md。

  雪崩 avalanche    大批 key 同时失效。治理 = TTL 随机抖动。
                    抖动只解决"同一秒集体过期"，不解决"Redis 整个挂了"——
                    后者靠降级（熔断）兜住，见下方 CircuitBreaker。

一个贯穿本模块的取舍：**Redis 是加速器，不是数据源**。
任何 Redis 异常都不能影响跳转是否成功，所以每个 Redis 调用都吞掉异常、
打开熔断、退化成"直接查库"。代价是故障期间 DB 压力回到无缓存水平，
收益是缓存故障不会变成服务故障。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import CacheStrategy, Settings

logger = logging.getLogger(__name__)

# 缓存键：u:<code>。值是一条带逻辑过期时间的记录，
# 而不是裸 URL——见 _encode_entry 的说明。
URL_KEY = "u:{code}"
LOCK_KEY = "lock:u:{code}"
HITS_KEY = "hits"

# 释放锁必须"比对 token 再删"：直接 DEL 会误删别人的锁。
# 场景：A 拿锁 → A 超时/卡住 → 锁自动过期 → B 拿锁 → A 醒来 DEL 掉了 B 的锁。
# 用 Lua 把"读 + 比较 + 删"做成原子操作。
_LOCK_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

_ENCODING_SEP = ":"


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """缓存里的一条记录。

    url 为 None 表示负缓存（确认该短码不存在）。
    expire_at_ms 是**逻辑**过期时间，与 Redis 键自身的 TTL 是两个概念：
    逻辑过期到了，值还躺在 Redis 里（stale），专门用来给"逻辑过期"策略读旧值。
    """

    url: str | None
    expire_at_ms: int

    @property
    def is_negative(self) -> bool:
        return self.url is None


def _encode_entry(url: str | None, expire_at_ms: int) -> str:
    # 格式 "<expire_at_ms>:<url>"，负缓存是 "<expire_at_ms>:"（空 url）。
    # 选它而不是 JSON：热路径上每条记录都要解析，partition 比 json.loads 便宜一个数量级，
    # 而这里只需要两个字段。URL 里本来就有 ':'，所以按**第一个**分隔符切分。
    return f"{expire_at_ms}{_ENCODING_SEP}{url or ''}"


def _decode_entry(raw: str) -> CacheEntry | None:
    head, sep, tail = raw.partition(_ENCODING_SEP)
    if not sep:
        return None  # 格式对不上（旧版本写入 / 被人手工改过），当未命中处理
    try:
        expire_at_ms = int(head)
    except ValueError:
        return None
    return CacheEntry(url=tail or None, expire_at_ms=expire_at_ms)


class CircuitBreaker:
    """Redis 故障熔断。

    没有它的时候，一个挂掉的 Redis 会让**每个**请求都等满 socket timeout
    (0.5s)，服务不是"降级"，而是"每次多 500ms 的降级"。
    熔断打开后，冷却期内直接跳过 Redis，把故障成本从"每请求一次超时"
    摊成"每个冷却期一次探测"。
    """

    def __init__(self, cooldown_seconds: float) -> None:
        self._cooldown = cooldown_seconds
        self._opened_at: float | None = None
        self.trips = 0

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self._cooldown:
            self._opened_at = None  # 冷却结束，放一个请求去探活
            return False
        return True

    def trip(self) -> None:
        if self._opened_at is None:
            self.trips += 1
        self._opened_at = time.monotonic()


Loader = Callable[[], Awaitable[str | None]]


class ShortlinkCache:
    """短链读缓存。

    对上层只暴露三件事：resolve / store / invalidate。
    策略差异全部收在 resolve 里，路由不需要知道当前用的是互斥锁还是逻辑过期。
    """

    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        *,
        wall_clock: Callable[[], float] = time.time,
        mono_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._redis = redis
        self._enabled = settings.cache_enabled
        self._strategy: CacheStrategy = (
            settings.cache_strategy if settings.cache_enabled else "none"
        )
        self._ttl = settings.cache_ttl_seconds
        self._ttl_jitter = settings.cache_ttl_jitter_ratio
        self._negative_ttl = settings.cache_negative_ttl_seconds
        self._negative_jitter = settings.cache_negative_jitter_ratio
        self._negative_enabled = settings.cache_negative_enabled
        self._stale_grace = settings.cache_stale_grace_seconds
        self._lock_ttl_ms = settings.cache_lock_ttl_ms
        self._lock_wait = settings.cache_lock_wait_ms / 1000
        self._lock_wait_attempts = settings.cache_lock_wait_attempts
        self._clock = wall_clock
        self.breaker = CircuitBreaker(settings.cache_breaker_cooldown_seconds)
        self._bg_tasks: set[asyncio.Task[None]] = set()
        # 进程内正在刷新的短码。用来在发起任何 Redis 往返之前挡掉重复刷新
        self._inflight: set[str] = set()

        # 进程内计数：热路径上不再多写一次 Redis 计数器，
        # 代价是多 worker 部署时各自统计（Week 7 用 Prometheus 汇总）
        self.stats = CacheStats()

    # ---- 对外接口 --------------------------------------------------------

    @property
    def strategy(self) -> CacheStrategy:
        return self._strategy

    async def resolve(self, code: str, loader: Loader) -> str | None:
        """返回长链；确认不存在时返回 None。loader 负责回源查库。"""
        if self._strategy == "none" and not self._enabled:
            # 完全关闭缓存：连"读一次 Redis"都不做。
            # 这必须是独立分支——如果只是把策略当 none 却仍然 GET，
            # 基线就会白拿一次缓存命中的收益，对比数字是假的。
            return await self._call_loader(loader)

        entry = await self._read(code)
        if entry is not None:
            if entry.is_negative:
                self.stats.negative_hits += 1
                return None
            if self._strategy == "logical_expire" and self._is_stale(entry):
                # 逻辑过期：旧值立即返回，刷新丢给后台。请求不会被重建拖慢。
                self.stats.stale_hits += 1
                self._spawn_refresh(code, loader)
                return entry.url
            self.stats.hits += 1
            return entry.url

        self.stats.misses += 1
        if self._strategy == "mutex":
            return await self._resolve_with_lock(code, loader)
        return await self._load_and_store(code, loader)

    async def _call_loader(self, loader: Loader) -> str | None:
        """所有回源都走这里，好让 db_loads 是"真的查了几次库"的唯一口径。

        以前用 misses 当回源次数，但两者并不相等：互斥锁让 N 个未命中
        只回源 1 次，逻辑过期的后台刷新则发生在命中之后。压测时
        用错指标会把"锁生效了"读成"锁没生效"。
        """
        self.stats.db_loads += 1
        return await loader()

    async def store(self, code: str, url: str) -> None:
        """写入/覆盖缓存。新建短链时调用，用来盖掉可能被"预埋"的负缓存。

        短码是顺序可预测的（Base62(自增 ID)），攻击者可以先请求一个尚未生成的
        短码把负缓存打进去，等链接真的生成后，用户在负缓存 TTL 内访问会得到 404。
        所以写入路径必须主动覆盖，而不是等它自然过期。
        """
        await self._write(code, url, self._ttl, self._ttl_jitter)

    async def invalidate(self, code: str) -> None:
        """删除缓存。Cache Aside 的写侧约定：先更库、再删缓存（不是更新缓存）。

        为什么删而不是更新：
          - 并发写时"先更库再更缓存"仍可能被乱序覆盖，删除是幂等的
          - 删除后下一次读自然回源，不存在"缓存里是新值但库里是旧值"
        为什么不是先删缓存再更库：删完到更库之间有空窗，读请求会把**旧值**
        重新灌回缓存，而且之后不会再失效——比不删更糟。
        """
        if self.breaker.is_open:
            return
        try:
            await self._redis.delete(URL_KEY.format(code=code))
        except (RedisError, OSError) as e:
            self.breaker.trip()
            logger.warning("缓存删除失败 code=%s err=%s", code, e)

    async def record_hit(self, code: str) -> None:
        """访问埋点。Week 6 改成队列投递 + 批量聚合，这里先保留原格式。"""
        if self.breaker.is_open:
            return
        try:
            await self._redis.lpush(HITS_KEY, f"{code}|{int(self._clock() * 1000)}")
        except (RedisError, OSError) as e:
            self.breaker.trip()
            logger.warning("埋点写入失败 code=%s err=%s", code, e)

    async def drain_background(self) -> None:
        """等待后台刷新收尾（优雅关闭用；测试也用它消除不确定性）。"""
        if self._bg_tasks:
            await asyncio.gather(*tuple(self._bg_tasks), return_exceptions=True)

    # ---- 三种策略的实现 --------------------------------------------------

    async def _resolve_with_lock(self, code: str, loader: Loader) -> str | None:
        """互斥锁重建：只放一个请求回源，其余请求短暂等待后复用结果。

        等待必须**有上限**：拿不到锁又不肯等太久，最多等 3×50ms 就自己回源。
        无上限地等锁等于把缓存故障放大成请求堆积——这是把"击穿"修成"雪崩"的经典写法。
        """
        lock = await self._acquire_lock(code)
        if lock is None:
            for _ in range(self._lock_wait_attempts):
                await asyncio.sleep(self._lock_wait)
                entry = await self._read(code)
                if entry is not None:
                    return None if entry.is_negative else entry.url
            self.stats.lock_waits_exhausted += 1
            return await self._call_loader(loader)  # 等不到就自己回源，宁可多打一次 DB 也不排队

        try:
            entry = await self._read(code)  # 双检：等锁期间可能已有人写好
            if entry is not None:
                return None if entry.is_negative else entry.url
            return await self._load_and_store(code, loader)
        finally:
            await self._release_lock(code, lock)

    async def _load_and_store(self, code: str, loader: Loader) -> str | None:
        url = await self._call_loader(loader)
        if url is None:
            await self._write(code, None, self._negative_ttl, self._negative_jitter)
        else:
            await self._write(code, url, self._ttl, self._ttl_jitter)
        return url

    def _spawn_refresh(self, code: str, loader: Loader) -> None:
        """spawn 后台刷新任务，并在**进程内**先做一次单飞。

        为什么单飞不能只靠 Redis 锁：过期瞬间 N 个请求同时发现过期，
        如果每个都 spawn 一个任务，就会产生 N 次 SETNX 往返 + N 个 task。
        压测实测：100 并发过期命中时，仅"每个请求都去抢一次锁"就把
        P99 从 121ms 推到 325ms，QPS 腰斩——保护措施本身变成了瓶颈。

        本地集合判断是零成本的，能在发起任何 IO 之前就把 99 个请求挡掉。
        Redis 锁仍然必要：它管的是**跨进程**的单飞。
        """
        if code in self._inflight:
            return
        self._inflight.add(code)
        task = asyncio.create_task(self._refresh(code, loader))
        self._bg_tasks.add(task)

        def _done(t: asyncio.Task[None]) -> None:
            self._inflight.discard(code)
            self._bg_tasks.discard(t)

        # 持有 task 引用，否则 asyncio 只保留弱引用，任务可能被 GC 掉
        task.add_done_callback(_done)

    async def _refresh(self, code: str, loader: Loader) -> None:
        """后台刷新。失败只记日志——旧值还能继续服务，没必要影响用户。"""
        lock = await self._acquire_lock(code)
        if lock is None:
            return  # 已有别的请求在刷，直接认输（这就是单飞）
        try:
            # 拿到锁后必须重新确认"还需要刷"。
            # 少了这一步，单飞是漏的：并发 100 个请求各 spawn 一个刷新任务，
            # 第一个刷完释放锁时，排在它后面的任务会**依次**再拿到锁，
            # 于是 100 个任务串行刷 100 次——实测 db_loads 从 1 涨到 901。
            # 这不是理论问题，是压测直接打出来的。
            entry = await self._read(code)
            if entry is not None and not self._is_stale(entry):
                return
            url = await self._call_loader(loader)
            if url is None:
                await self._write(code, None, self._negative_ttl, self._negative_jitter)
            else:
                await self._write(code, url, self._ttl, self._ttl_jitter)
        except Exception:  # noqa: BLE001 — 后台任务绝不能把异常漏给事件循环
            logger.exception("后台刷新缓存失败 code=%s", code)
        finally:
            await self._release_lock(code, lock)

    # ---- Redis 原语（全部吞异常 + 熔断） ----------------------------------

    def _is_stale(self, entry: CacheEntry) -> bool:
        return int(self._clock() * 1000) >= entry.expire_at_ms

    def _jittered_ttl(self, base: int, ratio: float) -> int:
        """TTL 抖动：把"同一批写入"的键在时间轴上打散，避免集体过期。

        为什么是写入时抖动而不是过期后随机重试：抖动加在写入侧等于把
        expiry 点均匀展开；加在读取侧只是把雪崩推迟到下一轮。
        """
        if base <= 0:
            return 0
        delta = base * ratio
        return max(1, int(base + random.uniform(-delta, delta)))

    async def _read(self, code: str) -> CacheEntry | None:
        if self.breaker.is_open:
            return None
        try:
            raw = await self._redis.get(URL_KEY.format(code=code))
        except (RedisError, OSError) as e:
            self.breaker.trip()
            logger.warning("缓存读取失败，降级回源 code=%s err=%s", code, e)
            return None
        if raw is None:
            return None
        if isinstance(raw, bytes):  # decode_responses=True 时不会发生，防御配置改动
            raw = raw.decode("utf-8")
        return _decode_entry(raw)

    async def _write(self, code: str, url: str | None, base_ttl: int, jitter: float) -> None:
        if self.breaker.is_open:
            return
        if url is None and not self._negative_enabled:
            return  # 关掉负缓存（压测屏蔽用）
        ttl = self._jittered_ttl(base_ttl, jitter)
        if ttl <= 0:
            return  # 负缓存 TTL 配成 0 = 关闭负缓存
        expire_at_ms = int(self._clock() * 1000) + ttl * 1000
        # 逻辑过期模式下 Redis 键要比逻辑过期点多活一段，否则 stale 值被 Redis 删掉，
        # "读旧值"就退化成"未命中"，逻辑过期白做
        redis_ttl = ttl + (self._stale_grace if self._strategy == "logical_expire" else 0)
        try:
            await self._redis.set(
                URL_KEY.format(code=code),
                _encode_entry(url, expire_at_ms),
                ex=redis_ttl,
            )
        except (RedisError, OSError) as e:
            self.breaker.trip()
            logger.warning("缓存写入失败 code=%s err=%s", code, e)

    async def _acquire_lock(self, code: str) -> str | None:
        if self.breaker.is_open:
            return None
        token = f"{id(self):x}-{int(self._clock() * 1_000_000)}-{random.getrandbits(32):x}"
        try:
            ok = await self._redis.set(
                LOCK_KEY.format(code=code), token, nx=True, px=self._lock_ttl_ms
            )
        except (RedisError, OSError) as e:
            self.breaker.trip()
            logger.warning("取锁失败 code=%s err=%s", code, e)
            return None
        return token if ok else None

    async def _release_lock(self, code: str, token: str) -> None:
        try:
            await self._redis.eval(_LOCK_RELEASE, 1, LOCK_KEY.format(code=code), token)
        except (RedisError, OSError) as e:
            # 释放失败不致命：锁有自己的 TTL，最多让后续请求多等一会儿
            logger.warning("释放锁失败 code=%s err=%s", code, e)


@dataclass
class CacheStats:
    """进程内计数。多 worker 部署时每个进程各一份，Week 7 交给 Prometheus 汇总。"""

    hits: int = 0
    stale_hits: int = 0
    negative_hits: int = 0
    misses: int = 0
    # 真正调用 loader（= 查了一次库）的次数。与 misses 不同：
    # 互斥锁会把 N 个 miss 合并成 1 次 db_load，逻辑过期的刷新发生在命中之后。
    db_loads: int = 0
    lock_waits_exhausted: int = 0

    def reset(self) -> None:
        """清零。压测要分阶段统计（冷启动 / 过期瞬间），不清零两段会混在一起。"""
        self.hits = 0
        self.stale_hits = 0
        self.negative_hits = 0
        self.misses = 0
        self.db_loads = 0
        self.lock_waits_exhausted = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.stale_hits + self.negative_hits + self.misses

    @property
    def hit_rate(self) -> float:
        total = self.lookups
        return 0.0 if total == 0 else round((total - self.misses) / total, 4)
