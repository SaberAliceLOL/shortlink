"""ShortLink 入口。"""

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="ShortLink", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
