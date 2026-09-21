"""Options-flow anomaly generator — flags unusual flow, claims no direction.

**This strategy does not claim options flow predicts direction.** Per spec
section 11, it deliberately does not hardcode a rule like "PCR high means
bullish" — that is a widely-repeated but unvalidated claim, and baking it in
would be exactly the kind of hidden assumption this project's research
posture forbids. Instead, every measured quantity (PCR, OI change, IV and
IV change, Greeks, strike/expiry distance, liquidity) is recorded as a
``Candidate.features`` value, the emitted ``direction`` is always
``"NEUTRAL"``, and ``candidate_score`` measures only how *unusual* the
current flow is relative to a simple statistical baseline — never how
"bullish" or "bearish" it supposedly is. A downstream consumer that wants to
test a directional hypothesis about flow can do so against the recorded
features; this module refuses to pre-judge the answer.

Input shape is different from the other four strategies: this one reads an
option chain snapshot, not bar OHLCV, via :class:`OptionsFlowContext`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from research.strategies.candidate import Candidate, Strategy, StrategyContext

#: PCR values inside this band are treated as unremarkable ("neutral")
#: baseline flow — chosen as a fixed, documented band rather than fit to
#: any dataset, per the module's "no hidden assumptions" posture. A caller
#: with a longer rolling PCR history can instead pass one via
#: ``pcr_history`` for a z-score-based anomaly read.
_NEUTRAL_PCR_BAND = (0.7, 1.3)


@dataclass(frozen=True)
class OptionsFlowContext(StrategyContext):
    """Chain-level context for :class:`OptionsFlowStrategy`.

    Extends :class:`StrategyContext` — ``frame``/``at``/``underlying`` are
    still required by the base class, but this strategy does not read
    ``frame`` at all; a caller with no bar history may pass an empty
    DataFrame with a one-row DatetimeIndex matching ``at``.

    Attributes:
        chain: Current option chain, shaped like
            ``research.data.provider.OptionStoreProvider.get_option_chain``'s
            return value: ``{"strikes": [{"strike": float, "call": {...ltp,
            bid, ask, open_interest, volume, iv, delta...}, "put": {...}},
            ...], "spot": float}``.
        previous_chain: An earlier chain snapshot for the same
            underlying+expiry, used to compute OI/IV change. ``None`` when
            no prior snapshot is available — in that case, every
            change-based feature is ``None``, never ``0`` (absence, not a
            default).
        pcr_history: Optional rolling history of past PCR readings, used to
            z-score the current PCR against its own recent distribution
            instead of (or in addition to) the fixed neutral band.
    """

    chain: dict[str, Any] = None  # type: ignore[assignment]
    previous_chain: dict[str, Any] | None = None
    pcr_history: list[float] | None = None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sum_oi(chain: dict[str, Any], side: str) -> float | None:
    total = 0.0
    seen = False
    for row in chain.get("strikes") or []:
        if not isinstance(row, dict):
            continue
        leg = row.get(side)
        if not isinstance(leg, dict):
            continue
        oi = _as_float(leg.get("open_interest"))
        if oi is None:
            continue
        total += oi
        seen = True
    return total if seen else None


def _atm_row(chain: dict[str, Any], spot: float) -> dict[str, Any] | None:
    best_row = None
    best_diff = float("inf")
    for row in chain.get("strikes") or []:
        if not isinstance(row, dict):
            continue
        strike = _as_float(row.get("strike"))
        if strike is None:
            continue
        diff = abs(strike - spot)
        if diff < best_diff:
            best_diff = diff
            best_row = row
    return best_row


def _avg_iv(chain: dict[str, Any]) -> float | None:
    values: list[float] = []
    for row in chain.get("strikes") or []:
        if not isinstance(row, dict):
            continue
        for side in ("call", "put"):
            leg = row.get(side)
            if not isinstance(leg, dict):
                continue
            iv = _as_float(leg.get("iv"))
            if iv is not None:
                values.append(iv)
    if not values:
        return None
    return sum(values) / len(values)


def _liquidity_snapshot(row: dict[str, Any] | None) -> dict[str, float | None]:
    if row is None:
        return {"bid_ask_spread": None, "volume": None}
    spreads: list[float] = []
    volumes: list[float] = []
    for side in ("call", "put"):
        leg = row.get(side)
        if not isinstance(leg, dict):
            continue
        bid = _as_float(leg.get("bid"))
        ask = _as_float(leg.get("ask"))
        if bid is not None and ask is not None:
            spreads.append(ask - bid)
        volume = _as_float(leg.get("volume"))
        if volume is not None:
            volumes.append(volume)
    return {
        "bid_ask_spread": sum(spreads) / len(spreads) if spreads else None,
        "volume": sum(volumes) if volumes else None,
    }


_DEFAULT_PARAMS: dict[str, Any] = {
    "neutral_pcr_low": _NEUTRAL_PCR_BAND[0],
    "neutral_pcr_high": _NEUTRAL_PCR_BAND[1],
    "min_strikes": 3,
}


class OptionsFlowStrategy(Strategy):
    """Flags unusual options flow. Emits NEUTRAL only — see module docstring.

    Args:
        params: Overrides for ``_DEFAULT_PARAMS``.
    """

    version = "1.0.0"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        merged = dict(_DEFAULT_PARAMS)
        if params:
            merged.update(params)
        self.params = merged

    def generate_candidates(self, context: StrategyContext) -> list[Candidate]:
        if not isinstance(context, OptionsFlowContext):
            return []
        chain = context.chain
        if not isinstance(chain, dict):
            return []
        rows = [r for r in (chain.get("strikes") or []) if isinstance(r, dict)]
        if len(rows) < self.params["min_strikes"]:
            return []
        spot = _as_float(chain.get("spot"))

        call_oi = _sum_oi(chain, "call")
        put_oi = _sum_oi(chain, "put")
        if call_oi is None or put_oi is None or call_oi == 0:
            return []
        pcr = put_oi / call_oi

        previous = context.previous_chain
        oi_change_pct: float | None = None
        iv_change: float | None = None
        avg_iv = _avg_iv(chain)
        if previous is not None and isinstance(previous, dict):
            prev_call_oi = _sum_oi(previous, "call")
            prev_put_oi = _sum_oi(previous, "put")
            if prev_call_oi and prev_put_oi is not None:
                prev_total = prev_call_oi + prev_put_oi
                cur_total = call_oi + put_oi
                if prev_total > 0:
                    oi_change_pct = (cur_total - prev_total) / prev_total * 100.0
            prev_avg_iv = _avg_iv(previous)
            if prev_avg_iv is not None and avg_iv is not None:
                iv_change = avg_iv - prev_avg_iv
        # else: oi_change_pct/iv_change stay None — "absence, not a default,"
        # never fabricated as 0 when no previous snapshot was supplied.

        history = context.pcr_history
        pcr_z: float | None = None
        if history and len(history) >= 2:
            mean = sum(history) / len(history)
            variance = sum((h - mean) ** 2 for h in history) / len(history)
            std = variance ** 0.5
            if std > 0:
                pcr_z = (pcr - mean) / std

        low, high = self.params["neutral_pcr_low"], self.params["neutral_pcr_high"]
        # Anomaly score: how far outside the neutral band (or, when a rolling
        # history is available, how many std-equivalents from its own recent
        # mean) — never a directional read.
        if pcr_z is not None:
            unusualness = min(abs(pcr_z) / 2.0, 1.0)
        elif pcr < low:
            unusualness = min((low - pcr) / low, 1.0)
        elif pcr > high:
            unusualness = min((pcr - high) / high, 1.0)
        else:
            unusualness = 0.0
        score = round(unusualness * 100.0, 2)

        atm_row = _atm_row(chain, spot) if spot is not None else None
        liquidity = _liquidity_snapshot(atm_row)

        atm_call_greeks = None
        atm_put_greeks = None
        if atm_row is not None:
            call_leg = atm_row.get("call") if isinstance(atm_row.get("call"), dict) else None
            put_leg = atm_row.get("put") if isinstance(atm_row.get("put"), dict) else None
            if call_leg is not None:
                atm_call_greeks = {
                    k: _as_float(call_leg.get(k)) for k in ("delta", "gamma", "theta", "vega")
                }
            if put_leg is not None:
                atm_put_greeks = {
                    k: _as_float(put_leg.get(k)) for k in ("delta", "gamma", "theta", "vega")
                }

        features: dict[str, Any] = {
            "pcr": round(pcr, 4),
            "call_oi": call_oi,
            "put_oi": put_oi,
            "oi_change_pct": round(oi_change_pct, 4) if oi_change_pct is not None else None,
            "avg_iv": round(avg_iv, 4) if avg_iv is not None else None,
            "iv_change": round(iv_change, 4) if iv_change is not None else None,
            "pcr_z_score": round(pcr_z, 4) if pcr_z is not None else None,
            "spot": spot,
            "atm_strike": _as_float(atm_row.get("strike")) if atm_row else None,
            "bid_ask_spread": liquidity["bid_ask_spread"],
            "liquidity_volume": liquidity["volume"],
            "strike_count": float(len(rows)),
            "expiry": chain.get("expiry"),
        }
        if atm_call_greeks is not None:
            features["atm_call_delta"] = atm_call_greeks.get("delta")
            features["atm_call_gamma"] = atm_call_greeks.get("gamma")
            features["atm_call_theta"] = atm_call_greeks.get("theta")
            features["atm_call_vega"] = atm_call_greeks.get("vega")
        if atm_put_greeks is not None:
            features["atm_put_delta"] = atm_put_greeks.get("delta")
            features["atm_put_gamma"] = atm_put_greeks.get("gamma")
            features["atm_put_theta"] = atm_put_greeks.get("theta")
            features["atm_put_vega"] = atm_put_greeks.get("vega")

        regime_label = context.regime_state.label if context.regime_state is not None else None
        candidate = Candidate(
            candidate_id=f"OPTIONS_FLOW-{context.at.isoformat()}-{context.underlying}",
            timestamp=context.at,
            underlying=context.underlying,
            strategy="OPTIONS_FLOW",
            strategy_version=self.version,
            direction="NEUTRAL",
            regime=regime_label,
            candidate_score=score,
            features=features,
            params=dict(self.params),
            rationale=(
                f"PCR={pcr:.3f} "
                + (
                    f"({abs(pcr_z):.2f} std from its own rolling mean); "
                    if pcr_z is not None
                    else f"(outside neutral band [{low},{high}]); "
                    if pcr < low or pcr > high
                    else "(within the neutral band, unremarkable); "
                )
                + "flagged as an anomaly measurement only — no directional claim is made."
            ),
        )
        return [candidate]
