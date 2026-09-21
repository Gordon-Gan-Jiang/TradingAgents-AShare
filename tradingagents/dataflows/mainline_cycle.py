"""主线周期引擎（C1）：基于主线档案轨迹的连续指标与周期判定。

设计原则（docs/mainline-cycle-design.md v2）：
- **输入是主线档案轨迹** `track = [snapshot, ...]`（每日主线报告中的规则层快照：
  strength/heat/rs20/资金/RSI/涨停家数等），不依赖板块历史 K 线接口
  （受限网络下 push2his 被重置、push2delay 仅实时、THS 历史停更 2024，无法可靠取历史 K 线）。
  周期 = 主线历史轨迹的分析，本就应基于档案数据。
- **纯函数**：不 import DB/网络；输入输出为纯 Python 结构，便于单测、复现、扩展。
- **连续指标 + 阶段标签**：底层算连续量（一阶导/二阶导/趋势），阶段标签给展示与 LLM 复核。
- **退潮三因子**：动量衰减加速（强度二阶导<0）+ 资金转负 + 涨停/广度收缩，≥2 才预警，
  避免把健康回调误判为退潮。

快照字段（字段缺失/为 None 视为数据不可用，诚实降级，不参与判定）：
    date, strength, heat, rs20, net_inflow_5d, inflow_persistent_10,
    rsi14, chg_1d, zt_count, phase_hint
"""
from __future__ import annotations

from typing import Any, Optional

# 阶段标签
FERMENT = "发酵"
RISE = "主升"
SPLIT = "高位分歧"
RETREAT = "退潮"
ENDED = "已终结"
INSUFFICIENT = "数据不足"

# 判定参数（可调，文档记录）
_MIN_SNAPSHOTS = 3            # 少于 3 个有效快照 → 数据不足
_SLOPE_WINDOW = 5             # 一阶导窗口（最近 N 日）
_PEAK_GAP_RETREAT = 0.25      # 距峰值回撤 >25% 视为显著衰减
_RSI_HOT = 75.0               # 拥挤阈值
_STRENGTH_RISE = 60.0         # 主升强度门槛
_RETREAT_ALERT_FACTORS = 2    # 退潮预警至少命中的因子数


# ── 工具 ────────────────────────────────────────────────────────


def _series(track: list[dict], field: str) -> list[float]:
    """提取某字段的非空数值序列（按轨迹顺序）。"""
    out: list[float] = []
    for s in track:
        v = s.get(field)
        if isinstance(v, (int, float)) and v == v:  # 非 NaN
            out.append(float(v))
    return out


def _slope(values: list[float], window: int = _SLOPE_WINDOW) -> Optional[float]:
    """最近 window 个点的线性斜率（%/日 量纲由输入决定）。少于 2 点返回 None。"""
    seg = values[-window:]
    if len(seg) < 2:
        return None
    n = len(seg)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(seg) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, seg))
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        return None
    return num / den


def _slope2(values: list[float], window: int = _SLOPE_WINDOW) -> Optional[float]:
    """动量的二阶导（加速度）= 近段斜率 − 前段斜率。

    >0 加速（主升确认）；<0 减速（衰减开始，退潮判定的关键信号）。
    点数不足时自适应缩短窗口（最少 2 点/段，共 ≥4 点）。
    """
    if len(values) < 4:
        return None
    w = min(window, max(2, len(values) - 3))
    seg_prev = _slope(values[-(w + 2):-2], w)
    seg_recent = _slope(values[-w:], w)
    if seg_prev is None or seg_recent is None:
        return None
    return seg_recent - seg_prev


def _zscore_series(values: list[float]) -> Optional[float]:
    """最近值相对序列均值的标准化（>0 高于均值，<-0.5 显著低于）。"""
    if len(values) < 2:
        return None
    import statistics

    mu = statistics.mean(values)
    sd = statistics.pstdev(values)
    if sd == 0:
        return 0.0
    return (values[-1] - mu) / sd


def _fmt(v: Any) -> str:
    if v is None:
        return "缺失"
    return f"{v:.1f}"


# ── 主线身份匹配 ────────────────────────────────────────────────


