"""依赖注入：FastAPI 的 Depends 体系。

核心思想：路由函数不自己创建连接，而是"声明需要什么"（async session、redis、settings），
由框架在请求进来时注入、请求结束时回收。好处：
  1. 路由可测试——测试时用 dependency_overrides 换成假实现
  2. 生命周期统一管理——session 必然被 close，不会泄漏

Week 3 的缓存层同样走 DI。它有三个可替换的部分（Redis、重建策略、前置过滤器），
如果直接 new 在路由里，测试和压测就只能靠改环境变量 + 重启进程来切换。
"""

from collections.abc import AsyncGenerator

from redis.asyncio import Redis, from_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.services.idgen import SegmentIDGen
from app.services.shortlink_service import ShortlinkService

_settings = get_settings()

# engine 和 session 工厂是进程级单例（模块导入时创建一次），
# 真正按请求分配的是 session —— 这就是"连接池"的用法雏形
engine = create_async_engine(
    _settings.mysql_dsn,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    pool_timeout=_settings.db_pool_timeout_seconds,
    pool_pre_ping=True,
    # pool_recycle 必须小于 MySQL 的 wait_timeout（默认 28800s），
    # 否则连接会被服务端单方面掐断，池里留下死连接。
    # 1800s 是保守值；配 pool_pre_ping 双保险。
    pool_recycle=1800,
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# socket_timeout 是熔断能生效的前提：没有超时，Redis 挂起时请求会永远等下去，
# "降级"就无从谈起。0.5s 对缓存来说已非常宽裕（正常 P99 是个位数毫秒）。
redis_client: Redis = from_url(
    _settings.redis_url,
    decode_responses=True,
    max_connections=_settings.redis_max_connections,
    socket_timeout=_settings.redis_socket_timeout_seconds,
    socket_connect_timeout=_settings.redis_socket_timeout_seconds,
    health_check_interval=30,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """每个请求一个 session；yield 让 finally 语义变成框架接管。"""
    async with AsyncSessionLocal() as session:
        yield session


async def get_redis() -> Redis:
    return redis_client


def get_app_settings() -> Settings:
    return _settings


# 发号器单例：号段缓存必须跨请求共享，所以是模块级对象；
# 但对外暴露的是函数，测试可以用 overrides 换成绑定测试库的实例
_idgen = SegmentIDGen(AsyncSessionLocal, biz_tag="shortlink")


def get_idgen() -> SegmentIDGen:
    return _idgen


# 缓存与前置过滤器都是进程级单例：缓存要共享熔断状态与进程内计数，
# 水位守卫要共享本地水位（否则每个请求都要去 DB refresh 一次）
_service = ShortlinkService.build(redis_client, AsyncSessionLocal, _settings)


def get_shortlink_service() -> ShortlinkService:
    return _service


__all__ = [
    "engine",
    "AsyncSessionLocal",
    "redis_client",
    "get_db",
    "get_redis",
    "get_app_settings",
    "get_idgen",
    "get_shortlink_service",
]
