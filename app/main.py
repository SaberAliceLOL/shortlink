"""ShortLink 入口。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.api.landing import router as landing_router
from app.api.shortlink import router as shortlink_router
from app.core.deps import engine, get_shortlink_service, redis_client
from app.services.shortlink_service import ShortlinkService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    # 关闭时释放连接池（压测必配，否则大量 TIME_WAIT）
    await engine.dispose()
    await redis_client.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="ShortLink", version="0.1.0", lifespan=lifespan)

    # 注意顺序：兜底路由 /{code} 会吃掉一切单段路径，所以 /health 必须先注册。
    # 顺带一提 "/health" 这六个字符全在 Base62 字符集里，如果注册在后面，
    # 访问它会被当成一个短码去查库，返回 404 而不是健康检查。
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # 缓存命中率/熔断状态。单机多 worker 时每个进程各一份计数，
    # Week 7 换成 Prometheus 指标后这个端点就可以退场。
    @app.get("/internal/cache-stats")
    async def cache_stats(
        service: ShortlinkService = Depends(get_shortlink_service),
    ) -> dict[str, object]:
        return service.cache_stats()

    # 首页也必须先于 /{code} 注册，否则 "/" 会被当作空短码
    app.include_router(landing_router)
    app.include_router(shortlink_router)

    return app


app = create_app()
