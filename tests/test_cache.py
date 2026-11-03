"""缓存层测试：三种异常 + 两种击穿重建策略 + 降级。

这一组测试的价值不在于"跑绿"，而在于它们把每个设计取舍都钉成了可执行断言。
面试讲"我用逻辑过期而不是互斥锁"时，可以指着
`test_logical_expire_returns_stale_immediately` 说"这是它的证明"。
"""

import asyncio

import pytest
from fakeredis.aioredis import FakeRedis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.services.cache import LOCK_KEY, URL_KEY, CacheEntry, _decode_entry
from app.services.shortlink_service import ShortlinkService
from tests.conftest import FakeClock, build_service, make_settings

CODE = "abc12"
URL = "https://example.com/target"


def loader_returning(value: str | None, counter: list[int] | None = None):
    """可观测的 loader：counter 记录真实回源次数。"""

    async def load() -> str | None:
        if counter is not None:
            counter.append(1)
        return value

    return load


class TestEncoding:
    def test_roundtrip(self) -> None:
        entry = _decode_entry(f"1700000000000:{URL}")
        assert entry == CacheEntry(url=URL, expire_at_ms=1_700_000_000_000)

    def test_negative_entry(self) -> None:
        entry = _decode_entry("1700000000000:")
        assert entry is not None and entry.is_negative

    def test_url_containing_colon(self) -> None:
        """URL 里本来就有 ':'，按第一个分隔符切分才不会切坏。"""
        entry = _decode_entry("1:http://a.b/c?d=e:f")
        assert entry is not None and entry.url == "http://a.b/c?d=e:f"

    @pytest.mark.parametrize("raw", ["garbage", "notanint:x", ""])
    def test_malformed_treated_as_miss(self, raw: str) -> None:
        assert _decode_entry(raw) is None


class TestCacheAside:
    async def test_miss_then_hit(self, fake_redis: FakeRedis, session_factory) -> None:
        service = build_service(fake_redis, session_factory, make_settings(cache_strategy="none"))
        cache = service.cache
        calls: list[int] = []
        load = loader_returning(URL, calls)

        assert await cache.resolve(CODE, load) == URL
        assert await cache.resolve(CODE, load) == URL
        assert len(calls) == 1  # 第二次命中缓存

    async def test_ttl_has_jitter(self, fake_redis: FakeRedis, session_factory) -> None:
        """雪崩防护：同一批写入的键，Redis TTL 不应该全部相同。"""
        service = build_service(fake_redis, session_factory, make_settings())
        cache = service.cache
        for i in range(20):
            await cache.store(f"code{i:02d}", URL)
        ttls = {await fake_redis.ttl(URL_KEY.format(code=f"code{i:02d}")) for i in range(20)}
        assert len(ttls) > 1, f"TTL 没有抖动，全部是 {ttls}"

    async def test_jitter_stays_near_base_ttl(self, fake_redis: FakeRedis, session_factory) -> None:
        settings = make_settings(cache_ttl_seconds=300, cache_ttl_jitter_ratio=0.2)
        service = build_service(fake_redis, session_factory, settings)
        for i in range(20):
            await service.cache.store(f"code{i:02d}", URL)
        ttls = {await fake_redis.ttl(URL_KEY.format(code=f"code{i:02d}")) for i in range(20)}
        assert all(240 <= ttl <= 360 for ttl in ttls)

    async def test_invalidate_removes_key(self, fake_redis: FakeRedis, session_factory) -> None:
        service = build_service(fake_redis, session_factory, make_settings())
        await service.cache.store(CODE, URL)
        assert await fake_redis.exists(URL_KEY.format(code=CODE)) == 1
        await service.cache.invalidate(CODE)
        assert await fake_redis.exists(URL_KEY.format(code=CODE)) == 0


