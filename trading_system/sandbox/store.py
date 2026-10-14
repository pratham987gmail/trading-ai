"""Point-in-time data store for the blind sandboxes.

The engine owns a DataStore (all history). Strategies never see it: on each session they get a
View that is cut off at that session's close. Every View accessor slices to rows <= today, and the
leak test (sandbox.leakcheck) proves it end-to-end by scrambling all data after a cutoff date and
checking that every decision up to the cutoff is bit-for-bit identical.

Data (read once, cached as a pickle):
  equity EOD (NSE, 2005-2026, incl. delisted names) -> adjusted/raw/turnover panels via the bot's
      own bot_v1.backtest.build_panels (split snapping etc.)
  NIFTY index close (2010-), India VIX (2010-), NIFTY option EOD chains (2015-)
  'NIFTYBEES*' = an index-fund proxy: NIFTY close + 1.25%/yr accrual (dividends ~1.3% less ~0.05%
      fee), priced at NIFTY/100 so integer units work. The real NIFTYBEES feed has bad opens.
"""
from __future__ import annotations

import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from bot_v1.backtest import build_panels, load_equity

INDEX_FUND = "NIFTYBEES*"
FUND_ACCRUAL = 0.0125


class LookaheadError(RuntimeError):
    pass


@dataclass
class DataStore:
    dates: pd.DatetimeIndex
    adj: pd.DataFrame          # signal prices (adjusted), incl. INDEX_FUND
    raw: pd.DataFrame          # traded close (ffilled), incl. INDEX_FUND
    tv: pd.DataFrame           # traded value
    ca: pd.DataFrame           # split/bonus qty factor on ex-date
    present: pd.DataFrame
    hist: pd.DataFrame         # sessions of history so far
    nifty: pd.Series
    vix: pd.Series
    options: dict              # trade_date -> DataFrame of that day's NIFTY option EOD rows

    # numpy mirrors for speed
    def __post_init__(self):
        self.R = (self.adj / self.adj.shift(1) - 1).fillna(0.0).to_numpy()
        self.cols = list(self.adj.columns)
        self.col_ix = {c: i for i, c in enumerate(self.cols)}
        self.month_end = np.array([i + 1 < len(self.dates) and self.dates[i + 1].month != self.dates[i].month
                                   for i in range(len(self.dates))])

    def view(self, t: int) -> "View":
        return View(self, t)

    def pos(self, day) -> int:
        return int(self.dates.searchsorted(pd.Timestamp(day)))


class View:
    """Everything a strategy may know at the close of session t. Nothing after t is reachable."""

    def __init__(self, s: DataStore, t: int):
        self._s, self.t, self.today = s, t, s.dates[t]

    # calendar: exchange calendars are published in advance, so knowing the next session is legit
    def is_month_end(self) -> bool:
        return bool(self._s.month_end[self.t])

    def next_session(self) -> pd.Timestamp | None:
        return self._s.dates[self.t + 1] if self.t + 1 < len(self._s.dates) else None

    def _guard(self, n: int) -> slice:
        return slice(max(0, self.t - n + 1), self.t + 1)

    def adj_window(self, cols: list[str], n: int) -> pd.DataFrame:
        return self._s.adj.iloc[self._guard(n)][cols]

    def returns_today(self) -> np.ndarray:
        return self._s.R[self.t]

    def nifty(self, n: int = 10_000) -> pd.Series:
        s = self._s.nifty
        return s.iloc[: s.index.searchsorted(self.today, side="right")].iloc[-n:]

    def vix(self) -> float:
        s = self._s.vix
        i = s.index.searchsorted(self.today, side="right")
        return float(s.iloc[i - 1]) if i else float("nan")

    def vix_is_fresh(self) -> bool:
        return self.today in self._s.vix.index

    def fund_price(self) -> float:
        return float(self._s.raw.iat[self.t, self._s.col_ix[INDEX_FUND]])

    def universe(self, n: int = 200) -> list[str]:
        s, t = self._s, self.t
        win = s.tv.iloc[max(0, t - 59): t + 1]
        m = win.mean().where(win.notna().sum() >= 40)
        ok = s.present.iloc[t] & (s.hist.iloc[t] >= 250)
        m = m.where(ok).dropna()
        m = m.drop(INDEX_FUND, errors="ignore")
        return m.sort_values(ascending=False).head(n).index.tolist()

    def option_chain(self) -> pd.DataFrame:
        return self._s.options.get(self.today, pd.DataFrame())

    def at(self, t: int):
        raise LookaheadError("strategies cannot move the clock")


