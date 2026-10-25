"""week2: 下线 Week 0 练习用的 notes 表

Revision ID: d91a5c3e7b28
Revises: c4e8d2a6f910
Create Date: 2026-10-25 12:55:00.000000

notes 是 Week 0 Day 5 用来练 FastAPI + Redis 的最小 CRUD demo，
主战场切到 url_map 后它的路由早就没注册了，只剩一张空表和三个源文件。
删掉源文件后模型的 metadata 里没有它，alembic check 会一直报"模型与库不一致"。
迁移里显式 drop，保证 upgrade head 的库与模型对齐——否则每次 autogenerate
都会把这张表重新生成为一次 drop_table，干扰后续真实变更的评审。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d91a5c3e7b28"
down_revision: str | Sequence[str] | None = "c4e8d2a6f910"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """删除练习表。"""
    op.drop_table("notes")


def downgrade() -> None:
    """恢复练习表结构（数据不恢复）。"""
    op.create_table(
        "notes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
