from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests

from .errors import SourceThrottled


@dataclass(frozen=True)
class HttpRetry:
    attempts: int = 4
    base_sleep_s: float = 0.6
    timeout_s: float = 10.0


# 「直连」的显式标记：`resolve_proxies()` 返回 None 的含义是"交给 requests 决定"，
# 而 requests 默认 `trust_env=True` 会**静默使用系统代理**（macOS 上尤其容易：
# 用户开了 Clash 类工具，`urllib.request.getproxies()` 就有值）。
# 实测这台机器：走系统代理请求东财直接 `ProxyError`，强制直连则 `RemoteDisconnected`
# ——两条路都不通，但语义完全不同，代码里必须能区分，否则"没配代理"会被误读成"直连"。
_DIRECT_SENTINEL = {"http": None, "https": None}


def resolve_proxies() -> dict[str, str] | None:
    """出口代理配置。

    存在理由：东方财富对高频/并发访问做 **IP 级封禁**（实测 `RemoteDisconnected`，
    见 akshare issue #6061）。东财系接口承载着筹码、个股资金流、板块资金流等
    维度，一旦被封，判定就只能停在"证据不足"。走代理是最低成本恢复这些维度的
    方式（换出口 IP，而不是换数据源）。

    优先级：`TA_EM_PROXY`（本平台专用，便于只给东财流量走代理）
          → 标准 `HTTPS_PROXY` / `HTTP_PROXY`（部署环境通用约定）
    """
    explicit = (os.getenv("TA_EM_PROXY") or "").strip()
    if explicit:
        return {"http": explicit, "https": explicit}
    https = (os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or "").strip()
    http = (os.getenv("HTTP_PROXY") or os.getenv("http_proxy") or "").strip()
    if https or http:
        return {"http": http or https, "https": https or http}
    return None


# ── 出口闸门（egress gate）────────────────────────────────────────────────
#
# 存在理由（实测 2026-09-29）：平台原先**只有单请求指数退避**，没有任何跨请求
# 共享的节流。后果有两类，都是自己造成的：
#
# 1. **自伤**：多个模块叠加打同一个域名（东财系尤其），等于自己把自己推向封禁。
#    外部参照 a-stock-data 的裁决是「串行 + ≥1s + 抖动」，它的封禁事故正是
#    10 线程并发、无节流、45000+ 请求/小时打出来的（IP 级封禁 20+ 小时）。
# 2. **白等**：`cn_eastmoney_http.get_individual_fund_flow` 在 push2 被掐时，
#    一次调用要跑 "2 个 host × 6 次重试 × 指数退避" = **38889ms** 才失败。
#
# 这里加三件事，都不改变调用方契约：
#   * `_acquire()`：按 host 串行 + 最小间隔 + 随机抖动（不同 host 互不阻塞）；
#   * 失败分流：403/418 这类**风控信号绝不重试**（重试即攻击），4xx 业务错误不重试，
#     只有连接层失败与 5xx/429 才值得退避重试；
#   * 连续连接层失败达阈值 → 该 host **冷却**，冷却期内直接抛 `SourceThrottled`
#     快速失败，让调用方立刻降级到下一个源，而不是继续等。
#
# 逃生舱：`TA_EGRESS_DISABLED=1` 让闸门整体退化为"什么都没有"，用于排障。
# 单 host 节流覆盖：`TA_EGRESS_MIN_INTERVAL_<HOST>`（host 里的点和冒号换成下划线，
# 全大写），例如 `TA_EGRESS_MIN_INTERVAL_PUSH2_EASTMONEY_COM=0`。

#: 默认最小间隔。**必须按 host 分级**，不能一刀切 1s：腾讯/新浪/交易所官方并不
#: 封 IP，给它们也上 1s 只会让全站变慢；真正需要严格串行的是东财系。
_DEFAULT_MIN_INTERVAL_S = 0.2
_DEFAULT_JITTER_S = 0.1
_DEFAULT_COOLDOWN_S = 300.0
#: 连续多少次"连接层"失败就把该 host 判为冷却
_CONSEC_FAIL_TO_OPEN = 3

_HOST_MIN_INTERVAL: dict[str, float] = {
    # 东财系：有风控、会封 IP → 严格串行（a-stock-data 的做法是 1.0s）
    "push2.eastmoney.com": 1.0,
    "push2his.eastmoney.com": 1.0,
    "push2delay.eastmoney.com": 1.0,
    "datacenter-web.eastmoney.com": 1.0,
    # 官方/腾讯/新浪：不封 IP，只去抖
    "qt.gtimg.cn": 0.15,
    "ifzq.gtimg.cn": 0.15,
    "web.ifzq.gtimg.cn": 0.15,
    "hq.sinajs.cn": 0.15,
    "vip.stock.finance.sina.com.cn": 0.2,
    "www.tdx.com.cn": 0.5,
}
_HOST_COOLDOWN: dict[str, float] = {
    # 东财：封禁实测确实持续 20+ 小时，但**它也是间歇性的**——探测里曾记过
    # `OK 103ms`，直接复测又全败。冷却窗口取 300s：足够盖住一轮探测/采集的
    # 连续突发（这才是 38889ms 白等的来源），又不至于把 5 分钟后已恢复的访问
    # 继续挡在门外（过长的窗口会**降低**可用性，把间歇性成功一并误杀）。
    "push2.eastmoney.com": 300.0,
    "push2his.eastmoney.com": 300.0,
    "datacenter-web.eastmoney.com": 300.0,
}

