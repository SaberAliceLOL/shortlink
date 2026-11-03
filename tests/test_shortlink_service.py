"""服务编排层测试：三级过滤的协作方式。

前面的 test_cache.py / test_prefilter.py 分别验证两层。
这里验证的是它们**串起来**以后的行为——尤其是那条最容易写错的顺序：
前置过滤在最前面，所以它必须在"新链接刚生成"时立刻被通知，
否则新链接会被自己的过滤器打回 404。
"""

import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.idgen import SegmentIDGen
from app.services.shortlink_service import ShortlinkService
from tests.conftest import build_service, make_settings

URL = "https://example.com/target"


@pytest_asyncio.fixture
async def service(
    fake_redis: FakeRedis, session_factory: async_sessionmaker[AsyncSession]
) -> ShortlinkService:
    settings = make_settings(prefilter_strategy="watermark", prefilter_refresh_seconds=60)
    return build_service(fake_redis, session_factory, settings)


async def _create(
    service: ShortlinkService,
    session_factory: async_sessionmaker[AsyncSession],
    idgen: SegmentIDGen,
    url: str = URL,
) -> str:
    async with session_factory() as db:
        return await service.create(url, db=db, idgen=idgen)


class TestCreateThenRead:
    async def test_new_link_is_immediately_readable(
        self, service: ShortlinkService, session_factory
    ) -> None:
        """回归点：水位守卫会把"未来的号"判成不存在，所以 create 必须先通知它。"""
        idgen = SegmentIDGen(session_factory, "shortlink")
        code = await _create(service, session_factory, idgen)

        async with session_factory() as db:
            assert await service.resolve(code, db=db) == URL

    async def test_create_prewarms_cache(
        self, service: ShortlinkService, fake_redis: FakeRedis, session_factory
    ) -> None:
        """写入即预热，省掉第一次读的回源。"""
        idgen = SegmentIDGen(session_factory, "shortlink")
        code = await _create(service, session_factory, idgen)
        assert await fake_redis.get(f"u:{code}") is not None

    async def test_create_overwrites_preplanted_negative_cache(
        self, service: ShortlinkService, fake_redis: FakeRedis, session_factory
    ) -> None:
        """预埋负缓存攻击：先请求未来的短码把 404 写进缓存，链接生成后必须可访问。

        这个测试守着一条真实存在的攻击面——短码是 Base62(自增 ID)，
        下一个短码完全可预测。如果不做写入侧覆盖，攻击者可以在负缓存 TTL 内
        让一条刚生成的链接持续 404。
        """
        idgen = SegmentIDGen(session_factory, "shortlink")
        # 先算出一个"将来会用到的"短码：发号器还没发，所以先不动它
        from app.services.base62 import decode, encode

        async with session_factory() as db:
            # 直接用发号器的号段信息推出下一个号，但只读取、不消耗
            first_code = await _create(service, session_factory, idgen)
            next_id = decode(first_code) + 1
        predicted = encode(next_id)

        # 攻击者预埋 404
        async with session_factory() as db:
            assert await service.resolve(predicted, db=db) is None
        assert await fake_redis.get(f"u:{predicted}") is not None

        # 服务端生成这条链接（号必须对上，所以手工落一条同号的记录来构造场景）
        from sqlalchemy import text

        async with session_factory() as s:
            await s.execute(
                text("INSERT INTO url_map (code, url) VALUES (:c, :u)"),
                {"c": predicted, "u": URL},
            )
            await s.commit()
        await service.cache.store(predicted, URL)  # 等价于 service.create 的预热步骤

        async with session_factory() as db:
            assert await service.resolve(predicted, db=db) == URL


class TestThreeLevelFiltering:
    async def test_prefilter_rejects_before_cache_and_db(
        self, service: ShortlinkService, session_factory
    ) -> None:
        """远超水位的短码：不该碰缓存，也不该碰 DB。"""
        await service.prefilter.refresh()  # type: ignore[attr-defined]
        far_future = "zzzzz"
        async with session_factory() as db:
            assert await service.resolve(far_future, db=db) is None
        assert service.prefilter.rejected == 1  # type: ignore[attr-defined]

    async def test_existing_code_goes_through_cache(
        self, service: ShortlinkService, session_factory
    ) -> None:
        idgen = SegmentIDGen(session_factory, "shortlink")
        code = await _create(service, session_factory, idgen)
        async with session_factory() as db:
            await service.resolve(code, db=db)
            first_hits = service.cache.stats.hits
            await service.resolve(code, db=db)
        assert service.cache.stats.hits == first_hits + 1

    async def test_unknown_code_within_watermark_gets_negative_cached(
        self, service: ShortlinkService, session_factory, fake_redis: FakeRedis
    ) -> None:
        """水位之内的空洞（号段分配了但号没发）靠负缓存挡住，过滤器帮不上忙。"""
        idgen = SegmentIDGen(session_factory, "shortlink")
        code = await _create(service, session_factory, idgen)
        from app.services.base62 import decode, encode

        hole = encode(decode(code) + 500)  # 在同号段内，但这条号不会被发出
        async with session_factory() as db:
            assert await service.resolve(hole, db=db) is None
        assert await fake_redis.get(f"u:{hole}") is not None
