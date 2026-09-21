### Requirement: 自然语言规划
The system SHALL accept arbitrary natural-language investment questions and produce an explicit, auditable plan before executing any tool.

#### Scenario: 规则未知的自然问句
- **WHEN** 用户提交「帮我物色几个短线机会」这类规则分类器无法判定的问句
- **THEN** 系统 MUST 交由 LLM 规划层产出结构化 Plan，MUST NOT 直接以「未能识别标的」拒绝

#### Scenario: 计划结构化且可校验
- **WHEN** 规划层返回计划
- **THEN** 计划 MUST 通过 schema 校验（意图、标的、档位、步骤、是否需澄清），校验失败 MUST 降级为中档并追问，MUST NOT 直接执行

#### Scenario: 计划可审计
- **WHEN** 一轮对话结束
- **THEN** 该轮的计划 MUST 被持久化，使同一问句的路径可事后复盘

#### Scenario: 知识型问题不等于需要工具
- **WHEN** 用户问的是平台无法用数据回答的概念性问题（如「什么是洗盘」）
- **THEN** 系统 MUST 允许模型直接回答并标注该回答不依赖平台数据，MUST NOT 强行调用工具

### Requirement: 工具调用受确定性信封约束
The system SHALL enforce budget, timeout, idempotency and circuit-breaking in code, regardless of what the plan requests.

#### Scenario: 超预算在执行前拒绝
- **WHEN** 计划预估成本超出该档预算
- **THEN** 系统 MUST 在执行前拒绝该计划并说明原因，MUST NOT 执行到一半再截断

#### Scenario: 结果复用
- **WHEN** 同一会话内对同一标的、同一基准日重复请求同一工具
- **THEN** 系统 MUST 复用已有结果，MUST NOT 重复发起同一次外部取数

#### Scenario: 数据源熔断
- **WHEN** 某数据源在短时间内连续失败达到阈值
- **THEN** 系统 MUST 短路该源并在回答中声明，MUST NOT 继续等待每次超时

### Requirement: 平台能力原子化
The system SHALL expose platform capabilities as tools at decision granularity, and every tool SHALL be declared once and shared by all consumers.

#### Scenario: 决策型工具
- **WHEN** 用户询问需要判断的问题（个股、组合、主线）
- **THEN** 系统 MUST 调用对应的决策型工具，且该工具的结论 MUST NOT 由模型改写

#### Scenario: 分钟级能力异步化
- **WHEN** 用户要求生成完整研报这类分钟级能力
- **THEN** 系统 MUST 以异步任务方式调用（提交后返回任务标识，轮询获取终态），MUST NOT 在同步工具调用中等待完成

#### Scenario: 工具清单按需注入
- **WHEN** 工具数量增长
- **THEN** 系统 MUST 只向模型注入与当前意图相关的证据型工具，决策型工具常驻

### Requirement: 多轮对话有显式状态
The system SHALL maintain explicit conversation slots and an evidence ledger, rather than relying on raw message history.

#### Scenario: 指代消解确定化
- **WHEN** 用户在多轮中说「它」「这只」且上一轮已确定标的
- **THEN** 系统 MUST 通过槽位确定指向，MUST NOT 依赖模型从历史文本中猜测

#### Scenario: 未覆盖项跨轮延续
- **WHEN** 上一轮声明某维度未覆盖，本轮问题仍涉及该维度
- **THEN** 系统 MUST 重新尝试取数或再次声明未覆盖，MUST NOT 静默当成已覆盖

### Requirement: 变更必须可证明更优
The system SHALL be evaluated against a golden set before replacing existing behaviour.

#### Scenario: 回归门槛
- **WHEN** 新版本规划层上线前
- **THEN** 系统 MUST 在黄金集上给出规划准确率、工具精确率/召回率、弃权正确率、P95 延迟与单轮成本，且规划准确率 MUST 不低于上一版本
