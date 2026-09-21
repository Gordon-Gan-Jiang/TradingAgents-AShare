"""板块相对强度：东财被封后走新浪。

实测东财系全挂 → `relative_strength`（个股换手 vs 板块中位换手）长期缺失。
新浪可用（`stock_sector_spot` 49 个行业 + `stock_sector_detail` 含 `turnoverratio`），
但**新浪没有"按个股查行业"的接口**，只能扫板块建映射——因此必须缓存，
否则每次判定都要扫 49 个板块（判定总预算只有 60s）。

本文件锁住：
1. 东财失败时必须退新浪，并返回既有契约的 (行业名, 板块行情表, 成分股表)。
2. 板块行情表要有 `板块名称`/`涨跌幅`；成分股表要有 `换手率`（否则中位数算不出来）。
3. 映射**必须缓存**：第二次调用不得再扫一遍。
4. 找不到该股所属行业时返回 None（诚实），不猜一个板块。
"""

from __future__ import annotations

import pandas as pd
import pytest


def _sector_list() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "label": ["new_blhy", "new_jzjc"],
            "板块": ["玻璃行业", "建筑建材"],
            "公司家数": [19, 30],
            "涨跌幅": [1.25, -0.4],
        }
    )


def _sector_detail(label: str) -> pd.DataFrame:
    if label == "new_blhy":
        return pd.DataFrame(
            {
                "symbol": ["sh600176", "sh600660"],
                "code": ["600176", "600660"],
                "name": ["中国巨石", "福耀玻璃"],
                "turnoverratio": [1.8, 2.4],
            }
        )
    return pd.DataFrame(
        {
            "symbol": ["sz000786"],
            "code": ["000786"],
            "name": ["北新建材"],
            "turnoverratio": [0.9],
        }
    )


class _FakeAk:
    def __init__(self):
        self.detail_calls: list[str] = []

    def stock_individual_info_em(self, **kwargs):
        raise ConnectionError("Remote end closed connection")

    def stock_sector_spot(self, **kwargs):
        return _sector_list()

    def stock_sector_detail(self, **kwargs):
        label = kwargs.get("sector")
        self.detail_calls.append(label)
        return _sector_detail(label)


def test_peer_frames_fall_back_to_sina(monkeypatch, tmp_path):
    from tradingagents.dataflows.providers import cn_research_data as mod

    fake = _FakeAk()
    monkeypatch.setattr(mod, "_ak", lambda: fake)
    monkeypatch.setattr(mod, "_SECTOR_MAP_CACHE_FILE", tmp_path / "sector_map.json", raising=False)

    result = mod.fetch_peer_frames("600176.SH", "2026-09-19")

    assert result is not None, "东财挂了但新浪可用，不该直接判为取不到"
    industry, board_frame, cons_frame = result
    assert industry == "玻璃行业"
    assert "板块名称" in board_frame.columns and "涨跌幅" in board_frame.columns
    assert "换手率" in cons_frame.columns, f"成分股表必须能算出换手中位数，实际 {list(cons_frame.columns)}"
    assert cons_frame["换手率"].tolist() == [1.8, 2.4]


def test_sector_map_is_cached_across_calls(monkeypatch, tmp_path):
    """扫 49 个板块是有成本的；判定预算只有 60s，绝不能每次都扫。"""
    from tradingagents.dataflows.providers import cn_research_data as mod

    fake = _FakeAk()
    monkeypatch.setattr(mod, "_ak", lambda: fake)
    cache = tmp_path / "sector_map.json"
    monkeypatch.setattr(mod, "_SECTOR_MAP_CACHE_FILE", cache, raising=False)

    mod.fetch_peer_frames("600176.SH", "2026-09-19")
    first_scan = len(fake.detail_calls)
    assert cache.exists(), "映射必须落盘缓存"

    fake.detail_calls.clear()
    mod.fetch_peer_frames("600176.SH", "2026-09-19")
    assert fake.detail_calls == [x for x in fake.detail_calls if x == "new_blhy"], (
        f"第二次应只取该行业成分股，不应全量重扫，实际 {fake.detail_calls}"
    )
    assert first_scan >= 1


def test_unknown_symbol_returns_none(monkeypatch, tmp_path):
    from tradingagents.dataflows.providers import cn_research_data as mod

    monkeypatch.setattr(mod, "_ak", lambda: _FakeAk())
    monkeypatch.setattr(mod, "_SECTOR_MAP_CACHE_FILE", tmp_path / "sector_map.json", raising=False)

    assert mod.fetch_peer_frames("999999.SH", "2026-09-19") is None, (
        "找不到所属行业时必须诚实返回 None，而不是猜一个板块"
    )
