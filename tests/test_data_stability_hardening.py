"""稳定性加固的回归测试（2026-09-29）。

## 这个文件锁住的是四件"曾经静默失败"的事

每一件都有实测证据，不是假想：

1. **`_normalize_hist_df` 的撞列**（本次故障的根因）。新浪 `stock_zh_a_daily` 与
   腾讯 `stock_zh_a_hist_tx` 同时返回 `volume` 与 `amount`，而列映射把两者都映到
   `"Volume"`，rename 后出现**重名列** → `out["Volume"]` 是 DataFrame 不是 Series →
   `pd.to_numeric` 抛 `TypeError` → 被兜底分支的 `except: pass` 吞掉。
   结果：新浪与腾讯各返回 19 行**真实数据**却被丢弃，报成"all failed"。
2. **出口闸门不存在**。多个模块叠加打同一域名（东财系尤其）无人节流 = 自己制造封禁。
3. **403 被重试**。`except Exception` 兜住一切并退避重试，于是被风控时把 4 次请求
   变成 16 次。实测 `cn_eastmoney_http` 一次调用白等 **38889ms**。
4. **yfinance 的空结果被写成缓存**。实测磁盘上留下 3 个 42 字节的
   `600519.SS-YFin-data-*.csv`（**只有表头、没有一行数据**），于是每个交易日都被
   渲染成"N/A：该交易日暂无数据"，而路由器还记成 `status=hit` ——
   A 股指标由美股源"成功"供给全空值。
"""

from __future__ import annotations



import pandas as pd
import pytest
import requests

from tradingagents.dataflows.providers import _http_utils
from tradingagents.dataflows.providers.errors import SourceEmpty, SourceThrottled, SourceUnavailable


# ── 1. 撞列回归：新浪/腾讯的真实列形状 ────────────────────────────────────

#: 取自 akshare 实测输出（`ak.stock_zh_a_daily`），含 volume **与** amount
_SINA_SHAPE = pd.DataFrame(
    {
        "date": ["2026-09-25", "2026-09-28"],
        "open": [1.0, 2.0],
        "high": [2.0, 3.0],
        "low": [0.5, 1.0],
        "close": [1.5, 2.5],
        "volume": [100.0, 200.0],
        "amount": [1000.0, 2000.0],
        "outstanding_share": [9.0, 9.0],
        "turnover": [0.1, 0.2],
    }
)

#: 取自 akshare 实测输出（`ak.stock_zh_a_hist_tx`），列序与新浪不同，同样两者都有
_TENCENT_SHAPE = pd.DataFrame(
    {
        "date": ["2026-09-25", "2026-09-28"],
        "open": [1.0, 2.0],
        "close": [1.5, 2.5],
        "high": [2.0, 3.0],
        "low": [0.5, 1.0],
        "volume": [100.0, 200.0],
        "turnover": [0.1, 0.2],
        "amount": [1000.0, 2000.0],
    }
)


@pytest.mark.parametrize("shape", [_SINA_SHAPE, _TENCENT_SHAPE], ids=["sina", "tencent"])
def test_hist_normalize_survives_volume_and_amount_together(shape):
    """volume 与 amount 并存时不得撞列——这是整条兜底链失效的根因。"""
    from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider

    out = CnAkshareProvider()._normalize_hist_df(shape)

    assert list(out.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]
    # 关键断言：取出来必须是 Series（重名列时会是 DataFrame，进而 TypeError）
    assert isinstance(out["Volume"], pd.Series)
    # 且必须是**成交量**（100/200），不是成交额（1000/2000）
    assert list(out["Volume"]) == [100.0, 200.0]


def test_hist_normalize_still_accepts_amount_only_frames():
    """只给成交额、不给成交量的端点仍要能用（退而求其次，但要好过判失败）。"""
    from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider

    frame = pd.DataFrame(
        {
            "date": ["2026-09-25"],
            "open": [1.0],
            "high": [2.0],
            "low": [0.5],
            "close": [1.5],
            "amount": [1234.0],
        }
    )
    out = CnAkshareProvider()._normalize_hist_df(frame)
    assert list(out["Volume"]) == [1234.0]


