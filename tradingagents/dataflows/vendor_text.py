"""vendor 返回的**文本**到底是数据、还是「没数据」、还是「失败」——唯一权威判断。

## 为什么必须只有一处

同一个判断曾被写了两遍，而且**各自只认识一半文案**：

* `tradingagents/tools/data.py:_VENDOR_FAILURE_MARKERS`（工具层）：
  认识中文的「调用失败 / 暂无数据 / 暂不可用」与少数英文；
* `api/services/data_source_probe_service.py:_EMPTY_MARKERS`（探测层）：
  认识 `no data found`。

于是 `cn_akshare_provider.py:953` **真实返回**的
``No data found for symbol '600519.SH' between 2026-01-01 and 2026-03-01``
在探测层被正确记成「降级/空」，在工具层却被当成**有效数据**交给模型。

这不是"某个关键词忘了加"，而是**同一语义有两份实现**必然产生的漂移：补一处，
另一处照旧漏。所以本模块是唯一实现，工具层与探测层都改为**委托**到这里
（引擎层不能 import 应用层，故权威实现必须放在 `dataflows/`）。

## 三分类的语义（与 `providers/errors.py` 对齐）

* ``data``    —— 有内容，可用；
* ``empty``   —— 源是**好的**，只是这次没数据（停牌 / 非交易日 / 上游为空）。
  对应 `SourceEmpty`：不该触发"换源重试"式的告警，但**必须**降级声明；
* ``failure`` —— 源**坏了**（连不上 / 解不出 / 上游改版 / 把异常吞成文案返回）。
  对应 `SourceUnavailable`：要换源、要留运维信号。

把 ``empty`` 与 ``failure`` 混为一谈，就会出现两种相反的谎：把"源挂了"说成
"今天没数据"（用户以为正常），或把"今天没数据"说成"源坏了"（运维被误导）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

VendorTextKind = Literal["data", "empty", "failure"]

#: 「源是好的，只是没数据」的文案标记。
#:
#: 判据逐条来自 provider 的**实际返回**（`grep -n 'return f"No' providers/`），
#: 不是猜的：
#:   * `cn_akshare_provider.py:953`  ``No data found for symbol '...' between A and B``
#:   * `cn_akshare_provider.py:1182` ``No data found for ... for indicator ...``
#:   * `cn_akshare_provider.py:1378,1388` ``No news found for ...``
#:   * `cn_baostock_provider.py:313,423`、`y_finance.py:30`、`yfinance_news.py:70,97`
#:   * `alpha_vantage_indicator.py:155` ``Error: No data returned for ...``
#:   * `alpha_vantage_indicator.py:209` ``No data available for the specified date range.``
#:   * `stockstats_utils.py:93`、`trade_calendar.py:151`、`y_finance.py:307`
#:     ``N/A：该日期暂无数据（可能未收盘、数据延迟或非交易日）``
VENDOR_EMPTY_MARKERS = (
    "no data found",
    "no news found",
    "no data returned",
    "no data available",
    # 「源的能力不足以覆盖请求窗口」的显式文案（`cn_akshare_provider.get_news`）。
    # 它表达的是**不知道**，而不是"没有"；必须被认成"拿不到"，
    # 否则「无法判断该窗口是否有新闻」会被当成一条可用信息。
    "【窗口覆盖不足】",
    "窗口覆盖不足",
    "数据暂不可用",
    "暂无数据",
    "无数据",
    "未获取到",
    "暂不可用",
    "暂时不可用",
)

#: 「源坏了」的文案标记：provider 把异常**吞成一句失败文案再正常 return** 的痕迹。
#: 实测 `cn_akshare` 有 20+ 处这样写，例如
#:   ``return f"板块历史行情获取失败（无替代源）：{type(exc).__name__}: {exc}"``
#: 这类返回值**不是异常**，按"非空文本 = 有数据"处理就会把一个**失败**记成可用。
VENDOR_FAILURE_MARKERS = (
    "调用失败",
    "获取失败",
    "查询失败",
    "解析失败",
    "数据获取异常",
    "取数失败",
    # `data_collector._safe` 兜住异常后返回值的统一前缀（见该模块
    # `DATA_FETCH_FAILURE_NOTICE`）。登记在这里，是为了这类值无论流向哪一层
    # 都会被认成"没有数据"，而不是被当成内容。
    "【取数失败·本项无数据】",
    # 以下三条原在工具层的标记表里，收敛时必须原样保留——删掉它们等于**减少**
    # 识别能力，那是把修复做成回归。
    "temporarily unavailable",
    "unavailable for",
    "all failed",
    "no available vendor",
    "traceback (most recent call last)",
    # `Error fetching` 是本仓库自己产生的前缀（`dataflows/yfinance_news.py`），
    # 不是供应商文案，所以它比任何猜测的关键词都可靠。
    # 实测（2026-09-22）`get_global_news` 返回 'Error fetching global news:
    # Too Many Requests...' 时，旧标记一个都不命中 → **限流被当成有效数据**
    # 交给模型解读。
    "error fetching",
    "too many requests",
    "rate limited",
    "try after a while",
)

#: 空 JSON 载荷（`{}` / `[]`）：结构合法但**内容为空**。
#: 判据刻意收得很窄——只有能解析成 JSON 且为空容器才算，散文解析会抛异常直接放过，
#: 因此不存在把正常文案误判的路径。
_JSON_EMPTY_RE = re.compile(r"^\s*[\[{][\s\S]*[\]}]\s*$")


def _normalise(rendered: Any) -> str:
    if isinstance(rendered, str):
        return rendered.strip()
    return ""


def _is_empty_json_payload(text: str) -> bool:
    if not text or not _JSON_EMPTY_RE.match(text):
        return False
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001 — 不是 JSON 就按散文处理
        return False
    return isinstance(parsed, (dict, list)) and not parsed


def looks_like_empty_text(rendered: Any) -> bool:
    """是否为「源没坏、只是没数据」的文案。"""
    text = _normalise(rendered)
    if not text:
        return False
    lowered = text.lower()
    if any(marker in lowered for marker in VENDOR_EMPTY_MARKERS):
        return True
    return _is_empty_json_payload(text)


def looks_like_failure_text(rendered: Any) -> bool:
    """是否为「源坏了」的文案。"""
    text = _normalise(rendered)
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in VENDOR_FAILURE_MARKERS)


def classify_vendor_text(rendered: Any) -> VendorTextKind:
    """把 vendor 返回值分成 数据 / 空 / 失败 三类。

    **非字符串返回值一律是 ``data``**：`[]`、空 DataFrame 都是合法结果形状，
    由各自的消费方判断行数；路由层若把它们判成失败，一个返回空列表的 provider
    会把整条链拖成异常——那是把"兼容空结果"改成"兼容不了空结果"。
    """
    if not isinstance(rendered, str):
        return "data"
    text = rendered.strip()
    if not text:
        # 空字符串：没拿到任何东西。归入 empty（源没坏，只是这次没有）。
        return "empty"
    # **failure 必须先判**：`VENDOR_EMPTY_MARKERS` 里有 `"无数据"`/`"暂无数据"`/
    # `"暂不可用"` 这类极泛的子串，而显式失败文案经常同时含它们——例如
    # `data_collector.py:256` 的 `DATA_FETCH_FAILURE_NOTICE` 原文就是
    # `"【取数失败·本项无数据】"`，而 `_safe` 在**任何工具异常**时都会产出它。
    # 旧顺序（empty 先）把这条真实、高频的取数失败判成 empty「源是好的，只是没数据」，
    # 于是在 `interface.py` 里记成 `status=empty` 而非 `status=fallback`，
    # 数据源探针把一次失败报告成正常。失败措辞是更具体、更可诉的主张；
    # 把失败说成"良性为空"是 fail-open 方向。
    if looks_like_failure_text(text):
        return "failure"
    if looks_like_empty_text(text):
        return "empty"
    return "data"


def looks_like_vendor_failure(rendered: Any) -> bool:
    """**兼容入口**：这个返回值是否「不能当数据用」。

    保留 `tools/data.py` 的原始语义：非字符串（含 `None`）与空文本也算「不能用」，
    因为工具层拿它来判断"该源这次到底有没有交付"。
    """
    if not isinstance(rendered, str):
        return True
    if not rendered.strip():
        return True
    return looks_like_empty_text(rendered) or looks_like_failure_text(rendered)


__all__ = [
    "VENDOR_EMPTY_MARKERS",
    "VENDOR_FAILURE_MARKERS",
    "VendorTextKind",
    "classify_vendor_text",
    "looks_like_empty_text",
    "looks_like_failure_text",
    "looks_like_vendor_failure",
]
