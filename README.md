# AlphaPilot A-Share

A 股多智能体投研与决策系统。项目将研究流程拆成分析、辩论、交易与风控节点，通过 FastAPI + React 提供完整产品化能力（分析任务、研报管理、自选与定时、推荐洞察、模拟盘等）。

---

## 1. 项目说明

### 1.1 核心能力
- 多智能体分析链路：分析师并行产出 + 多空研究员辩论 + 交易员 + 风控评审 + 决策校验
- 自然语言驱动分析：支持输入自然语言意图，自动解析标的与分析范围
- 结构化研报：报告落库、检索、详情与导出
- 投研运营能力：自选股、定时分析、跟踪看板、推荐统计、T+1 反馈、模拟盘
- 多模型接入：可在设置中切换模型厂商与模型，并支持 warmup 验证

### 1.2 技术栈
- 后端：`FastAPI`、`SQLAlchemy`、`LangGraph`、`uv`
- 前端：`React`、`TypeScript`、`Vite`、`Zustand`
- 数据：默认 `SQLite`（支持通过 `DATABASE_URL` 切换）
- 运行：源码模式 / Docker 镜像模式

### 1.3 仓库结构
```text
.
├─ api/                    # FastAPI 应用与服务层
│  ├─ main.py              # API 入口、生命周期、任务调度、路由
│  ├─ database.py          # DB engine、ORM 模型、轻量 schema ensure
│  └─ services/            # 业务服务（report/scheduled/recommendation 等）
├─ tradingagents/          # 多智能体核心引擎（graph/agents/dataflows/prompts）
├─ frontend/               # React 前端
├─ tests/                  # pytest 测试
├─ Dockerfile              # 多阶段构建镜像
├─ pyproject.toml          # Python 依赖与脚本入口
└─ uv.lock                 # 锁文件
```

---

## 2. 快速启动

## 2.1 方式 A：Docker 启动（推荐）

```bash
docker pull ghcr.io/kylinmountain/tradingagents-ashare:latest

mkdir -p "$(pwd)/data"
export TA_APP_SECRET_KEY="$(openssl rand -base64 32)"

docker run -d -p 8000:8000 \
  --name tradingagents \
  -v "$(pwd)/data:/app/data" \
  -e DATABASE_URL="sqlite:///./data/tradingagents.db" \
  -e TA_APP_SECRET_KEY="${TA_APP_SECRET_KEY}" \
  ghcr.io/kylinmountain/tradingagents-ashare:latest
```

访问：`http://localhost:8000`

> Docker 镜像启动命令等价于：`uv run --no-sync tradingagents-api`

## 2.2 方式 B：源码启动（开发常用）

### 前置要求
- Python `>= 3.10`
- Node.js `>= 18`，npm `>= 9`
- 建议安装 `nvm`（仓库内提供 `.nvmrc`：Node `20`）

### 安装依赖
```bash
cd AlphaPilot-A-Share

# 后端依赖
uv sync

# 前端依赖
cd frontend
nvm use
npm install
cd ..
```

### 配置环境变量
当前仓库提供模板文件：`.env copy.example`

```bash
cp ".env copy.example" .env
```

> 如果你的分支存在 `.env.example`，也可使用：`cp .env.example .env`

### 启动（推荐：一键脚本）
```bash
./scripts/dev.sh start
```

访问前端：`http://127.0.0.1:5173`  
后端：`http://127.0.0.1:8000`（文档 `/docs`）

常用命令：
```bash
./scripts/dev.sh status     # 查看 PID 与健康检查
./scripts/dev.sh logs       # 跟踪后端日志（logs f 看前端）
./scripts/dev.sh restart    # 重启
./scripts/dev.sh stop       # 停止并释放端口
./scripts/dev.sh preview    # 编译后以 vite preview 运行（4173）
```

请用 `127.0.0.1` 打开页面。macOS 上 `localhost` 常解析到 IPv6（`::1`），容易出现登录请求 30 秒超时。

### 启动后端（手动）
```bash
uv run python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

### 启动前端（开发热更新，单独终端）
```bash
cd frontend
nvm use
npm run dev
```

前端开发地址：`http://127.0.0.1:5173`  
后端地址：`http://127.0.0.1:8000`

---

## 3. 项目配置（完整）

配置分为两层：
- **系统级环境变量**：通过 `.env` 或容器环境注入
- **用户运行时配置**：通过前端“设置”页或 `PATCH /v1/config` 持久化

