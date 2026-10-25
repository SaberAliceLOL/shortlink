"""SQLAlchemy 声明式基类。

单独成文件而不是挂在某个业务模型上：alembic 的 target_metadata 需要它，
但它不属于任何一个业务实体。Week 0 时 Base 与练习用的 Note 放在一起，
Note 下线后这个依赖关系就变得别扭了。
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