# ── 2/3. 出口闸门：不重试风控信号，连续失败要冷却 ─────────────────────────


def _patch_requests(monkeypatch, handler):
    """替换 `_http_utils.requests`（沿用既有测试的替身风格：只需要一个 `.get`）。"""
    calls: list[str] = []

    class _Requests:
        @staticmethod
        def get(url, **kwargs):
            calls.append(url)
            return handler(url)

    monkeypatch.setattr(_http_utils, "requests", _Requests, raising=False)
    return calls


def _resp(status: int):
    class _Resp:
        status_code = status

        def raise_for_status(self):
            if status >= 400:
                raise requests.exceptions.HTTPError(response=self)

        def json(self):
            return {"ok": True}

        @property
        def text(self):
            return "ok"

    return _Resp()


def test_403_is_never_retried(monkeypatch):
    """403 是风控信号：重试即攻击。必须只请求一次。"""
    calls = _patch_requests(monkeypatch, lambda url: _resp(403))

    with pytest.raises(RuntimeError):
        _http_utils.http_get_json("https://push2his.eastmoney.com/api/qt/x", retry=_http_utils.HttpRetry(attempts=4))

    assert len(calls) == 1, f"403 不该重试，实际请求了 {len(calls)} 次"


def test_transient_error_is_retried(monkeypatch):
    """连接层失败是**瞬时**故障，仍应退避重试（别把闸门做成"一律不重试"）。"""
    calls = _patch_requests(monkeypatch, lambda url: _resp(500))
    monkeypatch.setattr(_http_utils.time, "sleep", lambda s: None)

    with pytest.raises(RuntimeError):
        _http_utils.http_get_json("https://example.com/a", retry=_http_utils.HttpRetry(attempts=3))

    assert len(calls) == 3


def test_host_goes_into_cooldown_after_repeated_connect_failures(monkeypatch):
    """连续连接层失败达阈值后，该 host 冷却：**快速失败**，不再打网络。

    这就是实测 38889ms 那个问题的解法——冷却期内一次都不该发出去。
    """
    def _boom(url):
        raise requests.exceptions.ConnectionError("RemoteDisconnected")

    calls = _patch_requests(monkeypatch, _boom)
    monkeypatch.setattr(_http_utils.time, "sleep", lambda s: None)
    url = "https://push2his.eastmoney.com/api/qt/y"
    retry = _http_utils.HttpRetry(attempts=1)

    for _ in range(3):
        with pytest.raises(RuntimeError):
            _http_utils.http_get_json(url, retry=retry)
    assert len(calls) == 3

    # 第 4 次：冷却生效 → 立刻抛 SourceThrottled，且**请求数不增加**
    with pytest.raises(SourceThrottled) as excinfo:
        _http_utils.http_get_json(url, retry=retry)
    assert len(calls) == 3, "冷却期内绝不发请求"
    assert excinfo.value.retry_after_s and excinfo.value.retry_after_s > 0
    assert excinfo.value.kind == "throttled"


def test_one_failing_call_with_many_retries_is_only_one_consecutive_failure(monkeypatch):
    """**一次调用**（哪怕内部重试 6 次）只能算一次"连续失败"。

    这是实测踩到的坑：`cn_eastmoney_http` 用 `HttpRetry(attempts=6)`，若按 attempt
    计数，**单次**失败就攒够阈值 → 打一次就把整个 host 冷却掉；而东财是**间歇性**的
    （探测里曾记过 `OK 103ms`），于是把本来能成功的后续调用一起挡掉——
    这是探测可用数从 26 掉到 21 的元凶之一。
    """
    def _boom(url):
        raise requests.exceptions.ConnectionError("x")

    calls = _patch_requests(monkeypatch, _boom)
    monkeypatch.setattr(_http_utils.time, "sleep", lambda s: None)
    url = "https://example.com/many-retries"

    with pytest.raises(RuntimeError):
        _http_utils.http_get_json(url, retry=_http_utils.HttpRetry(attempts=6))

    assert len(calls) == 6, "重试本身要照常发生"
    assert _http_utils.egress_snapshot()["cooling"] == {}, "单次调用不该触发冷却"