## 3.1 环境变量总览（基于 `.env copy.example` + 默认配置）

| 变量 | 是否必填 | 说明 | 示例 |
|---|---|---|---|
| `TA_API_KEY` | 建议 | 默认模型 API Key（也可在用户设置中单独配置） | `sk-xxx` |
| `TA_BASE_URL` | 建议 | 模型网关地址 | `https://api.openai.com/v1` |
| `TA_LLM_PROVIDER` | 建议 | 默认模型提供方标识 | `openai` |
| `TA_LLM_QUICK` | 建议 | 快速模型名（分析链路中快思考节点） | `gpt-4o-mini` |
| `TA_LLM_DEEP` | 建议 | 深度模型名（管理/裁决等重推理节点） | `gpt-4o` |
| `TA_LLM_TEMPERATURE` | 可选 | 采样温度，默认 `0.0` | `0` |
| `TA_APP_SECRET_KEY` | **生产必填** | JWT 与用户密钥加密主密钥，不可随意更换 | `openssl rand -base64 32` |
| `DATABASE_URL` | 可选 | 数据库连接，默认 `sqlite:///./tradingagents.db` | `sqlite:///./data/tradingagents.db` |
| `TA_MAX_DEBATE` | 可选 | 投资辩论轮数 | `1` |
| `TA_MAX_RISK` | 可选 | 风控讨论轮数 | `1` |
| `TA_DECISION_CRITIC_REVISION_THRESHOLD` | 可选 | 决策批评器触发阈值 | `40` |
| `TA_LANGUAGE` | 可选 | 提示词语言（`zh`/`en`/`auto`） | `zh` |
| `TA_TRACE` | 可选 | provider trace 开关 | `1` |
| `CORS_ALLOW_ORIGINS` | 可选 | 逗号分隔 CORS 白名单 | `http://localhost:5173` |
| `CORS_ALLOW_ORIGIN_REGEX` | 可选 | CORS 正则白名单 | `https://.*\\.example\\.com` |
| `ENV` | 可选 | `prod` 时关闭 docs/openapi 暴露 | `prod` |
| `APP_VERSION` | 可选 | 服务版本号 | `v0.2.0` |
| `TA_MAX_WORKERS` | 可选 | 线程池 worker 数 | `2` |
| `TA_JOB_TIMEOUT` | 可选 | 分析任务超时秒数 | `1800` |
| `TA_SCHEDULED_ANALYSIS_MAX_CONCURRENCY` | 可选 | 定时分析并发上限 | `10` |

### 方法论 / 策略知识库相关（可选）
| 变量 | 说明 |
|---|---|
| `TA_METHODOLOGY_ASHARE_FUNDAMENTALS` | 启用 A 股基本面方法论 |
| `TA_METHODOLOGY_ASHARE_NEWS` | 启用新闻事件方法论 |
| `TA_METHODOLOGY_ASHARE_MACRO` | 启用行业宏观方法论 |
| `TA_METHODOLOGY_EXTRA` / `_NEWS` / `_MACRO` | 额外方法论文档路径 |
| `FINSKILLS_ROOT` / `TA_FINSKILLS_ROOT` | 本地 FinSkills 仓库根目录 |
| `TA_METHODOLOGY_STOCK_TEAM` | 启用 stock-analysis-team 方法论 |
| `STOCK_ANALYSIS_TEAM_ROOT` / `TA_STOCK_ANALYSIS_TEAM_ROOT` | stock-analysis-team 根目录 |

### 通知/运营相关（可选）
| 变量 | 说明 |
|---|---|
| `TA_TRACKING_PRICE_ALERTS` | 跟踪看板价格提醒开关 |
| `TA_TRACKING_ALERT_INTERVAL_SEC` | 价格提醒轮询间隔 |
| `TA_TRACKING_SURGE_PCT` | 异动阈值（百分比） |
| `TA_TRACKING_SURGE_COOLDOWN_SEC` | 提醒冷却时间 |
| `TA_RECOMMEND_PUSH_ENABLED` | 推荐推送总开关 |
| `TA_RECOMMEND_PUSH_INTERVAL_SEC` | 推荐推送轮询间隔 |

### VLM / 邮件（可选）
| 变量 | 说明 |
|---|---|
| `TA_VLM_API_KEY` / `TA_VLM_BASE_URL` / `TA_VLM_MODEL` | 持仓截图识别模型配置 |
| `MAIL_HOST` / `MAIL_PORT` / `MAIL_USER` / `MAIL_PASS` / `MAIL_FROM` / `MAIL_SSL` | 邮箱验证码登录配置 |

