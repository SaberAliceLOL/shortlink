"""号段式发号器单测。

不连真实 MySQL：用 SQLite 内存库模拟 segment_alloc 表（aiosqlite），
测的是发号器自身的并发语义与续段逻辑——这是单测的正确边界。
"""

import asyncio

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base
from app.services.idgen import SegmentIDGen


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # 种子：起点 62^4，步长 5（小步长方便触发续段）
    async with factory() as s:
        await s.execute(
            text(
                "INSERT INTO segment_alloc (biz_tag, max_id, step) "
                "VALUES ('shortlink', :start, :step)"
            ),
            {"start": 62**4 - 1, "step": 5},
        )
        await s.commit()
    yield factory
    await engine.dispose()


class TestSegmentIDGen:
    async def test_ids_are_sequential(self, session_factory) -> None:
        gen = SegmentIDGen(session_factory, "shortlink")
        ids = [await gen.next_id() for _ in range(3)]
        assert ids == [62**4, 62**4 + 1, 62**4 + 2]

    async def test_segment_reload_on_exhaust(self, session_factory) -> None:
        # 步长 5：第 6 个号触发续段
        gen = SegmentIDGen(session_factory, "shortlink")
        ids = [await gen.next_id() for _ in range(6)]
        assert ids[:5] == [62**4 + i for i in range(5)]
        assert ids[5] == 62**4 + 5  # 新号段起点

    async def test_concurrent_unique(self, session_factory) -> None:
        """100 个并发取号，必须严格唯一且连续（锁 + double-check 的意义）。"""
        gen = SegmentIDGen(session_factory, "shortlink")
        ids = await asyncio.gather(*(gen.next_id() for _ in range(100)))
        assert len(set(ids)) == 100
        assert sorted(ids) == list(range(62**4, 62**4 + 100))

    async def test_db_records_allocation(self, session_factory) -> None:
        """并发结束后，DB 里的 max_id 必须覆盖所有已发出的号。"""
        gen = SegmentIDGen(session_factory, "shortlink")
        await asyncio.gather(*(gen.next_id() for _ in range(12)))  # 12 > 2 个号段
        async with session_factory() as s:
            max_id = (await s.execute(text("SELECT max_id FROM segment_alloc"))).scalar()
        assert max_id is not None and max_id >= 62**4 + 12 - 1

    async def test_next_code_shape(self, session_factory) -> None:
        gen = SegmentIDGen(session_factory, "shortlink")
        code = await gen.next_code()
        assert isinstance(code, str) and len(code) == 5  # 起点 62^4 → 5 位
