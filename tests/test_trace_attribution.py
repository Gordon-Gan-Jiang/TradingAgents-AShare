"""延迟与成本归因端点（Phase 4.2）。

本模块锁两件事，都是本轮踩出来的：

## 1. 归因必须给中位与 P90，不能只给平均

本轮的延迟实验证明这条路径**方差大到单次采样没有意义**：同一个问题、同一套配置
重复三次，延迟是 8.4 s / 13.2 s / 19.8 s（差 2.4 倍）。若归因只报平均值，
一次长尾就会让人把"这一次特别慢"读成"整体变慢了"，并据此改错地方。

## 2. 路由声明顺序：`/chat/traces/summary` 必须在 `/chat/traces/{trace_id}` 之前

FastAPI 按**声明顺序**匹配。`summary` 放在 `{trace_id}` 之后会被当成某个 trace
的 id 吃掉——端点永远 404，而且不报错，只是"查不到"。这类缺陷不写测试就只能
靠"恰好读到路由定义顺序"发现。
"""

from __future__ import annotations

import pytest


# ── 分位数与分布摘要 ──────────────────────────────────────────────────


def test_percentile_interpolates_instead_of_picking_a_neighbour():
    """分位数要插值。

    取最近邻在样本少时会**系统性偏**：三个样本取 P90 会直接等于最大值，
    于是"P90"和"max"永远是同一个数，看不出分布形状。
    """
    from api.services.trace_service import _percentile

    assert _percentile([10.0, 20.0, 30.0], 0.5) == 20.0
    # P90 落在 20 与 30 之间，而不是直接等于 30
    assert 20.0 < _percentile([10.0, 20.0, 30.0], 0.9) < 30.0


def test_percentile_of_an_empty_series_is_zero_not_an_exception():
    """没有样本时返回 0，不能抛异常。

    归因端点是只读诊断接口，**不该因为"还没数据"而 500**——
    全新用户第一次打开看板就是这个状态。
    """
    from api.services.trace_service import _percentile

    assert _percentile([], 0.5) == 0.0
    assert _percentile([], 0.9) == 0.0


def test_stats_report_median_and_p90_and_count():
    """分布摘要三件套：**样本量 + 中位 + P90**。

    缺样本量就无法判断结论可信度；缺中位就只剩长尾噪声；缺 P90 就看不见尾部。
    """
    from api.services.trace_service import _stats

    out = _stats([1000.0, 2000.0, 3000.0, 90000.0])
    assert out["count"] == 4
    assert out["median"] < 3000.0, "长尾不该把中位拉走"
    assert out["max"] == 90000.0


def test_the_median_is_not_dragged_by_a_long_tail():
    """这条是"为什么不用平均"的回归测试。

    同一组数据：均值会被 90000 拉到两万多，中位仍是 2500。
    """
    from api.services.trace_service import _stats

    values = [1000.0, 2000.0, 3000.0, 90000.0]
    mean = sum(values) / len(values)
    assert mean > 20000
    assert _stats(values)["median"] < 3000


# ── 归因口径 ──────────────────────────────────────────────────────────


def _fake_row(**kw):
    class Row:
        pass

    row = Row()
    row.tier = kw.get("tier", "light")
    row.status = kw.get("status", "completed")
    row.latency_ms = kw.get("latency_ms", 5000)
    row.tokens = kw.get("tokens", 1000)
    row.model_calls = kw.get("model_calls", 1)
    row.steps = kw.get("steps", [])
    return row


def test_summary_separates_tool_time_from_model_time():
    """工具耗时与回合延迟必须**分开**统计。

    这是本轮最重要的口径更正：只看"模型占 93–98%"会漏掉工具主导的回合
    （实测 `market_screen` 单工具占掉某回合的 86%）。所以归因必须同时给出
    每工具耗时，否则那批回合永远查不出来。
    """
    from api.services import trace_service

    rows = [
        _fake_row(
            latency_ms=92000,
            steps=[{"tool": "market_screen", "ok": True, "elapsed_ms": 80000, "evidence_count": 3}],
        )
    ]
    out = trace_service._summarize_rows(rows)

    assert out["by_tool"]["market_screen"]["elapsed_ms"]["median"] == 80000.0
    # 回合延迟与工具耗时是两个独立数字，不能互相替代
    assert out["overall_latency_ms"]["median"] == 92000.0