## 3.2 运行时配置（用户维度）

通过设置页或 API 维护：
- 模型厂商、模型名、API Key、Base URL
- 辩论轮数与风险轮数
- 决策批评器开关与阈值
- 企业微信/WPS webhook 与开关

相关接口：
- `GET /v1/config`
- `PATCH /v1/config`
- `POST /v1/config/warmup`
- `POST /v1/config/wecom/warmup`
- `POST /v1/config/wps/warmup`

---

## 4. 常用命令

## 4.1 后端（Python / uv）

| 命令 | 说明 |
|---|---|
| `uv sync` | 安装/同步依赖 |
| `uv run tradingagents-api` | 使用 pyproject 脚本启动 API |
| `uv run python -m uvicorn api.main:app --port 8000` | 手动启动 API |
| `uv run pytest` | 运行测试 |

## 4.2 前端（frontend）

| 命令 | 说明 |
|---|---|
| `npm run dev` | 本地开发 |
| `npm run build` | 生产构建 |
| `npm run preview` | 本地预览构建结果 |
| `npm run lint` | ESLint 检查 |

---

## 5. API 使用说明（摘要）

认证方式：
- 登录后创建 API Token
- 请求头携带：`Authorization: Bearer <TOKEN>`

高频接口：
- 分析：`POST /v1/analyze`
- 任务状态：`GET /v1/jobs/{job_id}`
- 任务结果：`GET /v1/jobs/{job_id}/result`
- 报告列表：`GET /v1/reports`
- 自选管理：`GET/POST/DELETE /v1/watchlist*`
- 定时任务：`GET/POST/PATCH/DELETE /v1/scheduled*`
- 推荐与洞察：`/v1/recommendations*`、`/v1/insights/t1*`
- 模拟盘：`/v1/paper-portfolio*`、`/v1/paper-trades`

健康检查：
- `GET /healthz`

---

## 6. 生产部署建议

- 必须设置 `TA_APP_SECRET_KEY`，并固定保存
- 明确设置 `DATABASE_URL`（生产建议使用托管数据库）
- 设置 `ENV=prod`，关闭公开文档端点
- 配置 `CORS_ALLOW_ORIGINS`，避免宽泛跨域
- 用反向代理（Nginx/Caddy）提供 HTTPS
- 对定时分析并发设置 `TA_SCHEDULED_ANALYSIS_MAX_CONCURRENCY`
- 建议配置日志采集与告警（重点关注 `Scheduler`/`RecommendationPush`/`TrackingAlert`）

---

## 7. 常见问题（FAQ）

### Q1：启动后无法访问前端页面，或登录「发送验证码」超时？
- 优先用 `./scripts/dev.sh start`，浏览器打开 `http://127.0.0.1:5173`（不要用 `localhost`，避免 IPv6 超时）
- `./scripts/dev.sh status` 确认后端 `healthz=ok`；失败则看 `./scripts/dev.sh logs`
- 源码模式也可单独 `npm run dev`（Vite 会把 `/v1` 代理到后端）或 `npm run build` 后由后端静态托管
- Docker 模式镜像已内置前端构建产物

### Q2：分析任务一直 pending / running？
- 检查模型配置是否可用（设置页执行 warmup）
- 查看后端日志中是否有上游模型超时/鉴权错误
- 检查 `TA_JOB_TIMEOUT` 与外部网络连通性

### Q3：定时任务不触发？
- 确认任务 `is_active=true`
- 确认系统当前时间、交易日判断与触发时间窗口
- 查看调度日志前缀 `[Scheduler]`

### Q4：更换 `TA_APP_SECRET_KEY` 后无法解密历史配置？
- 该密钥涉及用户密钥加密与 JWT，生产环境应一次设置后长期固定
- 临时切换密钥可能导致历史密文不可读

---

## 8. 测试与质量

```bash
uv run pytest
```

测试目录：`tests/`  
覆盖重点：API 冒烟、调度流程、推荐链路、配置流程、通知与模拟盘核心路径。

---

## 9. 许可与免责声明

- 本项目基于 [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) 二次开发
- 许可说明以仓库根目录 `LICENSE` 为准
- 本项目仅供学习研究与技术演示，不构成任何投资建议

