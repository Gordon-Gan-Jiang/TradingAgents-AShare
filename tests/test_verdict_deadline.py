"""判定必须有**端到端截止时间**。

## 实测背景

一次判定在真实环境里卡了 **10 分钟没返回**（`evaluate_symbol` 直接超时被杀）。
原因是取数链路里每个源都没有超时，而 akshare 内部还有重试——任何一环慢下来，
整个判定就一直挂着。用户看到的是"页面转圈"，而系统既不返回也不报错。

对投研产品的意义：**"没有结论"必须是一个结论**。挂在半路的判定比"数据不足"
糟得多，因为用户无法据此做任何决定，也无法知道系统还活着。

## 本文件锁住什么

1. 超时必须**按时返回**，且抛出可读的错误（路由转 422），而不是无限等。
2. 超时后不得把半成品当判定返回。
3. 消息里要说明"是超时"以及超时预算，让用户知道该重试还是该换标的。
"""

from __future__ import annotations

import time

import pytest


def test_slow_evaluation_times_out_with_a_readable_error(monkeypatch):
    from api.services import signal_service

    def _hang(**kwargs):
        time.sleep(5)
        raise AssertionError("不该跑到这里")

    monkeypatch.setattr(signal_service, "_evaluate_core", _hang, raising=False)
    monkeypatch.setattr(signal_service, "VERDICT_DEADLINE_SECONDS", 0.3, raising=False)

    started = time.time()
    with pytest.raises(signal_service.SignalEvaluationError) as excinfo:
        signal_service.evaluate_symbol(
            symbol="600519.SH", as_of="2026-09-21", window_days=20, user_id=None, persist=False
        )
    elapsed = time.time() - started

    assert elapsed < 3, f"超时应在预算附近返回，实际 {elapsed:.1f}s"
    assert "超时" in str(excinfo.value), f"错误信息要说明是超时：{excinfo.value}"


def test_timeout_error_names_the_budget(monkeypatch):
    from api.services import signal_service

    monkeypatch.setattr(signal_service, "_evaluate_core", lambda **kw: time.sleep(5), raising=False)
    monkeypatch.setattr(signal_service, "VERDICT_DEADLINE_SECONDS", 0.2, raising=False)

    with pytest.raises(signal_service.SignalEvaluationError) as excinfo:
        signal_service.evaluate_symbol(
            symbol="000001.SZ", as_of="2026-09-21", window_days=20, user_id=None, persist=False
        )
    assert "0.2" in str(excinfo.value) or "秒" in str(excinfo.value), (
        f"用户需要知道超时预算才能决定是否重试：{excinfo.value}"
    )


def test_fast_call_passes_through_untouched():
    """正常路径不得被截止时间机制改变行为（返回值原样透传）。"""
    from api.services.signal_service import _run_with_deadline

    sentinel = object()
    assert _run_with_deadline(lambda: sentinel, seconds=5, symbol="X") is sentinel


def test_exception_from_the_core_still_propagates(monkeypatch):
    """截止时间机制不得吞掉真实异常——那会把"取数失败"变成"超时"。"""
    from api.services.signal_service import _run_with_deadline

    def _boom():
        raise ValueError("vendor 挂了")

    with pytest.raises(ValueError):
        _run_with_deadline(_boom, seconds=5, symbol="X")
