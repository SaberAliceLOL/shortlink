"""接口层集成测试：用 SQLite + fakeredis 替身跑真实 HTTP 请求。

为什么要替身而不是真 MySQL：
  - CI 环境没有 MySQL 容器，测试必须自包含
  - 接口层测的是"路由 + 校验 + 编排"，数据库方言不是被测对象
  - 依赖注入让替换成为一行 dependency_overrides（Week 0 讲的 DI 价值在这里兑现）

真 MySQL 的行为（字符集、索引、认证插件）由 Week 5 压测和手工验证覆盖。
"""

import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.deps import get_db, get_redis
from app.main import create_app
from app.models import Base


@pytest_asyncio.fixture
async def client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # 种子号段：起点 62^4，步长 1000（生产同款）
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO segment_alloc (biz_tag, max_id, step) "
                "VALUES ('shortlink', :start, 1000)"
            ),
            {"start": 62**4 - 1},
        )
        await s.commit()

    async def override_db():
        async with session_factory() as session:
            yield session

    fake_redis = FakeRedis(decode_responses=True)
    app = create_app()
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_redis] = lambda: fake_redis

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
        yield ac

    await fake_redis.aclose()
    await engine.dispose()


LONG_URL = "https://example.com/a/very/long/path?with=query&more=1"


class TestShorten:
    async def test_create_returns_code_and_short_url(self, client) -> None:
        resp = await client.post("/api/shorten", json={"url": LONG_URL})
        assert resp.status_code == 201
        body = resp.json()
        assert body["long_url"] == LONG_URL
        assert len(body["code"]) == 5
        assert body["short_url"].endswith("/" + body["code"])

    async def test_codes_are_distinct(self, client) -> None:
        codes = {
            (await client.post("/api/shorten", json={"url": f"{LONG_URL}#{i}"})).json()["code"]
            for i in range(5)
        }
        assert len(codes) == 5

    async def test_reject_javascript_scheme(self, client) -> None:
        resp = await client.post("/api/shorten", json={"url": "javascript:alert(1)"})
        assert resp.status_code == 422

    async def test_reject_ftp_scheme(self, client) -> None:
        resp = await client.post("/api/shorten", json={"url": "ftp://example.com/f"})
        assert resp.status_code == 422

    async def test_reject_too_long_url(self, client) -> None:
        resp = await client.post("/api/shorten", json={"url": "https://e.com/" + "a" * 2100})
        assert resp.status_code == 422

    async def test_reject_missing_url(self, client) -> None:
        resp = await client.post("/api/shorten", json={})
        assert resp.status_code == 422


class TestRedirect:
    async def test_302_with_location(self, client) -> None:
        code = (await client.post("/api/shorten", json={"url": LONG_URL})).json()["code"]
        resp = await client.get(f"/{code}", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == LONG_URL

    async def test_second_hit_uses_cache(self, client) -> None:
        code = (await client.post("/api/shorten", json={"url": LONG_URL})).json()["code"]
        first = await client.get(f"/{code}", follow_redirects=False)
        second = await client.get(f"/{code}", follow_redirects=False)
        assert first.headers["location"] == second.headers["location"] == LONG_URL

    async def test_unknown_code_404(self, client) -> None:
        assert (await client.get("/zzzzz", follow_redirects=False)).status_code == 404

    async def test_malformed_code_404(self, client) -> None:
        # 太短 / 含非法字符 → 404（不暴露"格式错"的 422）
        assert (await client.get("/ab", follow_redirects=False)).status_code == 404
        assert (await client.get("/abc-de", follow_redirects=False)).status_code == 404

    async def test_hit_recorded(self, client) -> None:
        code = (await client.post("/api/shorten", json={"url": LONG_URL})).json()["code"]
        await client.get(f"/{code}", follow_redirects=False)
        # 埋点格式：code|timestamp_ms
        redis = client._transport.app.dependency_overrides[get_redis]()  # type: ignore[attr-defined]
        items = await redis.lrange("hits", 0, -1)
        assert len(items) == 1 and items[0].startswith(f"{code}|")


class TestHealth:
    async def test_health_not_shadowed_by_code_route(self, client) -> None:
        """兜底路由 /{code} 不能吃掉 /health——路由顺序的回归防线。"""
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
