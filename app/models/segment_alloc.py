"""segment_alloc：号段发号器分配表（设计文档 D1）。

biz_tag 区分业务（预留多实例），max_id 是已分配出去的最大号。
实例启动时 UPDATE max_id = max_id + step，然后用 [旧max_id+1, 新max_id] 发号。
"""

from sqlalchemy import BIGINT, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.note import Base


class SegmentAlloc(Base):
    __tablename__ = "segment_alloc"

    biz_tag: Mapped[str] = mapped_column(String(32), primary_key=True)
    max_id: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0)
    step: Mapped[int] = mapped_column(BIGINT, nullable=False, default=1000)