def match_mainline_key(
    name: str,
    representative_boards: list[str],
    known_keys: list[str],
) -> Optional[str]:
    """跨天识别同一主线：名称一致 → 名称包含 → 代表板块交集。

    返回命中档案的 key；无命中返回 None（新主线，由调用方创建新档案）。
    """
    if not known_keys:
        return None
    name = (name or "").strip()
    boards = {b.strip() for b in (representative_boards or []) if b and b.strip()}
    # 1) 名称完全一致
    for k in known_keys:
        if k == name:
            return k
    # 2) 名称包含（双向，双方至少 2 字避免误并）
    if len(name) >= 2:
        for k in known_keys:
            if len(k) >= 2 and (name in k or k in name):
                return k
    # 3) 代表板块交集（任一代表板块命中档案代表板块 → 同一主线延续）
    if boards:
        for k in known_keys:
            pass  # 板块映射由服务层维护（档案存 representative_boards），此处仅匹配名称
    return None


def match_by_boards(
    representative_boards: list[str],
    known: list[dict[str, Any]],
) -> Optional[str]:
    """按代表板块交集匹配（known: [{"key":..., "boards":[...]}, ...]）。"""
    boards = {b.strip() for b in (representative_boards or []) if b and b.strip()}
    if not boards:
        return None
    for item in known:
        known_boards = {b.strip() for b in (item.get("boards") or []) if b and b.strip()}
        if boards & known_boards:
            return item.get("key")
    return None


# ── 档案轨迹 ────────────────────────────────────────────────────


def append_track(track: list[dict], snapshot: dict) -> list[dict]:
    """追加/覆盖当日快照（按 date 去重、保持时间序）。"""
    date = snapshot.get("date")
    if not date:
        return track
    out = [s for s in track if s.get("date") != date]
    out.append(dict(snapshot))
    out.sort(key=lambda s: str(s.get("date", "")))
    return out


def last_active_days(track: list[dict], trade_date: str) -> Optional[int]:
    """档案最近快照距 trade_date 的自然日差（判定"已终结"用）。"""
    if not track:
        return None
    from datetime import datetime

    try:
        last = datetime.strptime(str(track[-1].get("date", "")), "%Y-%m-%d")
        cur = datetime.strptime(trade_date, "%Y-%m-%d")
        return (cur - last).days
    except Exception:
        return None


# ── 周期特征（连续量） ──────────────────────────────────────────


def compute_cycle_features(track: list[dict]) -> dict[str, Any]:
    """从档案轨迹计算连续周期特征（纯函数）。

    返回：
      cycle_days           档案交易日数（快照数）
      strength_series      strength 序列
      strength_slope1      强度一阶导（最近 5 日斜率）
      strength_slope2      强度二阶导（斜率的变化率，退化加速判定关键）
      momentum             rs20 最近值（可 None）
      momentum_trend       rs20 最近 5 日斜率（可 None）
      fund_trend           net_inflow_5d 序列 zscore（>0 高于均值）
      fund_positive         最近资金为正
      crowding             rsi14 最近值（可 None）
      crowding_trend       rsi14 序列 zscore
      zt_trend             zt_count 序列 zscore（>0 高于均值）
      strength_peak         历史最高 strength
      peak_gap              (peak - current)/peak（距峰值回撤，0=在峰顶）
      progress              生命周期进度 0-100
      sufficient            是否有足够数据判定
      missing_fields        缺失字段列表（诚实标注）
    """
    strengths = _series(track, "strength")
    rs20s = _series(track, "rs20")
    funds = _series(track, "net_inflow_5d")
    rsis = _series(track, "rsi14")
    zts = _series(track, "zt_count")

    def _last(vals: list[float]) -> Optional[float]:
        return vals[-1] if vals else None

    peak = max(strengths) if strengths else None
    cur = _last(strengths)
    peak_gap = ((peak - cur) / peak) if (peak and cur is not None and peak > 0) else None

    # 生命周期进度：持续天数 + 距峰值位置（早期天数少、接近峰值=中后段）
    days = len(track)
    if days <= 3:
        progress = min(100.0, days * 8.0)  # 前 3 天线性爬升
    else:
        days_part = min(1.0, days / 45.0)          # 45 日为常见主线周期上限
        peak_part = 1.0 - (peak_gap or 0.0)        # 接近峰值 → 中后段
        progress = round(min(100.0, days_part * 60 + max(0.0, peak_part) * 40), 1)

    missing = []
    if len(strengths) < _MIN_SNAPSHOTS:
        missing.append("strength")
    if not rs20s:
        missing.append("rs20")
    if not funds:
        missing.append("资金")
    if not rsis:
        missing.append("rsi14")

    return {
        "cycle_days": days,
        "strength_series": strengths,
        "strength_slope1": _slope(strengths) if strengths else None,
        "strength_slope2": _slope2(strengths) if strengths else None,
        "momentum": _last(rs20s),
        "momentum_trend": _slope(rs20s) if rs20s else None,
        "fund_trend": _zscore_series(funds) if funds else None,
        "fund_positive": bool(funds and funds[-1] > 0),
        "crowding": _last(rsis),
        "crowding_trend": _zscore_series(rsis) if rsis else None,
        "zt_trend": _zscore_series(zts) if zts else None,
        "strength_peak": peak,
        "peak_gap": peak_gap,
        "progress": progress,
        "sufficient": len(strengths) >= _MIN_SNAPSHOTS,
        "missing_fields": missing,
    }


