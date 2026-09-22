"""L3 分钟级能力的**提交入口**（服务层）与三个新工具。

## 本文件锁住什么

背景：全文研报生成 / 每日复盘 / 模型对比这三个分钟级能力，此前**只**能从
`api/main.py` 的路由里发起——"建任务记录 → 推 job.created → 投递后台任务"三步
内联在路由处理函数里。于是服务层没有可复用的提交入口，对话 Agent 也就无从调用
（照抄一份 = 两条迟早漂移的任务链路）。

本文件分四段，逐段对应一条纪律：

1. **工具协议**（L3 必须异步、证据必须含任务号+参数+「不得声称已有结论」、
   提交失败要如实降级且不外传上游原文）；
2. **服务提交序列**（建记录 / 推事件 / 投递后台任务，且**不真的跑**分钟级任务）；
3. **路由改调服务但行为不变**（同入参校验、同返回体）；
4. **端口注入**（user_id 在闭包里绑定；未注入即不注册）。

## 为什么不联网、不跑真实任务

后台执行被整体替换成 `_FakeRuntime`：`create_task` 只记录协程并 close 掉，
`run_analysis_job` 永不执行。分钟级管道在测试里跑一次就是几分钟 + 真金白银的
token，这不是"慢"，是根本不该发生。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

import pytest


# ══════════════════════════════════════════════════════════════════════════
# 一、工具协议：L3 异步提交
# ══════════════════════════════════════════════════════════════════════════


class _FakeTaskPort:
    """回测端口（既有协议）+ 三个新提交方法；只记录调用，不跑任何任务。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.backtests: list[dict] = []
        self.deep_analyses: list[dict] = []
        self.daily_reviews: list[dict] = []
        self.arena_runs: list[dict] = []

    # —— 既有：回测 + 轮询 ——
    def submit_backtest(self, *, symbol, start_date, end_date, hold_days, sample_interval):
        self.backtests.append(
            {
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
                "hold_days": hold_days,
                "sample_interval": sample_interval,
            }
        )
        if self._error is not None:
            raise self._error
        return "bt-1"

    def job_status(self, *, job_id):
        if self._error is not None:
            raise self._error
        return {"status": "pending"}

    # —— 新增：三个分钟级提交 ——
    def submit_deep_analysis(self, *, symbol, trade_date, horizons, selected_analysts, query):
        self.deep_analyses.append(
            {
                "symbol": symbol,
                "trade_date": trade_date,
                "horizons": list(horizons),
                "selected_analysts": list(selected_analysts),
                "query": query,
            }
        )
        if self._error is not None:
            raise self._error
        return {"job_id": "analysis-1"}

    def submit_daily_review(self, *, mode, top_k, market, horizons, selected_analysts):
        self.daily_reviews.append(
            {
                "mode": mode,
                "top_k": top_k,
                "market": market,
                "horizons": list(horizons),
                "selected_analysts": list(selected_analysts),
            }
        )
        if self._error is not None:
            raise self._error
        return {"run_id": "run-1", "status": "pending", "queued_jobs": 3, "job_ids": ["j1", "j2", "j3"]}

    def submit_model_arena(self, *, symbol, trade_date, horizons, selected_analysts, model_profile_ids, query):
        self.arena_runs.append(
            {
                "symbol": symbol,
                "trade_date": trade_date,
                "horizons": list(horizons),
                "selected_analysts": list(selected_analysts),
                "model_profile_ids": list(model_profile_ids),
                "query": query,
            }
        )
        if self._error is not None:
            raise self._error
        return {
            "experiment_id": "exp-1",
            "input_snapshot_hash": "hash-1",
            "job_ids": ["a1", "a2"],
        }


class _BacktestOnlyPort:
    """只有回测方法的端口：三个新工具**不得**注册（不留一调就炸的条目）。"""

    def submit_backtest(self, *, symbol, start_date, end_date, hold_days, sample_interval):
        return "bt-1"

    def job_status(self, *, job_id):
        return None


def _tools(port: Any) -> dict[str, Any]:
    from tradingagents.tools.tasks import make_task_tools

    return {spec.name: spec for spec in make_task_tools(port)}


def _evidence_text(result: Any) -> str:
    return " ".join(str(item.value) for item in result.evidence)


