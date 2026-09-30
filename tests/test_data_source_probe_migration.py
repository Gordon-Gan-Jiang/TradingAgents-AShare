"""探测表的**加列迁移**必须能在「表已存在」的库上生效。

## 为什么单独测这个

`Base.metadata.create_all()` **只建缺失的表，不给已存在的表加列**。而这两张表在
本机是**已经存在且有历史数据**的（2 轮 / 95 条明细）。所以给它们加
`timeout_count` / `row_unit` 时，`create_all` 是**完全无效**的——
必须走 `_ensure_data_source_probe_schema()` 的 ALTER。

如果漏了这个守卫，后果不是启动报错，而是**查询时才炸**
（`no such column: timeout_count`），页面探测报告整页崩——而且只有在
"从旧库升级上来"的机器上才复现，测试环境（每次全新临时库）永远发现不了。

所以这里**故意造一个旧版schema 的库**，再跑 `init_db()`，验证列被补上。
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, inspect as sa_inspect

_OLD_RUNS_DDL = """
CREATE TABLE data_source_probe_runs (
    id VARCHAR(36) PRIMARY KEY,
    started_at DATETIME,
    finished_at DATETIME,
    status VARCHAR(16),
    trigger VARCHAR(16),
    trading_day BOOLEAN,
    total_count INTEGER,
    ok_count INTEGER,
    degraded_count INTEGER,
    failed_count INTEGER,
    unsupported_count INTEGER,
    duration_ms INTEGER,
    note TEXT,
    created_at DATETIME
)
"""

_OLD_RESULTS_DDL = """
CREATE TABLE data_source_probe_results (
    id VARCHAR(36) PRIMARY KEY,
    run_id VARCHAR(36),
    source VARCHAR(64),
    tool VARCHAR(64),
    label VARCHAR(128),
    category VARCHAR(64),
    status VARCHAR(16),
    latency_ms INTEGER,
    row_count INTEGER,
    version VARCHAR(32),
    error_kind VARCHAR(32),
    error_type VARCHAR(64),
    error_message TEXT,
    checked_at DATETIME,
    created_at DATETIME
)
"""


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """造一个「旧版 schema + 一行历史数据」的库，并把 api.database 指过去。"""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(_OLD_RUNS_DDL)
        conn.execute(_OLD_RESULTS_DDL)
        # 历史数据：迁移不许丢数据
        conn.execute(
            "INSERT INTO data_source_probe_runs (id, status, total_count, failed_count) "
            "VALUES ('old-run', 'failed', 47, 11)"
        )
        conn.execute(
            "INSERT INTO data_source_probe_results (id, run_id, source, tool, status, row_count) "
            "VALUES ('old-row', 'old-run', 'cn_akshare', 'get_daily_bars_df', 'failed', 0)"
        )
        conn.commit()
    finally:
        conn.close()

    # api.database 在导入时就按 env 建好了 engine，所以这里重建成指向旧库
    import api.database as database

    engine = create_engine(f"sqlite:///{path}")
    monkeypatch.setattr(database, "engine", engine, raising=False)
    return path


def _columns(path, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def test_legacy_schema_really_lacks_the_new_columns(legacy_db):
    """先证明"旧库确实缺列"——否则下面的迁移测试可能是空转。"""
    assert "timeout_count" not in _columns(legacy_db, "data_source_probe_runs")
    assert "row_unit" not in _columns(legacy_db, "data_source_probe_results")


def test_create_all_alone_does_not_add_columns_to_an_existing_table(legacy_db):
    """坐实这个坑：`create_all` 对已存在的表**不加列**。

    这条测试是"为什么必须有 ALTER 守卫"的证据本身。
    """
    from api.database import Base

    engine = create_engine(f"sqlite:///{legacy_db}")
    Base.metadata.create_all(bind=engine)

    assert "timeout_count" not in _columns(legacy_db, "data_source_probe_runs")
    assert "row_unit" not in _columns(legacy_db, "data_source_probe_results")


def test_init_db_adds_the_new_columns_and_keeps_the_data(legacy_db, monkeypatch):
    """`init_db()` 必须把新列补上，且**不动**已有数据。"""
    import api.database as database

    # init_db 内部用模块级 engine，这里让它用指向旧库的那个
    monkeypatch.setattr(
        database, "engine", create_engine(f"sqlite:///{legacy_db}"), raising=False
    )
    database._ensure_data_source_probe_schema()

    assert "timeout_count" in _columns(legacy_db, "data_source_probe_runs")
    assert "row_unit" in _columns(legacy_db, "data_source_probe_results")

    conn = sqlite3.connect(legacy_db)
    try:
        run = conn.execute(
            "SELECT status, total_count, failed_count, timeout_count "
            "FROM data_source_probe_runs WHERE id='old-run'"
        ).fetchone()
        row = conn.execute(
            "SELECT source, status, row_count, row_unit "
            "FROM data_source_probe_results WHERE id='old-row'"
        ).fetchone()
    finally:
        conn.close()

    assert run[:3] == ("failed", 47, 11), "迁移动了历史汇总数据"
    assert run[3] is None or run[3] == 0, "新列应有默认值（0/NULL）"
    assert row[:3] == ("cn_akshare", "failed", 0), "迁移动了历史明细数据"
    assert row[3] is None, "历史行的 row_unit 应为 NULL（旧数据没有单位概念）"


def test_migration_is_idempotent(legacy_db, monkeypatch):
    """重复执行不能报错（每次启动都会跑一遍）。"""
    import api.database as database

    monkeypatch.setattr(
        database, "engine", create_engine(f"sqlite:///{legacy_db}"), raising=False
    )
    database._ensure_data_source_probe_schema()
    database._ensure_data_source_probe_schema()  # 第二次不应抛
    database._ensure_data_source_probe_schema()

    assert "timeout_count" in _columns(legacy_db, "data_source_probe_runs")


def test_migration_is_safe_when_tables_do_not_exist(tmp_path, monkeypatch):
    """全新库上表还不存在时，守卫必须**安静返回**而不是报错。

    `init_db()` 里 `create_all` 在前、守卫在后，所以正常路径下表已存在；
    但守卫自己不能依赖这个顺序（否则调整顺序就会启动失败）。
    """
    import api.database as database

    empty = tmp_path / "empty.db"
    empty.touch()
    monkeypatch.setattr(database, "engine", create_engine(f"sqlite:///{empty}"), raising=False)

    database._ensure_data_source_probe_schema()  # 不应抛

    assert sa_inspect(create_engine(f"sqlite:///{empty}")).get_table_names() == []
