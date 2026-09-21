### Requirement: 无标的的全市场筛选意图
The system SHALL recognize a market-wide screening request that names no individual instrument, and SHALL execute it instead of rejecting the turn.

#### Scenario: 明确的筛选问句
- **WHEN** 用户提交「今天走到好的票，放量票找十个给我」，且消息中不含任何个股标的
- **THEN** 系统 MUST 判定为全市场筛选意图并执行扫描，MUST NOT 返回「未能识别标的」的 400

#### Scenario: 显式代码优先
- **WHEN** 消息中同时含筛选措辞与 6 位股票代码（如「600519.SH 放量了吗」）
- **THEN** 系统 MUST 按单标的研判处理，MUST NOT 触发全市场扫描

#### Scenario: 无法识别且非筛选
- **WHEN** 消息既无标的也不属于筛选意图（如「帮我看看还能不能拿」）
- **THEN** 系统 MUST 返回 400，且提示必须同时给出「补标的」与「改用筛选措辞」两条可行动路径

### Requirement: 筛选参数的解析
The system SHALL parse the requested candidate count and scoring profile from the screening text using a deterministic, zero-LLM function.

#### Scenario: 数量与档案
- **WHEN** 用户说「放量票找十个给我」
- **THEN** 解析结果 MUST 给出 `top_k=10` 且档案 MUST 为 `breakout`

#### Scenario: 缺省值保守
- **WHEN** 用户未说明数量
- **THEN** `top_k` MUST 取保守缺省值且 MUST NOT 超过扫描器允许的上限

### Requirement: 候选与依据必须可溯源
The system SHALL derive every candidate and every stated reason from the scanner result, so that a claim about a candidate is verifiable against evidence.

#### Scenario: 逐候选证据
- **WHEN** 扫描返回 N 只候选
- **THEN** 工具结果 MUST 为每只候选产出可引用的证据条目（含代码、名称、得分与入选理由）
- **THEN** 回答中出现的候选代码 MUST 能在被引用的证据中找到，否则校验器 MUST 拒绝该条断言

#### Scenario: 空结果不编造
- **WHEN** 扫描返回 0 只候选
- **THEN** 回答 MUST 如实说明未取得候选，MUST NOT 生成任何候选代码

### Requirement: 数据缺失必须显式声明
The system SHALL declare unavailable screening factors rather than presenting an unconfirmed factor as confirmed.

#### Scenario: 量比缺失
- **WHEN** 用户要求「放量票」而扫描结果中 `volume_ratio` 为缺失值
- **THEN** 回答 MUST 声明量比未提供、放量未经确认，MUST NOT 表述为已确认放量

#### Scenario: 扫描范围与数据时点
- **WHEN** 扫描完成
- **THEN** 回答 MUST 可引用地给出扫描股票数、评分股票数、数据时点与数据来源

### Requirement: 耗时必须诚实告知
The system SHALL disclose the expected and actual cost of a full-market scan.

#### Scenario: 开始前告知
- **WHEN** 筛选回合开始
- **THEN** 系统 MUST 在扫描前发出过程提示，说明扫描耗时量级

#### Scenario: 超出档位目标延迟
- **WHEN** 回合实际耗时超过该档位声明的目标延迟
- **THEN** 回合结果 MUST 记录该事实并向用户提示实际耗时，MUST NOT 静默超出