class TestPenetration:
    """穿透：不存在的短码也要挡住，别每次都打 DB。"""

    async def test_negative_cache_prevents_second_query(
        self, fake_redis: FakeRedis, session_factory
    ) -> None:
        service = build_service(fake_redis, session_factory, make_settings())
        calls: list[int] = []
        load = loader_returning(None, calls)

        assert await service.cache.resolve(CODE, load) is None
        assert await service.cache.resolve(CODE, load) is None
        assert await service.cache.resolve(CODE, load) is None
        assert len(calls) == 1  # 只有第一次回源

    async def test_negative_ttl_is_short(self, fake_redis: FakeRedis, session_factory) -> None:
        settings = make_settings(cache_negative_ttl_seconds=30, cache_negative_jitter_ratio=0.0)
        service = build_service(fake_redis, session_factory, settings)
        await service.cache.resolve(CODE, loader_returning(None))
        ttl = await fake_redis.ttl(URL_KEY.format(code=CODE))
        assert ttl == 30

    async def test_negative_cache_can_be_disabled(
        self, fake_redis: FakeRedis, session_factory
    ) -> None:
        settings = make_settings(cache_negative_enabled=False)
        service = build_service(fake_redis, session_factory, settings)
        calls: list[int] = []
        load = loader_returning(None, calls)
        await service.cache.resolve(CODE, load)
        await service.cache.resolve(CODE, load)
        assert len(calls) == 2  # 每次都回源——这正是关掉负缓存要测的效果

    async def test_store_overwrites_negative_entry(
        self, fake_redis: FakeRedis, session_factory
    ) -> None:
        """预埋负缓存的攻击场景：请求一个还没生成的短码，等链接生成后必须能访问。"""
        service = build_service(fake_redis, session_factory, make_settings())
        cache = service.cache
        load = loader_returning(None)

        assert await cache.resolve(CODE, load) is None  # 负缓存写入
        await cache.store(CODE, URL)  # 链接真的生成了
        assert await cache.resolve(CODE, loader_returning("should-not-be-used")) == URL


class TestBreakdownMutex:
    """击穿（策略一）：SETNX 互斥锁。并发请求里只有一个回源。"""

    async def test_concurrent_misses_load_once(
        self, fake_redis: FakeRedis, session_factory
    ) -> None:
        service = build_service(fake_redis, session_factory, make_settings(cache_strategy="mutex"))
        cache = service.cache
        calls: list[int] = []

        async def slow_load() -> str:
            calls.append(1)
            await asyncio.sleep(0.05)  # 模拟一次慢查询，给其他请求"等锁"的窗口
            return URL

        results = await asyncio.gather(*(cache.resolve(CODE, slow_load) for _ in range(20)))
        assert results == [URL] * 20
        assert len(calls) == 1, f"回源 {len(calls)} 次，互斥锁失效"

    async def test_lock_is_released_after_load(
        self, fake_redis: FakeRedis, session_factory
    ) -> None:
        service = build_service(fake_redis, session_factory, make_settings(cache_strategy="mutex"))
        await service.cache.resolve(CODE, loader_returning(URL))
        assert await fake_redis.exists(LOCK_KEY.format(code=CODE)) == 0

    async def test_waiter_falls_back_to_db_when_lock_never_frees(
        self, fake_redis: FakeRedis, session_factory
    ) -> None:
        """锁一直被占：等待方必须有上限，超时就自己回源，不能无限排队。"""
        settings = make_settings(
            cache_strategy="mutex", cache_lock_wait_ms=5, cache_lock_wait_attempts=2
        )
        service = build_service(fake_redis, session_factory, settings)
        # 手工占住锁，模拟一个卡死的重建者
        await fake_redis.set(LOCK_KEY.format(code=CODE), "someone-else", px=10_000)

        calls: list[int] = []
        result = await service.cache.resolve(CODE, loader_returning(URL, calls))
        assert result == URL
        assert len(calls) == 1
        assert service.cache.stats.lock_waits_exhausted == 1

    async def test_stale_lock_from_dead_process_expires(
        self, fake_redis: FakeRedis, session_factory
    ) -> None:
        """锁带 TTL：进程崩溃不会留下永久死锁。"""
        settings = make_settings(cache_strategy="mutex", cache_lock_ttl_ms=20)
        service = build_service(fake_redis, session_factory, settings)
        await fake_redis.set(LOCK_KEY.format(code=CODE), "dead-process", px=20)
        await asyncio.sleep(0.05)
        assert await service.cache.resolve(CODE, loader_returning(URL)) == URL


