"""uvloop 接入，以及一个可复现的启动入口。

uvloop 是 libuv 的 Python 绑定，替换 asyncio 默认的纯 Python 事件循环。
它值不值，取决于你的时间花在哪：网络 IO 密集（大量小包往返）收益明显，
CPU 密集（JSON 序列化、模板渲染）几乎没有——那是 worker 数和代码的问题。

所以这里只提供开关，不替你下结论：`BENCH_LOOP=uvloop` 起服务，
用 scripts/bench_cache.py 同一套负载去打，数据自己说话。

实现方式选了 `uvloop.run()` 而非 `uvloop.install()` + 手写 run_until_complete：
后者在 3.12 上会因为信号处理器的注册时机报警告，前者是官方推荐的入口。
"""

import asyncio
import importlib.util
import os
from collections.abc import Coroutine
from typing import Any

import uvicorn


def uvloop_available() -> bool:
    return importlib.util.find_spec("uvloop") is not None


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """在 uvloop（若可用且被要求）上跑一个协程，否则用默认事件循环。"""
    if os.getenv("BENCH_LOOP", "asyncio") == "uvloop" and uvloop_available():
        import uvloop

        result: T = uvloop.run(coro)
        return result
    return asyncio.run(coro)


def main() -> None:
    """`python -m app.runtime`：按 BENCH_LOOP 选择事件循环策略启动 uvicorn。"""
    loop_choice = os.getenv("BENCH_LOOP", "asyncio")
    if loop_choice == "uvloop" and not uvloop_available():
        raise SystemExit("BENCH_LOOP=uvloop 但未安装 uvloop：uv add uvloop")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        loop=loop_choice if loop_choice in {"uvloop", "asyncio"} else "auto",
        log_level=os.getenv("LOG_LEVEL", "warning"),
    )


if __name__ == "__main__":
    main()
