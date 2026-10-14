"""Market data for bot_v1: NIFTY 200 universe, daily closes, NIFTY index, India VIX.

Universe: NSE's published NIFTY 200 constituent CSV (download it from niftyindices.com,
'ind_nifty200list.csv', and save under data/bot_v1/). Instrument keys come from the
project's Upstox instrument master (core.upstox_instruments) — never hard-coded.
Closes are cached per symbol in data/bot_v1/closes/<SYMBOL>.csv and topped up daily.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from .upstox_api import UpstoxAPI

NIFTY_KEY = "NSE_INDEX|Nifty 50"
VIX_KEY = "NSE_INDEX|India VIX"


def load_universe(path: Path) -> list[str]:
    df = pd.read_csv(path)
    col = next(c for c in df.columns if c.strip().lower() == "symbol")
    return sorted(s for s in df[col].astype(str).str.strip().unique() if not is_fund(s))


# ETFs / index funds / liquid & commodity funds trade as NSE "EQ" but are not stocks. A near-zero-volatility
# liquid fund gets a huge vol-scaled momentum score, so they must be excluded (stocks-only re-test, Oct 2026:
# holdout 15.2%/yr vs NIFTY 11.1%, max drawdown 27.8% vs 38.4% — still passes).
FUND_PATTERN = re.compile(r"BEES|ETF|LIQUID|CASE|GOLD|SILVER|NIFTY|SENSEX|JUNIOR|^N100|^MON100|^MAFANG|^HNG", re.I)


def is_fund(symbol: str) -> bool:
    return bool(FUND_PATTERN.search(symbol))


def build_liquidity_universe(equities_db: Path, out: Path, n: int = 200, days: int = 60) -> Path:
    """Research universe: top-n NSE EQ stocks by average traded value over the last `days` sessions,
    requiring ~1 year of history. Built from the project's equities.db (NSE bhavcopy)."""
    import sqlite3
    con = sqlite3.connect(f"file:{equities_db}?mode=ro", uri=True)
    last = [r[0] for r in con.execute(
        "SELECT DISTINCT trade_date FROM equity_eod WHERE series='EQ' ORDER BY trade_date DESC LIMIT ?", (days,))]
    if not last:
        raise RuntimeError("equities.db has no EQ rows")
    year_ago = con.execute("SELECT DISTINCT trade_date FROM equity_eod WHERE series='EQ' AND trade_date <= date(?, '-365 days') "
                           "ORDER BY trade_date DESC LIMIT 1", (last[0],)).fetchone()
    q = f"""SELECT symbol, AVG(traded_value) v, COUNT(*) c FROM equity_eod
            WHERE series='EQ' AND trade_date >= ? GROUP BY symbol HAVING c >= {int(days * 0.66)}"""
    df = pd.read_sql(q, con, params=(last[-1],))
    if year_ago:
        old = {r[0] for r in con.execute("SELECT symbol FROM equity_eod WHERE series='EQ' AND trade_date=?", (year_ago[0],))}
        df = df[df.symbol.isin(old)]
    df = df[~df.symbol.map(is_fund)]
    df = df.sort_values("v", ascending=False).head(n)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"Symbol": df.symbol, "avg_traded_value": df.v, "as_of": last[0]}).to_csv(out, index=False)
    return out


MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"


def load_nse_master(cache_dir: Path, max_age_hours: float = 20) -> pd.DataFrame:
    """Upstox's public NSE instrument master (no auth), cached locally, refreshed daily."""
    import gzip, json, time
    import requests
    f = cache_dir / "NSE.json.gz"
    cache_dir.mkdir(parents=True, exist_ok=True)
    if not f.exists() or (time.time() - f.stat().st_mtime) > max_age_hours * 3600:
        r = requests.get(MASTER_URL, timeout=60)
        r.raise_for_status()
        f.write_bytes(r.content)
    return pd.DataFrame(json.loads(gzip.decompress(f.read_bytes())))


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
