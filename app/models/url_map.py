"""url_map：短码 → 长链映射主表（设计文档 D2）。"""

from datetime import datetime

from sqlalchemy import CHAR, DateTime, String, func
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UrlMap(Base):
    __tablename__ = "url_map"

    # Base62 大小写敏感，排序规则必须是 utf8mb4_bin：
    # 用库默认的 utf8mb4_unicode_ci 会让 '1012w' 与 '1012W' 撞主键，
    # 且跳转查询会命中错误大小写的记录（迁移 c4e8d2a6f910 记录了事故经过）。
    #
    # 用 with_variant 而不是直接写 collation="utf8mb4_bin"：
    # 后者会把 MySQL 专有排序规则带进单测用的 SQLite，CREATE TABLE 直接报
    # "no such collation sequence"。按方言分发，测试替身不受影响。
    code: Mapped[str] = mapped_column(
        CHAR(7).with_variant(mysql.CHAR(7, collation="utf8mb4_bin"), "mysql"),
        primary_key=True,
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
