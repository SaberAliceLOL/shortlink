"""穿透前置过滤：在碰缓存和数据库之前，先拒掉"必然不存在"的短码。

两种实现都写出来，但默认用**水位**。这不是"挑一个用"，而是这个项目里
一个真实的结论：

  短码不是随机字符串，它是 Base62(自增 ID)。因此"某个短码是否存在"
  有一个极便宜的充分判断——ID 是否超过已分配的最大 ID。

布隆过滤器适合"键空间任意、无法排序"的场景。短链的键空间是严格单调的，
用它等于放弃了手里最重要的那个信息。文档 docs/cache.md 里有完整对比。

两者的共同点：只能单侧判断。
  - "一定不存在" → 直接 404，不碰任何存储
  - "可能存在"   → 照常走缓存 + DB（可能有假阳性，代价只是多一次查询）
对于穿透防护，单侧判断就够用了：要挡的是"确定不存在的键"，而不是"证明存在"。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from collections.abc import Callable
from typing import Protocol

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.services.base62 import decode

logger = logging.getLogger(__name__)

BLOOM_KEY = "bf:shortlink"


class Prefilter(Protocol):
    """前置过滤器的共同接口。"""

    rejected: int

    async def is_definitely_absent(self, code: str) -> bool: ...

    async def add(self, code: str) -> None: ...

    async def refresh(self) -> None: ...


class NullPrefilter:
    """关闭前置过滤时的空实现（压测基线用）。"""

    rejected = 0

    async def is_definitely_absent(self, code: str) -> bool:
        return False

    async def add(self, code: str) -> None:
        return None

    async def refresh(self) -> None:
        return None


class WatermarkPrefilter:
    """水位守卫：ID > 已分配最大 ID 的短码，一定还没被生成过。

    正确性方向是单向的，这点必须搞清楚：
      - ID <= 水位  → **不保证存在**（号段已分配但号还没发完），只是不能否定
      - ID >  水位  → **保证不存在**（发号器严格递增，从不回头）

    所以它只做否定判断。这也是它可以零内存、零假阴性的原因。

    ## 安全边界

    水位是本地缓存的，可能落后于真实值。落后的窗口里，刚生成的短码会
    被判成"不存在"——这是唯一危险的方向（真实链接 404）。

    做法是留出余量：判定阈值 = 已分配 max_id + margin × step。
      - 一次号段分配推进 step 个号
      - 所以 margin × step 就是"刷新窗口内最多可能被分配出去的号数"
      - margin=10、step=1000 → 每秒 1 万个新链接以内都是安全的

    同进程内还有一道保险：发号后立刻 `note_issued_id` 抬高本地水位，
    自己刚发的号绝不会被自己拒掉。边界只用来跨进程兜底。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        biz_tag: str = "shortlink",
        refresh_seconds: float = 1.0,
        margin_steps: int = 10,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = session_factory
        self._biz_tag = biz_tag
        self._refresh_seconds = refresh_seconds
        self._margin_steps = margin_steps
        self._clock = clock
        self._max_id = -1
        self._step = 1000
        self._refreshed_at: float | None = None
        # 刷新失败后必须整体放行，不能沿用旧水位继续判。
        # 旧水位只落后一个刷新周期时 margin 能兜住；但刷新连续失败意味着
        # 我们不知道这期间又发出去多少个号，margin 不再是有界误差，
        # 继续用它判”不存在”就会把真实链接打成 404——这是唯一不可接受的方向。
        self._healthy = True
        self._lock = asyncio.Lock()
        self.rejected = 0

    @property
    def threshold(self) -> int:
        """超过这个 ID 就一定不存在。-1 表示还没取到水位（此时一律放行）。"""
        if self._max_id < 0 or not self._healthy:
            return -1
        return self._max_id + self._margin_steps * self._step

    async def is_definitely_absent(self, code: str) -> bool:
        await self._ensure_fresh()  # 失败不抛：过滤器出问题必须放行，不能误杀
        threshold = self.threshold
        if threshold < 0:
            return False
        try:
            numeric = decode(code)
        except ValueError:
            return False  # 格式由路由层先校验，这里兜底放行
        if numeric > threshold:
            self.rejected += 1
            return True
        return False

    async def add(self, code: str) -> None:
        """写入路径通知：抬升本地水位。无 DB 交互。"""
        try:
            self.note_issued_id(decode(code))
        except ValueError:
            return

    def note_issued_id(self, numeric_id: int) -> None:
        if numeric_id > self._max_id:
            self._max_id = numeric_id

    async def refresh(self) -> None:
        """从 DB 重新读水位。"""
        async with self._factory() as session:
            row = (
                await session.execute(
                    text("SELECT max_id, step FROM segment_alloc WHERE biz_tag = :tag"),
                    {"tag": self._biz_tag},
                )
            ).first()
        if row is not None:
            max_id, step = row
            self._max_id = max(self._max_id, int(max_id))
            self._step = int(step) or 1000
        self._healthy = True
        self._refreshed_at = self._clock()

    async def _ensure_fresh(self) -> None:
        if not self._is_stale():
            return
        # 单飞：刷新只让一个请求做，其余请求用现有水位继续判断（宁可放行也不排队）
        if self._lock.locked():
            return
        async with self._lock:
            if not self._is_stale():
                return
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 — 过滤器故障必须放行
                logger.warning("水位刷新失败，后续判定一律放行", exc_info=True)
                self._healthy = False
                self._refreshed_at = self._clock()  # 退避，避免每个请求都去撞同一个错误

    def _is_stale(self) -> bool:
        if self._refreshed_at is None:
            return True
        return self._clock() - self._refreshed_at >= self._refresh_seconds


