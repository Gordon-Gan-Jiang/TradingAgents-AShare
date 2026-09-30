from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pandas as pd

from ..trade_calendar import cn_today_str
from ._http_utils import HttpRetry, http_get_json, http_get_text
from .cn_market_only_base import CnMarketOnlyProvider
from .errors import SourceEmpty, SourceUnavailable, SourceUnsupported

_SINA_BOARD_FLOW_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "MoneyFlow.ssl_bkzj_bk"
)
_SINA_BOARD_FLOW_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "http://vip.stock.finance.sina.com.cn/moneyflow/",
    "Accept": "application/json,text/plain,*/*",
    "Connection": "close",
}

# ── 新浪行业板块快照（东财 spot 的兜底源）─────────────────────────────
#
# 实测（2026-09-23，本机）：49 个板块；单次 1.1–3.5s（冷启动 3.5s，之后 1.1–1.8s）。
# 同一台机器上东财的 `stock_board_industry_spot_em` 不通——走系统代理是
# ProxyError、强制直连是 RemoteDisconnected（见 `_http_utils.resolve_proxies`
# 的实测注记），所以板块这一维度需要一个非东财的源。
# 端点返回的不是 JSON 而是 `var S_Finance_bankuai_sinaindustry = {...};`，
# 值的部分是以逗号拼接的 13 字段串，所以走 `http_get_text` 后自己切出 `{...}`。
_SINA_BOARD_SPOT_URL = "http://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
_SINA_BOARD_SPOT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "http://finance.sina.com.cn/stock/sl/",
    "Connection": "close",
}
_SINA_BOARD_SPOT_RETRY = HttpRetry(attempts=3, base_sleep_s=0.5, timeout_s=10.0)

# 字段顺序**就是**契约（与 akshare `stock_sector_spot` 的列名逐字一致，
# 这样就能复用东财那套 `_normalize_board_spot_df`，不必再抄一份列名映射）。
#
# 为什么把 13 个名字写死而不是"按位置硬编索引"：这个串没有表头，
# 位置一旦错一位，就会把"公司家数"当成"平均价格"——**两边都是数字，
# 错位从数值上完全看不出来**。写出名字至少让映射可被 review，
# 并且 `len(fields) != len(FIELDS)` 时能整行拒绝。
_SINA_BOARD_SPOT_FIELDS = (
    "label",
    "板块",
    "公司家数",
    "平均价格",
    "涨跌额",
    "涨跌幅",
    "总成交量",
    "总成交额",
    "股票代码",
    "个股-涨跌幅",
    "个股-当前价",
    "个股-涨跌额",
    "股票名称",
)

# 东财列契约里有、而新浪行业快照**没有**的字段。工具 docstring 承诺了换手率与
# 涨跌家数，所以渲染时必须把这几个名字**说出来**：不声不响地少几列，读的人只会
# 以为"这些板块今天没有这些数"——这正是"拿不到 ≠ 没有"要防的那类误读。
_SINA_BOARD_SPOT_NOT_COVERED = ("换手率", "上涨家数", "下跌家数", "总市值", "净流入")

# 本 provider 的板块快照只覆盖行业。理由不是"嫌麻烦"，而是**分类体系不同源**：
# 新浪的"概念"（newFLJK.php?param=class）与东财的 400 多个概念板块不是同一套
# 划分，互换会让"CPO概念"这种名字在两边指向完全不同的成分股。
# 这与 `fetch_board_spot_df_chain` 的既有裁决同一条（那里也只给 industry 落新浪）。
_SINA_BOARD_SPOT_SECTOR_TYPES = ("industry",)