# ------------------------------------------------------------------------------------------- build
def lot_size(expiry: pd.Timestamp) -> int:
    """NIFTY lot size by expiry (approximate switch dates; NSE circulars)."""
    e = pd.Timestamp(expiry)
    for until, lot in (("2015-11-01", 25), ("2021-07-01", 75), ("2024-05-01", 50), ("2025-01-01", 25),
                       ("2026-01-01", 75)):
        if e < pd.Timestamp(until):
            return lot
    return 65


def build(realdata: Path, cache: Path) -> DataStore:
    if cache.exists():
        with open(cache, "rb") as f:
            return pickle.load(f)
    eq = load_equity(realdata / "equity_eod.pkl")
    raw, adj, tv, ca, present = build_panels(eq)
    del eq
    nifty = pd.read_pickle(realdata / "nifty.pkl")
    nifty = pd.Series(nifty.close.values, index=pd.to_datetime(nifty.trade_date)).sort_index()
    # NIFTY history starts 2010: splice NIFTYBEES closes before that (scaled on the first overlap day;
    # its pre-2010 closes are clean, its 2019 1:10 split is after the splice)
    allq = pd.read_pickle(realdata / "equity_eod.pkl", )
    bees = allq[allq.symbol == "NIFTYBEES"]
    del allq
    bees = pd.Series(bees.close.values, index=pd.to_datetime(bees.trade_date)).sort_index()
    first = nifty.index[0]
    pre = bees[bees.index < first] * (nifty.iloc[0] / bees.loc[first])
    nifty = pd.concat([pre, nifty])
    vix = pd.read_pickle(realdata / "vix.pkl")
    vix = pd.Series(vix.close.values, index=pd.to_datetime(vix.trade_date)).sort_index()
    dates = raw.index[raw.index >= pd.Timestamp("2005-06-01")]
    raw, adj, tv, ca, present = (x.loc[dates] for x in (raw, adj, tv, ca, present))
    # index fund proxy, defined from 2010 (NIFTY history start)
    n = nifty.reindex(dates)
    acc = np.exp(np.log1p(FUND_ACCRUAL) / 252 * np.arange(len(dates)))
    fund = n / 100 * acc
    adj[INDEX_FUND] = fund
    raw = raw.ffill()
    raw[INDEX_FUND] = fund.ffill()
    tv[INDEX_FUND] = np.nan
    ca[INDEX_FUND] = np.nan
    present[INDEX_FUND] = fund.notna()
    hist = present.cumsum()
    o = pd.read_pickle(realdata / "nifty_options_eod.pkl")
    o = o[o.contracts > 0][["trade_date", "expiry_date", "strike", "option_type", "close"]].copy()
    o["trade_date"] = pd.to_datetime(o.trade_date)
    o["expiry_date"] = pd.to_datetime(o.expiry_date)
    options = {d: g.reset_index(drop=True) for d, g in o.groupby("trade_date")}
    s = DataStore(dates, adj.astype("float32").astype("float64"), raw, tv, ca, present, hist, nifty, vix, options)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with open(cache, "wb") as f:
        pickle.dump(s, f, protocol=5)
    return s


def poisoned(s: DataStore, cutoff: str, seed: int = 7) -> DataStore:
    """Copy of the store with every value AFTER `cutoff` scrambled (prices x random factors, VIX
    shuffled, option closes x random). Used by the leak test only."""
    rng = np.random.default_rng(seed)
    c = pd.Timestamp(cutoff)
    fut = s.dates > c

    def scr(df):
        df = df.copy()
        df.loc[fut] = df.loc[fut].to_numpy() * rng.uniform(0.5, 1.5, size=df.loc[fut].shape)
        return df

    nifty = s.nifty.copy(); m = nifty.index > c
    nifty[m] = nifty[m].to_numpy() * rng.uniform(0.8, 1.2, m.sum())
    vix = s.vix.copy(); m = vix.index > c
    vix[m] = rng.permutation(vix[m].to_numpy()) * rng.uniform(0.5, 2.0, m.sum())
    opts = {}
    for d, g in s.options.items():
        if d > c:
            g = g.copy(); g["close"] = g.close * rng.uniform(0.3, 3.0, len(g))
        opts[d] = g
    return DataStore(s.dates, scr(s.adj), scr(s.raw), scr(s.tv), s.ca, s.present, s.hist, nifty, vix, opts)