def test_three_new_submit_tools_are_l3_async():
    from tradingagents.tools.spec import PLAN_LAYER_TASK

    tools = _tools(_FakeTaskPort())
    for name in ("submit_deep_analysis_job", "submit_daily_review_job", "submit_model_arena_job"):
        spec = tools[name]
        assert spec.plan_layer == PLAN_LAYER_TASK, f"{name} 是分钟级能力，必须落在 L3"
        assert spec.asynchronous is True, f"{name} 必须异步：分钟级管道放进同步工具面 = 客户端必然超时重试"


def test_deep_analysis_submit_evidence_has_job_id_params_and_no_claim_rule():
    port = _FakeTaskPort()
    spec = _tools(port)["submit_deep_analysis_job"]
    result = spec.handler(symbol="600519.SH", trade_date="2026-09-18", as_of="2026-09-21")

    assert result.data["job_id"] == "analysis-1"
    text = _evidence_text(result)
    assert "analysis-1" in text, "任务号必须进证据"
    assert "600519.SH" in text, "参数必须进证据"
    assert "poll_async_job" in text, "要告诉模型怎么续接"
    assert "不得" in text, "要明确禁止声称已有结论——这是防编造的关键一条"


def test_deep_analysis_submit_defaults_are_declared_not_silent():
    """未给 horizons/analysts 时给出**明示的**默认，而不是空列表糊过去。"""
    port = _FakeTaskPort()
    spec = _tools(port)["submit_deep_analysis_job"]
    spec.handler(symbol="600519.SH")

    call = port.deep_analyses[-1]
    assert call["horizons"], "空 horizons 会让后台任务建不出 horizon"
    assert call["selected_analysts"], "空分析师列表等于提交一个什么都不同做的任务"


def test_daily_review_submit_evidence_reports_queued_jobs():
    port = _FakeTaskPort()
    spec = _tools(port)["submit_daily_review_job"]
    result = spec.handler(mode="recommended", top_k=3, as_of="2026-09-21")

    assert result.data["run_id"] == "run-1"
    text = _evidence_text(result)
    assert "run-1" in text
    assert "3" in text, "排入队列的任务数必须进证据（否则模型不知道有多少活在跑）"
    assert "poll_async_job" in text and "不得" in text


def test_model_arena_submit_evidence_reports_experiment_and_jobs():
    port = _FakeTaskPort()
    spec = _tools(port)["submit_model_arena_job"]
    result = spec.handler(symbol="600519.SH", as_of="2026-09-21")

    assert result.data["experiment_id"] == "exp-1"
    text = _evidence_text(result)
    assert "exp-1" in text
    assert "a1" in text and "a2" in text, "每个模型一个任务号，必须逐个列出"
    assert "poll_async_job" in text and "不得" in text


def test_submits_require_their_minimum_input():
    tools = _tools(_FakeTaskPort())
    for name in ("submit_deep_analysis_job", "submit_model_arena_job"):
        result = tools[name].handler(symbol="")
        assert result.degraded is True, f"{name} 缺 symbol 必须降级而不是提交一个空任务"
        assert result.coverage.unavailable == ("async_task",)


def test_submit_failure_degrades_without_leaking_upstream_detail():
    for name in ("submit_deep_analysis_job", "submit_daily_review_job", "submit_model_arena_job"):
        spec = _tools(_FakeTaskPort(error=RuntimeError("queue full: internal detail")))[name]
        kwargs = {"as_of": "2026-09-21"}
        if name != "submit_daily_review_job":
            kwargs["symbol"] = "600519.SH"
        result = spec.handler(**kwargs)

        assert result.degraded is True
        assert "RuntimeError" in str(result.degraded_reason), "原因类型要给出来"
        blob = f"{result.data} {result.degraded_reason} {_evidence_text(result)}"
        assert "internal detail" not in blob, "上游原文不得外传"
        assert result.coverage.unavailable == ("async_task",)


def test_zero_valued_params_are_clamped_not_replaced_by_default():
    """`x or default` 会把 0 换成默认值——本仓库已踩过四次，这里逐条锁住。"""
    port = _FakeTaskPort()
    tools = _tools(port)

    tools["submit_daily_review_job"].handler(mode="recommended", top_k=0)
    assert port.daily_reviews[-1]["top_k"] == 1, "top_k=0 应被 clamp 到下限 1，而不是变成默认值"

    tools["submit_backtest_job"].handler(
        symbol="600519.SH", start_date="2026-01-01", end_date="2026-06-30",
        hold_days=0, sample_interval=0,
    )
    assert port.backtests[-1]["hold_days"] == 1
    assert port.backtests[-1]["sample_interval"] == 1


