"""Shared builders for the Phase 4A signal tests.

Not collected by pytest (the leading underscore keeps it out of ``test_*.py``), so
it can hold helpers without pretending to be a test module.

**Expiries are computed, never hardcoded.** A literal ``"28AUG2026"`` would make
every test in this suite start failing the day it passed, and the failure would look
like a logic bug rather than a calendar one. Every builder derives its expiry from
today plus an offset.

**Quotes are explicit.** The pricing and cost tests need exact arithmetic to check
against, so a chain is built from a mapping of strike to ``(bid, ask)`` rather than
from a model. :func:`ladder_chain` exists separately for the selector and engine
tests, which need many strikes and do not care about exact premiums.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

#: Default contract lot size in these fixtures. Real code reads this from the
#: instrument master; the tests pin it so the arithmetic is checkable.
LOT_SIZE = 65


def expiry_in(days: int) -> str:
    """Return a broker-format expiry a given number of days from today.

    Args:
        days: Days ahead.

    Returns:
        Expiry as ``DDMMMYYYY``, e.g. ``31AUG2026``.
    """
    return (date.today() + timedelta(days=days)).strftime("%d%b%Y").upper()


def make_leg(
    bid: float | None,
    ask: float | None,
    *,
    ltp: float | None = None,
    bid_qty: int | None = 5000,
    ask_qty: int | None = 5000,
    volume: int | None = 25_000,
    open_interest: int | None = 500_000,
    lot_size: int = LOT_SIZE,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Build one option leg.

    Args:
        bid: Best bid, ``None`` for no bid.
        ask: Best ask, ``None`` for no ask.
        ltp: Last traded price. Defaults to the midpoint when a book exists.
        bid_qty: Resting bid size.
        ask_qty: Resting ask size.
        volume: Contracts traded.
        open_interest: Open interest.
        lot_size: Contract lot size.
        symbol: Trading symbol.

    Returns:
        A leg mapping in the shape ``fetch_option_chain`` produces.
    """
    if ltp is None and bid is not None and ask is not None:
        ltp = round((bid + ask) / 2.0, 2)
    return {
        "symbol": symbol,
        "token": None,
        "ltp": ltp,
        "bid": bid,
        "ask": ask,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "volume": volume,
        "open_interest": open_interest,
        "previous_close": None,
        "change_pct": None,
        "exchange_timestamp": None,
        "lot_size": lot_size,
    }


def make_chain(
    quotes: dict[float, dict[str, tuple[float | None, float | None] | None]],
    *,
    spot: float = 24_500.0,
    underlying: str = "NIFTY",
    expiry_days: int = 7,
    lot_size: int = LOT_SIZE,
    **leg_kwargs: Any,
) -> dict[str, Any]:
    """Build a chain from explicit quotes.

    Args:
        quotes: ``{strike: {"call": (bid, ask), "put": (bid, ask)}}``. A side set to
            ``None`` is absent from the chain entirely.
        spot: Spot price.
        underlying: Underlying symbol.
        expiry_days: Days until expiry.
        lot_size: Contract lot size.
        **leg_kwargs: Passed to :func:`make_leg` for every leg.

    Returns:
        A chain mapping in the shape ``fetch_option_chain`` produces.
    """
    strikes = []
    for strike in sorted(quotes):
        sides = quotes[strike]
        row: dict[str, Any] = {"strike": float(strike), "call": None, "put": None}
        for side in ("call", "put"):
            quote = sides.get(side)
            if quote is None:
                continue
            row[side] = make_leg(quote[0], quote[1], lot_size=lot_size, **leg_kwargs)
        strikes.append(row)

    return {
        "underlying": underlying,
        "expiry": expiry_in(expiry_days),
        "exchange": "NFO",
        "spot": spot,
        "atm_strike": min(quotes, key=lambda s: abs(s - spot)) if quotes else None,
        "strike_count": len(strikes),
        "strikes": strikes,
        "totals": {},
        "note": None,
        "fetched_at": "2026-08-24T11:00:00+05:30",
    }


