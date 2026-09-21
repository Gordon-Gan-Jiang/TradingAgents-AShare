"""Tests for stock-analysis-team skill service helpers."""

from api.services import stock_analysis_skill_service as svc


def test_validate_symbol_cn():
    assert svc.validate_symbol("600519.SH")
    assert svc.validate_symbol("000001.sz")
    assert not svc.validate_symbol("600519")
    assert not svc.validate_symbol("'; DROP TABLE--")


def test_validate_symbol_us():
    assert svc.validate_symbol("AAPL")
    assert svc.validate_symbol("BRK.B")
    assert not svc.validate_symbol("")
