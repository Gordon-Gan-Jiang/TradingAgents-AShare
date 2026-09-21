"""Tests for Decision Critic runtime config helpers."""

import os

import pytest

from tradingagents.agents.utils import critic_config


def test_kill_switch_env_disables_regardless_of_config(monkeypatch):
    monkeypatch.setenv("TA_DECISION_CRITIC", "0")
    assert critic_config.is_decision_critic_enabled({"decision_critic_enabled": True}) is False


def test_enabled_when_env_on_and_config_true(monkeypatch):
    monkeypatch.delenv("TA_DECISION_CRITIC", raising=False)
    assert critic_config.is_decision_critic_enabled({"decision_critic_enabled": True}) is True


def test_disabled_from_config(monkeypatch):
    monkeypatch.delenv("TA_DECISION_CRITIC", raising=False)
    assert critic_config.is_decision_critic_enabled({"decision_critic_enabled": False}) is False


def test_revision_threshold_from_config():
    assert critic_config.decision_critic_revision_threshold({"decision_critic_revision_threshold": 55.5}) == 55.5


def test_revision_threshold_default():
    assert critic_config.decision_critic_revision_threshold({}) == 40.0
