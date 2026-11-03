"""Pydantic Settings：从 .env 读取配置。

依赖注入的意义在 core/deps.py；这里只负责把环境变量变成带类型的对象。

Week 3 新增的每一项都能从环境变量覆盖，因为压测要做"控制变量"：
同一份代码只改一个环境变量，就能对比互斥锁/逻辑过期、开/关前置过滤、
uvloop/asyncio。硬编码的常量没法做这件事。
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

CacheStrategy = Literal["none", "mutex", "logical_expire"]
PrefilterStrategy = Literal["none", "watermark", "bloom"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_env: str = "dev"
    public_host: str = "localhost:8000"

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "shortlink"
    mysql_password: str = "change-me"
    mysql_database: str = "shortlink"

    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_password: str = ""

    # ---- 连接池 ----------------------------------------------------------
    # 池大小必须配着 MySQL max_connections 一起看：8 个 worker × (10+20) 就能顶满
    # 一个默认 151 连接的 MySQL。Week 5 会用压测找到这个拐点。
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_seconds: float = 5.0
    redis_max_connections: int = 50
    redis_socket_timeout_seconds: float = 0.5

    # ---- 读缓存 ----------------------------------------------------------
    cache_enabled: bool = True
    # none  = 纯 Cache Aside，不做任何击穿保护（基线）
    # mutex = SETNX 互斥锁，只有一个请求回源，其余短暂等待后复用结果
    # logical_expire = 逻辑过期，过期后先返回旧值、后台单飞刷新
    #
    # 默认选 logical_expire：短链是不可变数据（无编辑/删除接口），
    # 旧值不会"错"，只会"稍旧"。用陈旧度换掉所有请求的排队等待。
    cache_strategy: CacheStrategy = "logical_expire"
    cache_ttl_seconds: int = 300
    # 雪崩防护：真实 TTL = ttl × (1 ± ratio)，把同一批写入的键打散
    cache_ttl_jitter_ratio: float = 0.2
    # 负缓存（穿透的第一道防线）：不存在的短码也缓存，但 TTL 要短。
    # 单独开关是给压测留的：测前置过滤的效果时必须能把它关掉，
    # 否则两个防线叠在一起，说不清是谁挡住的。
    cache_negative_enabled: bool = True
    cache_negative_ttl_seconds: int = 60
    cache_negative_jitter_ratio: float = 0.5
    # 逻辑过期模式下，Redis 键比逻辑过期时间多活这么久，保证"旧值"还在
    cache_stale_grace_seconds: int = 600
    cache_lock_ttl_ms: int = 2000
    cache_lock_wait_ms: int = 50
    cache_lock_wait_attempts: int = 3
    # Redis 故障后的熔断冷却期：期间不再尝试 Redis，直接回源，
    # 避免每个请求都等满一个 socket timeout
    cache_breaker_cooldown_seconds: float = 5.0

    # ---- 穿透前置过滤 ----------------------------------------------------
    prefilter_strategy: PrefilterStrategy = "watermark"
    # 水位（max_id）的本地刷新间隔。越短越准，越长越省的 DB 往返
    prefilter_refresh_seconds: float = 1.0
    # 水位安全边界 = margin_steps × step。见 docs/cache.md 的失效分析
    prefilter_watermark_margin_steps: int = 10
    bloom_capacity: int = 1_000_000
    bloom_error_rate: float = 0.01

    # ---- 统计埋点 --------------------------------------------------------
    # 跳转时 LPUSH 一条访问记录。Week 6 会改成队列投递 + 批量聚合，
    # 现在先用开关把它从压测里摘出去，避免 1:1 的 Redis 写干扰对比。
    hits_record_enabled: bool = True

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