class TestBreakdownLogicalExpire:
    """击穿（策略二）：逻辑过期。旧值立即返回，刷新在后台单飞。"""

    async def test_logical_expire_returns_stale_immediately(
        self, fake_redis: FakeRedis, session_factory, clock: FakeClock
    ) -> None:
        settings = make_settings(
            cache_strategy="logical_expire", cache_ttl_seconds=300, cache_ttl_jitter_ratio=0.0
        )
        service = build_service(fake_redis, session_factory, settings, clock=clock)
        cache = service.cache

        await cache.store(CODE, URL)
        # 键还在 Redis 里，但逻辑上已经过期
        expired = FakeClock(start=clock.now + 301)
        cache._clock = expired

        refreshed = asyncio.Event()

        async def slow_load() -> str:
            await asyncio.sleep(0.02)
            refreshed.set()
            return "https://example.com/refreshed"

        # 过期瞬间：不等后台刷新，立刻返回旧值
        assert await cache.resolve(CODE, slow_load) == URL
        assert not refreshed.is_set(), "逻辑过期不应该让请求等重建"

        await cache.drain_background()
        assert refreshed.is_set()
        # 后台刷新已经把新值写进去了（此时逻辑时间 = 新的过期点 + 未过期）
        assert cache.stats.stale_hits == 1

    async def test_single_flight_refresh(
        self, fake_redis: FakeRedis, session_factory, clock: FakeClock
    ) -> None:
        """并发命中过期值时，后台只应该有一次重建（单飞）。"""
        settings = make_settings(
            cache_strategy="logical_expire", cache_ttl_seconds=300, cache_ttl_jitter_ratio=0.0
        )
        service = build_service(fake_redis, session_factory, settings, clock=clock)
        cache = service.cache
        await cache.store(CODE, URL)

        cache._clock = FakeClock(start=clock.now + 301)
        calls: list[int] = []

        async def slow_load() -> str:
            calls.append(1)
            await asyncio.sleep(0.05)
            return URL

        results = await asyncio.gather(*(cache.resolve(CODE, slow_load) for _ in range(20)))
        assert results == [URL] * 20
        await cache.drain_background()
        assert len(calls) == 1, f"后台刷新了 {len(calls)} 次，单飞失效"

    async def test_redis_key_outlives_logical_expiry(
        self, fake_redis: FakeRedis, session_factory, clock: FakeClock
    ) -> None:
        """逻辑过期依赖"旧值还在"：Redis TTL 必须比逻辑 TTL 长一个 grace。"""
        settings = make_settings(
            cache_strategy="logical_expire",
            cache_ttl_seconds=300,
            cache_ttl_jitter_ratio=0.0,
            cache_stale_grace_seconds=600,
        )
        service = build_service(fake_redis, session_factory, settings, clock=clock)
        await service.cache.store(CODE, URL)
        assert await fake_redis.ttl(URL_KEY.format(code=CODE)) == 900

    async def test_refresh_failure_keeps_stale_value(
        self, fake_redis: FakeRedis, session_factory, clock: FakeClock
    ) -> None:
        """刷新失败不影响读：旧值继续服务，这正是逻辑过期的容错优势。"""
        settings = make_settings(
            cache_strategy="logical_expire", cache_ttl_seconds=300, cache_ttl_jitter_ratio=0.0
        )
        service = build_service(fake_redis, session_factory, settings, clock=clock)
        cache = service.cache
        await cache.store(CODE, URL)
        cache._clock = FakeClock(start=clock.now + 301)

        async def boom() -> str:
            raise RuntimeError("DB 挂了")

        assert await cache.resolve(CODE, boom) == URL  # 这次能返回
        await cache.drain_background()  # 后台刷新抛异常，但不能把异常漏出来
        assert await cache.resolve(CODE, boom) == URL  # 旧值仍在

    async def test_refresh_rechecks_after_acquiring_lock(
        self, fake_redis: FakeRedis, session_factory, clock: FakeClock
    ) -> None:
        """单飞必须在拿到锁后复查，否则排队中的刷新任务会串行各刷一次。

        这是压测打出来的真实 bug：100 个并发过期命中各 spawn 一个刷新任务，
        第一个刷完释放锁后，后面的任务依次拿到锁、各自再查一次库
        （实测 db_loads 从 1 变成 901）。锁只保证"同时只有一个"，
        不保证"后来者知道前面已经干完了"。
        """
        settings = make_settings(
            cache_strategy="logical_expire",
            cache_ttl_seconds=300,
            cache_ttl_jitter_ratio=0.0,
        )
        service = build_service(fake_redis, session_factory, settings, clock=clock)
        cache = service.cache
        await cache.store(CODE, URL)
        cache._clock = FakeClock(start=clock.now + 301)
        calls: list[int] = []

        async def slow_load() -> str:
            calls.append(1)
            await asyncio.sleep(0.02)
            return URL

        await asyncio.gather(*(cache.resolve(CODE, slow_load) for _ in range(50)))
        await cache.drain_background()
        assert len(calls) == 1, f"过期瞬间回源了 {len(calls)} 次，刷新复查缺失"


