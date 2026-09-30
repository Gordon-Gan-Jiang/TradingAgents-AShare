"""`providers/support.py` 的判定口径测试。

这个模块是"配置不许说谎"的地基：能力矩阵与 vendor 链的一致性测试都建立在
`implemented_methods` 的判断之上。地基判错，上层全是假绿灯——所以这里要把
**四种容易混淆的写法**分别钉住：

1. 纯占位（整个方法体就一句 `raise NotImplementedError`）→ 未实现
2. 分支占位（取不到数据时才抛）→ **已实现**（在 `cn_akshare_provider` 里是常态写法）
3. 空体 / 只有 docstring → 未实现
4. 子类用占位覆盖父类真实现 → 未实现（否则又会造出一个说谎的 provider）
"""

from __future__ import annotations

from tradingagents.dataflows.providers import support
from tradingagents.dataflows.providers.base import BaseMarketDataProvider


class _PureStub:
    def get_news(self, ticker, start_date, end_date):
        """占位：本项目未覆盖新闻。"""
        raise NotImplementedError("stub does not provide news yet.")

    def get_fundamentals(self, ticker, curr_date=None):
        raise NotImplementedError


class _BranchStub:
    """分支抛异常——必须算已实现。"""

    def get_board_spot(self, board_type="industry"):
        if board_type == "concept":
            raise NotImplementedError("concept 未覆盖")
        return "ok-industry"


class _EmptyBody:
    def get_cashflow(self, ticker, freq="quarterly", curr_date=None):
        """只有 docstring，没有实现。"""

    def get_balance_sheet(self, ticker, freq="quarterly", curr_date=None):
        pass


class _Real:
    def get_stock_data(self, symbol, start_date, end_date):
        return "date,open,close\n2026-09-23,1,2"

    def get_indicators(self, symbol, indicator, curr_date, look_back_days):
        if indicator == "not_supported":
            raise NotImplementedError("不支持该 indicator")
        return "rsi=55"


class _Parent:
    def get_income_statement(self, ticker, freq="quarterly", curr_date=None):
        return "real-implementation"


class _ChildOverridesWithStub(_Parent):
    def get_income_statement(self, ticker, freq="quarterly", curr_date=None):
        raise NotImplementedError("cn_stub does not provide income statement yet.")


def test_pure_stub_is_detected_as_not_implemented():
    assert support.stub_methods(_PureStub) == {"get_news", "get_fundamentals"}
    assert support.implemented_methods(_PureStub) == frozenset()


def test_branch_level_raise_still_counts_as_implemented():
    """关键口径：`raise NotImplementedError` 出现在分支里，不等于没实现。"""
    assert support.stub_methods(_BranchStub) == frozenset()
    assert "get_board_spot" in support.implemented_methods(_BranchStub)


def test_empty_body_and_docstring_only_are_not_implemented():
    assert support.stub_methods(_EmptyBody) == {"get_cashflow", "get_balance_sheet"}


def test_method_with_real_body_but_partial_raise_is_implemented():
    assert "get_indicators" in support.implemented_methods(_Real)


def test_derived_stub_overrides_parent_implementation():
    """子类用占位覆盖真实现 → 必须判为未实现（取 MRO 上最派生的一份）。"""
    assert "get_income_statement" in support.implemented_methods(_Parent)
    assert "get_income_statement" in support.stub_methods(_ChildOverridesWithStub)


def test_property_and_private_names_are_not_treated_as_interface_methods():
    class _WithProperty:
        @property
        def name(self) -> str:
            return "x"

        def _internal(self):
            return 1

        def public_thing(self):
            return 2

    assert support.declared_methods(_WithProperty) == {"public_thing"}


def test_unsupported_helper_reports_missing_methods():
    missing = support.unsupported(["get_news", "get_fundamentals"], _PureStub)
    assert missing == {"get_news", "get_fundamentals"}
    assert support.unsupported(["get_news"], _BranchStub) == {"get_news"}


def test_abstract_base_methods_count_as_stubs():
    """基类抽象方法体本身就是 `raise NotImplementedError`：
    没覆盖它的子类必须被判为"未实现"，否则空壳 provider 会被当成能力提供者。"""
    stubs = support.stub_methods(BaseMarketDataProvider)
    assert "get_fundamentals" in stubs
    assert "get_news" in stubs
    assert support.implemented_methods(BaseMarketDataProvider) == frozenset()
