"""A credential-free :class:`~core.chain_recorder.ChainFetcher` for replay/demo use.

``core.chain_recorder.ChainFetcher`` is declared as a ``Protocol`` specifically
so the recorder's real loop can be driven without a broker session (see that
module's docstring). :class:`ReplayFetcher` is the drop-in double: it
generates a small, seeded, realistic-looking option chain sequence for
NIFTY/BANKNIFTY/SENSEX with no network access and no credentials, so
``scripts/replay_collector.py`` can exercise ``ChainRecorder`` and
``OptionChainStore`` end to end.

Nothing here is market data. Every chain carries a ``note`` field saying so,
mirroring the same discipline ``core.data_fetcher`` uses for its own
diagnostic fields.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from typing import Any, Callable

from core.market_hours import now_ist

#: Round-number strike spacing per underlying, mirroring real NSE contracts.
STRIKE_STEP: dict[str, float] = {"NIFTY": 50.0, "BANKNIFTY": 100.0, "SENSEX": 100.0}

#: Plausible spot levels to drift around, purely for realistic-looking output.
DEFAULT_SPOT: dict[str, float] = {"NIFTY": 24_500.0, "BANKNIFTY": 51_000.0, "SENSEX": 81_000.0}

#: Lot sizes, matching the ballpark used elsewhere in the test fixtures.
LOT_SIZE: dict[str, int] = {"NIFTY": 65, "BANKNIFTY": 35, "SENSEX": 20}


def synthetic_expiries(anchor: date, count: int = 3) -> list[str]:
    """Return ``count`` weekly expiries after ``anchor``, as ``DDMMMYYYY``.

    Args:
        anchor: The date to count forward from.
        count: How many expiries to return.

    Returns:
        Expiry strings, soonest first.
    """
    out: list[str] = []
    cursor = anchor
    while len(out) < count:
        cursor += timedelta(days=7)
        out.append(cursor.strftime("%d%b%Y").upper())
    return out


class ReplayFetcher:
    """Replays a small synthetic option chain sequence.

    No network, no broker, no credentials. Prices drift from a fixed seed so
    runs are reproducible; ``fetched_at`` is stamped from the injected clock so
    ``captured_at`` values line up with whatever is driving this fetcher.

    Args:
        clock: Returns the current time, used to stamp ``fetched_at`` and to
            derive synthetic expiries. Defaults to
            :func:`core.market_hours.now_ist`.
        seed: Random seed, for reproducible premium drift across runs.
        fail_on: A set of ``(underlying, call_index)`` pairs. When the given
            underlying's ``call_index``-th call to :meth:`fetch_option_chain`
            is reached, ``None`` is returned instead of a chain, simulating a
            broker outage for that one call. ``call_index`` counts from zero,
            per underlying.
        strike_span: Strikes generated on each side of the at-the-money
            strike, capped for fetch speed regardless of the caller's
            requested ``strike_count``.
    """

    def __init__(
        self,
        clock: Callable[[], datetime] = now_ist,
        seed: int = 20260920,
        fail_on: set[tuple[str, int]] | None = None,
        strike_span: int = 5,
    ) -> None:
        """Initialise the replay fetcher."""
        self._clock = clock
        self._rng = random.Random(seed)
        self._fail_on = fail_on or set()
        self._strike_span = max(1, strike_span)
        self._call_counts: dict[str, int] = {}

    def fetch_expiries(self, underlying: str, exchange: str = "NFO") -> list[str]:
        """Return synthetic unexpired expiries, soonest first."""
        return synthetic_expiries(self._clock().date())

    def fetch_option_chain(
        self,
        underlying: str,
        expiry: str | None = None,
        strike_count: int = 20,
        exchange: str = "NFO",
    ) -> dict[str, Any] | None:
        """Return a synthetic chain, or ``None`` for a configured failure.

        Args:
            underlying: Underlying name.
            expiry: Expiry as ``DDMMMYYYY``. Synthesised when omitted.
            strike_count: Requested strikes each side of spot; capped by
                ``strike_span`` for replay speed.
            exchange: Ignored; carried through to the returned chain.

        Returns:
            A chain dict shaped like ``fetch_option_chain``'s real output, or
            ``None`` when this call matches a configured ``fail_on`` entry.
        """
        index = self._call_counts.get(underlying, 0)
        self._call_counts[underlying] = index + 1
        if (underlying, index) in self._fail_on:
            return None

        step = STRIKE_STEP.get(underlying, 50.0)
        base_spot = DEFAULT_SPOT.get(underlying, 20_000.0)
        drift = self._rng.uniform(-0.4, 0.4) * step * (index + 1)
        spot = round(base_spot + drift, 2)
        atm = round(spot / step) * step

        stamp = self._clock()
        exch_ts = stamp.strftime("%d-%b-%Y %H:%M:%S")
        span = min(strike_count, self._strike_span)

        strikes = []
        for offset in range(-span, span + 1):
            strike = atm + offset * step
            call_premium = max(1.0, abs(spot - strike) * 0.05 + 40.0 + self._rng.uniform(-2, 2))
            put_premium = max(1.0, abs(spot - strike) * 0.05 + 35.0 + self._rng.uniform(-2, 2))
            strikes.append(
                {
                    "strike": strike,
                    "call": _leg(underlying, strike, "CE", call_premium, exch_ts),
                    "put": _leg(underlying, strike, "PE", put_premium, exch_ts),
                }
            )

        return {
            "underlying": underlying,
            "expiry": expiry or synthetic_expiries(stamp.date())[0],
            "exchange": exchange,
            "spot": spot,
            "atm_strike": atm,
            "strike_count": len(strikes),
            "strikes": strikes,
            "note": "synthetic replay data; not a market observation",
            "fetched_at": stamp.isoformat(),
        }


def _leg(
    underlying: str, strike: float, option_type: str, premium: float, exchange_timestamp: str
) -> dict[str, Any]:
    """Build one synthetic option leg."""
    ltp = round(premium, 2)
    spread = round(max(0.05, ltp * 0.01), 2)
    return {
        "symbol": f"{underlying}{int(strike)}{option_type}",
        "token": f"{underlying}-{int(strike)}-{option_type}",
        "ltp": ltp,
        "bid": round(ltp - spread, 2),
        "ask": round(ltp + spread, 2),
        "bid_qty": 500,
        "ask_qty": 500,
        "volume": 1_000,
        "open_interest": 50_000,
        "previous_close": round(ltp - 1.0, 2),
        "change_pct": 0.5,
        "exchange_timestamp": exchange_timestamp,
        "lot_size": LOT_SIZE.get(underlying, 50),
    }
