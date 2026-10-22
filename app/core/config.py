"""Pydantic Settings：从 .env 读取配置。

依赖注入的意义在 core/deps.py；这里只负责把环境变量变成带类型的对象。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_env: str = "dev"

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "shortlink"
    mysql_password: str = "change-me"
    mysql_database: str = "shortlink"

    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_password: str = ""

    @property
    def mysql_dsn(self) -> str:
        return (
            f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
        )

    @property
    def redis_url(self) -> str:
        pwd = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{pwd}{self.redis_host}:{self.redis_port}/0"


@lru_cache
def get_settings() -> Settings:
    """lru_cache 让全局只有一份 Settings 实例（也是可讲的优化点）。"""
    return Settings()
