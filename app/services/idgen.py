"""号段式发号器（设计文档 D1，美团 Leaf 的简化版）。

生命周期：
  启动/耗尽 → UPDATE segment_alloc SET max_id = max_id + step WHERE biz_tag = ?
           → 读取新 max_id，本地持有 [max_id - step + 1, max_id]
  发号     → 内存自增，零 DB 交互；耗尽时同步续段（持锁，避免双取号段）

并发安全性：
  - 单进程内：asyncio.Lock 保证"检查耗尽→取号段"原子性；
    发号本身是单线程事件循环里的整数自增，无竞态
  - DB 侧：UPDATE 是原子加，两个实例（不同 biz_tag 或步长错开）不会拿到重叠号段
"""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.base62 import encode


class SegmentExhausted(Exception):
    """号段取尽且续段失败（DB 不可用等）。"""


class SegmentIDGen:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], biz_tag: str) -> None:
        self._factory = session_factory
        self._biz_tag = biz_tag
        self._current = 0  # 下一个待发号
        self._max_allowed = -1  # 哨兵：-1 表示尚未取过号段，首次发号必然触发 _load_segment
        self._lock = asyncio.Lock()

    async def _load_segment(self) -> None:
        """从 DB 取下一段。UPDATE ... 原子加后读回，天然避免号段重叠。"""
        async with self._factory() as session:
            # UPDATE 原子性：并发实例各自拿到不重叠的号段
            await session.execute(
                text("UPDATE segment_alloc SET max_id = max_id + step " "WHERE biz_tag = :tag"),
                {"tag": self._biz_tag},
            )
            row = (
                await session.execute(
                    text("SELECT max_id, step FROM segment_alloc WHERE biz_tag = :tag"),
                    {"tag": self._biz_tag},
                )
            ).one()
            await session.commit()
        self._max_allowed, step = row
        self._current = self._max_allowed - step + 1

    async def next_id(self) -> int:
        """取下一个 ID。号段耗尽时在锁内续段（同进程串行，只续一次）。"""
        if self._current > self._max_allowed:  # > 而非 >=：_max_allowed 本身要发出去
            async with self._lock:
                if self._current > self._max_allowed:
                    await self._load_segment()
        value = self._current
        self._current += 1
        return value

    async def next_code(self) -> str:
        return encode(await self.next_id())
