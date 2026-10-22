"""依赖注入：FastAPI 的 Depends 体系。

核心思想：路由函数不自己创建连接，而是"声明需要什么"（async session、redis、settings），
由框架在请求进来时注入、请求结束时回收。好处：
  1. 路由可测试——测试时用 dependency_overrides 换成假实现
  2. 生命周期统一管理——session 必然被 close，不会泄漏
"""

from collections.abc import AsyncGenerator

from redis.asyncio import Redis, from_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings

# engine 和 session 工厂是进程级单例（模块导入时创建一次），
# 真正按请求分配的是 session —— 这就是"连接池"的用法雏形
engine = create_async_engine(
    get_settings().mysql_dsn,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

redis_client: Redis = from_url(
    get_settings().redis_url,
    decode_responses=True,
    max_connections=50,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """每个请求一个 session；yield 让 finally 语义变成框架接管。"""
    async with AsyncSessionLocal() as session:
        yield session


async def get_redis() -> Redis:
    return redis_client


def get_app_settings() -> Settings:
    return get_settings()


# 路由里的标准写法：db: AsyncSession = Depends(get_db)
__all__ = [
    "engine",
    "AsyncSessionLocal",
    "redis_client",
    "get_db",
    "get_redis",
    "get_app_settings",
]
