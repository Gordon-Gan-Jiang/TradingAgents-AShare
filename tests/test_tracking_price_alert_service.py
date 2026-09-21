from api.services.tracking_price_alert_service import infer_daily_limit_pct


def test_infer_daily_limit_pct_board_rules():
    assert infer_daily_limit_pct("600519.SH", "贵州茅台") == 10
    assert infer_daily_limit_pct("300750.SZ", "宁德时代") == 20
    assert infer_daily_limit_pct("301001.SZ", "创业板") == 20
    assert infer_daily_limit_pct("688001.SH", "科创板") == 20
    assert infer_daily_limit_pct("689009.SH", "科创板") == 20
    assert infer_daily_limit_pct("430047.BJ", "北交所") == 30
    assert infer_daily_limit_pct("600000.SH", "*ST测试") == 5
