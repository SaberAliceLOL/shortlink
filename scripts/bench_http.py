"""HTTP 层压测：打真实网络栈，对比 uvloop / asyncio 与两种重建策略。

与 scripts/bench_cache.py 的分工：
  bench_cache.py  在进程内直接调 service，测的是**逻辑**收益，噪音小
  本脚本           起真实 uvicorn 进程 + 真实 HTTP，测的是**端到端**（含 nginx 之外全栈）

为什么两个都要：
  进程内测试会把"序列化、事件循环、TCP 建立"这些成本全部抹掉。
  uvloop 的收益恰恰全在这部分，所以只有 HTTP 层才测得出它。
  反过来，HTTP 层噪音大，看不出"锁把 100 次回源压成 1 次"这种逻辑差异。

用法：
    uv run python scripts/bench_http.py
它会自己起服务、跑完、关掉。每个场景独立进程，避免状态互相污染。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass

from sqlalchemy import text

from app.core.config import get_settings

CONCURRENCY = 50
DURATION_SECONDS = 8
WARMUP_SECONDS = 1.5
HOT_CODE_COUNT = 50


@dataclass
class Result:
    name: str
    qps: float
    p50_ms: float
    p99_ms: float
    errors: int


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def seed_hot_codes() -> list[str]:
    """把热点短码写进真库。用同步驱动避免与压测进程抢事件循环。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    settings = get_settings()
    engine = create_async_engine(settings.mysql_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        await s.execute(text("DELETE FROM url_map"))
        await s.execute(
            text("UPDATE segment_alloc SET max_id = :m WHERE biz_tag = 'shortlink'"),
            {"m": 62**4 - 1},
        )
        await s.commit()

    from app.services.idgen import SegmentIDGen

    idgen = SegmentIDGen(factory, "shortlink")
    codes = [await idgen.next_code() for _ in range(HOT_CODE_COUNT)]
    async with factory() as s:
        for i, code in enumerate(codes):
            await s.execute(
                text("INSERT INTO url_map (code, url) VALUES (:c, :u)"),
                {"c": code, "u": f"https://example.com/hot/{i}"},
            )
        await s.commit()
    await engine.dispose()
    return codes


def start_server(port: int, env_extra: dict[str, str]) -> subprocess.Popen[bytes]:
    env = {**os.environ, "PORT": str(port), "LOG_LEVEL": "error", **env_extra}
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.runtime"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read().decode() if proc.stderr else ""
            raise RuntimeError(f"服务启动失败：\n{err}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return proc
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("服务启动超时")


async def flush_cache() -> None:
    from redis.asyncio import from_url

    redis = from_url(get_settings().redis_url, decode_responses=True)
    await redis.flushdb()
    await redis.aclose()


async def _request(host: str, port: int, path: str) -> tuple[int, float]:
    """发一个 HTTP/1.1 请求，返回 (状态码, 耗时ms)。

    用原生 asyncio 而不是 httpx：实测 httpx 在本机压测里自身开销过大
    （单请求 p50 230ms，同期 curl / 原生 socket 都是 3-8ms），
    瓶颈在客户端而不在服务端，会把所有场景的差异抹平。
    压测工具本身不能成为被测系统的瓶颈——这也是"控制变量"的一部分。
    """
    start = time.perf_counter()
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        head = await reader.readuntil(b"\r\n")
        status = int(head.split(b" ")[1])
        await reader.read()  # 读完响应体，确保服务端能正常关闭连接
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    return status, (time.perf_counter() - start) * 1000


async def run_load(base_url: str, codes: list[str]) -> Result:
    # base_url 形如 http://127.0.0.1:PORT，这里只要 host/port
    hostport = base_url.split("://", 1)[1]
    host, port_str = hostport.rsplit(":", 1)
    port = int(port_str)

    latencies: list[float] = []
    errors = 0
    stop_at = time.perf_counter() + DURATION_SECONDS
    lock = asyncio.Lock()
    paths = [f"/{code}" for code in codes] * 20  # 预展开，减少循环里的字符串拼接

    async def worker(offset: int) -> None:
        nonlocal errors
        i = offset
        while time.perf_counter() < stop_at:
            path = paths[i % len(paths)]
            i += CONCURRENCY
            try:
                status, elapsed = await _request(host, port, path)
                ok = status == 302
            except Exception:
                ok = False
                elapsed = 0.0
            async with lock:
                if ok:
                    latencies.append(elapsed)
                else:
                    errors += 1

    await asyncio.gather(*(worker(i) for i in range(CONCURRENCY)))

    latencies.sort()
    n = len(latencies)
    return Result(
        name="",
        qps=round(n / DURATION_SECONDS, 1),
        p50_ms=round(statistics.median(latencies), 2) if n else 0.0,
        p99_ms=round(latencies[int(n * 0.99)], 2) if n else 0.0,
        errors=errors,
    )


async def bench_one(name: str, env_extra: dict[str, str], codes: list[str]) -> Result:
    await flush_cache()  # 每个场景从冷缓存起步，否则会比的是上一轮留下的热数据
    port = free_port()
    proc = start_server(port, env_extra)
    try:
        time.sleep(WARMUP_SECONDS)
        result = await run_load(f"http://127.0.0.1:{port}", codes)
        result.name = name
        return result
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def print_table(results: list[Result]) -> None:
    print()
    print(f"{'场景':<40}{'QPS':>10}{'P50(ms)':>10}{'P99(ms)':>10}{'错误':>8}")
    print("-" * 80)
    for r in results:
        print(f"{r.name:<40}{r.qps:>10}{r.p50_ms:>10}{r.p99_ms:>10}{r.errors:>8}")
    print()


async def main() -> int:
    settings = get_settings()
    try:
        from sqlalchemy.ext.asyncio import create_async_engine as _c

        engine = _c(settings.mysql_dsn)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
    except Exception as e:
        print(f"连不上 MySQL：{e}", file=sys.stderr)
        return 1

    codes = await seed_hot_codes()
    print(f"热点短码 {len(codes)} 个，并发 {CONCURRENCY}，每场景 {DURATION_SECONDS}s")

    scenarios = [
        ("asyncio + 逻辑过期", {"BENCH_LOOP": "asyncio", "CACHE_STRATEGY": "logical_expire"}),
        ("asyncio + 互斥锁", {"BENCH_LOOP": "asyncio", "CACHE_STRATEGY": "mutex"}),
        ("uvloop + 逻辑过期", {"BENCH_LOOP": "uvloop", "CACHE_STRATEGY": "logical_expire"}),
        ("uvloop + 互斥锁", {"BENCH_LOOP": "uvloop", "CACHE_STRATEGY": "mutex"}),
        (
            "asyncio + 无缓存",
            {"BENCH_LOOP": "asyncio", "CACHE_ENABLED": "false", "CACHE_STRATEGY": "none"},
        ),
    ]

    results = []
    for name, env in scenarios:
        # 关掉埋点：它是 1:1 的 Redis 写，会把所有场景的下限一起拉平
        env = {**env, "HITS_RECORD_ENABLED": "false"}
        results.append(await bench_one(name, env, codes))

    print_table(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
