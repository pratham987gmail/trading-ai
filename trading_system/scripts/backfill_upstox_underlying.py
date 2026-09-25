"""One-off backfill: NIFTY + SENSEX index-level daily candles via Upstox's
regular (non-expired) history endpoint, into the same ``HistoricalBarStore``
the option backfill (``scripts/backfill_upstox_options.py``) already writes
to. Underlying bars are stored with ``strike=None, option_type=None`` so
they sit alongside the option bars without colliding with them (the store's
uniqueness key includes ``instrument_key``, and the index's key is never a
valid option instrument key).

Why this exists: every predictive-value study so far (technicals, options
flow, max-pain, FII/DII, chain-replay direction accuracy, mechanical
verticals) tested a directional or premium-decay edge and found none. A
Donchian-breakout/ATR study is a different edge class (volatility
expansion, not direction prediction) and needs real daily index OHLC to
compute Donchian channels and ATR -- which nothing backfilled so far
provides (the option backfill has strike-level premium bars only).

Run:
    python -m scripts.backfill_upstox_underlying
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import get_settings  # noqa: E402
from core.upstox_client import UpstoxClient  # noqa: E402
from core.upstox_instruments import download_instrument_master, load_instrument_master, resolve_underlying_index_key  # noqa: E402
from research.data.upstox.normalization import normalize_candle_response  # noqa: E402
from research.data.upstox.store import HistoricalBarStore  # noqa: E402

UNDERLYING_INDEX_KEYS: dict[str, str] = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    # SENSEX has no stable hardcoded key (unlike NIFTY's well-known
    # NSE_INDEX form) -- resolved live from the BSE master below instead of
    # guessing.
}

LOOKBACK_DAYS = 3.7 * 365  # matches what Upstox's regular-history endpoint actually serves


def main() -> int:
    settings = get_settings()
    if not settings.upstox_credentials_configured:
        print("UPSTOX_AUTH_UNAVAILABLE")
        return 1

    client = UpstoxClient(settings)
    store = HistoricalBarStore(str(Path(__file__).parent.parent / "data" / "historical_bars.db"))

    bse_master = download_instrument_master("BSE")
    bse_frame = load_instrument_master(bse_master)
    sensex_key = resolve_underlying_index_key(bse_frame, "SENSEX")
    if sensex_key is None:
        print("Could not resolve SENSEX index key from the BSE master")
        return 1

    keys = dict(UNDERLYING_INDEX_KEYS)
    keys["SENSEX"] = sensex_key

    to_date = date.today().isoformat()
    from_date = (date.today() - timedelta(days=int(LOOKBACK_DAYS))).isoformat()

    total_written = 0
    for underlying, instrument_key in keys.items():
        resp = client.get_historical_candles_v3(instrument_key, "days", 1, to_date, from_date)
        if not resp.ok or not resp.data:
            print(f"{underlying}: fetch failed ({resp.error})")
            continue
        bars, warnings = normalize_candle_response(
            {"data": resp.data},
            provider="upstox",
            instrument_key=instrument_key,
            underlying=underlying,
            expiry=None,
            strike=None,
            option_type=None,
            exchange="NSE" if underlying == "NIFTY" else "BSE",
            segment="INDEX",
            interval_unit="day",
            interval_value=1,
        )
        written = store.write_bars([b.as_dict() for b in bars])
        total_written += written
        print(f"{underlying}: {len(bars)} bars parsed, {written} newly written" + (f" ({len(warnings)} warnings)" if warnings else ""))

    print(f"\nTotal new bars written: {total_written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
