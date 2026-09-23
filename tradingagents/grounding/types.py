"""证据锚定层的值对象。

## 为什么证据项必须有稳定 ID

回答层的校验方式是「模型声明引用 → 校验器查表」，而不是「事后从自由文本里
猜哪些数字可疑」。稳定 ID（`E1`/`E2`…）是这个反转举证责任设计的前提：
没有 ID，模型无法声明来源，校验就退化成脆弱的正则比对。

## 为什么 Coverage 是独立类型而不是一个布尔量

「拿不到」与「拿到了但是空的」是两种完全不同的状态。把它们合并会让
「北向资金数据未覆盖」在回答里变成「北向资金无异常流入」——一个凭空
制造的结论。`available` / `unavailable` 分开列表，使回答层必须显式声明
没覆盖到什么。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# 具备量纲、可做百分比/比率换算的单位
DIMENSIONLESS_UNITS = frozenset({"ratio", "pct"})

NUMERIC_UNITS = frozenset({"ratio", "pct", "yuan", "count", "days"})


@dataclass(frozen=True)
class Coverage:
    """本轮工具调用覆盖到的与未覆盖到的数据维度。

    **两个字段说的是两件事，同一个维度可以同时出现在两边——这不是矛盾：**

    * `available` 说的是**取数通路**：这个源被问到了、且通路是健康的。
    * `unavailable` 说的是**这个维度的数据**：没拿到，或拿到了但不可引用。

    因此 `available=('trade_ledger',)` +
    `unavailable=('trade_ledger（没有已录入的交易流水）',)` 是一个**合法**声明
    （"工具是好的，只是你还没有流水"），不是自相矛盾。实测（2026-09-23，42 个
    同步工具全量调用）有 11 个工具是这个形状，是刻意设计。

    这层区分是产品语义，与 `degraded` 一起构成判据：

    * 没有记录 → `available` 含源键、`degraded=False`：模型该说"尚未录入"；
    * 读取失败 → `available` 为空、`degraded=True`：真取不到。

    把"没有记录"讲成"数据源故障"会让用户以为平台坏了，反过来把"故障"讲成
    "没有记录"会让用户以为自己没录。守这条的是
    `tests/test_l1_read_tools.py::test_trade_ledger_no_trades_keeps_the_source_available`
    的 docstring，以及 `tests/test_l1_decision_tools.py` /
    `tests/test_portfolio_risk_tool.py` 里同形的共 9 条测试。

    **不要**把 `available ∩ unavailable = ∅` 当不变量立起来（我一度想这么改：它会让
    上述 9 条测试全红，并把这两种情况在 coverage 上变得一模一样）。
    """

    available: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()

    def merged(self, other: "Coverage") -> "Coverage":
        return Coverage(
            available=tuple(dict.fromkeys(self.available + other.available)),
            unavailable=tuple(dict.fromkeys(self.unavailable + other.unavailable)),
        )


@dataclass(frozen=True)
class EvidenceItem:
    """一条可被引用的事实。"""

    id: str
    field: str
    value: Any
    unit: str
    source_key: str
    as_of: str

    @property
    def is_numeric(self) -> bool:
        return self.unit in NUMERIC_UNITS and isinstance(self.value, (int, float)) and not isinstance(self.value, bool)


@dataclass(frozen=True)
class EvidenceSet:
    """一轮回答所依据的全部证据。"""

    items: tuple[EvidenceItem, ...] = ()

    def get(self, item_id: str) -> EvidenceItem | None:
        for item in self.items:
            if item.id == item_id:
                return item
        return None

    def ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.items)

    def cited(self, refs: tuple[str, ...]) -> tuple[EvidenceItem, ...]:
        found = []
        for ref in refs:
            item = self.get(ref)
            if item is not None:
                found.append(item)
        return tuple(found)

    def as_of_of(self, refs: tuple[str, ...]) -> frozenset[str]:
        return frozenset(item.as_of for item in self.cited(refs))

    @classmethod
    def from_items(cls, items) -> "EvidenceSet":
        return cls(items=tuple(items))


@dataclass(frozen=True)
class Claim:
    """一条带引用的断言。正文由渲染器从它生成，而不是模型自由书写。"""

    text: str
    refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class StructuredAnswer:
    """模型输出的结构化回答。

    `declared_unavailable` 是「不知道」的显式出口：乱说的反面不是少说，
    而是明确说出哪里没有数据。
    """

    summary: str
    claims: tuple[Claim, ...] = ()
    verdict_ref: str | None = None
    declared_unavailable: tuple[str, ...] = ()
    falsifier_observed: str | None = None
    rendered_note: str | None = None


@dataclass(frozen=True)
class CheckResult:
    """校验结果。失败时必须携带足够信息让上层发起一次有界修复。"""

    ok: bool
    reason: str | None = None
    failing_claim: str | None = None
    detail: str | None = None
    refs_available: tuple[str, ...] = ()
    # 标记这是一次**弱校验**（文本数值后置校验）：它能确认数字存在，但不能
    # 确认引对了哪条证据。统计必须能区分强弱两条路径，否则「加了校验还会错」
    # 永远查不清。
    weak: bool = False

    @classmethod
    def accepted(cls, refs_available: tuple[str, ...] = ()) -> "CheckResult":
        return cls(ok=True, refs_available=refs_available)

    @classmethod
    def rejected(
        cls,
        reason: str,
        *,
        failing_claim: str | None = None,
        detail: str | None = None,
        refs_available: tuple[str, ...] = (),
    ) -> "CheckResult":
        return cls(
            ok=False,
            reason=reason,
            failing_claim=failing_claim,
            detail=detail,
            refs_available=refs_available,
        )


@dataclass(frozen=True)
class TurnVerdict:
    """一轮对话中由确定性探针产出的判定。回答只能引用它，不能改写。"""

    id: str
    probe: str
    symbol: str
    as_of: str
    state: str
    confidence: int
    falsifiers: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()
    daily_only: bool = False
    high_confidence: bool = False
    signal_version: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
