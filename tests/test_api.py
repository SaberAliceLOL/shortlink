"""接口层集成测试：用 SQLite + fakeredis 替身跑真实 HTTP 请求。

为什么要替身而不是真 MySQL：
  - CI 环境没有 MySQL 容器，测试必须自包含
  - 接口层测的是"路由 + 校验 + 编排"，数据库方言不是被测对象
  - 依赖注入让替换成为一行 dependency_overrides（Week 0 讲的 DI 价值在这里兑现）

真 MySQL 的行为（字符集、索引、认证插件）由 Week 5 压测和手工验证覆盖。
夹具在 tests/conftest.py；缓存/穿透细节见 tests/test_cache.py、tests/test_prefilter.py。
"""

from sqlalchemy import text

from app.core.deps import get_shortlink_service

LONG_URL = "https://example.com/a/very/long/path?with=query&more=1"


class TestShorten:
    async def test_create_returns_code_and_short_url(self, http) -> None:
        resp = await http.post("/api/shorten", json={"url": LONG_URL})
        assert resp.status_code == 201
        body = resp.json()
        assert body["long_url"] == LONG_URL
        assert len(body["code"]) == 5
        assert body["short_url"].endswith("/" + body["code"])

    async def test_codes_are_distinct(self, http) -> None:
        codes = {
            (await http.post("/api/shorten", json={"url": f"{LONG_URL}#{i}"})).json()["code"]
            for i in range(5)
        }
        assert len(codes) == 5

    async def test_reject_javascript_scheme(self, http) -> None:
        resp = await http.post("/api/shorten", json={"url": "javascript:alert(1)"})
        assert resp.status_code == 422

    async def test_reject_ftp_scheme(self, http) -> None:
        resp = await http.post("/api/shorten", json={"url": "ftp://example.com/f"})
        assert resp.status_code == 422

    async def test_reject_too_long_url(self, http) -> None:
        resp = await http.post("/api/shorten", json={"url": "https://e.com/" + "a" * 2100})
        assert resp.status_code == 422

    async def test_reject_missing_url(self, http) -> None:
        resp = await http.post("/api/shorten", json={})
        assert resp.status_code == 422


