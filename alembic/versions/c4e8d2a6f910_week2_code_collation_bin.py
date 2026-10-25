"""week2: url_map.code 改为大小写敏感排序规则（utf8mb4_bin）

Revision ID: c4e8d2a6f910
Revises: b7f3a91c2d40
Create Date: 2026-10-25 12:15:00.000000

为什么必须改：
  Base62 的字符表是 0-9A-Za-z，大小写不同就是不同的短码。
  但建表时沿用了库默认的 utf8mb4_unicode_ci（大小写不敏感），后果有两个：

    1. 写入：并发下先发出 1012w、后发出 1012W，第二条 INSERT 报
       Duplicate entry '1012W' for key 'PRIMARY' → 接口 500（发号器丢失一个号）
    2. 读取：WHERE code = '1012w' 会命中 code='1012W' 的记录
       → 访问者输错大小写，被重定向到别人的长链（这是正确性问题，不只是性能）

  用 utf8mb4_bin 后，比较按字节做，大小写敏感，两个问题一起消失。

注意：改排序规则会重建该列索引（表小，瞬间完成）。这里不做数据订正——
  url_map 里已有的短码是大小写混排的，但按 binary 比较不会产生歧义。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c4e8d2a6f910"
down_revision: str | Sequence[str] | None = "b7f3a91c2d40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """短码主键改二进制比较。"""
    op.execute(
        "ALTER TABLE url_map MODIFY code CHAR(7) "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL"
    )


def downgrade() -> None:
    """回退到库默认排序规则。"""
    op.execute(
        "ALTER TABLE url_map MODIFY code CHAR(7) "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL"
    )