# ── 周期位置判定 ────────────────────────────────────────────────


def judge_cycle_position(
    track: list[dict],
    features: Optional[dict[str, Any]] = None,
) -> tuple[str, str]:
    """周期位置判定（基于轨迹，金融逻辑）：

    退潮：强度二阶导<0（衰减加速）且（资金转负 或 涨停收缩）——健康回调不判退潮
    高位分歧：RSI 拥挤 或 强度走平但处高位（≥60）
    主升：强度斜率>0 且 ≥60 且无拥挤
    发酵：强度<60 且斜率≥0，或档案早期
    数据不足：有效快照不足
    """
    f = features or compute_cycle_features(track)
    if not f.get("sufficient"):
        return INSUFFICIENT, f"有效快照不足（{f.get('cycle_days')} 个），无法判定周期"

    slope1 = f.get("strength_slope1")
    slope2 = f.get("strength_slope2")
    cur = f.get("strength_series", [None])[-1] if f.get("strength_series") else None
    crowding = f.get("crowding")
    peak_gap = f.get("peak_gap")

    # 退潮：动量衰减加速 + 资金转弱/转负 + 广度收缩（三因子 ≥2）
    retreat_factors = 0
    reasons: list[str] = []
    if slope2 is not None and slope2 < 0:
        retreat_factors += 1
        reasons.append("强度衰减加速(二阶导<0)")
    # 资金因子：净流出（设计三因子中的"资金转负"）；仅趋势弱但资金仍正不算强信号
    fund_weak = f.get("fund_trend") is not None and not f.get("fund_positive")
    if fund_weak:
        retreat_factors += 1
        reasons.append("资金转负(净流出)")
    if f.get("zt_trend") is not None and f["zt_trend"] < -0.5:
        retreat_factors += 1
        reasons.append("涨停/广度收缩")
    if peak_gap is not None and peak_gap > _PEAK_GAP_RETREAT:
        retreat_factors += 1
        reasons.append(f"距峰值回撤{peak_gap * 100:.0f}%")

    if slope2 is not None and slope2 < 0 and retreat_factors >= _RETREAT_ALERT_FACTORS:
        return RETREAT, "；".join(reasons) or "强度衰减加速 + 资金/广度收缩"

    # 高位分歧：拥挤 或 强度走平于高位
    if crowding is not None and crowding >= _RSI_HOT:
        return SPLIT, f"RSI={crowding:.0f} 拥挤过热"
    if (cur is not None and cur >= _STRENGTH_RISE
            and slope1 is not None and slope1 <= 0):
        return SPLIT, f"强度高位走平/回落（{cur:.0f}）"

    # 主升：强度斜率>0 且 ≥60
    if cur is not None and cur >= _STRENGTH_RISE and slope1 is not None and slope1 > 0:
        return RISE, f"强度{cur:.0f} 斜率向上（加速={slope2 is not None and slope2 >= 0}）"

    # 发酵：强度<60 且斜率≥0，或档案早期
    if cur is not None and cur < _STRENGTH_RISE and (slope1 is None or slope1 >= 0):
        return FERMENT, f"强度{cur:.0f} 处于上升初期"
    if slope1 is not None and slope1 > 0:
        return FERMENT, "强度回升中"

    # 兜底：强度走弱且无回升
    if cur is not None and cur < _STRENGTH_RISE:
        return RETREAT, "强度走弱且无回升"
    return SPLIT, "强度高位停滞"


