from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests


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
    import os

    explicit = (os.getenv("TA_EM_PROXY") or "").strip()
    if explicit:
        return {"http": explicit, "https": explicit}
    https = (os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or "").strip()
    http = (os.getenv("HTTP_PROXY") or os.getenv("http_proxy") or "").strip()
    if https or http:
        return {"http": http or https, "https": https or http}
    return None


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
    last_exc: Exception | None = None
    for attempt in range(retry.attempts):
        try:
            kwargs: dict[str, Any] = {
                "params": params,
                "headers": headers,
                "timeout": retry.timeout_s,
            }
            proxies = resolve_proxies()
            if proxies:
                kwargs["proxies"] = proxies
            elif direct:
                kwargs["proxies"] = dict(_DIRECT_SENTINEL)
            resp = requests.get(url, **kwargs)
            resp.raise_for_status()
            # 用 `resp.text`（而不是自己 decode）：编码由响应头/requests 的嗅探决定，
            # 与 akshare 走同一个端点时的行为一致——实测该端点声明 gbk 且解码正确。
            return resp.text
        except Exception as exc:
            last_exc = exc
            if attempt < retry.attempts - 1:
                time.sleep(retry.base_sleep_s * (2**attempt))
                continue
            raise RuntimeError(
                f"http_get_text failed after retries: {type(exc).__name__}: {exc}"
            ) from exc
    raise RuntimeError(f"http_get_text failed: {last_exc}")


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
    last_exc: Exception | None = None
    for attempt in range(retry.attempts):
        try:
            # 只在**真的配了代理**时才传这个参数：无条件传 `proxies=None` 会改变
            # 本助手对调用方的契约（既有测试替身按老签名实现，会直接 TypeError）。
            # 兼容性优先——没配代理时行为必须与从前逐字一致。
            kwargs: dict[str, Any] = {
                "params": params,
                "headers": headers,
                "timeout": retry.timeout_s,
            }
            proxies = resolve_proxies()
            if proxies:
                kwargs["proxies"] = proxies
            elif direct:
                # 显式直连：绕过系统/环境代理。用于"必须自己控制出口"的调用点
                # （例如东财直连通道），避免被本机代理静默接管。
                kwargs["proxies"] = dict(_DIRECT_SENTINEL)
            resp = requests.get(url, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_exc = exc
            if attempt < retry.attempts - 1:
                time.sleep(retry.base_sleep_s * (2**attempt))
                continue
            raise RuntimeError(
                f"http_get_json failed after retries: {type(exc).__name__}: {exc}"
            ) from exc
    raise RuntimeError(f"http_get_json failed: {last_exc}")