def _num_or_nan(v: Any) -> float:
    """数值转换；取不到返回 NaN（**不是 0**，见审计 §4.12）。

    这里用 NaN 而不是 None，是因为调用方会把这些值拼进 DataFrame 做排序/求和，
    NaN 会被 pandas 自动跳过，而 0 会被当成一个真实的观测值参与计算。
    """
    if v is None or isinstance(v, bool):
        return float("nan")
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def fetch_sina_industry_board_fund_flow_df() -> pd.DataFrame:
    """Fetch Sina industry board fund-flow rankings (fenlei=0).

    Stable fallback when Eastmoney push2 endpoints are blocked or reset connections.
    """
    params = {
        "page": 1,
        "num": 100,
        "sort": "netamount",
        "asc": 0,
        "fenlei": 0,
    }
    obj = http_get_json(
        _SINA_BOARD_FLOW_URL,
        params=params,
        headers=_SINA_BOARD_FLOW_HEADERS,
        retry=HttpRetry(attempts=4, base_sleep_s=0.5, timeout_s=10.0),
    )
    if not isinstance(obj, list) or not obj:
        raise SourceEmpty("sina board fund flow empty")

    rows: list[dict[str, Any]] = []
    for item in obj:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        # `float(x or 0)` 会把「字段缺失」写成「净流入恰好为 0」（审计 §4.12）。
        # 取不到就是 NaN，让 pandas 的聚合/排名天然忽略它，而不是把它当成一个
        # 真实观测值参与排序。
        rows.append(
            {
                "名称": name,
                "今日涨跌幅": _num_or_nan(item.get("avg_changeratio")),
                "今日主力净流入-净额": _num_or_nan(item.get("netamount")),
                "今日主力净流入-净占比": _num_or_nan(item.get("ratioamount")),
            }
        )
    if not rows:
        raise SourceUnavailable("sina board fund flow parse failed")
    return pd.DataFrame(rows)


@dataclass(frozen=True)
class _MoneyflowRow:
    date: str  # YYYY-MM-DD
    # `None` = 该字段本次取不到（**不是 0**，见审计 §4.12）
    main_net_yuan: float | None
    total_net_yuan: float | None


def _parse_sina_board_spot_payload(text: str) -> pd.DataFrame:
    """把 `var S_Finance_bankuai_sinaindustry = {...};` 解析成**新浪列名**的原始帧。

    解析失败一律抛 `NotImplementedError`（与 `fetch_sina_industry_board_fund_flow_df`
    的既有约定一致）：这个函数返回空表会被下游当成"今天没有板块"，
    而真相是"这次没拿到"——路由层需要的是一个异常，好让它去试下一个源。
    """
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise SourceUnavailable("sina industry board spot payload has no object")
    try:
        obj = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise SourceUnavailable(f"sina industry board spot not parsable: {exc}") from exc
    if not isinstance(obj, dict) or not obj:
        raise SourceEmpty("sina industry board spot empty")

    rows: list[dict[str, str]] = []
    for value in obj.values():
        fields = str(value).split(",")
        # 字段数不符 = 位置契约已不成立（上游改了字段）。整行丢弃而不是硬塞进列里：
        # 错位的一行会让"公司家数"冒充"平均价格"，比少一行危险得多。
        # 若上游整体改版，这里会一行都不剩，于是上面的 empty 检查抛出——
        # 那正是我们要的"响亮的失败"，路由会去试东财。
        if len(fields) != len(_SINA_BOARD_SPOT_FIELDS):
            continue
        rows.append(dict(zip(_SINA_BOARD_SPOT_FIELDS, fields)))
    if not rows:
        raise SourceUnavailable("sina industry board spot parse failed")
    return pd.DataFrame(rows)


def fetch_sina_industry_board_spot_df(sector_type: str = "industry") -> pd.DataFrame:
    """抓新浪行业板块快照（对应东财 `stock_board_industry_spot_em`），归一化。

    返回的列**就是东财那套契约**（`name` / `code` / `latest` / `chg_1d` / `leader` /
    `leader_chg` …）——直接复用 `_normalize_board_spot_df`，而不是在本地再写一份
    映射：列名一旦分成两份，下游（规则层 `mainline_scoring` 按列名取值）就会
    "同一个工具在不同源背后给出不同列名"，且只有真打到那个源时才会暴露。

    `sector_type` 只支持能真正映射到新浪接口的取值：
    - `industry`：新浪行业（`newSinaHy.php`，实测 49 个板块）。
    - `concept`：**未覆盖**，抛 `NotImplementedError` 让路由交给东财
      （新浪概念与东财概念不同源，见 `_SINA_BOARD_SPOT_SECTOR_TYPES` 处的说明）。
    - 其它取值：抛 `ValueError`，与东财 `fetch_board_spot_df` 同一个契约。

    **不支持的取值一律抛错，绝不按默认值取数**：静默取一份 industry 数据回去，
    调用方会以为拿到的是自己要的那个板块集合（这正是本仓库"容错是止血，不是治疗"
    那条例子的形状）。
    """
    if sector_type not in ("industry", "concept"):
        raise ValueError(
            f"Unknown sector_type={sector_type!r}, expected 'industry' or 'concept'"
        )
    if sector_type not in _SINA_BOARD_SPOT_SECTOR_TYPES:
        raise SourceUnsupported(
            f"cn_sina_moneyflow 未覆盖 sector_type={sector_type!r}："
            f"新浪板块快照只提供行业（{_SINA_BOARD_SPOT_URL}，实测 49 个板块）；"
            "概念板块的划分与东财不同源、不可互换，交由下一个数据源处理"
        )

    text = http_get_text(
        _SINA_BOARD_SPOT_URL,
        headers=_SINA_BOARD_SPOT_HEADERS,
        retry=_SINA_BOARD_SPOT_RETRY,
    )
    raw = _parse_sina_board_spot_payload(text)

    # 延迟导入：`cn_akshare_provider` 是可选/较重的一环（注册表对它的 import
    # 都包了 try/except）。放在函数里，Sina 这条链路的可用性就不受它影响。
    from tradingagents.dataflows.providers.cn_akshare_provider import _normalize_board_spot_df

    return _normalize_board_spot_df(raw)