class TestDegradation:
    """缓存故障不能变成服务故障。"""

    async def test_redis_down_falls_back_to_loader(
        self, fake_redis: FakeRedis, session_factory
    ) -> None:
        service = build_service(fake_redis, session_factory, make_settings())
        cache = service.cache

        async def broken_get(*args, **kwargs):
            raise RedisConnectionError("redis is down")

        cache._redis.get = broken_get  # type: ignore[method-assign]
        calls: list[int] = []
        assert await cache.resolve(CODE, loader_returning(URL, calls)) == URL
        assert calls == [1]
        assert cache.breaker.trips == 1

    async def test_breaker_skips_redis_after_trip(
        self, fake_redis: FakeRedis, session_factory
    ) -> None:
        service = build_service(fake_redis, session_factory, make_settings())
        cache = service.cache
        attempts = 0

        async def broken_get(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            raise RedisConnectionError("redis is down")

        cache._redis.get = broken_get  # type: ignore[method-assign]
        await cache.resolve(CODE, loader_returning(URL))
        attempts_after_first = attempts
        for _ in range(10):
            await cache.resolve(CODE, loader_returning(URL))
        # 熔断打开后不再尝试 Redis，只回源
        assert attempts == attempts_after_first
        assert cache.breaker.is_open

    async def test_breaker_retries_after_cooldown(
        self, fake_redis: FakeRedis, session_factory
    ) -> None:
        settings = make_settings(cache_breaker_cooldown_seconds=0.02)
        service = build_service(fake_redis, session_factory, settings)
        cache = service.cache
        cache.breaker.trip()
        assert cache.breaker.is_open
        await asyncio.sleep(0.04)
        assert not cache.breaker.is_open  # 冷却结束，允许探测

    async def test_write_failure_does_not_break_read(
        self, fake_redis: FakeRedis, session_factory
    ) -> None:
        service = build_service(fake_redis, session_factory, make_settings())
        cache = service.cache

        async def broken_set(*args, **kwargs):
            raise RedisConnectionError("redis is down")

        cache._redis.set = broken_set  # type: ignore[method-assign]
        assert await cache.resolve(CODE, loader_returning(URL)) == URL

    async def test_negative_write_failure_returns_none(
        self, fake_redis: FakeRedis, session_factory
    ) -> None:
        service = build_service(fake_redis, session_factory, make_settings())
        cache = service.cache

        async def broken_set(*args, **kwargs):
            raise RedisConnectionError("redis is down")

        cache._redis.set = broken_set  # type: ignore[method-assign]
        assert await cache.resolve(CODE, loader_returning(None)) is None


class TestStats:
    async def test_hit_rate_accounting(self, fake_redis: FakeRedis, session_factory) -> None:
        service = build_service(fake_redis, session_factory, make_settings())
        cache = service.cache
        load = loader_returning(URL)
        await cache.resolve(CODE, load)  # miss
        await cache.resolve(CODE, load)  # hit
        await cache.resolve(CODE, load)  # hit
        assert cache.stats.misses == 1
        assert cache.stats.hits == 2
        assert cache.stats.hit_rate == pytest.approx(0.6667, abs=1e-4)

    async def test_negative_hits_counted_separately(
        self, fake_redis: FakeRedis, session_factory
    ) -> None:
        service = build_service(fake_redis, session_factory, make_settings())
        cache = service.cache
        load = loader_returning(None)
        await cache.resolve(CODE, load)
        await cache.resolve(CODE, load)
        assert cache.stats.negative_hits == 1

    async def test_service_stats_snapshot(self, fake_redis: FakeRedis, session_factory) -> None:
        service = ShortlinkService.build(fake_redis, session_factory, make_settings())
        await service.cache.resolve(CODE, loader_returning(URL))
        snapshot = service.cache_stats()
        assert snapshot["strategy"] == "mutex"
        assert snapshot["lookups"] == 1
        assert snapshot["breaker_open"] is False
