"""短链读写的编排层。

放这一层而不是塞进路由，是因为"跳转"现在有四个可能提前返回的分支
（格式非法 → 前置过滤 → 负缓存 → 回源），散在路由里会变成嵌套 if。
路由只负责 HTTP 形状（状态码、Location 头），决策留在这里。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.url_map import UrlMap
from app.services.cache import ShortlinkCache
from app.services.idgen import SegmentIDGen
from app.services.prefilter import Prefilter, build_prefilter

logger = logging.getLogger(__name__)


class ShortlinkService:
    def __init__(
        self,
        cache: ShortlinkCache,
        prefilter: Prefilter,
        *,
        record_hits: bool = True,
    ) -> None:
        # 公开而非私有：压测脚本与运维视角都需要看/操作这两层
        # （重置熔断、刷新水位、读命中率）。把它们藏成 _cache 只会逼调用方
        # 去碰私有属性——那才是真正的封装破损。
        self.cache = cache
        self.prefilter = prefilter
        self._record_hits = record_hits

    @classmethod
    def build(
        cls,
        redis: Redis,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        biz_tag: str = "shortlink",
        wall_clock: Callable[[], float] | None = None,
    ) -> ShortlinkService:
        """按一份 Settings 装配整条链路。

        测试和压测都需要"同一套代码、只换配置"来构造独立实例
        （各自的 Redis、各自的熔断状态），所以装配逻辑收在这里，
        而不是让调用方自己拼 cache + prefilter。
        """
        cache_kwargs = {} if wall_clock is None else {"wall_clock": wall_clock}
        cache = ShortlinkCache(redis, settings, **cache_kwargs)
        prefilter = build_prefilter(redis, session_factory, settings, biz_tag=biz_tag)
        return cls(cache, prefilter, record_hits=settings.hits_record_enabled)

    async def create(self, url: str, db: AsyncSession, idgen: SegmentIDGen) -> str:
        """生成短码并落库，返回 code。异常向上抛给路由处理。"""
        code = await idgen.next_code()
        db.add(UrlMap(code=code, url=url))
        await db.commit()

        # 先通知过滤器再写缓存：顺序反了的话，中间这段时间的请求会
        # 先被过滤器拒掉，白写的缓存值也没人读
        await self.prefilter.add(code)
        # 写入后立刻预热缓存。除了省一次回源，更重要的是盖掉可能存在的
        # 负缓存——短码可预测，攻击者可以提前把负缓存"预埋"到未生成的号上
        await self.cache.store(code, url)
        return code

    async def resolve(self, code: str, db: AsyncSession) -> str | None:
        """返回长链；确实不存在时返回 None。

        三级过滤，越靠前越便宜：
          1. 水位/布隆：纯内存或一次位图读，命中即 404，不碰 DB
          2. 缓存：负缓存直接 404，正缓存直接返回
          3. 回源：只有这里才碰 MySQL
        """
        if await self.prefilter.is_definitely_absent(code):
            return None

        async def load() -> str | None:
            row = await db.get(UrlMap, code)
            return None if row is None else row.url

        return await self.cache.resolve(code, load)

    async def record_hit(self, code: str) -> None:
        if not self._record_hits:
            return
        await self.cache.record_hit(code)

    def cache_stats(self) -> dict[str, object]:
        """进程内计数快照。压测脚本用它核对"这批请求里有多少真回源了"。"""
        stats = self.cache.stats
        return {
            "strategy": self.cache.strategy,
            "lookups": stats.lookups,
            "hits": stats.hits,
            "stale_hits": stats.stale_hits,
            "negative_hits": stats.negative_hits,
            "misses": stats.misses,
            "db_loads": stats.db_loads,
            "hit_rate": stats.hit_rate,
            "lock_waits_exhausted": stats.lock_waits_exhausted,
            "breaker_open": self.cache.breaker.is_open,
            "breaker_trips": self.cache.breaker.trips,
            "prefilter_rejections": self.prefilter.rejected,
        }