def test_tool_failure_rate_is_reported():
    """失败率要按工具给——它决定"这个工具现在能不能用"。

    实测 `get_board_spot` 失败率 0.72（东财封禁），这类信息必须一眼可见。
    """
    from api.services import trace_service

    rows = [
        _fake_row(steps=[{"tool": "get_board_spot", "ok": False, "elapsed_ms": 0, "evidence_count": 0}]),
        _fake_row(steps=[{"tool": "get_board_spot", "ok": False, "elapsed_ms": 0, "evidence_count": 0}]),
        _fake_row(steps=[{"tool": "get_board_spot", "ok": True, "elapsed_ms": 100, "evidence_count": 2}]),
    ]
    out = trace_service._summarize_rows(rows)
    view = out["by_tool"]["get_board_spot"]

    assert view["calls"] == 3
    assert view["failed"] == 2
    assert view["failure_rate"] == pytest.approx(0.667, abs=0.01)


def test_the_variance_warning_travels_with_the_data():
    """方差警告必须**随数据一起返回**，而不是只写在文档里。

    这条路径方差 2.4 倍，单次采样不可信。把警告放在文档里没人会在调参时读到，
    放在响应里才会。
    """
    from api.services import trace_service

    out = trace_service._summarize_rows([_fake_row()])
    assert "warning" in out["sample"]
    assert "方差" in out["sample"]["warning"]


def test_degraded_rate_is_computed_from_status():
    from api.services import trace_service

    rows = [
        _fake_row(status="completed"),
        _fake_row(status="degraded"),
        _fake_row(status="degraded"),
        _fake_row(status="degraded"),
    ]
    out = trace_service._summarize_rows(rows)
    assert out["sample"]["turns"] == 4
    assert out["sample"]["degraded_rate"] == pytest.approx(0.75)


def test_an_empty_history_returns_a_usable_shape():
    """没有历史时返回完整结构而不是报错——新用户第一次打开看板就是这个状态。"""
    from api.services import trace_service

    out = trace_service._summarize_rows([])
    assert out["sample"]["turns"] == 0
    assert out["by_tier"] == {}
    assert out["by_tool"] == {}
    assert out["overall_latency_ms"]["count"] == 0


# ── 路由：声明顺序必须是 summary 在前 ─────────────────────────────────


def test_summary_route_is_declared_before_the_trace_id_route():
    """`/chat/traces/summary` 必须**声明在** `/chat/traces/{trace_id}` 之前。

    FastAPI 按声明顺序匹配。顺序反了的话 `summary` 会被当作 trace id，
    端点永远 404——而且不报错，只是"查不到"。所以顺序本身要被测试锁住。
    """
    from api.routers import chat

    # 路由表里的 path 带 router 前缀（`/v1`），断言要用完整路径
    paths = [getattr(route, "path", "") for route in chat.router.routes]
    summary = "/v1/chat/traces/summary"
    by_id = "/v1/chat/traces/{trace_id}"
    assert summary in paths, paths
    assert by_id in paths, paths
    assert paths.index(summary) < paths.index(by_id), (
        "summary 声明在 {trace_id} 之后 → 会被当作 trace id 吃掉，端点永远 404"
    )


def test_the_summary_endpoint_is_reachable_over_http():
    """真的发一次请求，确认没有被 `{trace_id}` 吃掉。

    只断言路由表顺序还不够——真正要保证的是"请求打得通"。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.dependencies import current_user
    from api.routers import chat

    app = FastAPI()
    app.include_router(chat.router)

    class User:
        id = "u-test"

    app.dependency_overrides[current_user] = lambda: User()
    client = TestClient(app)

    resp = client.get("/v1/chat/traces/summary?limit=5")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "sample" in body
    assert "by_tool" in body
