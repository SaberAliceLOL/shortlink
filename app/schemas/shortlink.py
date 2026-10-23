"""短链相关 Pydantic 模型（设计文档 D3）。"""

from pydantic import BaseModel, ConfigDict, Field


class ShortenRequest(BaseModel):
    url: str = Field(
        min_length=8,  # "https://a" 是最短合法形态
        max_length=2048,
        examples=["https://example.com/some/very/long/path?query=1"],
    )


class ShortenResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    short_url: str
    long_url: str