def _sina_board_spot_leaders(rows: pd.DataFrame) -> list[str]:
    """渲染"领涨股"说明行用：只取真有名字的行。"""
    leaders: list[str] = []
    for _, row in rows.iterrows():
        name = str(row.get("name") or "").strip()
        leader = str(row.get("leader") or "").strip()
        if name and leader and leader.lower() != "nan":
            leaders.append(f"{name}={leader}")
    return leaders


def format_sina_board_spot_ranking(
    df: pd.DataFrame,
    *,
    sector_type: str = "industry",
    top_n: int = 20,
    snapshot_date: str | None = None,
) -> str:
    """把新浪板块快照渲染成**能被证据抽取器吃下**的文本。

    与东财 `format_board_spot_ranking` 的渲染方式一致（`排名` 列 + 定宽表格 +
    `数据截止 YYYY-MM-DD` 锚点 + `TopN/共N个板块` 标题），但有一处刻意不同：

    **表格里只放数值列**。抽取器（`tools/data.py::_evidence_items_from_ranked_whitespace`）
    的自证规则要求"名称之后每个字段都是数字"，而东财的表格里夹着 `leader`
    （股票名）这种非数字中间列，于是按 Phase 3.2 的裁定东财路径产不出任何证据
    （`tests/test_table_evidence.py` 守着这条）。这条路没必要重复那个缺口：
    领涨股改走表格之外的一行说明文本，数值列就能整块通过校验。

    表头用**人话**（板块/涨跌幅/领涨股涨跌幅）而不是内部代号（`name`/`chg_1d`）：
    抽取器逐字取表头当字段名，而字段名会出现在用户看到的证据清单里
    （`grounding/renderer.py`）。帧的列契约仍是东财那套（见 fetch 函数），
    代号只活在代码里，不活给用户看。
    """
    label = "行业" if sector_type == "industry" else "概念"
    if df is None or df.empty:
        return f"{label}板块涨幅榜数据暂不可用。"

    # 按涨跌幅降序（东财路径同款）。NaN 排在最后，由下面的 dropna 收掉。
    sub = df.sort_values("chg_1d", ascending=False).head(top_n).reset_index(drop=True)

    display = [("chg_1d", "涨跌幅"), ("leader_chg", "领涨股涨跌幅")]
    display = [(column, name) for column, name in display if column in sub.columns]
    if not display:
        return f"{label}板块涨幅榜数据暂不可用（新浪返回 {len(df)} 个板块，但涨跌幅字段缺失）。"

    # 数值缺失的行不能进表：抽取器的自证规则是"整块要么全对要么全放弃"，
    # 一个 `NaN` 会让**整块证据**消失（"宁可少证据，不要错位证据"）。
    # 剔掉它们，并在下面如实说明剔了几行——让证据消失得无声无息才是问题。
    kept = sub.dropna(subset=[column for column, _ in display]).reset_index(drop=True)
    dropped = len(sub) - len(kept)
    if kept.empty:
        return (
            f"{label}板块涨幅榜数据暂不可用"
            f"（新浪返回 {len(df)} 个板块，但涨跌幅字段无法解析）。"
        )

    table = pd.DataFrame({"板块": kept["name"].astype(str)})
    for column, name in display:
        table[name] = kept[column]
    table.insert(0, "排名", range(1, len(table) + 1))

    anchor = snapshot_date or cn_today_str()
    lines = [
        f"{label}板块涨幅榜 Top{top_n}（数据截止 {anchor}，共{len(df)}个板块，"
        f"数据源：新浪行业）：",
        table.to_string(index=False),
    ]
    leaders = _sina_board_spot_leaders(kept)
    if leaders:
        # 领涨股是**非数值**的，进不了上面的表格（进不去就等于没有逐值证据）。
        # 如实标注这一点，而不是让它看起来像一条可引用的数值证据。
        lines.append("领涨股（说明性文本，未抽成数值证据）：" + "、".join(leaders))
    notes = [
        f"注：新浪行业源不提供 {'/'.join(_SINA_BOARD_SPOT_NOT_COVERED)}"
        "（属「该源未覆盖」，不是「当日为 0」）。"
    ]
    if dropped:
        notes.append(f"另有 {dropped} 行因字段缺失未列出。")
    lines.append("".join(notes))
    return "\n".join(lines)