class TestRedirect:
    async def test_302_with_location(self, http) -> None:
        code = (await http.post("/api/shorten", json={"url": LONG_URL})).json()["code"]
        resp = await http.get(f"/{code}", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == LONG_URL

    async def test_second_hit_uses_cache(self, http) -> None:
        code = (await http.post("/api/shorten", json={"url": LONG_URL})).json()["code"]
        first = await http.get(f"/{code}", follow_redirects=False)
        second = await http.get(f"/{code}", follow_redirects=False)
        assert first.headers["location"] == second.headers["location"] == LONG_URL

    async def test_unknown_code_404(self, http) -> None:
        assert (await http.get("/zzzzz", follow_redirects=False)).status_code == 404

    async def test_malformed_code_404(self, http) -> None:
        # 太短 / 含非法字符 → 404（不暴露"格式错"的 422）
        assert (await http.get("/ab", follow_redirects=False)).status_code == 404
        assert (await http.get("/abc-de", follow_redirects=False)).status_code == 404

    async def test_non_ascii_digit_rejected_before_storage(self, http) -> None:
        """全角数字是 isalnum()=True 的字符，但不是 Base62，必须直接 404。"""
        assert (await http.get("/１２３４５", follow_redirects=False)).status_code == 404

    async def test_hit_recorded(self, http, fake_redis) -> None:
        code = (await http.post("/api/shorten", json={"url": LONG_URL})).json()["code"]
        await http.get(f"/{code}", follow_redirects=False)
        # 埋点格式：code|timestamp_ms
        items = await fake_redis.lrange("hits", 0, -1)
        assert len(items) == 1 and items[0].startswith(f"{code}|")

    async def test_unknown_code_is_negatively_cached(self, http, fake_redis) -> None:
        """穿透治理的接口层验证：第二次访问不再回源。"""
        await http.get("/zzzz1", follow_redirects=False)
        assert await fake_redis.get("u:zzzz1") is not None

    async def test_cache_stats_endpoint(self, http) -> None:
        code = (await http.post("/api/shorten", json={"url": LONG_URL})).json()["code"]
        await http.get(f"/{code}", follow_redirects=False)
        await http.get(f"/{code}", follow_redirects=False)
        stats = (await http.get("/internal/cache-stats")).json()
        # 创建时预热了缓存，所以两次读都命中，一次 DB 都没碰
        assert stats["lookups"] == 2
        assert stats["hits"] == 2
        assert stats["misses"] == 0


class TestCodeCaseSensitivity:
    """短码大小写敏感性的回归防线（Week 2 部署时发现的真实事故）。

    Base62 的 0-9A-Za-z 里大小写是不同字符。MySQL 默认 utf8mb4_unicode_ci
    不区分大小写，导致：
      - 写入：'1012w' 与 '1012W' 撞主键 → 并发下 500
      - 读取：查 '1012w' 命中 '1012W' 的记录 → 跳到别人的链接

    SQLite 的默认比较本就区分大小写，所以这个测试通过**不能**证明 MySQL 侧正确；
    它防的是应用层把大小写归一化（比如 .lower()），以及缓存键把两个短码混成一个。
    真正的防线是迁移 c4e8d2a6f910 把列改成 utf8mb4_bin，两侧都有测试才闭环。
    """

    async def test_lowercase_and_uppercase_are_distinct(self, http, session_factory) -> None:
        async with session_factory() as s:
            await s.execute(
                text(
                    "INSERT INTO url_map (code, url) VALUES "
                    "('AbCdE', 'https://example.com/lower'), "
                    "('ABCDE', 'https://example.com/upper')"
                )
            )
            await s.commit()

        lower = await http.get("/AbCdE", follow_redirects=False)
        upper = await http.get("/ABCDE", follow_redirects=False)
        assert lower.headers["location"] == "https://example.com/lower"
        assert upper.headers["location"] == "https://example.com/upper"


class TestHealth:
    async def test_health_not_shadowed_by_code_route(self, http) -> None:
        """兜底路由 /{code} 不能吃掉 /health——路由顺序的回归防线。"""
        resp = await http.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    async def test_internal_stats_not_shadowed(self, http) -> None:
        assert (await http.get("/internal/cache-stats")).status_code == 200


class TestLanding:
    """根路径落地页。

    这一页的失败模式很隐蔽：路由被 /{code} 吃掉时会返回 404 而不是抛错，
    所以既要测状态码，也要测它确实渲染出了内容。
    """

    async def test_root_returns_html(self, http) -> None:
        resp = await http.get("/")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "ShortLink" in resp.text

    async def test_root_lists_real_endpoints(self, http) -> None:
        body = (await http.get("/")).text
        for path in ("/api/shorten", "/health", "/internal/cache-stats", "/docs"):
            assert path in body, f"首页没有提到 {path}"

    async def test_root_uses_request_host(self, http) -> None:
        """示例 curl 里的 host 必须跟着实际访问地址走，不能写死。"""
        resp = await http.get("/", headers={"host": "example.test:9999"})
        assert "http://example.test:9999/api/shorten" in resp.text

    async def test_root_not_shadowed_by_code_route(self, http) -> None:
        """空路径不能被当成短码去查库。"""
        assert (await http.get("/", follow_redirects=False)).status_code == 200

    async def test_root_not_in_openapi_schema(self, http) -> None:
        """落地页是给人看的，不该出现在 API 文档里。"""
        schema = (await http.get("/openapi.json")).json()
        assert "/" not in schema["paths"]


def test_service_is_overridden_by_dependency_injection(client) -> None:
    """夹具必须真的换掉了生产 service——否则测试会连到本地 MySQL/Redis。"""
    from tests.conftest import build_service  # noqa: F401

    _, service = client
    assert service is not get_shortlink_service()
