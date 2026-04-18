from __future__ import annotations


def calc_position_size(
    capital: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
) -> tuple[float, float]:
    """
    Fixed-fractional position sizing.

    Returns (position_size_in_units, position_size_usd).
    Raises ValueError if distance_to_stop is zero or negative.
    """
    distance = abs(entry_price - stop_loss)
    if distance < 1e-8:
        raise ValueError("entry_price and stop_loss must differ")
    risk_amount = capital * (risk_pct / 100.0)
    units = risk_amount / distance
    notional = units * entry_price
    return units, notional


def calc_reward_risk(
    entry_price: float,
    stop_loss: float,
    take_profit: float,
) -> float:
    """Reward-to-risk ratio. Returns 0 if risk side is zero."""
    risk = abs(entry_price - stop_loss)
    reward = abs(take_profit - entry_price)
    return reward / risk if risk > 1e-8 else 0.0


def adjust_take_profit(
    entry_price: float,
    stop_loss: float,
    direction: str,
    min_rr: float = 1.5,
) -> float:
    """
    Returns a take-profit price that achieves at least min_rr reward-to-risk.
    Used when the strategy-suggested TP doesn't meet the minimum R:R.
    """
    risk = abs(entry_price - stop_loss)
    min_reward = risk * min_rr
    if direction == "LONG":
        return entry_price + min_reward
    return entry_price - min_reward
