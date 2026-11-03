"""缓存的对照压测：同一套负载，逐项只改一个变量。

它不用 Locust，理由是这个阶段要测的东西并不复杂：单个 URL 的并发读，
考察 QPS/P50/P99 与"真实回源次数"。写一个 200 行的 asyncio 压测器，
可以精确控制并发数、观测 loader 调用次数，并且不引入额外进程与服务端口。

Week 5 会换成 Locust（它能打真实网络栈、能做分布式施压）。
这一周的目标不是"最高 QPS"，而是**让每一项优化的贡献可测量**。

用法（先起依赖）：

    docker compose up -d
    cp .env.example .env
    uv run alembic upgrade head
    uv run python -m scripts.bench_cache

跑全部场景大约 1-2 分钟。数据写进 docs/cache.md 的表格。
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from redis.asyncio import from_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.services.base62 import encode
from app.services.idgen import SegmentIDGen
from app.services.shortlink_service import ShortlinkService

HOT_CODE_COUNT = 50
CONCURRENCY = 100
REQUESTS = 4000


@dataclass
class Result:
    name: str
    qps: float
    p50_ms: float
    p99_ms: float
    db_hits: int
    note: str = ""


@dataclass
class BenchContext:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    redis: object
    hot_codes: list[str] = field(default_factory=list)
    missing_codes: list[str] = field(default_factory=list)
    future_codes: list[str] = field(default_factory=list)


async def _prepare(ctx: BenchContext) -> None:
    """准备热数据：真实写进 MySQL，让回源路径也是真的。

    复用本地库意味着要清理历史数据，否则数字会随跑的次数漂移。
    """
    async with ctx.session_factory() as s:
        await s.execute(text("DELETE FROM url_map"))
        await s.execute(
            text("UPDATE segment_alloc SET max_id = :m WHERE biz_tag = 'shortlink'"),
            {"m": 62**4 - 1},
        )
        await s.commit()

    idgen = SegmentIDGen(ctx.session_factory, "shortlink")
    for i in range(HOT_CODE_COUNT):
        code = await idgen.next_code()
        async with ctx.session_factory() as s:
            await s.execute(
                text("INSERT INTO url_map (code, url) VALUES (:c, :u)"),
                {"c": code, "u": f"https://example.com/hot/{i}"},
            )
            await s.commit()
        ctx.hot_codes.append(code)

    # 不存在的短码分两类，测的防线完全不同：
    #   holes（水位之内）：号段已分配但号没发出去 → 过滤器管不了，只能靠负缓存
    #   future（水位之外）：ID 超过了已分配的最大值 → 过滤器直接拒，连缓存都不用碰
    base = 62**4
    ctx.missing_codes = [encode(base + 500 + i) for i in range(20)]
    ctx.future_codes = [encode(base + 500_000 + i) for i in range(20)]


async def _load(ctx: BenchContext, service: ShortlinkService, code: str, counters: dict):
    """一次读请求：走完整的 service.resolve（含前置过滤 + 缓存 + 回源）。"""
    async with ctx.session_factory() as db:
        # 用 SQL 计一次"是否真的查了库"：在 loader 外面数更直观
        result = await service.resolve(code, db=db)
        if result is None:
            counters["miss_results"] = counters.get("miss_results", 0) + 1
        return result


async def _run_load(
    ctx: BenchContext,
    service: ShortlinkService,
    *,
    codes: list[str],
    requests: int = REQUESTS,
    concurrency: int = CONCURRENCY,
) -> tuple[list[float], int]:
    """并发打固定数量的请求，返回每次的延迟（ms）。"""
    latencies: list[float] = []
    lock = asyncio.Lock()
    queue = list(range(requests))
    idx = 0
    counters: dict[str, int] = {}

    async def worker() -> None:
        nonlocal idx
        while True:
            async with lock:
                if idx >= len(queue):
                    return
                i = queue[idx]
                idx += 1
            code = codes[i % len(codes)]
            start = time.perf_counter()
            await _load(ctx, service, code, counters)
            elapsed = (time.perf_counter() - start) * 1000
            async with lock:
                latencies.append(elapsed)

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return latencies, counters.get("miss_results", 0)


def _summarise(name: str, latencies: list[float], db_hits: int, total: float, note: str) -> Result:
    latencies.sort()
    return Result(
        name=name,
        qps=round(len(latencies) / total, 1),
        p50_ms=round(statistics.median(latencies), 2),
        p99_ms=round(latencies[int(len(latencies) * 0.99)], 2),
        db_hits=db_hits,
        note=note,
    )


async def bench_strategy(
    ctx: BenchContext,
    *,
    name: str,
    strategy: str,
    prefilter: str,
    cache_enabled: bool = True,
) -> Result:
    settings = ctx.settings.model_copy(
        update={
            "cache_strategy": strategy,
            "cache_enabled": cache_enabled,
            "prefilter_strategy": prefilter,
            "hits_record_enabled": False,  # 摘掉 1:1 的 Redis 写，避免干扰
            "cache_ttl_jitter_ratio": 0.0,  # 控制变量：先固定 TTL
        }
    )
    service = ShortlinkService.build(ctx.redis, ctx.session_factory, settings)  # type: ignore[arg-type]
    await service.prefilter.refresh()  # type: ignore[attr-defined]
    # 冷启动：清掉上一轮留下的缓存
    await ctx.redis.flushdb()  # type: ignore[attr-defined]

    started = time.perf_counter()
    latencies, _ = await _run_load(ctx, service, codes=ctx.hot_codes)
    total = time.perf_counter() - started
    stats = service.cache_stats()
    await service.cache.drain_background()  # type: ignore[attr-defined]
    return _summarise(
        name,
        latencies,
        int(stats["db_loads"]),
        total,
        note=f"hit_rate={stats['hit_rate']} stale={stats['stale_hits']}",
    )


async def bench_penetration(ctx: BenchContext) -> list[Result]:
    """穿透：不存在短码的重复访问。

    分两类打，因为挡它们的是**不同的防线**：
      future（ID > 水位）：前置过滤直接拒，连 Redis 都不用碰
      hole（ID <= 水位但没发号）：过滤器无法否定，只能靠负缓存

    只测 hole 会让人误以为"前置过滤器没用"，只测 future 又夸大了它的覆盖范围。
    """
    results: list[Result] = []

    for label, codes in [
        ("未来号(>水位)", ctx.future_codes),
        ("号段内空洞(<=水位)", ctx.missing_codes),
    ]:
        for name, negative, prefilter in [
            ("无任何防护", False, "none"),
            ("仅负缓存", True, "none"),
            ("负缓存+水位过滤", True, "watermark"),
        ]:
            settings = ctx.settings.model_copy(
                update={
                    "cache_strategy": "none",
                    "cache_negative_enabled": negative,
                    "prefilter_strategy": prefilter,
                    "hits_record_enabled": False,
                }
            )
            service = ShortlinkService.build(ctx.redis, ctx.session_factory, settings)  # type: ignore[arg-type]
            await service.prefilter.refresh()  # type: ignore[attr-defined]
            await ctx.redis.flushdb()  # type: ignore[attr-defined]

            started = time.perf_counter()
            latencies, _ = await _run_load(ctx, service, codes=codes, requests=2000)
            total = time.perf_counter() - started
            stats = service.cache_stats()
            results.append(
                _summarise(
                    f"穿透[{label}] {name}",
                    latencies,
                    int(stats["db_loads"]),
                    total,
                    note=(
                        f"neg_hits={stats['negative_hits']} "
                        f"filtered={stats['prefilter_rejections']}"
                    ),
                )
            )
    return results


async def bench_breakdown(ctx: BenchContext) -> list[Result]:
    """击穿：热点 key 并发失效的瞬间。

    这里必须区分两种"失效"，它们的结论正好相反：

      cold（冷启动）  key 从未被写过。逻辑过期没有任何旧值可返回，
                      所以它退化成和"无保护"一样——这是它的固有边界，
                      不是我实现错了。互斥锁在这个场景才真正体现价值。

      expired（过期）  键存在但逻辑过期。逻辑过期返回旧值、后台刷新，
                      请求延迟不受重建影响；互斥锁则让 99 个请求排队等待
                      或退化为回源。

    只测 cold 会得出"逻辑过期没用"的错误结论；只测 expired 又会掩盖
    它在冷启动上的短板。两个都测，才说得清"什么时候该用哪个"。
    """
    results: list[Result] = []
    hot = ctx.hot_codes[:1]

    for name, strategy in [
        ("无保护", "none"),
        ("SETNX 互斥锁", "mutex"),
        ("逻辑过期", "logical_expire"),
    ]:
        # --- 场景 A：冷启动（key 从未写入）---
        settings = ctx.settings.model_copy(
            update={
                "cache_strategy": strategy,
                "prefilter_strategy": "watermark",
                "hits_record_enabled": False,
            }
        )
        service = ShortlinkService.build(ctx.redis, ctx.session_factory, settings)  # type: ignore[arg-type]
        await service.prefilter.refresh()
        await ctx.redis.flushdb()

        started = time.perf_counter()
        latencies, _ = await _run_load(ctx, service, codes=hot, requests=1000, concurrency=100)
        total = time.perf_counter() - started
        stats = service.cache_stats()
        await service.cache.drain_background()
        results.append(
            _summarise(
                f"击穿[冷启动] {name}",
                latencies,
                int(stats["db_loads"]),
                total,
                note=f"exhausted={stats['lock_waits_exhausted']}",
            )
        )

        # --- 场景 B：过期瞬间 ---
        # 关键点：必须让 key **真的存在于 Redis**，但逻辑上已过期。
        # 三种策略对"失效"的定义不同，所以构造方式也不同：
        #   none / mutex  只认 Redis TTL。要模拟"刚过期"，用极短 TTL 写入，
        #                 等它自然被删——这才是这两种策略下的真实失效形态。
        #   logical_expire 认记录里的 expire_at_ms。用回溯时钟写入，
        #                 让逻辑过期点落在过去，而 Redis 键因 grace 仍存活。
        warm_cache = service.cache
        if strategy == "logical_expire":
            # 回溯量必须 > ttl（让写入的记录一落地就是"逻辑过期"），
            # 但只回溯一点点。如果回溯一天，刷新时写回的新记录
            # expire_at = 一天前 + ttl 依然是过期的，于是每个刷新任务复查后
            # 都判定"还要再刷"——DB 回源会飙到 900+。
            # 这正是实测踩到的坑（见 docs/cache.md 的"压测构造本身会骗人"）。
            past = time.time() - (settings.cache_ttl_seconds + 100)
            warm_cache._clock = _constant_clock(past)  # type: ignore[method-assign]
            await warm_cache.store(hot[0], "https://example.com/hot/0")
            warm_cache._clock = time.time  # type: ignore[method-assign]
            # 确认它确实处于"存在但过期"的状态，否则这轮测的不是过期场景
            entry = await warm_cache._read(hot[0])
            assert entry is not None and warm_cache._is_stale(entry), "场景构造失败"
        else:
            await ctx.redis.set(
                f"u:{hot[0]}",
                f"{int(time.time() * 1000) - 1_000}:https://example.com/hot/0",
                px=1,
            )
            await asyncio.sleep(0.02)  # 等 TTL 到期，键被真正删除
        service.cache.stats.reset()  # 只统计这一阶段

        started = time.perf_counter()
        latencies, _ = await _run_load(ctx, service, codes=hot, requests=1000, concurrency=100)
        total = time.perf_counter() - started
        stats = service.cache_stats()
        await service.cache.drain_background()
        results.append(
            _summarise(
                f"击穿[过期瞬间] {name}",
                latencies,
                int(stats["db_loads"]),
                total,
                note=f"stale_hits={stats['stale_hits']}",
            )
        )
    return results


def _constant_clock(value: float) -> Callable[[], float]:
    """固定时钟：用来把"逻辑过期"提前触发（不 sleep 一整天）。"""
    return lambda: value


def print_table(results: list[Result]) -> None:
    print()
    print(f"{'场景':<28}{'QPS':>10}{'P50(ms)':>10}{'P99(ms)':>10}{'DB回源':>10}  备注")
    print("-" * 100)
    for r in results:
        print(f"{r.name:<28}{r.qps:>10}{r.p50_ms:>10}{r.p99_ms:>10}{r.db_hits:>10}  {r.note}")
    print()


async def main() -> int:
    settings = get_settings()
    # 压测器自己也要够用的连接池：100 并发 × 每请求一个 session，
    # 池配小了会先卡在等连接上，测出来的是池的瓶颈而不是被测对象的。
    engine = create_async_engine(settings.mysql_dsn, pool_size=50, max_overflow=50)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    # Redis 同理：默认 max_connections=100，100 并发加上后台刷新任务会打满
    redis = from_url(settings.redis_url, decode_responses=True, max_connections=300)

    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        print(
            f"连不上 MySQL：{e}\n先跑 docker compose up -d 并执行 alembic upgrade head",
            file=sys.stderr,
        )
        return 1

    ctx = BenchContext(settings=settings, session_factory=session_factory, redis=redis)
    await _prepare(ctx)
    print(f"准备完成：{len(ctx.hot_codes)} 个热点短码，并发 {CONCURRENCY}，每轮 {REQUESTS} 请求")

    try:
        results = [
            await bench_strategy(
                ctx, name="基线：无缓存", strategy="none", prefilter="none", cache_enabled=False
            ),
            await bench_strategy(
                ctx, name="Cache Aside", strategy="none", prefilter="none", cache_enabled=True
            ),
            await bench_strategy(
                ctx, name="Cache Aside + 水位过滤", strategy="none", prefilter="watermark"
            ),
        ]
        results += await bench_breakdown(ctx)
        results += await bench_penetration(ctx)
        print_table(results)
    finally:
        await redis.aclose()
        await engine.dispose()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