class CnSinaMoneyflowProvider(CnMarketOnlyProvider):
    """CN fund flow provider via Sina Finance public endpoint (free).

    Uses:
    - MoneyFlow.ssl_qsfx_zjlrqs: historical main net inflow series (r0_net) and total net (netamount).

    Notes:
    - Sina's response is valid JSON array (as of now), so parsing is stable.
    - This provider focuses on "主力净流向" trend (近5日) for Smart Money analyst.
    """

    RETRY = HttpRetry(attempts=4, base_sleep_s=0.5, timeout_s=10.0)

    @property
    def name(self) -> str:
        return "cn_sina_moneyflow"

    # ── Helpers ──
    @staticmethod
    def _normalize(symbol: str) -> str:
        import re

        m = re.search(r"(\d{6})", (symbol or "").strip())
        if not m:
            raise SourceUnsupported(f"cn_sina_moneyflow only supports 6-digit symbols, got: {symbol}")
        code = m.group(1)
        prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
        return f"{prefix}{code}"

    @staticmethod
    def _safe_float(v: Any) -> float | None:
        """数值转换；**取不到时返回 None，不返回 0.0**（审计 §4.12）。

        同步修掉 `fetch_sina_industry_board_fund_flow_df` 里的同形状写法
        ——那里更直接：`float(item.get("netamount") or 0)`，字段缺失直接变 0。
        """
        if v is None or isinstance(v, bool):
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return None if f != f else f  # NaN 也是"取不到"

    def _fetch_rows(self, symbol: str) -> list[_MoneyflowRow]:
        code = self._normalize(symbol)
        url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "MoneyFlow.ssl_qsfx_zjlrqs"
        )
        params = {"page": 1, "num": 5, "sort": "opendate", "asc": 0, "daima": code}
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://vip.stock.finance.sina.com.cn/",
            "Accept": "application/json,text/plain,*/*",
            "Connection": "close",
        }
        obj = http_get_json(url, params=params, headers=headers, retry=self.RETRY)
        if not isinstance(obj, list) or not obj:
            raise SourceEmpty(f"sina moneyflow empty for {symbol}")

        rows: list[_MoneyflowRow] = []
        for it in obj:
            if not isinstance(it, dict):
                continue
            d = str(it.get("opendate") or "").strip()
            if not d:
                continue
            rows.append(
                _MoneyflowRow(
                    date=d,
                    main_net_yuan=self._safe_float(it.get("r0_net")),
                    total_net_yuan=self._safe_float(it.get("netamount")),
                )
            )
        if not rows:
            raise SourceUnavailable(f"sina moneyflow parse failed for {symbol}")
        return rows

    # ── CN market tool ──
    def get_individual_fund_flow(self, symbol: str) -> str:
        """Return last 5 trading days main net inflow series.

        Sina fields:
        - opendate: YYYY-MM-DD
        - r0_net: 主力净流入（元）
        - netamount: 全部净流入（元）
        """
        rows = self._fetch_rows(symbol)
        # API is typically desc by date; we show asc for readability.
        rows_sorted = sorted(rows, key=lambda r: r.date)
        cutoff = rows_sorted[-1].date

        header = "日期 主力净流向(亿元) 全部净流向(亿元)"
        lines = [header]
        for r in rows_sorted:
            lines.append(f"{r.date[5:]} {r.main_net_yuan/1e8:>10.2f} {r.total_net_yuan/1e8:>12.2f}")
        return f"{symbol} 近5日主力资金净流向（数据截止 {cutoff}）：\n" + "\n".join(lines)

    def get_board_fund_flow(self) -> str:
        """Return today's industry board fund-flow ranking via Sina."""
        from tradingagents.dataflows.providers.cn_akshare_provider import format_board_fund_flow_ranking

        df = fetch_sina_industry_board_fund_flow_df()
        return format_board_fund_flow_ranking(df)

    # ── CN 板块工具（东财被封时的兜底源）──────────────────────────────
    #
    # 背景（实测 2026-09-23）：东财那条路在本机不通
    # （走系统代理 ProxyError、强制直连 RemoteDisconnected），而 `cn_board_data` 原本只配了
    # 东财一个源，于是板块快照/排行/历史一起变成"数据源故障"。同一台机器上
    # `cn_market_data` 因为配了新浪优先，`get_board_fund_flow` 一直有数——
    # 缺的不是"能不能拿到"，而是"这条链路有没有接上"。
    #
    # 三个工具里新浪只覆盖得了**快照**：快照有 49 个行业板块的实测数据，
    # 排行缺周期维度、历史压根没有源。覆盖不到的就在这里显式拒绝，
    # 让路由继续去找东财，而不是编一份数据出来。

    def get_board_spot(self, sector_type: str = "industry") -> str:
        """行业板块涨幅榜（新浪行业，实测 49 个板块），东财 `stock_board_industry_spot_em` 的兜底。

        两处刻意的设计：

        1. **失败不 catch**。`route_to_vendor` 只在**抛异常**时才去试下一个源；
           供应商自己吞掉异常、返回一句"获取失败"，路由会把它当成成功，
           于是东财那条兜底永远轮不到（与 `get_board_fund_flow` 的既有写法一致）。
        2. 未覆盖的 `sector_type` 由 fetch 层抛错（`concept` → 未覆盖，
           其它取值 → 与东财同契约的 `ValueError`），**绝不按默认值取数**。
        """
        df = fetch_sina_industry_board_spot_df(sector_type)
        return format_sina_board_spot_ranking(df, sector_type=sector_type)

    def get_board_rank(self, sector_type: str = "industry", period: str = "1d") -> str:
        """**未覆盖**：新浪的板块资金流没有多周期维度，故整条工具都不接。

        为什么连"只做 1d × industry"都不做（那是能直接映射的）：

        - 新浪的板块资金流端点（`MoneyFlow.ssl_bkzj_bk`）只有**当日**一个截面，
          没有 3/5/10 日维度，兑现不了 `get_board_rank` 的 `period` 契约。
        - 这同一个截面**已经**能通过 `get_board_fund_flow` 在这条链路上拿到
          （`cn_market_data` 已配新浪优先）。所以东财被封时这个数据并没有缺口，
          再实现一遍只是把同一份截面挂到第二个工具名下。
        - 更麻烦的是语义漂移：真这么做之后，`get_board_rank` 会**随背后源不同
          而含义不同**（东财：行业+概念 × 4 个周期；新浪：仅行业 · 仅当日），
          而调用方（规划器）从工具签名上看不出这个差别。

        按"宁可少支持一个工具，也不许编数据"：显式声明未覆盖，路由继续找东财。
        """
        raise SourceUnsupported(
            "cn_sina_moneyflow 未覆盖 get_board_rank：新浪板块资金流只有「当日」一个"
            f"截面（MoneyFlow.ssl_bkzj_bk），没有 3/5/10 日维度，无法兑现 period={period!r} 契约；"
            "同期同数据请走 get_board_fund_flow。交由下一个数据源处理。"
        )

    def get_board_hist(self, board_name: str, sector_type: str = "industry") -> str:
        """**未覆盖**：新浪不提供板块日线历史（个股日线有，板块没有）。

        不许拿个股行情或别的板块的日线冒充：板块历史的成分口径与个股完全不同，
        混用会让"板块历史走势"这句话背后的东西随源改变。板块历史当前只有东财一个源。
        """
        raise SourceUnsupported(
            f"cn_sina_moneyflow 未覆盖 get_board_hist：新浪不提供板块日线历史"
            f"（「{board_name}」/{sector_type}）；板块历史当前只有东财一个源。"
        )

