"""短链核心路由：生成 + 跳转（设计文档 D3/D4）。"""

import time

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import HttpUrl, ValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import AsyncSessionLocal, get_db, get_redis
from app.models.url_map import UrlMap
from app.schemas.shortlink import ShortenRequest, ShortenResponse
from app.services.idgen import SegmentIDGen

router = APIRouter(tags=["shortlink"])


def _now_ms() -> int:
    return int(time.time() * 1000)


def _validate_url(raw: str) -> str:
    """http/https 之外的协议（ftp/javascript/file…）一律拒绝。"""
    try:
        url = HttpUrl(raw)
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"非法 URL: {e.errors()[0]['msg']}",
        ) from e
    if url.scheme not in ("http", "https"):
        raise HTTPException(status_code=422, detail="只接受 http/https 协议")
    return str(url)


# 发号器是进程级单例：号段缓存的意义就在于此，每个请求共享同一段
_idgen = SegmentIDGen(AsyncSessionLocal, biz_tag="shortlink")


@router.post("/api/shorten", response_model=ShortenResponse, status_code=status.HTTP_201_CREATED)
async def shorten(payload: ShortenRequest, db: AsyncSession = Depends(get_db)) -> ShortenResponse:
    url = _validate_url(payload.url)
    code = await _idgen.next_code()

    row = UrlMap(code=code, url=url)
    db.add(row)
    try:
        await db.commit()
    except (
        Exception
    ) as e:  # noqa: BLE001  # 主键冲突等写失败：让发号器丢号也无妨（非连续不破坏唯一性）
        await db.rollback()
        raise HTTPException(status_code=500, detail="写入失败") from e

    settings = get_settings()
    short_url = f"http://{settings.public_host}/{code}"
    return ShortenResponse(code=code, short_url=short_url, long_url=url)


@router.get("/{code}", response_class=RedirectResponse, status_code=status.HTTP_302_FOUND)
async def redirect(
    code: str,
    db: AsyncSession = Depends(get_db),
    cache: Redis = Depends(get_redis),
) -> RedirectResponse:
    # 廉价防御：格式非法的 code 连存储都不碰（设计文档 D4）。
    # 用 404 而非 422：对外部访问者来说"这个短链不存在"比"你参数格式错"更准确
    if not (5 <= len(code) <= 7) or not code.isalnum():
        raise HTTPException(status_code=404, detail="not found")

    url = await cache.get(f"u:{code}")
    if url is None:
        row = await db.get(UrlMap, code)
        if row is None:
            raise HTTPException(status_code=404, detail="not found")
        url = row.url
        await cache.set(f"u:{code}", url, ex=3600)
    if isinstance(url, bytes):  # decode_responses=True 时不会发生，防御未来改配置
        url = url.decode("utf-8")

    # 埋点：Week 6 的统计入口，先落 Redis list，格式定了就不再动
    await cache.lpush("hits", f"{code}|{_now_ms()}")

    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)