def test_per_host_min_interval_is_enforced(monkeypatch):
    """同一 host 的两次请求之间必须有间隔（抖动也算在内）。"""
    sleeps: list[float] = []
    _patch_requests(monkeypatch, lambda url: _resp(200))
    monkeypatch.setattr(_http_utils.time, "sleep", lambda s: sleeps.append(s))

    url = "https://example.com/interval"
    _http_utils.http_get_json(url)
    _http_utils.http_get_json(url)

    assert sleeps and max(sleeps) >= 0.1, f"第二次请求应被节流，sleeps={sleeps}"


def test_egress_disabled_is_an_escape_hatch(monkeypatch):
    """`TA_EGRESS_DISABLED=1` 时闸门整体退化为"什么都没有"（排障用）。"""
    monkeypatch.setenv("TA_EGRESS_DISABLED", "1")
    sleeps: list[float] = []
    _patch_requests(monkeypatch, lambda url: _resp(403))
    monkeypatch.setattr(_http_utils.time, "sleep", lambda s: sleeps.append(s))

    for _ in range(5):
        with pytest.raises(RuntimeError):
            _http_utils.http_get_json(
                "https://push2his.eastmoney.com/api/qt/z",
                retry=_http_utils.HttpRetry(attempts=1),
            )

    assert sleeps == [], "闸门禁用时不应有任何节流 sleep"
    assert _http_utils.egress_snapshot()["cooling"] == {}


# ── 4. yfinance 空结果不许被当成"这天没数据"，也不许写缓存 ────────────────


