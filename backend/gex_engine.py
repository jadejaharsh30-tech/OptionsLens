# optionslens/backend/gex_engine.py
"""
Gamma Exposure (GEX) calculations.

GEX measures the total dollar gamma exposure dealers have at each strike.
Positive net GEX = dealers long gamma = stabilising (pin effect).
Negative net GEX = dealers short gamma = destabilising (trend amplification).

Formula per strike:
  gex = gamma × oi × lot_size × spot² × 0.01
  net_gex = call_gex - put_gex
  (dealers are assumed to be short options → delta-hedging creates gamma exposure)
"""
from typing import Any


def compute_gex_for_strike(
    gamma: float,
    oi: float,
    lot_size: int,
    spot: float,
    option_type: str,
) -> float:
    """
    GEX for a single strike+type.
    The 0.01 factor converts to dollar GEX per 1% spot move.
    """
    if oi == 0 or gamma == 0:
        return 0.0
    return gamma * oi * lot_size * (spot ** 2) * 0.01


def compute_net_gex_profile(
    chain_rows: list[dict[str, Any]],
    lot_size: int,
    spot: float,
) -> list[dict[str, Any]]:
    """
    Compute net GEX profile across all strikes.

    Args:
        chain_rows: List of dicts with keys: strike, option_type, gamma, oi
        lot_size: Contract lot size for this underlying
        spot: Current spot price

    Returns:
        List of {strike, call_gex, put_gex, net_gex} sorted by strike
    """
    # Group by strike
    by_strike: dict[float, dict] = {}

    for row in chain_rows:
        strike = row["strike"]
        if strike not in by_strike:
            by_strike[strike] = {"call_gex": 0.0, "put_gex": 0.0}

        gex = compute_gex_for_strike(
            gamma=row.get("gamma", 0.0),
            oi=row.get("oi", 0.0),
            lot_size=lot_size,
            spot=spot,
            option_type=row["option_type"],
        )

        if row["option_type"] == "CE":
            by_strike[strike]["call_gex"] += gex
        else:
            by_strike[strike]["put_gex"] += gex

    result = []
    for strike, gex_data in sorted(by_strike.items()):
        net = gex_data["call_gex"] - gex_data["put_gex"]
        result.append({
            "strike":   strike,
            "call_gex": round(gex_data["call_gex"], 2),
            "put_gex":  round(gex_data["put_gex"], 2),
            "net_gex":  round(net, 2),
        })

    return result
