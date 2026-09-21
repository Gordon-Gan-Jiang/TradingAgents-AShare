"""信号层的值对象与不变量。

## 为什么 contribution 是派生属性而不是字段

一个组件同时携带 `side`（方向）、`weight`（权重）、`magnitude`（强度）和
`contribution`（带符号贡献）时，四者之间存在强一致性约束。把 contribution
存成字段意味着可以构造出 `side="washout"` 而 `contribution < 0` 的矛盾结果——
这类矛盾无法靠测试穷举（组合太多），只能靠**结构**消除：让 contribution 由
其余三者推导，矛盾在类型层面就不存在。

## 为什么证据缺失要抛异常

`analyst_structured` 的提示词早已要求 evidence anchor 的 value「必须逐字拷贝
自上文工具数据」，但全仓库没有任何校验器——提示词约束被静默忽略。这里把
「有贡献就必须有锚点」写成 `assert_well_formed()` 并在探针出口调用，把
注释里的要求变成会失败的调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

# 判定状态（四态，见 spec `washout-probe`）
STATE_WASHOUT = "washout"
STATE_DISTRIBUTION = "distribution"
STATE_TREND_BREAK = "trend_break"
STATE_INSUFFICIENT = "insufficient_evidence"
VERDICT_STATES = (STATE_WASHOUT, STATE_DISTRIBUTION, STATE_TREND_BREAK, STATE_INSUFFICIENT)

# 方向
SIDE_WASHOUT = "washout"
SIDE_DISTRIBUTION = "distribution"
# 「趋势破坏」是第三个独立方向：它与派发在**交易含义**上不同——
# 派发是主力主动出货（可能仍有余波），趋势破坏是结构已坏（持有理由需重审）。
# 把它塞进派发侧会让用户看到一个错误的行动含义。
SIDE_TREND_BREAK = "trend_break"
SIDES = (SIDE_WASHOUT, SIDE_DISTRIBUTION, SIDE_TREND_BREAK)

Unit = Literal["ratio", "pct", "yuan", "count", "days", "date", "bool", "text"]


@dataclass(frozen=True)
class Evidence:
    """一条可被回答层引用的原始事实。

    单位（unit）不是装饰：回答层的机械校验器要靠它做单位归一，使
    「3200 万」与「0.32 亿」被判定为同一个值。缺少 unit 的值无法参与
    数值校验，因此这里是必填字段。
    """

    field: str
    value: float | int | str | bool
    unit: Unit
    source_key: str
    as_of: str


@dataclass(frozen=True)
class Component:
    """一个证据维度的计算结果。

    magnitude 是**与方向无关**的证据强度（0..1）；side 决定符号；
    weight 是该维度的先验权重（来自带版本号的权重集，可被校准）。
    """

    key: str
    core: bool
    available: bool
    magnitude: float
    side: str | None
    weight: float
    raw: Mapping[str, Any]
    evidence: tuple[Evidence, ...]
    unavailable_reason: str | None = None
    # 该维度的输入是**模型估计**而非原始数据（例如筹码分布由日线自算）。
    # 必须显式标出来：把估计当事实是本平台最不容忍的失败，而"可用"不等于"原始"。
    estimated: bool = False

    @property
    def weighted_magnitude(self) -> float:
        """无权重的带符号强度（仅取绝对值）：weight × magnitude。"""
        if not self.available or self.side is None:
            return 0.0
        return self.weight * self.magnitude

    @property
    def contribution(self) -> float:
        """带符号贡献：正 = 洗盘侧，负 = 非洗盘侧（派发或趋势破坏）。

        说明：这是**二侧投影**，用于需要单一净值的场景（例如日志、旧接口）。
        探针内部按三侧（washout / distribution / trend_break）分别累加质量，
        因此不要用 contribution 反推三侧质量——那样会把「派发」与「趋势破坏」
        合并，丢失行动含义的差别。
        """
        if not self.available or self.side is None:
            return 0.0
        sign = 1.0 if self.side == SIDE_WASHOUT else -1.0
        return sign * self.weight * self.magnitude


@dataclass(frozen=True)
class Modulator:
    """位置调制器。

    位置**不是**独立加权维度，而是乘在量价证据上的因子（见 design D5）：
    同样一根「缩量阴线」，启动初期是中继洗盘，翻倍高位是派发前兆。
    线性加权会把两者打成相近分数——这正是玩具级模型的典型特征。
    """

    key: str
    available: bool
    raw: Mapping[str, Any]
    factor: float
    evidence: tuple[Evidence, ...]
    unavailable_reason: str | None = None
    # 该维度的输入是**模型估计**而非原始数据（例如筹码分布由日线自算）。
    # 必须显式标出来：把估计当事实是本平台最不容忍的失败，而"可用"不等于"原始"。
    estimated: bool = False


@dataclass(frozen=True)
class Falsifier:
    """证伪条件：什么情况发生，这个判定就错了。

    证伪条件是研究结论与观点的分界线，也是回测判定的前提——没有可检验的
    预测，T+5 回填就无从判定对错。`derived_from` 指向它由哪个组件的观测
    推导而来，保证阈值可追溯、不是凭空写的数字。
    """

    observable: str
    threshold: str
    window_days: int
    description: str
    derived_from: str


@dataclass(frozen=True)
class ProbeResult:
    """所有探针的统一输出形状。

    持久化、回填、校准统计与前端卡片**只认这个类型**，因此新增探针
    （建仓/主升/退潮）不需要改动它们中的任何一个——`probe` 字段是
    分组键。
    """

    probe: str
    symbol: str
    as_of: str
    state: str
    confidence: int
    margin: float
    score_washout: float
    score_distribution: float
    # 趋势破坏是第三个独立质量：与派发合并会让「结构已坏」与「主力出货」
    # 这两个交易含义不同的结论变得无法区分。
    score_trend_break: float = 0.0
    components: tuple[Component, ...] = field(default_factory=tuple)
    modulators: tuple[Modulator, ...] = field(default_factory=tuple)
    falsifiers: tuple[Falsifier, ...] = field(default_factory=tuple)
    # 输入为**模型估计**的维度（如自算筹码）。回答必须说明其为估计，
    # 不得表述为交易所原始数据——这条不由模型自觉，靠证据与提示词共同保证。
    estimated_dimensions: tuple[str, ...] = ()
    # 「高置信」是结构条件（分时 + 筹码可得 且 margin ≥ 阈值），不是分数阈值
    high_confidence: bool = False
    # 本次评估中被触发的门控（例如缩量证据因板块换手缺失而被撤回）
    applied_gates: tuple[str, ...] = field(default_factory=tuple)
    unavailable: tuple[str, ...] = field(default_factory=tuple)
    daily_only: bool = False
    signal_version: str = ""
    # 本次判定实际用到的日线截止日。freshness 需要它才能区分 fresh 与 stale——
    # 没有它就只能标 unknown，而「不知道」对下游没有价值。
    data_cutoff: str = ""

    def component(self, key: str) -> Component | None:
        for c in self.components:
            if c.key == key:
                return c
        return None

    @property
    def modulator(self) -> Modulator | None:
        return self.modulators[0] if self.modulators else None

    def side_of(self, side: str) -> float:
        return {
            SIDE_WASHOUT: self.score_washout,
            SIDE_DISTRIBUTION: self.score_distribution,
            SIDE_TREND_BREAK: self.score_trend_break,
        }.get(side, 0.0)

    @property
    def is_abstention(self) -> bool:
        return self.state == STATE_INSUFFICIENT


def assert_well_formed(components: tuple[Component, ...] | list[Component]) -> None:
    """校验组件集合的证据完整性。

    规则：任何**非零贡献**的组件都必须携带至少一条证据锚点。
    未通过则说明该结果在断言一个无法溯源的事实，必须拒绝返回，
    而不是让它流到回答层——回答层的校验器不负责修复源头。
    """
    for c in components:
        if c.contribution == 0.0:
            continue
        if not c.evidence:
            raise ValueError(
                f"component {c.key!r} contributes {c.contribution} but carries no evidence"
            )


@dataclass(frozen=True)
class Aggregation:
    """三侧质量聚合结果。

    单独建模而不是返回一个 dict，是为了让「位置因子」与「门控」这两个
    **改变质量但本身不是质量** 的量有明确的位置 —— 它们必须可被回答层解释
    （「因为板块换手数据缺失，缩量侧加分被撤回」），而不能悄悄消失在数字里。
    """

    sides: Mapping[str, float]
    factor: float
    position_raw: Mapping[str, Any]
    gates: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()

    @property
    def total(self) -> float:
        return sum(self.sides.values())

    @property
    def leading_side(self) -> str | None:
        if not self.sides:
            return None
        return max(self.sides.items(), key=lambda kv: kv[1])[0]

    @property
    def margin(self) -> float:
        """相对分离度 = (最大质量 − 次大质量) / 总质量。

        用比值而非绝对分数，使不同标的、不同规模下的门控门槛一致 —— 否则
        大盘股与小盘股需要两套阈值，校准会退化成按标的类别手工调参。
        """
        if self.total <= 0:
            return 0.0
        ordered = sorted(self.sides.values(), reverse=True)
        top = ordered[0]
        second = ordered[1] if len(ordered) > 1 else 0.0
        return (top - second) / self.total
