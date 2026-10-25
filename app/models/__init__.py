"""ORM 模型聚合：alembic 与应用统一从这里 import Base。"""

from app.models.base import Base
from app.models.segment_alloc import SegmentAlloc
from app.models.url_map import UrlMap

__all__ = ["Base", "SegmentAlloc", "UrlMap"]