class BloomFilter:
    """Redis 位图实现的布隆过滤器（对比方案，不是默认）。

    参数按容量和误判率算：m = -n·ln p / (ln2)²，k = (m/n)·ln2。
    n=100 万、p=1% → m ≈ 9.6 Mbit ≈ 1.2 MiB，k=7。

    多进程共享（位图在 Redis 里），所以各 worker 写入的短码互相可见。

    ## 为什么它不是这里的最优解

    1. **需要预热**：上线时库里已有的短码必须全量灌进去，否则老链接会被误判
       成不存在（假阴性），这是正确性事故。水位没有这个问题。
    2. **内存随数据量线性增长**：1 亿条 ≈ 120 MiB，而且误判率是概率性的。
       水位是 O(1) 的两个整数。
    3. **重启/清空 Redis 就要重建**：水位从 DB 读一行就能恢复。
    4. 它的核心优势——"任意键空间"——在这里用不上，因为键是单调 ID。
    """

    def __init__(
        self,
        redis: Redis,
        *,
        capacity: int = 1_000_000,
        error_rate: float = 0.01,
        key: str = BLOOM_KEY,
    ) -> None:
        n = max(1, capacity)
        p = min(max(error_rate, 1e-6), 0.5)
        self._bits = max(1, math.ceil(-n * math.log(p) / (math.log(2) ** 2)))
        self._hashes = max(1, round((self._bits / n) * math.log(2)))
        self._redis = redis
        self._key = key

    @property
    def bit_count(self) -> int:
        return self._bits

    @property
    def hash_count(self) -> int:
        return self._hashes

    rejected = 0  # 只用于观测：被布隆挡下的请求数

    def _offsets(self, value: str) -> list[int]:
        # 双哈希构造 k 个位置：h_i = (h1 + i·h2) mod m。
        # 一次哈希算两个摘要，比调 k 次独立哈希快，误判率与独立哈希等价。
        raw = value.encode("utf-8")
        h1 = int.from_bytes(hashlib.blake2b(raw, digest_size=8, person=b"bf1").digest(), "big")
        h2 = int.from_bytes(hashlib.blake2b(raw, digest_size=8, person=b"bf2").digest(), "big")
        h2 |= 1  # 保证步长是奇数，避免和 m 有公因子导致位置退化
        return [(h1 + i * h2) % self._bits for i in range(self._hashes)]

    async def add(self, code: str) -> None:
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for offset in self._offsets(code):
                    pipe.setbit(self._key, offset, 1)
                await pipe.execute()
        except Exception:  # noqa: BLE001 — 过滤器是优化项，任何故障都不能影响请求
            logger.warning("布隆写入失败 code=%s", code, exc_info=True)

    async def is_definitely_absent(self, code: str) -> bool:
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for offset in self._offsets(code):
                    pipe.getbit(self._key, offset)
                bits = await pipe.execute()
        except Exception:  # noqa: BLE001 — 同上：故障时放行，宁可多查一次库
            logger.warning("布隆查询失败，放行 code=%s", code, exc_info=True)
            return False
        if not all(bits):
            self.rejected += 1
            return True
        return False  # 全部为 1：可能存在（布隆有假阳性）

    async def refresh(self) -> None:
        """布隆没有"水位"可刷新：位图在 Redis 里，跨进程本来共享。

        保留空实现是为了让两种实现满足同一个 Prefilter 协议，
        调用方（service / 压测脚本）不必按类型分支。
        """


def build_prefilter(
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    biz_tag: str = "shortlink",
) -> Prefilter:
    """按配置装配前置过滤器。"""
    if settings.prefilter_strategy == "watermark":
        return WatermarkPrefilter(
            session_factory,
            biz_tag=biz_tag,
            refresh_seconds=settings.prefilter_refresh_seconds,
            margin_steps=settings.prefilter_watermark_margin_steps,
        )
    if settings.prefilter_strategy == "bloom":
        return BloomFilter(
            redis,
            capacity=settings.bloom_capacity,
            error_rate=settings.bloom_error_rate,
        )
    return NullPrefilter()
