"""短链核心路由：生成 + 跳转（设计文档 D3/D4）。

路由只负责 HTTP 形状：校验输入、翻成状态码与 Location 头。
"要不要回源、回源是否被缓存挡住"这些决策在 services/shortlink_service.py，
因为跳转路径现在有好几个可能提前返回的分支（前置过滤 → 负缓存 → 回源），
塞进路由会长成嵌套 if。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import HttpUrl, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_db, get_idgen, get_shortlink_service
from app.schemas.shortlink import ShortenRequest, ShortenResponse
from app.services.base62 import is_valid_code
from app.services.idgen import SegmentIDGen
from app.services.shortlink_service import ShortlinkService

router = APIRouter(tags=["shortlink"])
logger = logging.getLogger(__name__)


def _validate_url(raw: str) -> str:
    """http/https 之外的协议（ftp/javascript/file…）一律拒绝。"""
    try:
        url = HttpUrl(raw)
    except ValidationError as e:
        raise HTTPException(
            status_code=422,
            detail=f"非法 URL: {e.errors()[0]['msg']}",
        ) from e
    if url.scheme not in ("http", "https"):
        raise HTTPException(status_code=422, detail="只接受 http/https 协议")
    return str(url)


# 发号器通过依赖注入获取：单例由 deps 持有，测试可 overrides 成绑定测试库的实例
@router.post("/api/shorten", response_model=ShortenResponse, status_code=status.HTTP_201_CREATED)
async def shorten(
    payload: ShortenRequest,
    db: AsyncSession = Depends(get_db),
    idgen: SegmentIDGen = Depends(get_idgen),
    service: ShortlinkService = Depends(get_shortlink_service),
) -> ShortenResponse:
    url = _validate_url(payload.url)
    try:
        code = await service.create(url, db=db, idgen=idgen)
    except Exception as e:  # noqa: BLE001
        # 主键冲突等写失败：发号器丢号无妨（非连续不破坏唯一性），但必须打日志。
        # 曾经这里是静默 except，并发下大量 IntegrityError 只表现为 500、日志无痕，
        # 排查时只能靠猜——异常被吞掉是运维事故的常见起点。
        await db.rollback()
        logger.exception("写入短链失败 url=%s", url[:200])
        raise HTTPException(status_code=500, detail="写入失败") from e

    settings = get_settings()
    short_url = f"http://{settings.public_host}/{code}"
    return ShortenResponse(code=code, short_url=short_url, long_url=url)


@router.get("/{code}", response_class=RedirectResponse, status_code=status.HTTP_302_FOUND)
async def redirect(
    code: str,
    db: AsyncSession = Depends(get_db),
    service: ShortlinkService = Depends(get_shortlink_service),
) -> RedirectResponse:
    # 廉价防御：格式非法的 code 连存储都不碰（设计文档 D4）。
    # 用 404 而非 422：对外部访问者来说"这个短链不存在"比"你参数格式错"更准确。
    #
    # 用 is_valid_code 而不是 code.isalnum()：后者对 Unicode 数字/字母也返回 True
    # （"１２３"、"café"），这些字符进不了 Base62，白跑一趟存储。
    if not is_valid_code(code):
        raise HTTPException(status_code=404, detail="not found")

    url = await service.resolve(code, db=db)
    if url is None:
        raise HTTPException(status_code=404, detail="not found")

    # 埋点：Week 6 的统计入口，先落 Redis list，格式定了就不再动
    await service.record_hit(code)

    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)