def _patch_cache_dir(monkeypatch, tmp_path):
    from tradingagents.dataflows import stockstats_utils as su

    monkeypatch.setattr(su, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    return su


def test_empty_yfinance_result_raises_and_writes_no_cache(tmp_path, monkeypatch):
    su = _patch_cache_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(su.yf, "download", lambda *a, **k: pd.DataFrame())

    with pytest.raises(SourceEmpty):
        su.StockstatsUtils.get_stock_stats("600519.SH", "close_50_sma", "2026-09-28")

    # 把失败写进缓存 = 把失败固化成数据（实测磁盘上就是这样留下 3 个空 CSV 的）
    assert list(tmp_path.iterdir()) == []


def test_poisoned_empty_cache_is_discarded_and_refetched(tmp_path, monkeypatch):
    """既有的空缓存（只有表头）必须被删掉重取，而不是继续拿它回答"N/A"。"""
    su = _patch_cache_dir(monkeypatch, tmp_path)

    today = pd.Timestamp.today()
    start = (today - pd.DateOffset(years=15)).strftime("%Y-%m-%d")
    end = (today + pd.DateOffset(days=1)).strftime("%Y-%m-%d")
    poisoned = tmp_path / f"600519.SH-YFin-data-{start}-{end}.csv"
    poisoned.write_text("Date,Adj Close,Close,High,Low,Open,Volume\n", encoding="utf-8")

    # 索引名必须是 "Date"：真实 yf.download 返回的就是这个索引名，
    # 代码随后依赖 `reset_index()` 造出 "Date" 列（无名索引会变成 "index"）。
    idx = pd.DatetimeIndex(["2026-09-25", "2026-09-28"], name="Date")
    fresh = pd.DataFrame(
        {
            "Open": [1.0, 2.0],
            "High": [2.0, 3.0],
            "Low": [0.5, 1.0],
            "Close": [1.5, 2.5],
            "Volume": [10.0, 20.0],
        },
        index=idx,
    )
    monkeypatch.setattr(su.yf, "download", lambda *a, **k: fresh)

    su.StockstatsUtils.get_stock_stats("600519.SH", "close_50_sma", "2026-09-28")

    assert len(poisoned.read_text(encoding="utf-8").strip().splitlines()) > 1


# ── 5. 路由器：报错要带全链原因，"空"与"坏"要分开 ─────────────────────────


class _FakeRegistry:
    def __init__(self, providers: dict):
        self._providers = providers

    def list_names(self):
        return list(self._providers)

    def get(self, name):
        return self._providers.get(name)


class _BoomProvider:
    def __init__(self, exc: Exception):
        self._exc = exc

    def get_stock_data(self, *a, **k):
        raise self._exc


def _route_with(monkeypatch, providers):
    from tradingagents.dataflows import interface

    monkeypatch.setattr(interface, "_registry", _FakeRegistry(providers))
    monkeypatch.setattr(interface, "get_vendor", lambda *a, **k: ",".join(providers))
    return interface.route_to_vendor("get_stock_data", "600519.SH", "2026-09-01", "2026-09-28")


def test_router_error_mentions_every_attempted_vendor(monkeypatch):
    """原实现只报 `last_exc`，被链尾占位 provider 顶掉后**把根因指错了**。"""
    with pytest.raises(RuntimeError) as excinfo:
        _route_with(
            monkeypatch,
            {
                "cn_akshare": _BoomProvider(SourceUnavailable("push2 不可达")),
                "cn_stub": _BoomProvider(NotImplementedError("placeholder")),
            },
        )

    msg = str(excinfo.value)
    assert "cn_akshare=unavailable:SourceUnavailable" in msg
    assert "cn_stub=" in msg


def test_router_reports_empty_when_no_source_has_data(monkeypatch):
    """"确实没有数据"不等于"源坏了"：应抛 SourceEmpty 而不是可怕的 RuntimeError。"""
    with pytest.raises(SourceEmpty):
        _route_with(monkeypatch, {"cn_akshare": _BoomProvider(SourceEmpty("停牌"))})


# ── 6. baostock 登录冷却（它走 socket，不经过 HTTP 闸门，必须自己熔断）────────


def test_baostock_login_cooldown_fast_fails_without_retrying_login(monkeypatch):
    """登录连续失败后进入冷却：**连登录请求都不再发**。

    实测依据：登录失败每次耗时 10027~12040ms，而 `get_daily_bars_df` 的配置链是
    `cn_baostock,cn_akshare`——baostock 排第一，于是每次调用先白等 10 秒才轮到
    能用的 akshare（实测 11318ms 中约 10000ms 是纯浪费）。
    实测冷却生效后的调用序列：13722ms → 5919ms → 1364ms → 1260ms。
    """
    from tradingagents.dataflows.providers import cn_baostock_provider as bsp

    bsp.reset_login_cooldown()
    attempts = {"n": 0}

    class _FakeBs:
        @staticmethod
        def login():
            attempts["n"] += 1

            class _R:
                error_code = "10002007"
                error_msg = "网络接收错误。"

            return _R()

        @staticmethod
        def logout():
            return None

    provider = bsp.CnBaoStockProvider()
    monkeypatch.setattr(provider, "_bs", lambda: _FakeBs)

    # 前两次真的去登录（阈值 2）
    for expected in (1, 2):
        with pytest.raises(SourceUnavailable):
            with provider._session():
                pass
        assert attempts["n"] == expected

    # 第三次：冷却生效 → 毫秒级失败，且**登录次数不增加**
    with pytest.raises(SourceUnavailable) as excinfo:
        with provider._session():
            pass
    assert attempts["n"] == 2, "冷却期内不该再发登录请求"
    assert "cooldown" in str(excinfo.value)


def test_baostock_successful_login_clears_the_streak(monkeypatch):
    """一次成功就清空连败计数——间歇性抖动不该累积成永久冷却。"""
    from tradingagents.dataflows.providers import cn_baostock_provider as bsp

    bsp.reset_login_cooldown()
    state = {"fail": True}

    class _FakeBs:
        @staticmethod
        def login():
            class _R:
                error_code = "1" if state["fail"] else "0"
                error_msg = "boom"

            return _R()

        @staticmethod
        def logout():
            return None

    provider = bsp.CnBaoStockProvider()
    monkeypatch.setattr(provider, "_bs", lambda: _FakeBs)

    with pytest.raises(SourceUnavailable):
        with provider._session():
            pass

    state["fail"] = False
    with provider._session():
        pass

    assert bsp._login_fail_streak == 0
    assert bsp._login_cooldown_remaining() == 0.0
