"""Option selection engine (spec section 13).

Given a directional :class:`~research.strategies.candidate.Candidate` and an
option chain, :func:`select_option` evaluates every requested strike
selection method and returns each one's raw economics — it does **not**
pick a winner. Per spec, ATM is not hardcoded as "the answer": all methods
are returned side by side so a backtest driver (not this module) can decide
empirically which one performs best.

Economics: wiring the full cost model
(``analysis.spread_pricer``/``analysis.signal_models.CostEstimate``) would
mean constructing a ``SpreadLeg``, a lot size, a ``CostModel``, and a
``PriceMode`` for a single-leg selection that has no spread structure yet —
heavier than this module's job. Instead, each selected method returns the
raw bid/ask/ltp/liquidity fields already present on the chain's leg data
(the same fields ``analysis.spread_selector.build_leg`` reads), which is
enough for a caller to compare methods without this module guessing at a
lot size or cost model on its behalf. This is a deliberate scope decision,
documented here rather than silently underspecified.
"""

from __future__ import annotations

from typing import Any

from research.strategies.candidate import Candidate

_OPTION_TYPE_FOR_DIRECTION = {"BULLISH": "call", "BEARISH": "put"}


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sorted_rows(chain: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        row
        for row in (chain.get("strikes") or [])
        if isinstance(row, dict) and _as_float(row.get("strike")) is not None
    ]
    return sorted(rows, key=lambda r: float(r["strike"]))


def _leg_economics(row: dict[str, Any], side: str) -> dict[str, Any] | None:
    leg = row.get(side)
    if not isinstance(leg, dict):
        return None
    strike = _as_float(row.get("strike"))
    if strike is None:
        return None
    bid = _as_float(leg.get("bid"))
    ask = _as_float(leg.get("ask"))
    return {
        "strike": strike,
        "option_type": "CE" if side == "call" else "PE",
        "symbol": leg.get("symbol"),
        "bid": bid,
        "ask": ask,
        "ltp": _as_float(leg.get("ltp")),
        "bid_ask_spread": (ask - bid) if (bid is not None and ask is not None) else None,
        "volume": leg.get("volume"),
        "open_interest": leg.get("open_interest"),
        "delta": _as_float(leg.get("delta")),
        "iv": _as_float(leg.get("iv")),
    }


def _select_atm(rows: list[dict[str, Any]], side: str, spot: float) -> dict[str, Any] | None:
    best_row = None
    best_diff = float("inf")
    best_index = None
    for index, row in enumerate(rows):
        strike = _as_float(row.get("strike"))
        if strike is None:
            continue
        diff = abs(strike - spot)
        if diff < best_diff:
            best_diff = diff
            best_row = row
            best_index = index
    if best_row is None:
        return None
    economics = _leg_economics(best_row, side)
    if economics is None:
        return None
    economics["strike_index"] = best_index
    return economics


def _select_atm_offset(
    rows: list[dict[str, Any]], side: str, spot: float, direction: str, offset: int
) -> dict[str, Any] | None:
    """ATM+1/ATM-1: one strike further OTM (offset > 0) or ITM (offset < 0).

    "Further OTM" depends on both option side and candidate direction:
    for a BULLISH candidate (calls), OTM is the higher-strike direction;
    for a BEARISH candidate (puts), OTM is the lower-strike direction.
    """
    atm = _select_atm(rows, side, spot)
    if atm is None or atm.get("strike_index") is None:
        return None
    atm_index = atm["strike_index"]

    if side == "call":
        # OTM calls sit above spot -> higher index moves further OTM.
        step = 1 if direction == "BULLISH" else -1
    else:
        # OTM puts sit below spot -> lower index moves further OTM.
        step = -1 if direction == "BEARISH" else 1

    target_index = atm_index + step * offset
    if target_index < 0 or target_index >= len(rows):
        return None
    row = rows[target_index]
    economics = _leg_economics(row, side)
    if economics is None:
        return None
    economics["strike_index"] = target_index
    return economics


def _select_delta(
    rows: list[dict[str, Any]], side: str, target_delta: float
) -> dict[str, Any] | None:
    best_row = None
    best_diff = float("inf")
    for row in rows:
        leg = row.get(side)
        if not isinstance(leg, dict):
            continue
        delta = _as_float(leg.get("delta"))
        if delta is None:
            continue
        diff = abs(abs(delta) - abs(target_delta))
        if diff < best_diff:
            best_diff = diff
            best_row = row
    if best_row is None:
        # No leg in the chain carries delta data at all -> this method is
        # skipped by the caller, not filled with a guess.
        return None
    return _leg_economics(best_row, side)


def select_option(
    candidate: Candidate,
    chain: dict[str, Any],
    *,
    selection_methods: list[str] | None = None,
    target_delta: float = 0.4,
) -> dict[str, Any]:
    """Evaluate each requested strike selection method against a chain.

    Args:
        candidate: A directional candidate (``BULLISH``/``BEARISH``). A
            ``NEUTRAL`` or otherwise absent direction returns ``{}`` — there
            is no option side to select for a directionless candidate here
            (``OptionsFlowStrategy`` candidates, which are always NEUTRAL by
            design, are not meant to be fed to this function).
        chain: Option chain, ``{"strikes": [{"strike": float, "call": {...},
            "put": {...}}], "spot": float}`` — same shape as
            ``OptionsFlowContext.chain``.
        selection_methods: Which methods to evaluate. Defaults to all four:
            ``["ATM", "ATM+1", "ATM-1", "DELTA"]``.
        target_delta: Target absolute delta for the ``DELTA`` method.

    Returns:
        A dict keyed by method name, each value the selected
        strike/option_type plus raw bid/ask/ltp/liquidity economics (and
        delta/iv when present). Methods that cannot be evaluated (no delta
        data for ``DELTA``, no valid strikes at all) are omitted from the
        result, not filled with a placeholder. Returns ``{}`` when the
        candidate is NEUTRAL/directionless or the chain has no evaluable
        strikes. This function does not decide which method "wins" — that
        is left to the caller (a backtest driver), per spec.
    """
    if selection_methods is None:
        selection_methods = ["ATM", "ATM+1", "ATM-1", "DELTA"]

    direction = getattr(candidate, "direction", None)
    side = _OPTION_TYPE_FOR_DIRECTION.get(direction)
    if side is None:
        return {}

    rows = _sorted_rows(chain)
    if not rows:
        return {}
    spot = _as_float(chain.get("spot"))
    if spot is None:
        return {}

    result: dict[str, Any] = {}
    for method in selection_methods:
        if method == "ATM":
            economics = _select_atm(rows, side, spot)
        elif method == "ATM+1":
            economics = _select_atm_offset(rows, side, spot, direction, offset=1)
        elif method == "ATM-1":
            economics = _select_atm_offset(rows, side, spot, direction, offset=-1)
        elif method == "DELTA":
            economics = _select_delta(rows, side, target_delta)
        else:
            continue

        if economics is None:
            continue
        economics.pop("strike_index", None)
        result[method] = economics

    return result