def test_new_submit_tools_are_not_registered_without_port_methods():
    from tradingagents.tools import build_registry

    without = build_registry(ports_factory=lambda *a: object(), news_fetcher=lambda *a: [])
    for name in ("submit_deep_analysis_job", "submit_daily_review_job", "submit_model_arena_job"):
        assert name not in without.names(), "没有端口就不该在册"

    partial = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [], task_port=_BacktestOnlyPort()
    )
    for name in ("submit_deep_analysis_job", "submit_daily_review_job", "submit_model_arena_job"):
        assert name not in partial.names(), "端口没实现的方法对应工具不注册（不留一调就炸的条目）"

    full = build_registry(
        ports_factory=lambda *a: object(), news_fetcher=lambda *a: [], task_port=_FakeTaskPort()
    )
    for name in ("submit_deep_analysis_job", "submit_daily_review_job", "submit_model_arena_job"):
        assert name in full.names_by_layer("L3")


# ══════════════════════════════════════════════════════════════════════════
# 二、服务提交序列（不跑真实任务）
# ══════════════════════════════════════════════════════════════════════════


class _FakeRuntime:
    """应用层任务运行时的替身：记录一切，执行零。"""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.events: list[tuple[str, str, dict]] = []
        self.created_tasks: list[Any] = []
        self.daily_runs: dict[str, dict] = {}
        self.executed: list[tuple] = []
        self.finalized: list[str] = []
        self._seq = 0

    def new_job_id(self) -> str:
        self._seq += 1
        return f"job-{self._seq}"

    def now_iso(self) -> str:
        return "2026-09-21T00:00:00+00:00"

    def set_job(self, job_key: str, **kwargs) -> None:
        # 与 `api.main._set_job(job_key, **kwargs)` 同形：任务号既是位置参数，
        # 也在 payload 的 job_id 字段里。
        self.jobs.setdefault(job_key, {}).update(kwargs)

    def get_job(self, job_id: str) -> Mapping[str, Any]:
        return dict(self.jobs.get(job_id, {}))

    def ensure_event_queue(self, job_id: str) -> Any:
        return None

    def emit_event(self, job_id: str, event: str, data: Mapping[str, Any]) -> None:
        self.events.append((job_id, event, dict(data)))

    def create_task(self, coro, **kwargs):
        # 真实实现是 asyncio.create_task + 引用保活；这里只记录并丢弃协程，
        # 保证分钟级管道**一次都不会被执行**。
        self.created_tasks.append(
            {
                "label": kwargs.get("label"),
                "coro": getattr(getattr(coro, "cr_code", None), "co_name", "?"),
            }
        )
        coro.close()
        return object()

    async def run_analysis_job(self, job_id, request, stream_events, save_report, user_id, source):
        self.executed.append((job_id, source))
        self.set_job(job_id, status="completed")

    def set_daily_run(self, run_id: str, **kwargs) -> None:
        self.daily_runs.setdefault(run_id, {}).update(kwargs)

    async def finalize_daily_run(self, run_id: str) -> None:
        self.finalized.append(run_id)


def _fake_runtime():
    from api.services import analysis_job_service

    fake = _FakeRuntime()
    runtime = analysis_job_service.JobRuntime(
        new_job_id=fake.new_job_id,
        now_iso=fake.now_iso,
        set_job=fake.set_job,
        get_job=fake.get_job,
        ensure_event_queue=fake.ensure_event_queue,
        emit_event=fake.emit_event,
        create_task=fake.create_task,
        run_analysis_job=fake.run_analysis_job,
        set_daily_run=fake.set_daily_run,
        finalize_daily_run=fake.finalize_daily_run,
    )
    return fake, runtime


def test_deep_analysis_submit_records_job_then_schedules():
    from api.services import analysis_job_service

    fake, runtime = _fake_runtime()
    submitted = analysis_job_service.submit_deep_analysis_task(
        user_id="u1", symbol="600519.SH", trade_date="2026-09-18", runtime=runtime
    )

    assert submitted.job_id == "job-1"
    assert submitted.status == "pending", "提交必须立即返回，不得等待分钟级结果"
    record = fake.jobs["job-1"]
    assert record["status"] == "pending"
    assert record["user_id"] == "u1"
    assert record["symbol"] == "600519.SH"
    assert fake.events[0][:2] == ("job-1", "job.created")
    assert len(fake.created_tasks) == 1, "投递了后台任务"
    assert fake.executed == [], "提交阶段不得执行分析管道"


