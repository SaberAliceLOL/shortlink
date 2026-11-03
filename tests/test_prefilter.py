"""前置过滤器测试：水位守卫 vs 布隆过滤器。

重点不是"能挡住不存在的短码"，而是**边界与故障下的方向性**：
  - 过滤器出错时必须放行，绝不能误杀真实链接（假阴性 = 用户 404 = 事故）
  - 水位落后的窗口里，靠 margin 和本地抬升兜住
  - 布隆的假阳性只应导致"多查一次库"，不是错误
"""

import pytest
from fakeredis.aioredis import FakeRedis
from sqlalchemy import text

from app.services.base62 import encode
from app.services.prefilter import (
    BloomFilter,
    NullPrefilter,
    WatermarkPrefilter,
    build_prefilter,
)
from tests.conftest import make_settings

BIZ = "shortlink"


async def _set_watermark(session_factory, max_id: int, step: int = 1000) -> None:
    async with session_factory() as s:
        await s.execute(
            text("UPDATE segment_alloc SET max_id = :m, step = :s WHERE biz_tag = :t"),
            {"m": max_id, "s": step, "t": BIZ},
        )
        await s.commit()


class TestWatermark:
    async def test_rejects_ids_beyond_watermark(self, session_factory) -> None:
        await _set_watermark(session_factory, 62**4 + 999)
        flt = WatermarkPrefilter(session_factory, refresh_seconds=0, margin_steps=0)
        await flt.refresh()
        assert await flt.is_definitely_absent(encode(62**4 + 5000)) is True
        assert flt.rejected == 1

    async def test_allows_ids_at_or_before_watermark(self, session_factory) -> None:
        """ID <= 水位只是"不能否定"，不代表存在——必须放行去查库。"""
        await _set_watermark(session_factory, 62**4 + 999)
        flt = WatermarkPrefilter(session_factory, refresh_seconds=0, margin_steps=0)
        await flt.refresh()
        assert await flt.is_definitely_absent(encode(62**4)) is False
        assert await flt.is_definitely_absent(encode(62**4 + 999)) is False

    async def test_margin_protects_against_stale_watermark(self, session_factory) -> None:
        """水位落后时，margin × step 之内的号必须放行——这是防误杀的唯一保险。"""
        await _set_watermark(session_factory, 62**4, step=1000)
        flt = WatermarkPrefilter(session_factory, refresh_seconds=60, margin_steps=10)
        await flt.refresh()

        just_issued = 62**4 + 5000  # 水位之后又发出去 5000 个号（5 个号段）
        assert await flt.is_definitely_absent(encode(just_issued)) is False

        way_beyond = 62**4 + 20_000  # 超出 margin 范围，可以安全否定
        assert await flt.is_definitely_absent(encode(way_beyond)) is True

    async def test_note_issued_id_raises_local_watermark(self, session_factory) -> None:
        """自己刚发的号，绝不能自己拒掉。"""
        await _set_watermark(session_factory, 62**4)
        flt = WatermarkPrefilter(session_factory, refresh_seconds=60, margin_steps=0)
        await flt.refresh()

        new_id = 62**4 + 1
        flt.note_issued_id(new_id)
        assert await flt.is_definitely_absent(encode(new_id)) is False

    async def test_add_updates_watermark_without_db(self, session_factory) -> None:
        flt = WatermarkPrefilter(session_factory, refresh_seconds=0, margin_steps=0)
        await flt.add(encode(62**4 + 123))
        assert flt.threshold >= 62**4 + 123

    async def test_db_failure_allows_everything(self, session_factory) -> None:
        """过滤器故障必须放行：误杀真实链接比多查一次库严重得多。"""
        flt = WatermarkPrefilter(session_factory, refresh_seconds=0, margin_steps=0)
        await flt.refresh()

        def broken_factory():
            raise RuntimeError("DB is down")

        flt._factory = broken_factory  # type: ignore[assignment]
        flt._refreshed_at = None  # 强制它去刷新（然后失败）
        # 刷新失败后阈值失效，一律放行——即便这个号远超水位
        assert await flt.is_definitely_absent(encode(62**9)) is False

    async def test_refresh_is_throttled(self, session_factory) -> None:
        """刷新间隔内不重复查库，否则每次跳转都多一次 DB 往返。"""
        calls = 0
        flt = WatermarkPrefilter(session_factory, refresh_seconds=60, margin_steps=10)

        original = flt.refresh

        async def counting_refresh() -> None:
            nonlocal calls
            calls += 1
            await original()

        flt.refresh = counting_refresh  # type: ignore[method-assign]
        await flt.is_definitely_absent(encode(62**4))
        for _ in range(5):
            await flt.is_definitely_absent(encode(62**4))
        assert calls == 1

    async def test_invalid_code_is_not_rejected(self, session_factory) -> None:
        """格式非法的输入不该被过滤器"猜"成不存在，交给路由层判 404。"""
        flt = WatermarkPrefilter(session_factory, refresh_seconds=0, margin_steps=0)
        await flt.refresh()
        assert await flt.is_definitely_absent("!!!") is False


