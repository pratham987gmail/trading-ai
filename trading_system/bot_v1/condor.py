"""Strategy B (SHADOW ONLY): expiry-eve NIFTY iron condor.

Rule from the research (rulebook_test, config B_WSIG): one trading day before a weekly
expiry, if India VIX >= 18, sell ~15-delta call and put (Black-Scholes delta with VIX as
volatility), buy wings 1 sigma further out (sigma = spot * VIX/100 * sqrt(days/365)),
hold to expiry, settle at intrinsic on the NIFTY close. Fills use REAL quotes: sell at
bid, buy at ask (conservative). Mid-price credit is logged too, for comparison.
Pure functions; no orders are ever placed for this strategy.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from math import erf, log, sqrt

from .config import CondorParams


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def bs_delta(spot: float, strike: float, years: float, vol: float, call: bool) -> float:
    if years <= 0 or vol <= 0:
        return (1.0 if spot > strike else 0.0) if call else (-1.0 if spot < strike else 0.0)
    d1 = (log(spot / strike) + 0.5 * vol * vol * years) / (vol * sqrt(years))
    return _ncdf(d1) if call else _ncdf(d1) - 1.0


@dataclass
class Leg:
    strike: float
    kind: str          # CE / PE
    side: str          # SELL / BUY
    bid: float
    ask: float
    instrument_key: str = ""


@dataclass
class CondorPlan:
    expiry: str
    spot: float
    vix: float
    days_to_expiry: float
    sigma_pts: float
    legs: list[Leg]
    credit_real: float      # sell at bid, buy at ask (points)
    credit_mid: float       # at mid prices (points)
    width: float
    max_loss_pts: float
    lot_size: int
    lots: int

    def to_dict(self) -> dict:
        d = asdict(self)
        d["legs"] = [asdict(x) for x in self.legs]
        return d


class NoTrade(Exception):
    """Raised with a human-readable reason when the rule says no trade."""


def plan_condor(chain: list[dict], spot: float, vix: float, expiry: str, days_to_expiry: float,
                lot_size: int, p: CondorParams, wing_mode: str = "sigma") -> CondorPlan:
    """chain: rows with strike, ce_bid, ce_ask, pe_bid, pe_ask (+ optional ce_key, pe_key).
    wing_mode: "sigma" (1 x sigma points, research config B_WSIG) or "points100" (fixed 100 points)."""
    if vix < p.min_vix:
        raise NoTrade(f"VIX {vix:.2f} < {p.min_vix} — rule says no trade")
    if not chain:
        raise NoTrade("empty option chain")
    years = max(days_to_expiry, 0.5) / 365.0
    vol = vix / 100.0
    sigma_pts = spot * vol * sqrt(years)
    strikes = sorted(r["strike"] for r in chain)
    by = {r["strike"]: r for r in chain}
    calls = [k for k in strikes if k > spot]
    puts = [k for k in strikes if k < spot]
    if not calls or not puts:
        raise NoTrade("chain does not straddle spot")
    sc = min(calls, key=lambda k: abs(bs_delta(spot, k, years, vol, True) - p.short_delta))
    sp = min(puts, key=lambda k: abs(abs(bs_delta(spot, k, years, vol, False)) - p.short_delta))
    if wing_mode == "sigma":
        w = p.wing_sigma * sigma_pts
    elif wing_mode == "points100":
        w = p.wing_points
    else:
        raise ValueError(f"unknown wing_mode {wing_mode!r}")

    def nearest(target: float, side: str) -> float:
        cands = [k for k in strikes if (k > sc if side == "C" else k < sp)]
        if not cands:
            raise NoTrade("no wing strike available")
        return min(cands, key=lambda k: abs(k - target))

    lc, lp = nearest(sc + w, "C"), nearest(sp - w, "P")
    legs = [
        Leg(sc, "CE", "SELL", by[sc]["ce_bid"], by[sc]["ce_ask"], by[sc].get("ce_key", "")),
        Leg(lc, "CE", "BUY", by[lc]["ce_bid"], by[lc]["ce_ask"], by[lc].get("ce_key", "")),
        Leg(sp, "PE", "SELL", by[sp]["pe_bid"], by[sp]["pe_ask"], by[sp].get("pe_key", "")),
        Leg(lp, "PE", "BUY", by[lp]["pe_bid"], by[lp]["pe_ask"], by[lp].get("pe_key", "")),
    ]
    for leg in legs:
        if not (leg.bid and leg.ask) or leg.bid <= 0 or leg.ask <= 0 or leg.ask < leg.bid:
            raise NoTrade(f"no valid two-sided quote for {leg.kind} {leg.strike:g} (bid {leg.bid}, ask {leg.ask})")
    real = sum(l.bid if l.side == "SELL" else -l.ask for l in legs)
    mid = sum((l.bid + l.ask) / 2 * (1 if l.side == "SELL" else -1) for l in legs)
    width = min(lc - sc, sp - lp)
    if real <= 0:
        raise NoTrade(f"credit at real quotes is {real:.2f} <= 0")
    return CondorPlan(expiry, spot, vix, days_to_expiry, sigma_pts, legs, real, mid, width,
                      width - real, lot_size, p.lots)


def settle(plan: CondorPlan | dict, nifty_close: float, credit_key: str = "credit_real") -> dict:
    """Expiry settlement at intrinsic value. Returns points and R (pnl / max loss)."""
    d = plan.to_dict() if isinstance(plan, CondorPlan) else plan
    legs = d["legs"]
    sc = next(l["strike"] for l in legs if l["kind"] == "CE" and l["side"] == "SELL")
    lc = next(l["strike"] for l in legs if l["kind"] == "CE" and l["side"] == "BUY")
    sp = next(l["strike"] for l in legs if l["kind"] == "PE" and l["side"] == "SELL")
    lp = next(l["strike"] for l in legs if l["kind"] == "PE" and l["side"] == "BUY")
    S = nifty_close
    payout = (max(S - sc, 0) - max(S - lc, 0)) + (max(sp - S, 0) - max(lp - S, 0))
    credit = d[credit_key]
    pnl_pts = credit - payout
    max_loss = d["width"] - credit
    return {"settle_spot": S, "payout_pts": payout, "pnl_pts": pnl_pts,
            "r": pnl_pts / max_loss if max_loss > 0 else float("nan"),
            "pnl_rupees_gross": pnl_pts * d["lot_size"] * d["lots"]}