_EGRESS_LOCK = threading.Lock()
_host_locks: dict[str, threading.Lock] = {}
_next_allowed: dict[str, float] = {}
_open_until: dict[str, float] = {}
_consec_fail: dict[str, int] = {}


def _egress_disabled() -> bool:
    return (os.getenv("TA_EGRESS_DISABLED") or "").strip().lower() in ("1", "true", "yes", "on")


def _host_of(url: str) -> str:
    try:
        return urlsplit(str(url)).netloc or "?"
    except Exception:
        return "?"


def _env_key(host: str) -> str:
    return "TA_EGRESS_MIN_INTERVAL_" + host.upper().replace(".", "_").replace(":", "_").replace("-", "_")


def _min_interval_for(host: str) -> float:
    raw = (os.getenv(_env_key(host)) or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return _HOST_MIN_INTERVAL.get(host, _DEFAULT_MIN_INTERVAL_S)


def _jitter_for(host: str) -> float:
    """抖动量：按最小间隔等比，东财的抖动大一些（防固定节奏被识别）。"""
    return min(0.5, max(_DEFAULT_JITTER_S, _min_interval_for(host) * 0.5))


def _cooldown_for(host: str) -> float:
    return _HOST_COOLDOWN.get(host, _DEFAULT_COOLDOWN_S)


def _lock_for(host: str) -> threading.Lock:
    with _EGRESS_LOCK:
        lk = _host_locks.get(host)
        if lk is None:
            lk = threading.Lock()
            _host_locks[host] = lk
        return lk


def _acquire(host: str) -> None:
    """进门：冷却检查 + 按 host 串行 + 最小间隔 + 抖动。

    锁是**按 host** 的，所以不同域名之间不会互相阻塞（把全局锁拿来 sleep 会让
    "打 10 个不同域名"变成串行，平白慢 10 倍）。
    """
    if _egress_disabled():
        return
    lk = _lock_for(host)
    with lk:
        now = time.time()
        until = _open_until.get(host, 0.0)
        if until > now:
            raise SourceThrottled(
                f"{host} 处于冷却期（{int(until - now)}s 后重试）：此前连续连接层失败，"
                f"继续重试只会加重封禁",
                retry_after_s=until - now,
            )
        wait = _next_allowed.get(host, 0.0) - now
        if wait > 0:
            time.sleep(wait)
        _next_allowed[host] = time.time() + _min_interval_for(host) + random.uniform(0, _jitter_for(host))


def _status_of(exc: Exception) -> int | None:
    """从异常里取 HTTP 状态码（`HTTPError` 带 `.response`，连接层异常没有）。"""
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    return getattr(resp, "status_code", None)


def _classify(exc: Exception) -> tuple[bool, bool]:
    """把失败分成 (是否值得退避重试, 是否立即冷却该 host)。

    这个分流就是 a-stock-data 那条经验的落地：**403 不是"瞬时故障"，是风控信号**。
    原实现用 `except Exception` 兜住一切并重试 4 次，于是在被风控时把 4 次请求
    变成 16 次——**重试即攻击**。
    """
    status = _status_of(exc)
    if status in (403, 418):
        # 明确的风控信号：不重试 + 立刻冷却
        return False, True
    if status == 429:
        # 明确限流：可以退避重试（这是它字面上的意思）
        return True, False
    if status is not None and 400 <= status < 500:
        # 其它 4xx：参数/路径/权限问题，重试无益
        return False, False
    if status is not None and status >= 500:
        return True, False
    # 连接层（ConnectionError / RemoteDisconnected / Timeout / ChunkedEncodingError）
    # 或拿不到状态码：值得重试，同时计入"连续失败"
    return True, False


_CONNECT_LEVEL = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def _record_success(host: str) -> None:
    if _egress_disabled():
        return
    with _EGRESS_LOCK:
        _consec_fail.pop(host, None)


def _record_failure(host: str, exc: Exception, *, trip_cooldown: bool) -> None:
    """记一次失败；连续连接层失败达阈值就把该 host 置为冷却。"""
    if _egress_disabled():
        # 闸门被关掉时**什么都不记**。否则排障时"关掉闸门 → 再打开"会继承一份
        # 陈旧的冷却状态，于是刚打开就开始快速失败，而 `egress_snapshot()` 也会骗人。
        return
    connect_level = isinstance(exc, _CONNECT_LEVEL)
    with _EGRESS_LOCK:
        if trip_cooldown:
            _consec_fail.pop(host, None)
            _open_until[host] = time.time() + _cooldown_for(host)
            return
        if not connect_level:
            return
        count = _consec_fail.get(host, 0) + 1
        _consec_fail[host] = count
        if count >= _CONSEC_FAIL_TO_OPEN:
            _open_until[host] = time.time() + _cooldown_for(host)


def reset_egress_state() -> None:
    """清空闸门状态（测试用：避免一个用例把某个 host 冷却掉，连累后续用例）。"""
    with _EGRESS_LOCK:
        _next_allowed.clear()
        _open_until.clear()
        _consec_fail.clear()


def egress_snapshot() -> dict[str, Any]:
    """当前闸门状态快照（排障/探测报告用）。"""
    with _EGRESS_LOCK:
        now = time.time()
        return {
            "disabled": _egress_disabled(),
            "cooling": {
                h: round(until - now, 1)
                for h, until in _open_until.items()
                if until > now
            },
            "consecutive_failures": dict(_consec_fail),
        }


def _get_with_retry(
    url: str,
    *,
    params: dict[str, Any] | None,
    headers: dict[str, str] | None,
    retry: HttpRetry,
    direct: bool,
) -> requests.Response:
    """共享的重试循环：进门（闸门）→ 请求 → 失败分流 → 退避或放弃。

    两个公开助手（text / json）此前各抄了一份循环；合成一处之后，
    "要不要限流、403 要不要重试、什么时候冷却"这类全局约定只有一份。
    """
    host = _host_of(url)
    last_exc: Exception | None = None
    # 「连续失败」必须按**逻辑请求**计数，不能按**重试次数**计数。
    # 反例（实测踩到）：`cn_eastmoney_http` 用 `HttpRetry(attempts=6)`，若每次
    # attempt 都记一次失败，**单次**调用就会攒够 3 次而被判为"连续失败"，
    # 于是打一次就冷却 600 秒——而东财是**间歇性**的（探测里曾记过 OK 103ms），
    # 过长的封禁窗口会把本来能成功的调用一起挡掉，反而降低可用性。
    trip_immediately = False

    for attempt in range(retry.attempts):
        # 冷却期内 `_acquire` 会抛 SourceThrottled——**故意放在 try 之外**，
        # 这样它不会被下面的 `except Exception` 吃掉再重试一遍。
        _acquire(host)
        try:
            kwargs: dict[str, Any] = {
                "params": params,
                "headers": headers,
                "timeout": retry.timeout_s,
            }
            # 只在**真的配了代理**时才传这个参数：无条件传 `proxies=None` 会改变
            # 本助手对调用方的契约（既有测试替身按老签名实现，会直接 TypeError）。
            # 兼容性优先——没配代理时行为必须与从前逐字一致。
            proxies = resolve_proxies()
            if proxies:
                kwargs["proxies"] = proxies
            elif direct:
                # 显式直连：绕过系统/环境代理。用于"必须自己控制出口"的调用点
                # （例如东财直连通道），避免被本机代理静默接管。
                kwargs["proxies"] = dict(_DIRECT_SENTINEL)
            resp = requests.get(url, **kwargs)
            resp.raise_for_status()
            _record_success(host)
            return resp
        except Exception as exc:
            last_exc = exc
            retryable, trip = _classify(exc)
            trip_immediately = trip_immediately or trip
            if retryable and attempt < retry.attempts - 1:
                time.sleep(retry.base_sleep_s * (2**attempt))
                continue
            break

    # 整条重试链走完，**只记一次**失败（风控信号除外，它要立刻冷却）。
    _record_failure(host, last_exc, trip_cooldown=trip_immediately)

    raise RuntimeError(
        f"request failed for host={host}: {type(last_exc).__name__}: {last_exc}"
    ) from last_exc


def http_get_text(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    retry: HttpRetry = HttpRetry(),
    direct: bool = False,
) -> str:
    """GET 纯文本 with retry + exponential backoff.

    存在理由：新浪的行业板块快照返回的是**一句 JS 赋值**
    （`var S_Finance_bankuai_sinaindustry = {...};`），不是 JSON——
    `http_get_json` 里的 `resp.json()` 必然抛错，但 HTTP 200 与响应体本身都是好的。
    所以「取文本」必须是一个独立入口，而不是让调用方各自 `requests.get` 一遍：
    这个助手与 `http_get_json` 共享 `resolve_proxies()` / `HttpRetry` /
    `raise_for_status` 的语义，于是「要不要走系统代理」「重试几次」这类全局约定
    依然只有一份（实测这台机器上走系统代理与直连的失败形状完全不同，见上）。
    """
    # 用 `resp.text`（而不是自己 decode）：编码由响应头/requests 的嗅探决定，
    # 与 akshare 走同一个端点时的行为一致——实测该端点声明 gbk 且解码正确。
    return _get_with_retry(
        url, params=params, headers=headers, retry=retry, direct=direct
    ).text


def http_get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    retry: HttpRetry = HttpRetry(),
    direct: bool = False,
) -> Any:
    """GET JSON with retry + exponential backoff.

    This is intentionally small and dependency-light so providers can share
    consistent behavior and errors.
    """
    return _get_with_retry(
        url, params=params, headers=headers, retry=retry, direct=direct
    ).json()
