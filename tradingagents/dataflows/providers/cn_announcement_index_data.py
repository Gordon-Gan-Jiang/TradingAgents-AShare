"""A 股**个股公告**与**指数行情**的取数实现（L2 证据工具的数据源补齐）。

## 这两个能力为什么此前是空的

`tradingagents/tools/evidence.py` 里 `announcements_read` 有工具、有端口，
但**没有任何取数实现**——组合根刻意不传 `announcements_port`，于是它不在册；
指数行情连工具都没有。缺的从来不是工具，是**数据源**。本模块把数据源补上，
工具才有东西可包。

## 实测结论（本机，2026-09-22，akshare 1.18.30）

| 通道 | 实测 |
|---|---|
| `ak.stock_notice_report(symbol=<代码>)` | ❌ `KeyError`：`symbol` 是**公告类别**（全部/重大事项/…），不是股票代码 |
| `ak.stock_notice_report(symbol="全部", date=D)` | ✅ 但**全市场按日**返回 1385 行 / 36.9s，单日一拉，逐标的窗口查询要 7 次 = 4 分钟，不可用 |
| `ak.stock_zh_a_disclosure_report_cninfo(...)` | ❌ `JSONDecodeError`：akshare 用 `params=` 而非表单体、且不带 UA/Referer，巨潮回了非 JSON |
| `ak.stock_zh_a_disclosure_relation_cninfo(...)` | ❌ `KeyError`：返回空，列选择失败 |
| 东财 `np-anotice-stock/api/security/ann`（直连，带 `stock_list`） | ✅ 1.1–3.4s，**按个股** + 日期区间，带公告类型 |
| 巨潮 `hisAnnouncement/query`（直连，表单体 + UA/Referer + orgId） | ✅ 0.4–3.2s，**按个股** + 日期区间（权威披露源） |
| `ak.stock_zh_index_spot_sina()` | ✅ 7.8s，562 个指数，`涨跌幅` 已是 float 百分数，**无快照时间戳** |
| `ak.stock_zh_index_spot_em(symbol="沪深重要指数")` | ✅ 38.8s，43 行，含全部主要指数，**无快照时间戳** |
| 新浪 `hq.sinajs.cn/list=...`（直连） | ✅ **0.6s**，一次请求全量代码，**带快照日期+时间** |

## 多源回退的顺序与理由

* **公告**：东财 → 巨潮。东财优先是**因为它带公告类型**（`columns[].column_name`），
  而巨潮不传 `category` 时 `announcementTypeName` 是 `null`——「类型」是这条证据
  四个字段之一，能拿到就别丢。巨潮是权威披露源，作为兜底与交叉校验。
* **指数**：新浪直连 → 新浪 akshare → 东财 akshare。直连优先是因为**只有它带
  快照时间戳**：没有时间戳，「这数据什么时候的」就只能靠猜，而指数点位恰恰是
  最容易被错误地贴到某个历史交易日上的数字。

## 三条不可让步的约束

1. **取不到返回 `None`，绝不返回空帧**。空帧会让上层把「没拉到」读成
   「这标的没有公告」/「指数没动」——凭空造出一个结论。
2. **失败只记日志、不向上抛**。取数层的异常属于「某个源今天不灵」，不是对话
   故障；抛出去会把一次可降级的缺数据变成一次失败的回答。
3. **列名归一成内部契约**（`published_at/type/title/source/url`、
   `code/name/price/change_pct/...`）。上游列名（`announcementTitle`、`涨跌幅`）
   不得泄漏到工具层，否则换源就会悄悄改变工具读到的东西。

## 纯函数与 IO 分离

`announcements_from_em` / `announcements_from_cninfo` / `index_quotes_from_sina_hq`
/ `index_quotes_from_sina_spot` / `index_quotes_from_em_spot` 是**纯函数**（载荷
进、行列表出，不着网），IO 只在 `_announcement_payload_via_*` /
`_index_*_via_*` 这几个薄函数里。测试因此可以拿**实测抓下来的真实载荷**离线
断言归一结果，而不必构造网络环境。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Sequence

import pandas as pd
import requests

from ._http_utils import HttpRetry, http_get_json

logger = logging.getLogger(__name__)

# ── 对外契约 ──────────────────────────────────────────────────────────────

#: 公告帧的内部契约列。`source` 由 fetcher 按**实际命中的源**贴上（归一函数不猜）。
ANNOUNCEMENT_COLUMNS: tuple[str, ...] = ("published_at", "type", "title", "source", "url")

#: 指数行情帧的内部契约列。
#: `quote_date` 是快照日期，**拿不到就留空**（不填今天）——「不知道」必须能被看见。
INDEX_QUOTE_COLUMNS: tuple[str, ...] = (
    "code",
    "name",
    "price",
    "change",
    "change_pct",
    "prev_close",
    "quote_date",
    "source",
)

SOURCE_EM = "东方财富"
SOURCE_CNINFO = "巨潮资讯"
SOURCE_SINA = "新浪财经"

#: 默认指数：上证 / 深证成指 / 创业板指（至少覆盖沪深两个市场与创业板）。
DEFAULT_INDEX_CODES: tuple[str, ...] = ("sh000001", "sz399001", "sz399006")

DEFAULT_ANNOUNCEMENT_LIMIT = 10
MAX_ANNOUNCEMENT_LIMIT = 50
MAX_INDEX_CODES = 12

# 时间戳口径：A 股披露与行情一律北京时间。中国自 1991 年起无夏令时，
# 用固定 +08:00 而不引 zoneinfo，避免把「取数」变得依赖系统时区库。
_CN_TZ = timezone(timedelta(hours=8))

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_EM_ANN_HEADERS = {"User-Agent": _UA, "Referer": "https://data.eastmoney.com/notices/"}
_CNINFO_HEADERS = {"User-Agent": _UA, "Referer": "http://www.cninfo.com.cn/"}
# 新浪行情必须带 Referer，否则 403（实测）
_SINA_HQ_HEADERS = {"User-Agent": _UA, "Referer": "https://finance.sina.com.cn"}

_EM_ANN_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
_CNINFO_ANN_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
_CNINFO_STOCK_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"
_SINA_HQ_URL = "https://hq.sinajs.cn/list="

# hq.sinajs.cn 的**指数**字段布局（实测 34 段）：
#   0 名称 1 今开 2 昨收 3 最新价 4 最高 5 最低 8 成交量 9 成交额 30 日期 31 时间
_SINA_HQ_NAME = 0
_SINA_HQ_PREV_CLOSE = 2
_SINA_HQ_PRICE = 3
_SINA_HQ_DATE = 30
_SINA_HQ_QUOTE_RE = re.compile(r'var hq_str_([a-z]{2}\d{6})="([^"]*)"')


# ── 纯函数：载荷 → 内部契约行 ─────────────────────────────────────────────


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _date10(value: Any) -> str:
    """任意日期表示 → `YYYY-MM-DD`；认不出返回空串（**不猜**）。"""
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", _text(value))
    return match.group(0) if match else ""


def _num(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _from_epoch_ms(value: Any) -> str:
    """毫秒时间戳 → 北京时间日期。

    巨潮给的是 UTC 毫秒，且北京 00:00 = UTC 前一天 16:00——按 UTC 取日期会
    **整批差一天**，而公告日期是回答里最常被引用的断言，差一天就是编造日期。
    """
    number = _num(value)
    if number is None or number <= 0:
        return ""
    try:
        return datetime.fromtimestamp(number / 1000.0, tz=_CN_TZ).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def _dict_rows(payload: Any, *path: str) -> list[dict]:
    """按路径取字典行列表；形状不符一律返回空列表（**不抛**）。

    上游换个版本就换形状是常态，而「形状变了」在本层的正确表现是「这个源今天
    没给数据」，让回退继续走，而不是把整次取数炸掉。
    """
    node: Any = payload
    for key in path:
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    if not isinstance(node, list):
        return []
    return [item for item in node if isinstance(item, dict)]


def _em_short_name(item: dict) -> str:
    codes = item.get("codes")
    if isinstance(codes, list) and codes and isinstance(codes[0], dict):
        return _text(codes[0].get("short_name"))
    return ""


def _strip_name_prefix(title: str, short_name: str) -> str:
    """去掉东财加的「简称:」前缀。

    东财标题形如 `贵州茅台:贵州茅台关于召开…`，前缀是它的排版产物，不是标题的
    一部分。只在**前缀逐字等于简称**时剥离：标题里本来就有冒号的（「公告：…」）
    必须原样保留，擅自动刀等于篡改原文。
    """
    if not title or not short_name:
        return title
    for sep in (":", "："):
        if sep in title:
            head, _, tail = title.partition(sep)
            if head.strip() == short_name and tail.strip():
                return tail.strip()
            break
    return title


def announcements_from_em(payload: Any) -> list[dict]:
    """东财公告载荷 → 内部契约行（纯函数）。"""
    rows: list[dict] = []
    for item in _dict_rows(payload, "data", "list"):
        published = _date10(item.get("notice_date"))
        title = _strip_name_prefix(_text(item.get("title")), _em_short_name(item))
        if not published or not title:
            # 没有日期或标题的行成不了证据：留着只会生成一条引用不了的「公告N」
            continue
        columns = item.get("columns")
        kinds = [
            _text(entry.get("column_name"))
            for entry in (columns if isinstance(columns, list) else [])
            if isinstance(entry, dict) and _text(entry.get("column_name"))
        ]
        code = ""
        codes = item.get("codes")
        if isinstance(codes, list) and codes and isinstance(codes[0], dict):
            code = _text(codes[0].get("stock_code"))
        art_code = _text(item.get("art_code"))
        url = (
            f"https://data.eastmoney.com/notices/detail/{code}/{art_code}.html"
            if code and art_code
            else ""
        )
        rows.append(
            {"published_at": published, "type": "、".join(kinds), "title": title, "url": url}
        )
    return rows


def announcements_from_cninfo(payload: Any) -> list[dict]:
    """巨潮公告载荷 → 内部契约行（纯函数）。

    巨潮不按类别查时 `announcementTypeName` 是 `null`——**不得**凭空编一个类型，
    留空即可（工具只在类型非空时才写进证据）。
    """
    rows: list[dict] = []
    for item in _dict_rows(payload, "announcements"):
        published = _from_epoch_ms(item.get("announcementTime"))
        title = _text(item.get("announcementTitle"))
        if not published or not title:
            continue
        adjunct = _text(item.get("adjunctUrl")).lstrip("/")
        rows.append(
            {
                "published_at": published,
                "type": _text(item.get("announcementTypeName")),
                "title": title,
                "url": f"http://static.cninfo.com.cn/{adjunct}" if adjunct else "",
            }
        )
    return rows


def normalize_index_code(code: Any) -> str:
    """指数代码 → `sh000001` 这种带市场前缀的规范形式。

    裸代码按**指数**口径补前缀：`000001` 是上证指数（不是平安银行），
    `399xxx` 是深证系列，`899050`（北证50）在 8/9 段。有歧义时请显式传前缀——
    `sz470052` 这种自定义深证指数靠裸码是分不出来的。
    """
    text = _text(code).lower()
    if not text:
        return ""
    if text[:2] in ("sh", "sz", "bj"):
        return text
    digits = "".join(ch for ch in text if ch.isdigit())[:6]
    if len(digits) < 6:
        return ""
    if digits.startswith(("399", "398")):
        return f"sz{digits}"
    if digits[0] in ("8", "9"):
        return f"bj{digits}"
    return f"sh{digits}"


def _bare_code(code: Any) -> str:
    """任意代码表示 → 6 位裸码（用于跨源匹配：东财给裸码，新浪给带前缀的）。"""
    return "".join(ch for ch in _text(code) if ch.isdigit())[:6]


def _quote_row(
    code: str,
    name: Any,
    price: Any,
    prev_close: Any,
    *,
    change: Any = None,
    change_pct: Any = None,
    quote_date: Any = "",
    source: str,
) -> dict | None:
    """组装一行行情；缺价格即返回 None（没有点位就成不了证据）。"""
    price_value = _num(price)
    if price_value is None:
        return None
    prev_value = _num(prev_close)
    change_value = _num(change)
    pct_value = _num(change_pct)
    if pct_value is None and prev_value not in (None, 0.0):
        pct_value = (price_value - prev_value) / prev_value * 100.0
    if change_value is None and prev_value is not None:
        change_value = price_value - prev_value
    return {
        "code": code,
        "name": _text(name),
        "price": price_value,
        "change": change_value,
        "change_pct": None if pct_value is None else round(pct_value, 6),
        "prev_close": prev_value,
        "quote_date": _date10(quote_date),
        "source": source,
    }


def _match_requested(by_bare: dict[str, dict], requested: Sequence[str] | None) -> list[dict]:
    """按**裸码**匹配请求并回填规范代码。

    必须是裸码匹配：东财返回 `000001`，请求的是 `sh000001`——按字面比会把
    所有指数都判成「没取到」，那是最隐蔽的一类静默失败。
    """
    order = [code for code in (requested or ()) if code]
    if not order:
        order = list(by_bare)
    out: list[dict] = []
    for code in order:
        row = by_bare.get(_bare_code(code))
        if row is not None:
            out.append({**row, "code": code})
    return out


def index_quotes_from_sina_hq(text: Any, *, requested: Sequence[str] | None = None) -> list[dict]:
    """新浪行情原文 → 内部契约行（纯函数）。

    `hq_str_sh999999=""` 这种空载荷表示**该代码查不到**，必须整行丢弃：
    把空串读成 0 点等于凭空造出一个跌停的指数。
    """
    raw = "" if text is None else str(text)
    by_bare: dict[str, dict] = {}
    for match in _SINA_HQ_QUOTE_RE.finditer(raw):
        code, payload = match.group(1), match.group(2)
        fields = payload.split(",")
        if len(fields) <= _SINA_HQ_PRICE or not _text(fields[_SINA_HQ_NAME]):
            continue
        row = _quote_row(
            code,
            fields[_SINA_HQ_NAME],
            fields[_SINA_HQ_PRICE],
            fields[_SINA_HQ_PREV_CLOSE],
            quote_date=fields[_SINA_HQ_DATE] if len(fields) > _SINA_HQ_DATE else "",
            source=SOURCE_SINA,
        )
        if row is not None:
            by_bare[_bare_code(code)] = row
    return _match_requested(by_bare, requested)


def _frame_records(frame: Any) -> list[dict]:
    if frame is None or not hasattr(frame, "to_dict"):
        return []
    try:
        if hasattr(frame, "columns"):
            return [dict(row) for row in frame.to_dict(orient="records")]
        return [dict(frame.to_dict())]
    except Exception:  # noqa: BLE001 — 形状不符就是没有数据
        return []


def _quotes_from_spot_records(
    records: Sequence[dict],
    *,
    requested: Sequence[str] | None,
    source: str,
) -> list[dict]:
    """把「中文列名的现货快照」归一成契约行（新浪/东财两个 akshare 源共用）。

    两个 akshare 快照接口的列名一致（`代码/名称/最新价/涨跌额/涨跌幅/昨收`），
    也都**不带**快照日期——所以这里没有日期可归一，`quote_date` 一律留空。
    """
    by_bare: dict[str, dict] = {}
    for record in records:
        code = _text(record.get("代码") or record.get("code"))
        if not code:
            continue
        row = _quote_row(
            code,
            record.get("名称") or record.get("name"),
            record.get("最新价") if "最新价" in record else record.get("price"),
            record.get("昨收") or record.get("prev_close"),
            change=record.get("涨跌额") if "涨跌额" in record else record.get("change"),
            change_pct=record.get("涨跌幅") if "涨跌幅" in record else record.get("change_pct"),
            source=source,
        )
        if row is not None:
            by_bare[_bare_code(code)] = row
    return _match_requested(by_bare, requested)


def index_quotes_from_sina_spot(frame: Any, *, requested: Sequence[str] | None = None) -> list[dict]:
    """`ak.stock_zh_index_spot_sina()` 的帧 → 契约行（纯函数）。

    该源**不带快照时间戳**（列里只有昨收/今开/最高/最低），所以 `quote_date`
    留空：把请求基准日填进去就等于伪造了数据时点。
    """
    return _quotes_from_spot_records(_frame_records(frame), requested=requested, source=SOURCE_SINA)


def index_quotes_from_em_spot(frame: Any, *, requested: Sequence[str] | None = None) -> list[dict]:
    """`ak.stock_zh_index_spot_em(symbol="沪深重要指数")` 的帧 → 契约行（纯函数）。"""
    return _quotes_from_spot_records(_frame_records(frame), requested=requested, source=SOURCE_EM)


# ── IO：公告（东财 → 巨潮） ───────────────────────────────────────────────


def _clamp_limit(limit: Any, *, default: int, maximum: int) -> int:
    """**不写 `limit or default`**：`limit=0` 是「一条都不要」，不是「用默认值」。"""
    value = default if limit is None else int(limit)
    return max(1, min(value, maximum))


def _announcement_payload_via_em(code: str, start: str, end: str) -> Any:
    params = {
        "sr": "-1",
        "page_size": str(MAX_ANNOUNCEMENT_LIMIT),
        "page_index": "1",
        "ann_type": "A",
        "client_source": "web",
        "f_node": "0",
        "s_node": "0",
        "stock_list": code,
        "begin_time": start or "",
        "end_time": end or "",
    }
    return http_get_json(
        _EM_ANN_URL,
        params=params,
        headers=_EM_ANN_HEADERS,
        retry=HttpRetry(attempts=2, base_sleep_s=0.5, timeout_s=8.0),
    )


def _cninfo_org_id(code: str) -> str:
    """巨潮查询必须带 `orgId`（形如 `gssh0600519`），拿不到就查不出东西。"""
    return _text(_cninfo_stock_map().get(code))


@lru_cache(maxsize=1)
def _cninfo_stock_map() -> dict[str, str]:
    """巨潮的代码→orgId 表（500KB 的 json，进程内只拉一次）。

    用 `lru_cache` 而不是模块级全局量：**失败不会被缓存**——一次网络抖动不该
    把这条源永久废掉（本仓库在 `dataflows/config.py` 的全局可变配置上踩过坑）。
    """
    payload = http_get_json(
        _CNINFO_STOCK_URL,
        headers=_CNINFO_HEADERS,
        retry=HttpRetry(attempts=2, base_sleep_s=0.5, timeout_s=10.0),
    )
    listing = payload.get("stockList") if isinstance(payload, dict) else None
    mapping: dict[str, str] = {}
    for item in listing if isinstance(listing, list) else []:
        if isinstance(item, dict) and _text(item.get("code")):
            mapping[_text(item.get("code"))] = _text(item.get("orgId"))
    return mapping


def _announcement_payload_via_cninfo(code: str, start: str, end: str) -> Any:
    org_id = _cninfo_org_id(code)
    if not org_id:
        return None
    payload = {
        "pageNum": "1",
        "pageSize": str(MAX_ANNOUNCEMENT_LIMIT),
        "column": "szse",
        "tabName": "fulltext",
        "plate": "",
        "stock": f"{code},{org_id}",
        "searchkey": "",
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": f"{start}~{end}" if start or end else "",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    # 必须走**表单体**（`data=`）而不是 query string：后者巨潮返回空结果集
    # （实测 totalAnnouncement=0），表现为「这个标的没有公告」的静默错误。
    response = requests.post(
        _CNINFO_ANN_URL, data=payload, headers=_CNINFO_HEADERS, timeout=10
    )
    response.raise_for_status()
    return response.json()


# 源的顺序即优先级：能拿到公告类型的那条先走。
#
# 每条源都经**薄包装函数**而不是直接塞函数对象：直接塞会在 import 期把实现
# 捕获进元组，测试（或运行期替换）再改模块属性就不生效了——「以为换了源、
# 其实还在打老源」是最难查的一类问题。包装函数在**调用时**才解析实现。
def _announcement_rows_via_em(code: str, start: str, end: str) -> list[dict]:
    return announcements_from_em(_announcement_payload_via_em(code, start, end))


def _announcement_rows_via_cninfo(code: str, start: str, end: str) -> list[dict]:
    return announcements_from_cninfo(_announcement_payload_via_cninfo(code, start, end))


_ANNOUNCEMENT_SOURCES: tuple[tuple[str, Any], ...] = (
    (SOURCE_EM, _announcement_rows_via_em),
    (SOURCE_CNINFO, _announcement_rows_via_cninfo),
)


def _frame(rows: Sequence[dict], columns: Sequence[str], *, limit: int, source: str = "") -> pd.DataFrame | None:
    """行列表 → 契约帧；**空行返回 None**（空帧会被读成「取到了，是空的」）。"""
    if not rows:
        return None
    frame = pd.DataFrame([dict(row) for row in rows])
    for column in columns:
        if column not in frame.columns:
            frame[column] = ""
    if source:
        frame["source"] = source
    return frame[list(columns)].head(limit).reset_index(drop=True)


def fetch_symbol_announcements(
    symbol: str, limit: Any = DEFAULT_ANNOUNCEMENT_LIMIT, *, start: str = "", end: str = ""
) -> pd.DataFrame | None:
    """某标的在 `[start, end]` 窗口内的公告；**取不到返回 None**。

    返回列见 `ANNOUNCEMENT_COLUMNS`。窗口为空串表示不设该侧的界（由调用方按
    自己的回看窗口传，本函数不读环境时钟）。
    """
    code = _bare_code(symbol)
    if len(code) != 6:
        logger.info("公告取数：无法从 %r 解析出 6 位代码", symbol)
        return None
    wanted = _clamp_limit(limit, default=DEFAULT_ANNOUNCEMENT_LIMIT, maximum=MAX_ANNOUNCEMENT_LIMIT)

    for label, seam in _ANNOUNCEMENT_SOURCES:
        try:
            rows = seam(code, start, end)
        except Exception as exc:  # noqa: BLE001 — 换源是常态，不是错误
            logger.info("公告源 %s 取数失败：%s", label, type(exc).__name__)
            continue
        frame = _frame(rows, ANNOUNCEMENT_COLUMNS, limit=wanted, source=label)
        if frame is not None:
            return frame
        logger.info("公告源 %s 在窗口 [%s, %s] 内没有返回条目", label, start, end)
    return None


# ── IO：指数行情（新浪直连 → 新浪 akshare → 东财 akshare） ────────────────


def _index_text_via_sina_hq(codes: Sequence[str]) -> str:
    response = requests.get(
        _SINA_HQ_URL + ",".join(codes), headers=_SINA_HQ_HEADERS, timeout=8
    )
    response.raise_for_status()
    # 新浪行情是 GBK；用 utf-8 解会把指数名读成乱码（而名字要进证据）
    response.encoding = "gbk"
    return response.text


def _index_frame_via_sina_akshare() -> Any:
    import akshare as ak

    return ak.stock_zh_index_spot_sina()


def _index_frame_via_em_akshare() -> Any:
    import akshare as ak

    # 「沪深重要指数」= 主要宽基指数，比逐个市场系列（`上证系列指数` 268 行 /
    # `深证系列指数` 945 行）快得多，且正好覆盖默认的三个标的。
    return ak.stock_zh_index_spot_em(symbol="沪深重要指数")


def _index_rows_via_sina_hq(codes: Sequence[str]) -> list[dict]:
    return index_quotes_from_sina_hq(_index_text_via_sina_hq(codes), requested=codes)


def _index_rows_via_sina_akshare(codes: Sequence[str]) -> list[dict]:
    return index_quotes_from_sina_spot(_index_frame_via_sina_akshare(), requested=codes)


def _index_rows_via_em_akshare(codes: Sequence[str]) -> list[dict]:
    return index_quotes_from_em_spot(_index_frame_via_em_akshare(), requested=codes)


_INDEX_SOURCES: tuple[tuple[str, Any], ...] = (
    (SOURCE_SINA, _index_rows_via_sina_hq),
    (SOURCE_SINA, _index_rows_via_sina_akshare),
    (SOURCE_EM, _index_rows_via_em_akshare),
)


def fetch_index_quotes(codes: Sequence[str] | None = None) -> pd.DataFrame | None:
    """主要指数的点位与涨跌幅；**取不到返回 None**。

    `codes=None` 用默认三个（上证/深证成指/创业板指）；显式空序列 = 什么都不查，
    返回 None（**不**被 falsy 换成默认值）。返回列见 `INDEX_QUOTE_COLUMNS`，
    其中 `change_pct` 是**百分数**（0.31 表示 +0.31%），与证据的 `pct` 单位一致。
    """
    requested: list[str] = []
    for raw in (DEFAULT_INDEX_CODES if codes is None else codes):
        code = normalize_index_code(raw)
        if code and code not in requested:
            requested.append(code)
    if not requested:
        return None
    requested = requested[:MAX_INDEX_CODES]

    for label, seam in _INDEX_SOURCES:
        try:
            rows = seam(requested)
        except Exception as exc:  # noqa: BLE001
            logger.info("指数源 %s 取数失败：%s", label, type(exc).__name__)
            continue
        frame = _frame(rows, INDEX_QUOTE_COLUMNS, limit=len(requested), source=label)
        if frame is not None:
            return frame
        logger.info("指数源 %s 未返回所请求的代码：%s", label, ",".join(requested))
    return None


# ── 端口适配：把帧接成工具层契约 ──────────────────────────────────────────


class SymbolAnnouncementsPort:
    """`tradingagents.tools.evidence.AnnouncementsPort` 的实现（结构类型，不继承）。

    端口层只做「帧 → 行字典」这一步转换，取数仍然全在 `fetch_symbol_announcements`
    里：工具层因此不碰 pandas，也不需要知道有多少条源、谁先谁后。
    """

    def list_announcements(
        self, *, symbol: str, start: str, end: str, limit: int
    ) -> Sequence[dict]:
        frame = fetch_symbol_announcements(symbol, limit, start=start, end=end)
        return _frame_records(frame)


__all__ = [
    "ANNOUNCEMENT_COLUMNS",
    "INDEX_QUOTE_COLUMNS",
    "DEFAULT_ANNOUNCEMENT_LIMIT",
    "DEFAULT_INDEX_CODES",
    "MAX_ANNOUNCEMENT_LIMIT",
    "MAX_INDEX_CODES",
    "SOURCE_CNINFO",
    "SOURCE_EM",
    "SOURCE_SINA",
    "SymbolAnnouncementsPort",
    "announcements_from_cninfo",
    "announcements_from_em",
    "fetch_index_quotes",
    "fetch_symbol_announcements",
    "index_quotes_from_em_spot",
    "index_quotes_from_sina_hq",
    "index_quotes_from_sina_spot",
    "normalize_index_code",
]
