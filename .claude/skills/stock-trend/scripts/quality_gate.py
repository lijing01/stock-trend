#!/usr/bin/env python3
"""Quality gate: signal consistency and recommendation filtering.

Detects contradictions between direction judgment and technical risk signals.
Used by market_leader.py to penalize or flag conflicting recommendations.
"""

# Bearish signal keywords found in risk text
BEARISH_KEYWORDS = [
    "空头排列",
    "死叉",
    "绿柱放大",
    "-DI>+DI确认空头",
    "OBV在20日均线下方",
    "资金净流出",
    "缩量下跌",
    "顶背离",
]

# Bullish signal keywords
BULLISH_KEYWORDS = [
    "多头排列",
    "金叉",
    "红柱放大",
    "+DI>-DI确认多头",
    "OBV在20日均线上方",
    "资金净流入",
    "底背离",
    "放量上涨",
]

# Keywords indicating overbought (conflict with bullish if in downtrend context)
OVERBOUGHT_KEYWORDS = ["超买"]
OVERSOLD_KEYWORDS = ["超卖"]


def check_signal_consistency(direction: str, risks: list[str]) -> dict:
    """Check if direction judgment conflicts with technical signals in risks.

    Args:
        direction: e.g. "震荡偏多", "偏空", "震荡偏空"
        risks: list of risk text strings from technical analysis

    Returns:
        dict with:
          - has_conflict: bool
          - bearish_signal_count: int
          - bullish_signal_count: int
          - penalty: float (0 if consistent, 0.10-0.20 if conflicting)
          - conflict_detail: str (human-readable explanation)
    """
    is_bullish_direction = "偏多" in direction or direction in ("多头", "bullish")
    is_bearish_direction = "偏空" in direction or direction in ("空头", "bearish")

    risk_text = " ".join(risks)

    bearish_count = sum(1 for kw in BEARISH_KEYWORDS if kw in risk_text)
    bullish_count = sum(1 for kw in BULLISH_KEYWORDS if kw in risk_text)
    has_overbought = any(kw in risk_text for kw in OVERBOUGHT_KEYWORDS)

    has_conflict = False
    penalty = 0.0
    detail = ""

    if is_bullish_direction:
        # Bullish direction but many bearish signals → conflict
        if bearish_count >= 3:
            has_conflict = True
            penalty = 0.20
            detail = f"方向偏多但有{bearish_count}个看空信号"
        elif bearish_count >= 2:
            has_conflict = True
            penalty = 0.10
            detail = f"方向偏多但有{bearish_count}个看空信号"
        # Overbought + bullish: warn but lower penalty
        if has_overbought and bearish_count >= 1:
            has_conflict = True
            penalty = max(penalty, 0.10)
            if not detail:
                detail = "超买区且有看空信号"

    elif is_bearish_direction:
        # Bearish direction with bullish signals → conflict (less common)
        if bullish_count >= 3:
            has_conflict = True
            penalty = 0.15
            detail = f"方向偏空但有{bullish_count}个看多信号"

    return {
        "has_conflict": has_conflict,
        "bearish_signal_count": bearish_count,
        "bullish_signal_count": bullish_count,
        "penalty": penalty,
        "conflict_detail": detail,
    }
