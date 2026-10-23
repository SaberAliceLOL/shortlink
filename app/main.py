"""ShortLink 入口。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.shortlink import router as shortlink_router
from app.core.deps import engine, redis_client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    # 关闭时释放连接池（压测必配，否则大量 TIME_WAIT）
    await engine.dispose()
    await redis_client.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="ShortLink", version="0.1.0", lifespan=lifespan)

    # 注意顺序：兜底路由 /{code} 会吃掉一切单段路径，所以 /health 必须先注册
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(shortlink_router)

    return app


app = create_app()
