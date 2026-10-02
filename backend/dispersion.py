# optionslens/backend/dispersion.py
"""
Implied correlation: how much of an index's implied volatility is explained by
its members' implied volatilities moving together (roadmap item 35).

THE IDEA
    An index's variance is its members' variances plus every pairwise
    covariance. Solve that identity for a single average correlation and you get
    the correlation the options market is implicitly pricing:

        σ_I² = Σ wᵢ² σᵢ² + ρ · [ (Σ wᵢ σᵢ)² − Σ wᵢ² σᵢ² ]

        ρ = (σ_I² − Σ wᵢ² σᵢ²) / ((Σ wᵢ σᵢ)² − Σ wᵢ² σᵢ²)

    This is the quantity behind the dispersion trade. Index options are bid for
    crash protection, and in a crash correlations go to one, so index implied
    vol usually prices MORE correlation than subsequently materialises — a
    correlation risk premium, the cross-sectional cousin of the variance risk
    premium. When implied correlation is historically rich, index vol is
    expensive relative to its own members.

WHAT THIS VERSION IS, EXACTLY — READ BEFORE READING A NUMBER FROM IT
    The configured basket is FIVE of NIFTY's fifty members (two of BANKNIFTY's
    twelve), equally weighted. So ρ here is "the correlation that would give an
    equal-weighted basket of these stocks the index's implied vol". It is not
    the index's true implied correlation, and its LEVEL is biased: large caps
    run lower vol than the average member, which pushes ρ up, and it can exceed
    1 on days the index is pricing more vol than this basket could produce at
    any correlation. The small basket biases the other way too: with few
    members the zero-correlation basket vol is high (two members at ~21% cannot
    diversify below ~15%), so an index priced under that floor implies a
    NEGATIVE ρ. Values outside [0, 1] are kept rather than clipped — clipping
    would flatten exactly the tails of the distribution the signal ranks.

    Why it is still useful: the bias is roughly constant, and the series is only
    ever ranked against ITS OWN HISTORY. A level biased by a steady amount still
    has informative variation. What it cannot support is any statement about
    "the" implied correlation of NIFTY, or a comparison with a published index
    of it.

    Equal weights rather than index weights on purpose: NSE revises weights
    monthly, and a fixed weight vector applied across two years of history
    would be wrong on almost every date in a way nobody could see.

THE BASKET IS FIXED PER SERIES
    A date computed from four stocks and a date computed from five are different
    quantities, so a series that silently drops a member on the days its IV is
    missing mixes two definitions — the failure that made IV Rank compare
    incomparable readings. The basket is chosen once per load (members with
    enough history to be worth including), and a date missing ANY of them is a
    gap. The chosen basket and the excluded members travel with the series.

All IV is the 30-day constant-maturity reading from `get_cm_iv_history`, for the
index and every member, so the tenors match by construction.
"""
from typing import Optional

from eod_vol import percentile_rank

# Matches the other ranked daily series.
MIN_OBSERVATIONS = 60

# At least this many members, or there is no cross-section to speak of.
MIN_MEMBERS = 2

# A member is included in the basket only if its IV exists on at least this
# share of the index's dates. Lower, and requiring every member on every date
# would gut the series; higher, and one patchy stock excludes itself.
MIN_MEMBER_COVERAGE = 0.5

# Realised-vol window, paired with the 30-day implied tenor as in vrp.py.
RV_WINDOW = 20


def implied_correlation(index_vol: float, member_vols: list[float],
                        weights: Optional[list[float]] = None) -> Optional[float]:
    """
    Average pairwise correlation implied by an index vol and its members' vols.

    Equal weights by default. Returns None when the cross-section cannot
    identify a correlation: fewer than two members, a non-positive vol, or a
    denominator of zero (all but one member at zero vol).
    """
    if index_vol is None or index_vol <= 0 or len(member_vols) < MIN_MEMBERS:
        return None
    if any(v is None or v <= 0 for v in member_vols):
        return None
    n = len(member_vols)
    w = weights or [1.0 / n] * n
    if len(w) != n:
        return None

    own = sum((wi * vi) ** 2 for wi, vi in zip(w, member_vols))
    total = sum(wi * vi for wi, vi in zip(w, member_vols)) ** 2
    denom = total - own
    if denom <= 0:
        return None
    return (index_vol ** 2 - own) / denom


