"""Tushare Pro 通道：补上东财被封后缺失的资金流与筹码维度。

## 为什么自己写 HTTP 而不装 SDK

Tushare 的接口就是一个 JSON POST（`http://api.tushare.pro`）：
`{api_name, token, params, fields}` → `{code, msg, data:{fields, items}}`。
为一个几十行的调用引入一个新依赖（`tushare`），会让部署多一个版本约束、
测试多一层替身，收益却只是省下这点代码。本仓库已有的 `requests` 足够。

## 为什么要有这个通道

实测：东财系接口被 IP 级封禁，而判定需要的 `capital_composition`（个股资金流）
与 `chip_distribution`（筹码分布）**只有东财有免费源**。于是判定长期停在
"证据不足"。Tushare 是付费/积分制官方源，稳定性换钱，是最短的补齐路径。

## 没有 token 时的行为

**如实声明不可用，不假装**：`TushareUnavailable` 由调用方转成"该维度未覆盖"，
这正是平台既有的诚实降级路径。绝不在没有凭证时返回空数据帧充当"取到了但没有"。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

TUSHARE_ENDPOINT = "https://api.tushare.pro"
# 单次请求超时：判定链路整体只有 60s 预算，单源不能吃掉大头
REQUEST_TIMEOUT_SECONDS = 15.0

_TOKEN_ENV_KEYS = ("TUSHARE_TOKEN", "TA_TUSHARE_TOKEN")


class TushareUnavailable(RuntimeError):
    """没有凭证或上游不可用。调用方据此声明该维度未覆盖。"""


def token() -> str | None:
    """从环境变量取 token。刻意不做文件/数据库回退：凭证来源必须唯一可审计。"""
    for key in _TOKEN_ENV_KEYS:
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    return None


def is_configured() -> bool:
    return token() is not None


def query(
    api_name: str,
    *,
    params: Mapping[str, Any] | None = None,
    fields: Sequence[str] | str = (),
) -> list[dict[str, Any]]:
    """调用一个 Tushare 接口，返回行字典列表。

    错误一律转成 `TushareUnavailable`：调用方只需要一个"能不能用"的信号，
    不需要区分是没 token、超时还是积分不足——那三种都只能声明未覆盖。
    """
    import json as _json
    import requests  # 延迟导入：无网络环境也能加载本模块

    token_value = token()
    if not token_value:
        raise TushareUnavailable("未配置 TUSHARE_TOKEN")

    payload = {
        "api_name": api_name,
        "token": token_value,
        "params": dict(params or {}),
        "fields": ",".join(fields) if isinstance(fields, (list, tuple)) else str(fields),
    }
    try:
        response = requests.post(
            TUSHARE_ENDPOINT,
            data=_json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
    except Exception as exc:  # noqa: BLE001
        raise TushareUnavailable(f"Tushare 请求失败：{type(exc).__name__}") from exc

    # 注意不能写 `body.get("code") or -1`：code=0 表示成功，会被 falsy 判成失败
    raw_code = body.get("code")
    if raw_code is None or int(raw_code) != 0:
        # 上游把"积分不足""频次超限"都放在 msg 里；原文只进日志，不外传
        logger.info("tushare %s rejected: %s", api_name, str(body.get("msg"))[:200])
        raise TushareUnavailable(f"Tushare 拒绝 {api_name}（code={body.get('code')}）")

    data = body.get("data") or {}
    names = list(data.get("fields") or [])
    rows = []
    for item in data.get("items") or []:
        rows.append(dict(zip(names, item)))
    return rows


def fetch_moneyflow_row(symbol: str, day: str) -> dict[str, Any] | None:
    """个股资金流（`moneyflow`）。返回当日一行；无数据返回 None。"""
    code, market = _ts_code(symbol)
    rows = query("moneyflow", params={"ts_code": code, "trade_date": _yyyymmdd(day)})
    if not rows:
        return None
    row = dict(rows[0])
    row["_symbol"] = symbol
    return row


def fetch_chip_row(symbol: str, day: str) -> dict[str, Any] | None:
    """筹码分布（`cyq_perf`）。返回该日一行；无数据返回 None。

    `cyq_perf` 需要一定积分；取不到时抛 `TushareUnavailable`，由调用方声明未覆盖。
    """
    code, _market = _ts_code(symbol)
    rows = query("cyq_perf", params={"ts_code": code, "trade_date": _yyyymmdd(day)})
    if not rows:
        return None
    row = dict(rows[0])
    row["_symbol"] = symbol
    return row


def _ts_code(symbol: str) -> tuple[str, str]:
    """内部 `600519.SH` → Tushare `600519.SH`（格式一致，仅做规范化与校验）。"""
    raw = str(symbol or "").strip().upper()
    if "." in raw:
        code, suffix = raw.split(".", 1)
        return f"{code}.{suffix}", suffix
    code = raw
    market = "SH" if code.startswith(("5", "6", "9")) else "SZ"
    return f"{code}.{market}", market


def _yyyymmdd(day: str) -> str:
    return str(day or "").replace("-", "")[:8]
