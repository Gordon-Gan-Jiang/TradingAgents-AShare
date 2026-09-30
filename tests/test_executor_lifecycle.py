"""执行器自愈：**"进过 lifespan 再离开"不能让后续请求挂掉**。

## 这个 bug 是怎么被发现的

补 `test_data_source_probe_scheduler.py`（里面有一条真的进 lifespan 的测试）之后，
**12 个完全无关的测试**开始报 500。单独跑它们却全绿——典型的"测试顺序污染"。

根因不是新测试写错了，而是一个**早就存在的应用缺陷**：

1. `lifespan` 退出时执行 `_light_executor.shutdown(...)`；
2. 而 `_light_executor` 是**模块级**的，生命周期长于 app；
3. `ThreadPoolExecutor.shutdown()` 之后**不能再提交任务**：
   `RuntimeError: cannot schedule new futures after shutdown`；
4. `/v1/auth/request-code` 等端点用 `run_in_executor(_light_executor, ...)` → HTTP 500。

原代码只在 lifespan **启动**时重建执行器，所以只有"再次进入 lifespan"能恢复；
而 `TestClient(app)` **不进 lifespan** 的用法（本仓库测试大量这么写）永远恢复不了。

证据：用**既有**的 `tests/test_startup_readiness.py`（也进 lifespan）去做同样的组合，
同样炸掉那 10 个测试——所以它是存量缺陷，不是新测试引入的。
它此前没被发现，只是因为按字母序 `test_startup_readiness` 排在 ``test_signals_chat_api``
**之后**，从没在后者之前进过 lifespan。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest


def test_live_executor_recreates_a_shutdown_executor():
    """已被 shutdown 的执行器必须被换成一个能用的。"""
    from api import main as api_main

    dead = ThreadPoolExecutor(max_workers=1)
    dead.shutdown(wait=False, cancel_futures=True)

    fresh = api_main._live_executor(dead, max_workers=1, thread_name_prefix="t")
    try:
        assert fresh is not dead, "停机的执行器没有被替换"
        assert fresh.submit(lambda: 42).result(timeout=5) == 42
    finally:
        fresh.shutdown(wait=False, cancel_futures=True)


def test_live_executor_keeps_a_healthy_executor():
    """还活着的执行器不能被无谓替换（否则每次请求都新建线程池）。"""
    from api import main as api_main

    alive = ThreadPoolExecutor(max_workers=1)
    try:
        assert api_main._live_executor(alive, max_workers=1) is alive
    finally:
        alive.shutdown(wait=False, cancel_futures=True)


def test_light_executor_accessor_recovers_after_shutdown(monkeypatch):
    """`_get_light_executor()` 在停机之后仍能给出可用执行器。"""
    from api import main as api_main

    # 模拟 lifespan 退出：把模块级执行器停机
    api_main._light_executor.shutdown(wait=False, cancel_futures=True)
    assert getattr(api_main._light_executor, "_shutdown", False)

    executor = api_main._get_light_executor()

    assert not getattr(executor, "_shutdown", False)
    assert executor.submit(lambda: "ok").result(timeout=5) == "ok"


def test_a_request_after_lifespan_exit_still_works(monkeypatch):
    """**端到端回归**：进过 lifespan 再退出，后续请求不能 500。

    这就是当初炸掉 12 个无关测试的那个场景。
    """
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from api.main import app

    with patch("api.main._load_cn_stock_map", return_value={}):
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200

    # 退出 lifespan 后：模块级执行器已被 shutdown
    # 再发一个走 run_in_executor(_light_executor, ...) 的请求
    with TestClient(app) as client:  # 注意：这次**不**依赖隐藏的启动重建
        response = client.post(
            "/v1/auth/request-code", json={"email": "executor-regression@example.com"}
        )
    assert response.status_code != 500, (
        f"lifespan 退出后请求 500：{response.text[:200]}——"
        "执行器自愈失效，任何『进过 lifespan 再离开』都会让后续请求挂掉"
    )


_EXECUTOR_NAMES = {"_executor", "_light_executor", "_market_executor"}
#: 允许引用这些名字的函数：自愈取用器本身，以及 lifespan（它负责 shutdown）
_ALLOWED_OWNERS = {
    "_live_executor",
    "_get_executor",
    "_get_light_executor",
    "_get_market_executor",
    "lifespan",
}


def test_no_endpoint_uses_a_raw_module_level_executor():
    """**戒律**：端点里不许再直接用裸的 `_light_executor` / `_market_executor`。

    必须走 `_get_light_executor()` / `_get_executor()` / `_get_market_executor()`，
    否则会绕开自愈，把已 shutdown 的执行器直接交给 `run_in_executor`。

    用 AST 而不是正则：裸引用经常写在**跨行**的调用里，例如

        pool = await loop.run_in_executor(
            _market_executor,          # ← 正则匹配不到这一行
            lambda: ...,
        )

    第一版正则守卫就漏掉了这个形态（变异测试抓出来的），所以改成按语法树找
    所有**读取**这些名字的位置，再按"所属函数"白名单过滤。
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path("api/main.py").read_text(encoding="utf-8"))

    # 收集每个函数用到的执行器名字（只看读取；模块级赋值是 Store，不算使用）
    owners: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            used = {
                sub.id
                for sub in ast.walk(node)
                if isinstance(sub, ast.Name)
                and sub.id in _EXECUTOR_NAMES
                and isinstance(sub.ctx, ast.Load)
            }
            if used:
                owners[node.name] = used

    offenders = {
        fn: sorted(names) for fn, names in owners.items() if fn not in _ALLOWED_OWNERS
    }
    assert not offenders, (
        f"这些函数直接用了模块级执行器（应用 _get_*_executor()）：{offenders}"
    )
    # 反向保险：白名单函数确实存在，否则这条测试会因"什么都没扫到"而空转
    assert _ALLOWED_OWNERS & set(owners), "一个白名单函数都没匹配到，守卫可能失效了"