def ladder_chain(
    *,
    spot: float = 24_500.0,
    step: float = 50.0,
    count: int = 6,
    expiry_days: int = 7,
    underlying: str = "NIFTY",
    lot_size: int = LOT_SIZE,
    base_premium: float = 90.0,
    slope: float = 0.45,
    tick: float = 0.5,
    **leg_kwargs: Any,
) -> dict[str, Any]:
    """Build a chain with many strikes and a monotone premium ladder.

    Calls get cheaper as strikes rise and puts get dearer, which is the only
    property the selector and engine tests depend on. Premiums are linear rather
    than Black-Scholes because these tests are about gating, not pricing.

    Args:
        spot: Spot price.
        step: Strike interval.
        count: Strikes each side of spot.
        expiry_days: Days until expiry.
        underlying: Underlying symbol.
        lot_size: Contract lot size.
        base_premium: At-the-money premium.
        slope: Premium change per point of moneyness.
        tick: Half-spread on every leg.
        **leg_kwargs: Passed to :func:`make_leg`.

    Returns:
        A chain mapping.
    """
    quotes: dict[float, dict[str, tuple[float | None, float | None] | None]] = {}
    for offset in range(-count, count + 1):
        strike = spot + offset * step
        moneyness = strike - spot
        call_mid = max(tick * 2 + 0.5, base_premium - slope * moneyness)
        put_mid = max(tick * 2 + 0.5, base_premium + slope * moneyness)
        quotes[strike] = {
            "call": (round(call_mid - tick, 2), round(call_mid + tick, 2)),
            "put": (round(put_mid - tick, 2), round(put_mid + tick, 2)),
        }
    return make_chain(
        quotes,
        spot=spot,
        underlying=underlying,
        expiry_days=expiry_days,
        lot_size=lot_size,
        **leg_kwargs,
    )


def make_candles(
    bars: int = 250,
    *,
    direction: str = "up",
    start_price: float = 24_000.0,
    drift: float = 4.0,
    noise: float = 6.0,
    seed: int = 7,
    freq: str = "15min",
) -> pd.DataFrame:
    """Build an OHLCV frame with a controllable trend.

    Args:
        bars: Bar count.
        direction: ``up``, ``down``, or ``flat``.
        start_price: Opening level.
        drift: Points of trend per bar.
        noise: Standard deviation of bar-to-bar noise.
        seed: Random seed, so a failure is reproducible.
        freq: Bar interval.

    Returns:
        A frame with ``open``, ``high``, ``low``, ``close``, ``volume`` and a
        timezone-aware ascending index.
    """
    rng = np.random.default_rng(seed)
    sign = {"up": 1.0, "down": -1.0, "flat": 0.0}[direction]

    steps = sign * drift + rng.normal(0.0, noise, bars)
    closes = start_price + np.cumsum(steps)

    opens = np.concatenate([[start_price], closes[:-1]])
    spans = np.abs(rng.normal(0.0, noise, bars)) + 2.0
    highs = np.maximum(opens, closes) + spans
    lows = np.minimum(opens, closes) - spans

    index = pd.date_range(
        "2026-08-01 09:15", periods=bars, freq=freq, tz="Asia/Kolkata", name="timestamp"
    )
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": rng.integers(50_000, 150_000, bars),
        },
        index=index,
    )


def settings_for(**overrides: Any) -> Any:
    """Build isolated settings that ignore any real ``.env``.

    Args:
        **overrides: Field overrides.

    Returns:
        A ``Settings`` instance.
    """
    from config.settings import Settings

    defaults: dict[str, Any] = {
        "TOTAL_CAPITAL": 100_000.0,
        "RISK_PER_TRADE_PCT": 1.0,
        "SIGNAL_MIN_CHAIN_COVERAGE": 0.85,
        "SIGNAL_MIN_DAYS_TO_EXPIRY": 2.0,
        "SIGNAL_MAX_DAYS_TO_EXPIRY": 14.0,
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)
