"""ShortLink 入口。"""

from fastapi import FastAPI

from app.api.notes import router as notes_router
from app.core.deps import engine


def create_app() -> FastAPI:
    app = FastAPI(title="ShortLink", version="0.1.0")
    app.include_router(notes_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()


@app.on_event("shutdown")  # Week 1 换成 lifespan；on_event 已废弃但 Day 5 够用
async def shutdown() -> None:
    await engine.dispose()
