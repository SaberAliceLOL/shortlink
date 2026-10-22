"""Pydantic 模型：请求/响应的边界校验。

FastAPI 会把请求体交给 NoteCreate 校验，非法输入根本进不了路由函数，
直接返回 422 —— 这就是"校验前移"。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class NoteCreate(BaseModel):
    title: str = Field(min_length=1, max_length=100, examples=["第一篇笔记"])
    content: str = Field(default="", max_length=10000, examples=["hello shortlink"])


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)  # 允许从 ORM 对象直接构造

    id: int
    title: str
    content: str
    created_at: datetime