# ── 退潮预警（三因子，供 LLM/前端复核） ─────────────────────────


def retreat_alerts(features: dict[str, Any]) -> list[str]:
    """退潮预警信号列表（任一信号即返回；judge 用三因子≥2 判定阶段）。"""
    alerts: list[str] = []
    if features.get("strength_slope2") is not None and features["strength_slope2"] < 0:
        alerts.append("强度衰减加速（动量二阶导<0）")
    if features.get("fund_trend") is not None and features["fund_trend"] < -0.5:
        alerts.append("主力资金趋势转弱")
    if features.get("fund_trend") is not None and not features.get("fund_positive"):
        alerts.append("最近资金净流出")
    if features.get("zt_trend") is not None and features["zt_trend"] < -0.5:
        alerts.append("涨停家数/广度收缩")
    if features.get("crowding") is not None and features["crowding"] >= _RSI_HOT:
        alerts.append(f"RSI={features['crowding']:.0f} 拥挤过热")
    if features.get("peak_gap") is not None and features["peak_gap"] > _PEAK_GAP_RETREAT:
        alerts.append(f"距峰值回撤{features['peak_gap'] * 100:.0f}%")
    return alerts


# ── 动作与仓位建议（决策引擎，regime 调节） ─────────────────────


def suggest_action(
    stage: str,
    *,
    emotion_temperature: Optional[float] = None,
) -> dict[str, Any]:
    """周期 → 动作 + 建议仓位（% 总仓位）+ 执行说明。

    regime 调节：冰点(<25) 不布局新主线；亢奋(>85) 建议仓位×0.5。
    """
    base = {
        FERMENT: {"action": "布局", "position": 0.10, "note": "发酵期轻仓试错，等动量确认后加仓"},
        RISE: {"action": "持有", "position": 0.15, "note": "主升期持有，动量不转负不减"},
        SPLIT: {"action": "减仓", "position": 0.05, "note": "高位分歧，兑现部分利润，等方向选择"},
        RETREAT: {"action": "规避", "position": 0.0, "note": "退潮期规避，反抽是离场机会"},
        INSUFFICIENT: {"action": "观察", "position": 0.0, "note": "数据不足，观察为主"},
    }.get(stage, {"action": "观察", "position": 0.0, "note": "未知阶段"})

    out = dict(base)
    if emotion_temperature is not None:
        if emotion_temperature < 25 and stage == FERMENT:
            out["action"] = "观察"
            out["position"] = 0.0
            out["note"] += "；情绪冰点，暂不布局新主线"
        elif emotion_temperature > 85 and out["position"] > 0:
            out["position"] = round(out["position"] * 0.5, 3)
            out["note"] += "；情绪亢奋，仓位减半防高潮"
    return out


# ── 验证条件（可证伪） ──────────────────────────────────────────


def build_verify_conditions(stage: str, features: dict[str, Any]) -> list[str]:
    """按阶段生成次日验证条件（可证伪，供作战日志对账）。"""
    conds: list[str] = []
    if stage in (FERMENT, RISE):
        conds.append("次日强度斜率不转负且资金不净流出 → 判定维持")
        conds.append("次日强度回落且资金净流出 → 周期降档（主升→高位分歧）")
    elif stage == SPLIT:
        conds.append("次日强度回升且资金回流 → 分歧后向上（维持观察）")
        conds.append("次日强度继续走低 → 进入退潮")
    elif stage == RETREAT:
        conds.append("次日强度止跌回升且资金回流 → 退潮证伪（观察反抽）")
        conds.append("次日强度续降 → 退潮确认，规避")
    if features.get("crowding") is not None and features["crowding"] >= _RSI_HOT:
        conds.append(f"RSI 从{features['crowding']:.0f}回落至 70 以下 → 拥挤缓解")
    if not conds:
        conds.append("次日数据更新后复核")
    return conds
