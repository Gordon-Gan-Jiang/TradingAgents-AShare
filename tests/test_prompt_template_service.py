import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.database import Base
from api.services import prompt_template_service


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_list_templates_seeds_builtins(db):
    items = prompt_template_service.list_templates(db, "user-1")
    ids = {item["id"] for item in items}
    assert prompt_template_service.DEFAULT_MANUAL_TEMPLATE_ID in ids
    assert prompt_template_service.DEFAULT_SCHEDULED_TEMPLATE_ID in ids


def test_create_and_update_custom_template(db):
    created = prompt_template_service.create_custom_template(
        db,
        "user-1",
        scope=prompt_template_service.SCOPE_DEEP_ANALYSIS,
        name="我的模板",
        description="desc",
        template_text="分析 {symbol}",
        intent_json={"focus_areas": ["执行"]},
    )
    assert created["is_builtin"] is False
    assert created["name"] == "我的模板"

    updated = prompt_template_service.update_custom_template(
        db,
        "user-1",
        created["id"],
        name="我的模板 v2",
        template_text="请分析 {name}（{symbol}）",
        is_active=False,
    )
    assert updated["name"] == "我的模板 v2"
    assert updated["is_active"] is False


def test_render_template_includes_context_and_vars(db):
    template = prompt_template_service.resolve_template(
        db,
        "user-1",
        prompt_template_service.DEFAULT_MANUAL_TEMPLATE_ID,
    )
    rendered = prompt_template_service.render_template(
        template,
        symbol="300750.SZ",
        name="宁德时代",
        horizon="medium",
        trade_date="2026-05-20",
        prompt_vars={"extra_note": "关注量价"},
        user_context={"current_position": 100},
    )
    assert "300750.SZ" in rendered["query"]
    assert rendered["user_intent"]["ticker"] == "300750.SZ"
    assert rendered["user_intent"]["horizons"] == ["medium"]
    assert rendered["user_intent"]["user_context"]["current_position"] == 100
