"""测试数据库隔离 —— 测试绝不允许碰生产库。

## 为什么需要这个文件

本项目此前**没有** `conftest.py`，也没有任何 pytest 配置，而
`api/database.py:12` 的默认值是：

    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./tradingagents.db")

测试通过 `api.database.get_db_ctx()` 拿会话，于是**直接读写生产库**
`./tradingagents.db`。例如 `tests/test_api_smoke.py` 会插入
`ReportDB(direction="bullish")` —— 实测生产库中确实存在 **1261 行**非规范的
`bullish`/`bearish` 方向值，而 B2 早已声称该字段在写入时就被规范化了。

后果不是"测试偶尔失败"，而是**测试夹具冒充生产记录**：任何按 `direction`
聚合的统计（方向分布、命中率、去重口径、校准曲线）都被污染，且无法事后区分
哪一行是真实研判、哪一行是夹具。这与本次 T+1 质量计划要建立的"可信度量"
是直接冲突的 —— 度量本身的地基不干净。

## 做法

在**任何** `api.*` 被 import 之前，把 `DATABASE_URL` 指向一次性临时目录里的
空库。`api/database.py` 在 import 时读取该变量并据此建 engine，而 pytest 保证
`conftest.py` 先于测试模块被导入，所以这个顺序是可靠的。

临时库通过 `api.database.init_db()` 建表（含全部 `_ensure_*_schema` 迁移守卫），
因此 schema 与生产库一致；测试需要的数据由测试自己 seed —— 这也是它们本来就
应该做的事。

## 逃生舱

确实需要针对某个已存在的库跑测试时（例如排查生产数据问题），显式指定：

    AP_TEST_ALLOW_REAL_DB=1 DATABASE_URL=sqlite:///./tradingagents.db \
        .venv/bin/python -m pytest -q -p no:cacheprovider

该开关必须显式设置，默认关闭：让"污染生产库"成为需要刻意为之的动作，而不是
默认行为。
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_ALLOW_REAL_DB = os.getenv("AP_TEST_ALLOW_REAL_DB", "").strip() in {"1", "true", "yes"}

if not _ALLOW_REAL_DB:
    # 必须在 import api.database 之前完成 —— engine 在该模块 import 时创建。
    _TMP_DIR = Path(tempfile.mkdtemp(prefix="ap-test-db-"))
    _TMP_DB = _TMP_DIR / "test.db"
    # SQLite 绝对路径需要四个斜杠：sqlite:////abs/path.db
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"

    @atexit.register
    def _cleanup_tmp_db() -> None:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)


def pytest_configure(config) -> None:
    """把"当前测试用的是哪个库"写在测试头上，避免误判。"""
    import api.database as _db

    url = getattr(_db, "DATABASE_URL", "?")
    if _ALLOW_REAL_DB:
        config._ap_db_note = f"⚠️  测试直连真实库（AP_TEST_ALLOW_REAL_DB=1）：{url}"
    else:
        config._ap_db_note = f"测试库（临时、用完即删）：{url}"
    print(f"\n[conftest] {config._ap_db_note}")


def pytest_report_header(config) -> str:
    return getattr(config, "_ap_db_note", "")


def pytest_sessionstart(session) -> None:
    """建表：临时库是空的，必须先把 schema 造出来。"""
    if _ALLOW_REAL_DB:
        return
    from api.database import init_db

    init_db()