def test_deep_analysis_dry_run_waits_inline():
    from api.services import analysis_job_service
    from api.main import AnalyzeRequest

    fake, runtime = _fake_runtime()
    request = AnalyzeRequest(symbol="600519.SH", trade_date="2026-09-18", dry_run=True,
                             query="分析贵州茅台短线机会")
    submitted = asyncio.run(
        analysis_job_service.submit_deep_analysis(request=request, user_id="u1", runtime=runtime)
    )

    assert fake.executed == [("job-1", "api")], "dry_run 就是就地跑完（HTTP 契约不变）"
    assert submitted.status == "completed"
    assert fake.created_tasks == []


def test_deep_analysis_async_path_does_not_wait():
    from api.services import analysis_job_service
    from api.main import AnalyzeRequest

    fake, runtime = _fake_runtime()
    request = AnalyzeRequest(symbol="600519.SH", trade_date="2026-09-18",
                             query="分析贵州茅台短线机会")
    submitted = asyncio.run(
        analysis_job_service.submit_deep_analysis(request=request, user_id="u1", runtime=runtime)
    )

    assert submitted.status == "pending"
    assert fake.executed == []
    assert len(fake.created_tasks) == 1


def test_deep_analysis_prompt_template_error_is_a_domain_error(monkeypatch):
    """模板 ID 无效 → 路由能翻译成 400；异常类型由服务层定义，不靠裸 ValueError 认脸。"""
    from api.services import analysis_job_service, prompt_template_service

    fake, runtime = _fake_runtime()

    def _boom(*args, **kwargs):
        raise ValueError("模板不存在")

    monkeypatch.setattr(prompt_template_service, "resolve_template", _boom)
    with pytest.raises(analysis_job_service.PromptTemplateError):
        analysis_job_service.submit_deep_analysis_task(
            user_id="u1", symbol="600519.SH", runtime=runtime
        )


def test_deep_analysis_task_binds_user_and_never_accepts_it_from_params():
    """user_id 只从入参拿（由端口闭包绑定），工具参数里没有它。"""
    from api.services import analysis_job_service

    fake, runtime = _fake_runtime()
    analysis_job_service.submit_deep_analysis_task(
        user_id="u-owner", symbol="300750.SZ", runtime=runtime
    )
    assert fake.jobs["job-1"]["user_id"] == "u-owner"


@pytest.fixture()
def _profile_user():
    """带一个启用模型档案的临时用户（测试库由 conftest 隔离）。"""
    from api.database import UserDB, get_db_ctx, init_db
    from api.services import auth_service, model_profile_service

    init_db()
    user_id = str(uuid4())
    now = datetime.now(timezone.utc)
    with get_db_ctx() as db:
        db.add(
            UserDB(
                id=user_id,
                email=auth_service.normalize_email(f"l3-{user_id[:8]}@test.com"),
                is_active=True,
                created_at=now,
                updated_at=now,
                last_login_at=now,
            )
        )
        db.commit()
        profile = model_profile_service.create_model_profile(
            db,
            user_id=user_id,
            name="L3 测试档案",
            llm_provider="openai",
            quick_think_llm="deepseek-v4-flash",
            deep_think_llm="deepseek-v4-pro",
            backend_url="https://api.deepseek.com/v1",
            is_default=True,
            is_active=True,
        )
    return user_id, profile


def _stub_recommendation(monkeypatch):
    """把推荐管道打成确定性输出：服务测试不触网、不跑扫描。"""
    from api.services import recommendation_service

    monkeypatch.setattr(
        recommendation_service,
        "recommend_for_user",
        lambda **kwargs: {
            "pool_size": 1,
            "scored_size": 1,
            "items": [
                {
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "score": 88.0,
                    "code": "600519.SH",
                    "reasons": ["测试用推荐理由"],
                }
            ],
            "scoring_model": {"profile": "ashare_balanced"},
        },
    )


