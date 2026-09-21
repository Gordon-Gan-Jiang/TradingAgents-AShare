"""测试库隔离的回归护栏 —— 防止"测试写生产库"再次悄悄回来。

## 这份测试要挡住什么

`tests/conftest.py` 把 `DATABASE_URL` 指向一次性临时库。但那是**约定**，不是
机制：任何人删掉 conftest、或在 `api/database.py` 里换掉默认值、或某个新测试
自己 `create_engine("sqlite:///./tradingagents.db")`，污染就会无声复活。

这不是假想风险，而是**已经发生过**的事，有第一手证据：

    实测生产库 `reports` 表在 2026-09-16 01:48:31 一次性新增 136 行，
    标的恰为测试夹具里写死的 600519.SH / 300750.SZ / 000001.SZ /
    601318.SH / 601398.SH，`direction` 为夹具里的非规范值
    'bullish' / 'bearish' / None。该时间戳与一次全量 pytest 运行完全吻合。

更早的批次可追到 2026-04-10，脚本 `scripts/clean_test_polluted_reports.py`
在 R1∧R2 规则下识别出 **989 行**（同一微秒上 ≥5 行、且方向非规范）。

## 一个必须说明的设计约束

护栏测试**绝不能**在证明隔离生效之前先写一行 —— 否则隔离一旦真的坏掉，这条
测试自己就成了污染源。所以下面的顺序被刻意固定为：

    1. 先断言 engine 指向临时库（纯读，零副作用）；
    2. 断言通过后才允许写入；
    3. 写入后确认这行**确实落在临时库文件里**，并且生产库文件字节未变。

第 3 步同时防止另一种失败：写入根本没发生（例如被静默吞掉），此时"生产库没变"
是假阳性。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


def _allow_real_db() -> bool:
    return os.getenv("AP_TEST_ALLOW_REAL_DB", "").strip().lower() in {"1", "true", "yes"}


def _production_db_paths() -> list[Path]:
    """生产库的候选路径（相对 cwd 与仓库根）。"""
    out: list[Path] = []
    for base in (Path.cwd(), Path(__file__).resolve().parent.parent):
        out.append((base / "tradingagents.db").resolve())
    return out


def _assert_engine_is_isolated() -> Path:
    """断言 engine 指向临时库，并返回该库的绝对路径。

    纯读操作，不产生任何写入 —— 因此可以在"可能已经坏掉"的情况下安全调用。
    """
    from api import database

    url = database.DATABASE_URL
    assert "tradingagents.db" not in url, (
        f"测试引擎指向了生产库：{url}\n"
        "conftest.py 应在 import api.database 之前改写 DATABASE_URL。"
    )
    assert url.startswith("sqlite:///"), url

    db_path = Path(url.replace("sqlite:///", "")).resolve()
    assert db_path not in _production_db_paths(), f"测试库解析后等同于生产库：{db_path}"
    # macOS 上 /var 是指向 /private/var 的符号链接，两边都要 resolve 后才能比较。
    tmp_root = Path(tempfile.gettempdir()).resolve()
    assert db_path.is_relative_to(tmp_root), (
        f"测试库不在临时目录下，可能在污染工作区：{db_path}（临时目录根：{tmp_root}）"
    )
    return db_path


def test_configured_database_is_not_the_production_file():
    """engine 指向的库绝不能是生产库文件（纯读断言）。"""
    if _allow_real_db():
        pytest.skip("显式设置 AP_TEST_ALLOW_REAL_DB=1，本次刻意直连真实库")
    _assert_engine_is_isolated()


def test_writing_through_the_app_session_lands_in_the_test_database():
    """写入必须落在临时库里，且生产库文件字节不变。

    顺序不可调换：先证明隔离（纯读），再写入。
    """
    if _allow_real_db():
        pytest.skip("显式设置 AP_TEST_ALLOW_REAL_DB=1，本次刻意直连真实库")

    test_db = _assert_engine_is_isolated()  # ← 必须先证明隔离，任何写入都在其后

    prod = Path.cwd() / "tradingagents.db"
    if not prod.exists():
        prod = Path(__file__).resolve().parent.parent / "tradingagents.db"
    prod_before = (prod.stat().st_mtime_ns, prod.stat().st_size) if prod.exists() else None

    from api.database import ReportDB, get_db_ctx

    with get_db_ctx() as db:
        db.add(
            ReportDB(
                id="isolation_guard_probe",
                user_id="isolation_guard",
                symbol="600519.SH",
                trade_date="2026-01-01",
                status="completed",
                decision="BUY",
                direction="看多",
            )
        )
        db.commit()

    # 1) 这行必须真的落在临时库**文件**里（防止"写入根本没发生"的假阳性）
    assert test_db.exists(), f"临时库文件不存在：{test_db}"
    conn = sqlite3.connect(str(test_db))
    try:
        found = conn.execute(
            "SELECT symbol FROM reports WHERE id = 'isolation_guard_probe'"
        ).fetchone()
    finally:
        conn.close()
    assert found is not None, "探针行未落进临时库 —— 这条护栏自身失效了"
    assert found[0] == "600519.SH"

    # 2) 生产库文件必须一个字节都没动
    if prod_before is not None:
        prod_after = (prod.stat().st_mtime_ns, prod.stat().st_size)
        assert prod_before == prod_after, (
            "测试写入落到了生产库上！\n"
            f"  路径：{prod}\n"
            f"  前：mtime={prod_before[0]} size={prod_before[1]}\n"
            f"  后：mtime={prod_after[0]} size={prod_after[1]}"
        )