def choose_basket(index_dates: set[str], member_dates: dict[str, set[str]],
                  min_coverage: float = MIN_MEMBER_COVERAGE,
                  ) -> tuple[tuple[str, ...], dict[str, float]]:
    """
    The members worth including, and the coverage of each one left out.

    Decided from the data once per series: a member missing from the archive
    entirely (ICICIBANK, where it was not downloaded) is excluded rather than
    blanking every date.

    Coverage is measured from the date the LATEST-STARTING member's history
    begins, not over the index's whole history. Measured over all of NIFTY
    (options from 2001) every stock looked thin — 34-50% — because stock
    options were American-style until 2011 and have no IV before then; all
    but one member would have been dropped and the series left empty. The
    trade-off: one member that starts much later shortens the window for the
    whole basket, so such a member is better excluded explicitly.
    """
    if not index_dates:
        return (), {}
    starts = [min(d) for d in member_dates.values() if d]
    window_start = max(starts) if starts else None
    window = ({d for d in index_dates if d >= window_start}
              if window_start else set(index_dates))
    if not window:
        window = set(index_dates)

    basket, excluded = [], {}
    for sym in sorted(member_dates):
        cov = len(member_dates[sym] & window) / len(window)
        if cov >= min_coverage:
            basket.append(sym)
        else:
            excluded[sym] = round(cov * 100, 1)
    return tuple(basket), excluded


def build_dispersion_series(index_iv: list[dict],
                            member_iv: dict[str, list[dict]],
                            basket: tuple[str, ...],
                            index_rv: Optional[list[dict]] = None,
                            member_rv: Optional[dict[str, list[dict]]] = None,
                            ) -> list[dict]:
    """
    One implied correlation per date on which the index AND every basket member
    have a reading. Joined on date, never by position.

    When realised vols are supplied, each row also carries the realised
    correlation over the trailing window and the correlation premium (implied
    minus realised) — kept as research features, not used by the signal.

    Returns [{"date", "index_iv", "mean_member_iv", "implied_corr",
    "realised_corr"?, "corr_premium"?}] ascending.
    """
    if len(basket) < MIN_MEMBERS:
        return []

    iv_by = {s: {r["date"]: r["iv"] for r in member_iv.get(s, [])
                 if r.get("iv") is not None} for s in basket}
    rv_idx = {r["date"]: r["rv"] for r in (index_rv or []) if r.get("rv")}
    rv_by = {s: {r["date"]: r["rv"] for r in (member_rv or {}).get(s, [])
                 if r.get("rv")} for s in basket}

    out = []
    for row in sorted(index_iv, key=lambda r: r["date"]):
        d, iv = row.get("date"), row.get("iv")
        if not d or iv is None:
            continue
        vols = [iv_by[s].get(d) for s in basket]
        if any(v is None for v in vols):
            continue                        # fixed basket: a gap, not a smaller basket
        rho = implied_correlation(iv, vols)
        if rho is None:
            continue
        rec = {
            "date":           d,
            "index_iv":       iv,
            "mean_member_iv": sum(vols) / len(vols),
            "implied_corr":   round(rho, 6),
        }
        rvs = [rv_by[s].get(d) for s in basket]
        if rv_idx.get(d) and all(v is not None for v in rvs):
            real = implied_correlation(rv_idx[d], rvs)
            if real is not None:
                rec["realised_corr"] = round(real, 6)
                rec["corr_premium"] = round(rho - real, 6)
        out.append(rec)
    return out


def corr_percentile(series: list[dict], current: float,
                    min_observations: int = MIN_OBSERVATIONS) -> Optional[float]:
    values = [r["implied_corr"] for r in series if r.get("implied_corr") is not None]
    if len(values) < min_observations:
        return None
    return percentile_rank(values, current)


