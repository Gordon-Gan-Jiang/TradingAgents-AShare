"""数据源失败的**三类语义**。

## 为什么必须分开

此前所有 provider 失败都写成 `NotImplementedError`（占位实现）或直接抛任意异常，
而 `interface.route_to_vendor` 对二者的处理都是"静默跳到下一个源"。结果是三种
**性质完全不同**的故障在日志里长得一模一样：

| 语义 | 含义 | 运维该做什么 |
|---|---|---|
| `SourceUnsupported` | 该源**结构上不支持**这个维度 | 调整配置，别把它排在这条链上 |
| `SourceUnavailable` | 支持，但**现在取不到**（网络/超时/上游改版/缺凭证） | 修网络、升级库、补 token |
| `SourceEmpty` | 取到了，但**这一天的数据本来就空**（停牌/未上市/非交易日） | 什么都不用做 |

事故案例：`cn_baostock` 拨的是上游已废弃的 `www.baostock.com:10030`，登录挂死
75 秒后失败。它当时被写成 `NotImplementedError("baostock login failed: ...")`——
于是一次**上游主机名迁移**被系统记录成"这个 provider 没实现该功能"，没有任何人去查。
把语义拆开之后，"连不上"才会以 `SourceUnavailable` 的身份出现在探测报告与日志里。

## 兼容性

`SourceUnsupported` 同时继承 `NotImplementedError`，因为既有代码（与 provider 的
占位实现）到处用后者表达"不支持"。这样新增语义不会破坏任何既有 `except
NotImplementedError` 分支，包括 `route_to_vendor` 的降级判断。
"""

from __future__ import annotations


class SourceError(RuntimeError):
    """数据源相关错误的基类。"""

    #: 供探测报告/日志使用的短标签
    kind = "source_error"


class SourceUnsupported(SourceError, NotImplementedError):
    """该源不支持这个能力（结构性缺失，重试无用）。"""

    kind = "unsupported"


class SourceUnavailable(SourceError):
    """该源支持这个能力，但当前取不到（可重试/需修环境）。"""

    kind = "unavailable"


class SourceThrottled(SourceUnavailable):
    """被限流/风控。

    与普通 `SourceUnavailable` 的区别**只在于该不该立刻重试**：
    `unavailable` 是"现在拿不到、待会儿再试"，而 `throttled` 是"你已经被盯上了，
    再试只会更糟"。出口闸门（`_http_utils`）在 host 冷却期内直接抛这个异常，
    于是调用方**毫秒级失败并降级到下一个源**，而不是把重试风暴砸向同一个域名。

    存在理由（实测）：`cn_eastmoney_http` 在 push2 被掐时，一次调用会因为
    "2 个 host × 6 次重试 × 指数退避"而**白等 38889ms**；有了冷却，
    首次判定之后同样的调用在 10ms 内返回并降级。
    """

    kind = "throttled"

    def __init__(self, msg: str, retry_after_s: float | None = None):
        super().__init__(msg)
        #: 建议等待多久再试（秒）；None 表示无明确建议
        self.retry_after_s = retry_after_s


class SourceEmpty(SourceError):
    """调用成功但没有数据（停牌、非交易日、未上市等正常情况）。"""

    kind = "empty"
