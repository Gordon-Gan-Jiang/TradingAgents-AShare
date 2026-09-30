"""失败语义三分类的**戒律测试**。

## 这个文件为什么存在

事故：`cn_baostock` 拨上游已废弃的主机名，登录挂死 75 秒后失败，而它当时的写法是
`raise NotImplementedError("baostock login failed: ...")`——一次**上游主机名迁移**
在系统里被记录成"这个 provider 没实现该功能"，永远不会有人去查。

修好 baostock 之后，同样的毛病在**其它 provider 里还在**（审计发现 45 处
`raise NotImplementedError`，其中相当一部分表达的是"连不上/解析失败/取到空"）。
所以这里既测运行时语义，也用**静态扫描**把"别再这么写"钉住：

* 结构性不支持（该源就没这个能力 / 刻意未覆盖 / 取值超范围）→ `SourceUnsupported`
* 瞬时故障（网络、超时、上游改版、解析失败、依赖缺失）→ `SourceUnavailable`（可重试）
* 调用成功但没数据（停牌、非交易日、上游确实返回空）→ `SourceEmpty`

三者**必须可区分**：`route_to_vendor` 对 `SourceUnsupported` 记 `status=skip`（配置问题），
对其它失败记 `status=fallback`（真故障）。混在一起就等于没有运维信号。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tradingagents.dataflows.providers.errors import (
    SourceEmpty,
    SourceError,
    SourceUnavailable,
    SourceUnsupported,
)

PROVIDERS_DIR = Path("tradingagents/dataflows/providers")

#: 允许用 `raise NotImplementedError` 的地方——**空集合是目标**，
#: 但只要还有存量就逐条列白名单，并在注释里写清为什么它是合理的。
#: 白名单为空 = 全项目都已用上有语义的异常类型。
_ALLOWED_NOT_IMPLEMENTED: dict[str, set[int]] = {}


# --------------------------------------------------------------- 类型关系


def test_source_unsupported_stays_notimplementederror_compatible():
    """`SourceUnsupported` 必须仍是 `NotImplementedError`。

    既有代码（与 provider 的占位实现）到处用后者表达"不支持"，且
    `support.py` 的占位检测靠异常名判断"这个方法没实现"。断掉这个继承
    会让空壳 provider 被当成能力提供者。
    """
    assert issubclass(SourceUnsupported, NotImplementedError)
    assert issubclass(SourceUnsupported, SourceError)


def test_unavailable_and_empty_are_not_notimplementederror():
    """`SourceUnavailable` / `SourceEmpty` **不能**是 `NotImplementedError`。

    这是整个修复的要点：连不上 ≠ 没实现。如果它们也能被
    `except NotImplementedError` 接住，"上游挂了"就会重新伪装成"功能没做"。
    """
    assert not issubclass(SourceUnavailable, NotImplementedError)
    assert not issubclass(SourceEmpty, NotImplementedError)


def test_each_kind_has_a_distinct_label():
    labels = {SourceUnsupported.kind, SourceUnavailable.kind, SourceEmpty.kind}
    assert len(labels) == 3, f"三类语义必须有不同标签，实际：{labels}"
    assert labels == {"unsupported", "unavailable", "empty"}


def test_all_kinds_are_source_errors_so_the_router_can_bucket_them():
    for cls in (SourceUnsupported, SourceUnavailable, SourceEmpty):
        assert issubclass(cls, SourceError)


# ----------------------------------------------------- 静态扫描：别写回去


def _provider_files() -> list[Path]:
    return sorted(p for p in PROVIDERS_DIR.glob("*.py") if p.name != "support.py")


def _raised_message(call: ast.Call) -> str:
    """取 `raise X(...)` 的消息文本，**必须能处理 f-string**。

    `ast.literal_eval` 对 `JoinedStr`（f-string）会抛错，早先版本吞掉这个异常后
    消息变成空串，导致检查被静默跳过——守卫测试因此永远不会失败。
    这里退回 `ast.unparse`（会渲染出字面量部分，足以匹配关键词）。
    """
    if not call.args:
        return ""
    arg = call.args[0]
    try:
        value = ast.literal_eval(arg)
        if isinstance(value, str):
            return value
    except Exception:
        pass
    try:
        return ast.unparse(arg)
    except Exception:
        return ""


#: 消息里出现这些词，说明讲的是瞬时故障或空数据，不能用「没实现」的类型。
_TRANSIENT_MARKERS = re.compile(
    r"unavailable|timeout|timed out|connect|refused|reset|empty|parse failed|"
    r"not found|temporarily|失败|超时|连不上|解析失败|为空",
    re.IGNORECASE,
)


def _scan_transient_notimplementederror(
    source: str, *, allowed: set[int] | frozenset[int] = frozenset()
) -> list[str]:
    """在一段源码里找「用 NotImplementedError 表达瞬时故障」的 raise。

    抽成独立函数是为了能被**合成样本**测试：侦探本身必须先被证明会响，
    否则在干净的代码库上它永远是绿的，等于没有。
    """
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        call = node.exc
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "NotImplementedError":
            continue
        if node.lineno in allowed:
            continue
        message = _raised_message(call)
        if message and _TRANSIENT_MARKERS.search(message):
            offenders.append(f"{node.lineno}: {message[:80]}")
    return offenders


def test_the_scanner_actually_detects_a_planted_offender():
    """**防空转**：先证明侦探会响，再拿它去证明代码干净。

    这条测试是上一版的血泪：早先用 `ast.literal_eval` 取消息，遇到 f-string
    静默返回空串，检查被跳过，于是"把回归注入回去测试照样绿"。
    """
    plant = '''\
def f(symbol):
    raise NotImplementedError(f"source is temporarily unavailable for {symbol}")
'''
    assert _scan_transient_notimplementederror(plant), "f-string 形式的回归没被抓到"

    plant_plain = '''\
def f():
    raise NotImplementedError("sina moneyflow empty")
'''
    assert _scan_transient_notimplementederror(plant_plain), "普通字符串形式的回归没被抓到"


def test_the_scanner_does_not_flag_legitimate_structural_gaps():
    """反面：结构性不支持的措辞**不应**被误报，否则守卫会逼人写错东西。"""
    ok = '''\
def f():
    raise NotImplementedError("akshare has no supported API: stock_xyz")
'''
    assert _scan_transient_notimplementederror(ok) == []


def test_no_provider_expresses_unavailability_as_notimplementederror():
    """**戒律**：不许再用裸 `NotImplementedError` 表达「连不上/取到空」。

    审计时全项目有 45 处 `raise NotImplementedError`，其中相当一部分讲的是
    瞬时故障（"temporarily unavailable"、"empty"、"parse failed"）或结构性范围限制，
    全部已改成语义正确的类型；这条测试防止有人写回去。
    """
    offenders: list[str] = []
    for path in _provider_files():
        source = path.read_text(encoding="utf-8")
        allowed = _ALLOWED_NOT_IMPLEMENTED.get(path.name, set())
        for hit in _scan_transient_notimplementederror(source, allowed=allowed):
            offenders.append(f"{path.name}:{hit}")
    assert not offenders, (
        "这些地方把「连不上/取到空」写成了 NotImplementedError（应改用 "
        "SourceUnavailable / SourceEmpty）：\n  " + "\n  ".join(offenders)
    )


def test_providers_that_raise_source_errors_import_them():
    """用了新异常类型就必须 import——否则是运行期 NameError（改完才炸）。"""
    for path in _provider_files():
        source = path.read_text(encoding="utf-8")
        used = set(re.findall(r"\braise (Source\w+)\(", source))
        if not used:
            continue
        declared = set()
        m = re.search(r"from \.errors import ([^\n]+)", source)
        if m:
            declared = {part.strip() for part in m.group(1).split(",")}
        missing = used - declared
        assert not missing, f"{path.name} 用了 {missing} 但没有 import"


def test_market_only_base_declares_unsupported_with_the_right_type():
    """CN 市场专属基类的默认实现是「结构性不支持」，不是「临时故障」。"""
    import inspect

    from tradingagents.dataflows.providers.cn_market_only_base import (
        CnMarketOnlyProvider,
    )

    stubs = [
        name
        for name in ("get_news", "get_fundamentals", "get_indicators", "get_global_news")
        if name in CnMarketOnlyProvider.__dict__
    ]
    assert stubs, "基类应该声明这些未覆盖的方法"

    # 用一个**真实**的 CN 专属 provider 实例（基类是 ABC，需要子类提供 name 等）。
    from tradingagents.dataflows.providers.cn_sina_moneyflow_provider import (
        CnSinaMoneyflowProvider,
    )

    instance = CnSinaMoneyflowProvider()
    for name in stubs:
        func = CnMarketOnlyProvider.__dict__[name]
        # 按签名补足必需参数，只关心抛出的异常类型
        required = [
            p
            for p in inspect.signature(func).parameters.values()
            if p.name != "self"
            and p.default is inspect.Parameter.empty
            and p.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        args = [object()] * len(required)
        with pytest.raises(SourceUnsupported):
            func(instance, *args)


# ------------------------------------------------- 运行时：确实分得开


def test_akshare_unavailable_is_not_notimplementederror():
    """AKShare 三条行情后端全失败时，异常类型必须是"不可用"而不是"没实现"。

    这就是审计里最刺眼的一条：探测报告里原本写着
    `error_type: NotImplementedError` + 消息 `temporarily unavailable`——**自相矛盾**。
    """
    from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider

    provider = CnAkshareProvider()

    class _Boom:
        @staticmethod
        def stock_zh_a_hist(**kwargs):
            raise ConnectionError("Remote end closed connection without response")

        @staticmethod
        def stock_zh_a_daily(**kwargs):
            raise ConnectionError("Remote end closed connection without response")

        @staticmethod
        def stock_zh_a_hist_tx(**kwargs):
            raise ConnectionError("Remote end closed connection without response")

    import tradingagents.dataflows.providers.cn_akshare_provider as mod

    monkeypatch_provider = provider
    original = monkeypatch_provider._ak

    def _fake_ak():
        return _Boom

    monkeypatch_provider._ak = _fake_ak  # type: ignore[method-assign]
    try:
        with pytest.raises(SourceUnavailable) as excinfo:
            provider.get_daily_bars_df("600519.SH", "2026-09-01", "2026-09-23")
        assert not isinstance(excinfo.value, NotImplementedError), (
            "『后端全挂』被写成了『没实现』——这正是本次要消灭的混淆"
        )
        assert excinfo.value.kind == "unavailable"
    finally:
        monkeypatch_provider._ak = original  # type: ignore[method-assign]
