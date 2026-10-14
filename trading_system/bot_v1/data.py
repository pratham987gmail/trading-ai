"""Market data for bot_v1: NIFTY 200 universe, daily closes, NIFTY index, India VIX.

Universe: NSE's published NIFTY 200 constituent CSV (download it from niftyindices.com,
'ind_nifty200list.csv', and save under data/bot_v1/). Instrument keys come from the
project's Upstox instrument master (core.upstox_instruments) — never hard-coded.
Closes are cached per symbol in data/bot_v1/closes/<SYMBOL>.csv and topped up daily.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from .upstox_api import UpstoxAPI

NIFTY_KEY = "NSE_INDEX|Nifty 50"
VIX_KEY = "NSE_INDEX|India VIX"


def load_universe(path: Path) -> list[str]:
    df = pd.read_csv(path)
    col = next(c for c in df.columns if c.strip().lower() == "symbol")
    return sorted(df[col].astype(str).str.strip().unique().tolist())


def equity_keys(symbols: list[str], master: pd.DataFrame) -> dict[str, str]:
    """Map NSE trading symbols to Upstox NSE_EQ instrument keys using the instrument master."""
    cols = {c.lower(): c for c in master.columns}
    seg = cols.get("segment"); ts = cols.get("trading_symbol") or cols.get("tradingsymbol"); key = cols["instrument_key"]
    m = master
    if seg:
        m = m[m[seg] == "NSE_EQ"]
    m = m.drop_duplicates(ts).set_index(ts)[key]
    return {s: m[s] for s in symbols if s in m.index}


def _cache(dirpath: Path, name: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    return dirpath / f"{name.replace('|', '_').replace(' ', '_')}.csv"


def update_closes(api: UpstoxAPI, keys: dict[str, str], cache_dir: Path, years: int = 2) -> pd.DataFrame:
    """Fetch/top-up daily closes; returns DataFrame (dates x symbols). Upstox daily candles: [ts,o,h,l,c,v,oi]."""
    today = date.today()
    frames = {}
    for sym, key in keys.items():
        f = _cache(cache_dir, sym)
        old = pd.read_csv(f, parse_dates=["date"]) if f.exists() else pd.DataFrame(columns=["date", "close"])
        start = (old["date"].max().date() + timedelta(days=1)) if len(old) else today - timedelta(days=365 * years + 30)
        if start <= today:
            candles = api.daily_candles(key, start.isoformat(), today.isoformat())
            new = pd.DataFrame([(pd.Timestamp(c[0]).tz_localize(None).normalize(), float(c[4])) for c in candles],
                               columns=["date", "close"])
            old = pd.concat([old, new]).drop_duplicates("date", keep="last").sort_values("date")
            old.to_csv(f, index=False)
        frames[sym] = old.set_index("date")["close"]
    return pd.DataFrame(frames).sort_index()


def update_index(api: UpstoxAPI, key: str, cache_dir: Path, years: int = 2) -> pd.Series:
    return update_closes(api, {key: key}, cache_dir, years)[key]


def stale(series: pd.Series, expected_last_trading_day: pd.Timestamp) -> bool:
    """Charter stale-data rule (daily version): the latest close must be the expected trading day."""
    return series.dropna().index.max().normalize() < expected_last_trading_day.normalize()
