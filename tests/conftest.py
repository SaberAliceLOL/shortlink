"""测试公共夹具：SQLite + fakeredis 替身。

依赖注入的价值在这里兑现——整套缓存/DB 栈都能在无容器环境里跑起来。
Week 3 把 service 也做成可替换的，所以这里多了一个 `build_service`：
同一份生产代码，靠一份 Settings 决定用哪种重建策略、开哪种穿透过滤。
"""

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.deps import get_db, get_idgen, get_shortlink_service
from app.main import create_app
from app.models import Base
from app.services.idgen import SegmentIDGen
from app.services.shortlink_service import ShortlinkService


def make_settings(**overrides: Any) -> Settings:
    """测试用 Settings：不读 .env，所有值显式给出，测试不受本地环境影响。"""
    base: dict[str, Any] = {
        "_env_file": None,
        "cache_enabled": True,
        "cache_strategy": "mutex",
        # 接口测试会手工 INSERT 任意短码，水位守卫会把它们判成"未来号"而拒掉，
        # 所以默认关掉前置过滤；过滤器本身在 tests/test_prefilter.py 里单独测。
        "prefilter_strategy": "none",
        "hits_record_enabled": True,
        "cache_ttl_seconds": 300,
        "cache_negative_ttl_seconds": 60,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


class FakeClock:
    """可控时钟：让"逻辑过期"在测试里立刻发生，不用 sleep 300 秒。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """裸 session 工厂：给需要直接构造数据的测试用（例如验证 SQL 层语义）。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        await s.execute(
            text(
                "INSERT INTO segment_alloc (biz_tag, max_id, step) "
                "VALUES ('shortlink', :start, 1000)"
            ),
            {"start": 62**4 - 1},
        )
        await s.commit()
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def fake_redis() -> AsyncIterator[FakeRedis]:
    client = FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


def build_service(
    redis: FakeRedis,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    clock: Callable[[], float] | None = None,
) -> ShortlinkService:
    """用真实生产代码装配一套独立链路（独立缓存、独立熔断、独立水位）。"""
    return ShortlinkService.build(redis, session_factory, settings, wall_clock=clock)


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    fake_redis: FakeRedis,
    settings: Settings,
) -> AsyncIterator[tuple[AsyncClient, ShortlinkService]]:
    async def override_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    service = build_service(fake_redis, session_factory, settings)
    app = create_app()
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_idgen] = lambda: SegmentIDGen(session_factory, "shortlink")
    app.dependency_overrides[get_shortlink_service] = lambda: service

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
        yield ac, service

    await service.cache.drain_background()


@pytest_asyncio.fixture
async def http(
    client: tuple[AsyncClient, ShortlinkService],
) -> AsyncIterator[AsyncClient]:
    """只要 HTTP 客户端的测试用这个（绝大多数）。"""
    yield client[0]