class TestBloom:
    async def test_parameters_match_target_error_rate(self, fake_redis: FakeRedis) -> None:
        """n=100 万、p=1% → m ≈ 959 万位（1.14 MiB）、k=7。"""
        bf = BloomFilter(fake_redis, capacity=1_000_000, error_rate=0.01)
        assert bf.bit_count == pytest.approx(9_585_059, rel=0.01)
        assert bf.hash_count == 7

    async def test_added_codes_are_never_rejected(self, fake_redis: FakeRedis) -> None:
        """布隆绝不能有假阴性——已写入的短码必须永远判为"可能存在"。"""
        bf = BloomFilter(fake_redis, capacity=10_000, error_rate=0.01)
        for i in range(500):
            code = encode(62**4 + i)
            await bf.add(code)
            assert await bf.is_definitely_absent(code) is False

    async def test_codes_not_added_are_mostly_rejected(self, fake_redis: FakeRedis) -> None:
        bf = BloomFilter(fake_redis, capacity=10_000, error_rate=0.01)
        for i in range(200):
            await bf.add(encode(62**4 + i))

        rejected, false_positive = 0, 0
        for i in range(1000, 2000):
            if await bf.is_definitely_absent(encode(62**4 + i)):
                rejected += 1
            else:
                false_positive += 1
        # 假阳性理论上 ~1%，给足余量
        assert false_positive < 100
        assert rejected > 900

    async def test_redis_failure_allows_everything(self, fake_redis: FakeRedis) -> None:
        bf = BloomFilter(fake_redis, capacity=1000)

        def broken_pipeline(*args, **kwargs):
            raise RuntimeError("redis is down")

        bf._redis.pipeline = broken_pipeline  # type: ignore[method-assign]
        assert await bf.is_definitely_absent(encode(62**4)) is False


class TestBuildPrefilter:
    def test_strategy_none(self, fake_redis: FakeRedis, session_factory) -> None:
        flt = build_prefilter(fake_redis, session_factory, make_settings(prefilter_strategy="none"))
        assert isinstance(flt, NullPrefilter)

    def test_strategy_watermark(self, fake_redis: FakeRedis, session_factory) -> None:
        settings = make_settings(prefilter_strategy="watermark")
        flt = build_prefilter(fake_redis, session_factory, settings)
        assert isinstance(flt, WatermarkPrefilter)

    def test_strategy_bloom(self, fake_redis: FakeRedis, session_factory) -> None:
        settings = make_settings(prefilter_strategy="bloom")
        flt = build_prefilter(fake_redis, session_factory, settings)
        assert isinstance(flt, BloomFilter)

    async def test_null_prefilter_never_rejects(self) -> None:
        flt = NullPrefilter()
        assert flt.rejected == 0
        assert await flt.is_definitely_absent("anything") is False
