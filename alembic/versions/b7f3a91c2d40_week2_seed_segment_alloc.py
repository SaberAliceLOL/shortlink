"""week2: 初始化发号器号段行（部署阻塞项）

Revision ID: b7f3a91c2d40
Revises: 4cc1d513b3af
Create Date: 2026-10-25 11:50:00.000000

为什么必须写进迁移：
  号段表为空时，发号器 UPDATE 影响 0 行、SELECT .one() 抛 NoResultFound，
  POST /api/shorten 直接 500。本地开发靠手工 INSERT 掩盖了这个问题，
  部署到新库必然炸——所以初始化必须和建表一样纳入迁移，由 upgrade head 一并完成。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7f3a91c2d40"
down_revision: str | Sequence[str] | None = "4cc1d513b3af"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 62^4 - 1：下一个 ID 即 62^4，Base62 后恰好 5 位（设计文档 D1）
INIT_MAX_ID = 62**4 - 1
STEP = 1000


def upgrade() -> None:
    """插入发号器初始号段（幂等）。

    用 INSERT IGNORE 而不是裸 INSERT：Week 1 的开发库里如果已经有手工写入的
    号段行，裸 INSERT 会撞主键让升级中断。迁移必须在"部分数据已存在"的库上
    也能跑——这正是它存在的意义（新库从零建、老库补齐）。
    """
    op.execute(
        sa.text(
            "INSERT IGNORE INTO segment_alloc (biz_tag, max_id, step) "
            "VALUES ('shortlink', :max_id, :step)"
        ).bindparams(max_id=INIT_MAX_ID, step=STEP)
    )


def downgrade() -> None:
    """删除发号器号段行。"""
    op.execute(sa.text("DELETE FROM segment_alloc WHERE biz_tag = 'shortlink'"))