def load_dispersion_history(db_path: str, index_symbol: str, days: int = 504,
                            basket: Optional[tuple[str, ...]] = None,
                            ) -> tuple[list[dict], dict]:
    """
    Build the series from `atm_iv_history` and `spot_history`.

    Returns (series, info), where info records the basket actually used and the
    members excluded for thin coverage, so a caller can see that a NIFTY series
    was built from four stocks rather than five.
    """
    from config import INDEX_BASKETS
    from realized_vol import rv_series_from_dated_closes
    from snapshot_store import get_cm_iv_history, get_spot_history

    configured = tuple(basket or INDEX_BASKETS.get(index_symbol, ()))
    info: dict = {"index": index_symbol, "configured": list(configured)}
    if not configured:
        info["note"] = f"{index_symbol} has no configured basket in INDEX_BASKETS."
        return [], info

    index_iv = get_cm_iv_history(db_path, index_symbol, days)
    member_iv = {s: get_cm_iv_history(db_path, s, days) for s in configured}

    chosen, excluded = choose_basket(
        {r["date"] for r in index_iv},
        {s: {r["date"] for r in rows} for s, rows in member_iv.items()})
    info.update({"basket": list(chosen), "excluded": excluded})
    if len(chosen) < MIN_MEMBERS:
        info["note"] = (f"Only {len(chosen)} member(s) of {index_symbol} have "
                        f"enough IV history; need {MIN_MEMBERS}.")
        return [], info

    def rv(sym: str) -> list[dict]:
        spots = get_spot_history(db_path, sym, days + RV_WINDOW + 5)
        return rv_series_from_dated_closes(
            sorted(spots, key=lambda r: r["date"]), window=RV_WINDOW)

    series = build_dispersion_series(
        index_iv, member_iv, chosen,
        index_rv=rv(index_symbol), member_rv={s: rv(s) for s in chosen})
    return series, info


def summarise(series: list[dict], info: Optional[dict] = None) -> dict:
    """
    Diagnostics for the API and the report.

    `pct_above_one` and `pct_below_zero` are the bias gauges: the share of
    dates on which the index is priced outside what this basket could produce
    at any correlation from 0 to 1. Large values say the basket is a poor
    stand-in for the index's members.
    """
    info = info or {}
    base = {"basket": info.get("basket"), "excluded": info.get("excluded")}
    if not series:
        return {**base, "observations": 0,
                "note": info.get("note") or
                ("No dates carry the index and every basket member's 30-day IV. "
                 "Has the bhavcopy import run for the constituents?")}
    rho = [r["implied_corr"] for r in series]
    prem = [r["corr_premium"] for r in series if r.get("corr_premium") is not None]
    return {
        **base,
        "observations":     len(series),
        "first_date":       series[0]["date"],
        "last_date":        series[-1]["date"],
        "latest_corr":      series[-1]["implied_corr"],
        "mean_corr":        round(sum(rho) / len(rho), 4),
        "pct_above_one":    round(sum(1 for v in rho if v > 1) / len(rho) * 100, 1),
        "pct_below_zero":   round(sum(1 for v in rho if v < 0) / len(rho) * 100, 1),
        "mean_corr_premium": round(sum(prem) / len(prem), 4) if prem else None,
    }


def _report() -> None:
    """`python -m dispersion` — basket and coverage per configured index."""
    from config import DB_PATH, INDEX_BASKETS

    for index in INDEX_BASKETS:
        series, info = load_dispersion_history(DB_PATH, index)
        s = summarise(series, info)
        print(f"{index}: basket {s['basket']}  excluded {s['excluded'] or '-'}")
        if not s["observations"]:
            print(f"  {s['note']}\n")
            continue
        prem = (f"{s['mean_corr_premium']:+.3f}"
                if s["mean_corr_premium"] is not None else "-")
        print(f"  {s['observations']} dates, {s['first_date']} to {s['last_date']}  "
              f"mean ρ {s['mean_corr']:.3f}  latest {s['latest_corr']:.3f}  "
              f"outside [0,1]: {s['pct_above_one']}% above, {s['pct_below_zero']}% below  "
              f"mean premium {prem}\n")
    print("ρ is a PROXY from a few large members, equally weighted. Read it only "
          "against its own history.")


if __name__ == "__main__":
    _report()