def test_daily_review_submit_queues_jobs_and_records_run(monkeypatch):
    from api.database import get_db_ctx
    from api.services import analysis_job_service

    _stub_recommendation(monkeypatch)
    fake, runtime = _fake_runtime()

    with get_db_ctx() as db:
        submitted = analysis_job_service.submit_daily_review(
            request=analysis_job_service.build_daily_review_request(mode="recommended", top_k=1),
            user_id="u-daily",
            db=db,
            runtime=runtime,
        )

    assert submitted.run_id
    assert submitted.status == "pending"
    assert len(submitted.jobs) == 1, "入选标的必须各排一个分析任务"
    analyze_job_id = submitted.jobs[0].job_id
    assert fake.jobs[analyze_job_id]["request_source"] == "daily_product_batch"
    assert fake.daily_runs[submitted.run_id]["status"] == "pending"
    assert fake.created_tasks[-1]["coro"] == "finalize_daily_run", (
        "批跑收尾任务（轮询 + 推送）必须被投递，否则批跑会永远停在 pending"
    )
    assert fake.executed == [], "提交阶段不得执行分析管道"


def test_daily_review_task_opens_its_own_session(monkeypatch):
    """对话工具没有 `Depends(get_db)`：服务必须能自己开会话跑完同一份实现。

    这条单独测，是因为 `db=None` 分支是**工具侧唯一会走到的分支**——只测"路由传会话"
    那条，等于工具路径的会话生命周期没有任何覆盖。
    """
    from api.services import analysis_job_service

    _stub_recommendation(monkeypatch)
    fake, runtime = _fake_runtime()
    submitted = analysis_job_service.submit_daily_review_task(
        user_id="u-tool", mode="recommended", top_k=1, runtime=runtime
    )

    assert submitted.run_id
    assert len(submitted.jobs) == 1
    assert fake.jobs[submitted.jobs[0].job_id]["user_id"] == "u-tool"
    assert fake.executed == []


def test_model_arena_submit_creates_one_job_per_profile(_profile_user):
    from api.services import analysis_job_service

    user_id, profile = _profile_user
    fake, runtime = _fake_runtime()
    submitted = analysis_job_service.submit_model_arena_task(
        user_id=user_id, symbol="600519.SH", trade_date="2026-09-18", runtime=runtime
    )

    assert len(submitted.jobs) == 1
    assert submitted.jobs[0].model_profile_id == profile["id"]
    record = fake.jobs[submitted.jobs[0].job_id]
    assert record["experiment_id"] == submitted.experiment_id
    assert record["input_snapshot_hash"] == submitted.input_snapshot_hash
    assert fake.executed == [], "提交阶段不得执行模型对比"


def test_model_arena_submit_rejects_unknown_profile(_profile_user):
    from api.services import analysis_job_service

    user_id, _profile = _profile_user
    fake, runtime = _fake_runtime()
    with pytest.raises(analysis_job_service.ArenaRequestError) as excinfo:
        analysis_job_service.submit_model_arena_task(
            user_id=user_id,
            symbol="600519.SH",
            model_profile_ids=["ghost-profile"],
            runtime=runtime,
        )
    assert "ghost-profile" in str(excinfo.value)
    assert fake.jobs == {}, "校验没过就不该留下任务记录"


def test_model_arena_submit_rejects_user_without_profiles():
    from api.database import UserDB, get_db_ctx, init_db
    from api.services import analysis_job_service, auth_service

    init_db()
    user_id = str(uuid4())
    now = datetime.now(timezone.utc)
    with get_db_ctx() as db:
        db.add(
            UserDB(
                id=user_id,
                email=auth_service.normalize_email(f"l3-none-{user_id[:8]}@test.com"),
                is_active=True,
                created_at=now,
                updated_at=now,
                last_login_at=now,
            )
        )
        db.commit()

    fake, runtime = _fake_runtime()
    with pytest.raises(analysis_job_service.ArenaRequestError):
        analysis_job_service.submit_model_arena_task(
            user_id=user_id, symbol="600519.SH", runtime=runtime
        )


# ══════════════════════════════════════════════════════════════════════════
# 三、路由改调服务、行为不变
# ══════════════════════════════════════════════════════════════════════════


def _client_and_headers():
    from fastapi.testclient import TestClient

    from api.database import UserDB, get_db_ctx, init_db
    from api.main import app
    from api.services import auth_service

    init_db()
    client = TestClient(app, raise_server_exceptions=False)
    email = auth_service.normalize_email(f"l3route-{uuid4().hex[:8]}@test.com")
    now = datetime.now(timezone.utc)
    with get_db_ctx() as db:
        user = UserDB(
            id=str(uuid4()),
            email=email,
            is_active=True,
            created_at=now,
            updated_at=now,
            last_login_at=now,
        )
        db.add(user)
        db.commit()
        token = auth_service.create_access_token(user)
    return client, {"Authorization": f"Bearer {token}"}


