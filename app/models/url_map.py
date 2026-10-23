"""url_map：短码 → 长链映射主表（设计文档 D2）。"""

from datetime import datetime

from sqlalchemy import CHAR, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.note import Base


class UrlMap(Base):
    __tablename__ = "url_map"

    code: Mapped[str] = mapped_column(CHAR(7), primary_key=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