def test_analyze_route_submits_through_the_service(monkeypatch):
    from api.services import analysis_job_service

    client, headers = _client_and_headers()
    seen: dict[str, Any] = {}

    async def _fake_submit(*, request, user_id, runtime=None):
        seen["symbol"] = request.symbol
        seen["user_id"] = user_id
        return analysis_job_service.SubmittedJob(
            job_id="svc-job-1", status="pending", created_at="2026-09-21T00:00:00+00:00"
        )

    monkeypatch.setattr(analysis_job_service, "submit_deep_analysis", _fake_submit)

    resp = client.post(
        "/v1/analyze",
        headers=headers,
        json={"symbol": "600519.SH", "trade_date": "2026-09-18", "query": "分析贵州茅台"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == "svc-job-1"
    assert body["status"] == "pending"
    assert body["created_at"] == "2026-09-21T00:00:00+00:00"
    assert seen["symbol"] == "600519.SH"
    assert seen["user_id"], "路由必须把自己的 current_user 传给服务"


def test_daily_product_route_submits_through_the_service(monkeypatch):
    from api.services import analysis_job_service

    client, headers = _client_and_headers()
    seen: dict[str, Any] = {}

    def _fake_submit(*, request, user_id, db=None, runtime=None):
        seen["mode"] = request.mode
        seen["user_id"] = user_id
        return analysis_job_service.SubmittedDailyReview(
            run_id="svc-run-1",
            status="pending",
            mode=request.mode,
            summary={"queued_jobs": 2, "total_targets": 2},
            recommendation=None,
            jobs=(),
        )

    monkeypatch.setattr(analysis_job_service, "submit_daily_review", _fake_submit)

    resp = client.post(
        "/v1/daily-product/run", headers=headers, json={"mode": "recommended", "top_k": 2}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] == "svc-run-1"
    assert body["status"] == "pending"
    assert body["mode"] == "recommended"
    assert body["summary"]["queued_jobs"] == 2
    assert body["jobs"] == []
    assert seen["user_id"]


def test_model_arena_route_submits_through_the_service(monkeypatch):
    from api.services import analysis_job_service

    client, headers = _client_and_headers()
    seen: dict[str, Any] = {}

    async def _fake_submit(*, request, user_id, runtime=None):
        seen["symbol"] = request.symbol
        return analysis_job_service.SubmittedArenaRun(
            experiment_id="svc-exp-1",
            input_snapshot_hash="svc-hash",
            symbol=request.symbol,
            trade_date=request.trade_date,
            jobs=(),
        )

    monkeypatch.setattr(analysis_job_service, "submit_model_arena_run", _fake_submit)

    resp = client.post(
        "/v1/model-arena/runs",
        headers=headers,
        json={"symbol": "600519.SH", "trade_date": "2026-09-18"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["experiment_id"] == "svc-exp-1"
    assert body["input_snapshot_hash"] == "svc-hash"
    assert body["symbol"] == "600519.SH"
    assert body["jobs"] == []
    assert seen["symbol"] == "600519.SH"


def test_model_arena_route_keeps_400_on_unusable_profile():
    """入参校验的**状态码与文案**是既有 HTTP 契约，重构后必须原样保留。"""
    client, headers = _client_and_headers()
    resp = client.post(
        "/v1/model-arena/runs",
        headers=headers,
        json={"symbol": "600519.SH", "model_profile_ids": ["nope"]},
    )
    assert resp.status_code == 400
    assert "nope" in resp.json()["detail"]


def test_model_arena_route_requires_symbol():
    client, headers = _client_and_headers()
    resp = client.post("/v1/model-arena/runs", headers=headers, json={"symbol": ""})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "symbol is required"


# ══════════════════════════════════════════════════════════════════════════
# 四、端口注入：user_id 闭包绑定；未注入即不注册
# ══════════════════════════════════════════════════════════════════════════


def test_chat_registry_binds_user_id_into_the_submit_ports(monkeypatch):
    from api.services import analysis_job_service, chat_service

    calls: dict[str, Any] = {}

    def _fake_deep(*, user_id, symbol, trade_date="", horizons=None, selected_analysts=None,
                   query="", runtime=None):
        calls["deep"] = {"user_id": user_id, "symbol": symbol}
        return analysis_job_service.SubmittedJob(
            job_id="port-job-1", status="pending", created_at="2026-09-21T00:00:00+00:00"
        )

    def _fake_daily(*, user_id, mode, top_k, market="cn", horizons=None,
                    selected_analysts=None, include_tracking=True, include_watchlist=True, runtime=None):
        calls["daily"] = {"user_id": user_id, "mode": mode, "top_k": top_k}
        return analysis_job_service.SubmittedDailyReview(
            run_id="port-run-1", status="pending", mode=mode, summary={}, recommendation=None, jobs=()
        )

    def _fake_arena(*, user_id, symbol, trade_date="", horizons=None, selected_analysts=None,
                    model_profile_ids=None, query="", runtime=None):
        calls["arena"] = {"user_id": user_id, "symbol": symbol}
        return analysis_job_service.SubmittedArenaRun(
            experiment_id="port-exp-1", input_snapshot_hash="h", symbol=symbol,
            trade_date=trade_date, jobs=(),
        )

    monkeypatch.setattr(analysis_job_service, "submit_deep_analysis_task", _fake_deep)
    monkeypatch.setattr(analysis_job_service, "submit_daily_review_task", _fake_daily)
    monkeypatch.setattr(analysis_job_service, "submit_model_arena_task", _fake_arena)

    registry = chat_service.build_registry(user_id="user-42")
    names = registry.names()
    for name in ("submit_deep_analysis_job", "submit_daily_review_job", "submit_model_arena_job"):
        assert name in names, f"{name} 该在应用内对话面上注册"

    registry.require("submit_deep_analysis_job").handler(symbol="600519.SH")
    registry.require("submit_daily_review_job").handler(mode="recommended", top_k=2)
    registry.require("submit_model_arena_job").handler(symbol="600519.SH")

    assert calls["deep"]["user_id"] == "user-42"
    assert calls["daily"]["user_id"] == "user-42"
    assert calls["arena"]["user_id"] == "user-42"


def test_poll_async_job_reads_the_shared_job_registry(monkeypatch):
    """新提交出来的任务是注册在应用层任务表里的，轮询必须能找到它们。"""
    from api.services import backtest_service, chat_service

    monkeypatch.setattr(backtest_service, "get_job", lambda job_id: None)
    import api.main as main_module

    monkeypatch.setattr(
        main_module, "_get_job", lambda job_id: {"status": "running", "progress": 40}
    )

    registry = chat_service.build_registry(user_id="user-42")
    result = registry.require("poll_async_job").handler(job_id="any-job")
    assert result.data["status"] == "running"


def test_background_dispatch_from_a_worker_thread_hops_to_the_app_loop():
    """工具 handler 跑在**线程池**里，那里没有事件循环。

    直接 `asyncio.create_task` 会抛 RuntimeError——而"抛错"在这里是最坏的失败形态：
    任务号已经返回给模型、任务记录已经落库，却没有任何东西会真的执行。因此投递必须
    转回应用主循环。
    """
    import asyncio
    import threading

    import api.main as main_module

    saved = main_module._MAIN_EVENT_LOOP
    executed: dict[str, bool] = {}
    worker_error: list[BaseException] = []

    def _worker() -> None:
        async def _job() -> None:
            executed["ran"] = True

        try:
            main_module._submit_background_task(_job(), label="loop-hop-test")
        except BaseException as exc:  # noqa: BLE001 — 线程里的异常要在主线程断言
            worker_error.append(exc)

    try:
        asyncio.run(_worker_hop_probe(main_module, _worker, executed))
    finally:
        main_module._MAIN_EVENT_LOOP = saved

    assert worker_error == [], f"投递不得抛错：{worker_error}"
    assert executed.get("ran") is True, "任务必须真的在主循环上被执行"


async def _worker_hop_probe(main_module, worker, executed) -> None:
    """在真正的运行中循环里跑一次"从线程投递"。"""
    import asyncio
    import threading

    main_module._MAIN_EVENT_LOOP = asyncio.get_running_loop()
    thread = threading.Thread(target=worker, name="l3-submit-probe")
    thread.start()
    thread.join(timeout=5)
    for _ in range(50):
        if executed.get("ran"):
            return
        await asyncio.sleep(0.05)
