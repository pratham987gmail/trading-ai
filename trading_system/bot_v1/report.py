"""Performance report and the condor promotion rule."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from .journal import Journal

# Promotion rule for the shadow condor (fixed in advance, mirrors the research protocol):
PROMO_MIN_TRADES = 50
PROMO_MIN_PF = 1.15
PROMO_MAX_P_NONPOSITIVE = 0.10


def condor_charges_points(plan: dict) -> float:
    """Approximate Indian F&O charges for 4 legs opened and held to expiry (in points per unit).
    Brokerage ₹20 x 4 entry orders (+18% GST), STT 0.1% on sold premium, exchange 0.035% of premium."""
    legs = plan["legs"]
    lot = plan["lot_size"] * plan["lots"]
    sold = sum(l["bid"] for l in legs if l["side"] == "SELL")
    turnover = sum((l["bid"] if l["side"] == "SELL" else l["ask"]) for l in legs)
    rupees = 4 * 20 * 1.18 + 0.001 * sold * lot + 0.00035 * turnover * lot * 1.18
    return rupees / lot


def condor_stats(journal: Journal, seed: int = 0) -> pd.DataFrame:
    rows = []
    for expiry, variant, plan, result in journal.settled_condors():
        p, r = json.loads(plan), json.loads(result)
        cost = condor_charges_points(p)
        net = r["pnl_pts"] - cost
        rows.append(dict(expiry=expiry, variant=variant, r_net=net / p["max_loss_pts"],
                         rupees_net=net * p["lot_size"] * p["lots"]))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    # v2: variant tags are "UNDERLYING:variant"; add pooled rows per variant. The promotion rule
    # applies to the pre-registered PRIMARY only: ALL:sigma (NIFTY + SENSEX pooled).
    if df.variant.str.contains(":").any():
        pooled = df[df.variant.str.contains(":")].copy()
        pooled["variant"] = "ALL:" + pooled.variant.str.split(":").str[1]
        df = pd.concat([df, pooled], ignore_index=True)
    rng = np.random.default_rng(seed)
    out = []
    for v, g in df.groupby("variant"):
        x = g.r_net.to_numpy()
        gp, gl = x[x > 0].sum(), -x[x < 0].sum()
        boot = np.array([rng.choice(x, len(x)).mean() for _ in range(2000)]) if len(x) > 1 else np.array([x.mean()])
        p0 = float((boot <= 0).mean())
        pf = gp / gl if gl > 0 else np.inf
        primary = v in ("ALL:sigma", "sigma")
        promote = primary and len(x) >= PROMO_MIN_TRADES and pf >= PROMO_MIN_PF and p0 < PROMO_MAX_P_NONPOSITIVE
        out.append(dict(variant=v, trades=len(x), win_rate=float((x > 0).mean()), avg_r=float(x.mean()),
                        profit_factor=pf, p_avg_r_le_0=p0, rupees_net=float(g.rupees_net.sum()),
                        promotion=("info only (secondary)" if not primary else "ELIGIBLE" if promote else f"not yet ({len(x)}/{PROMO_MIN_TRADES} trades)" if len(x) < PROMO_MIN_TRADES else "FAILS rule")))
    return pd.DataFrame(out)


def momentum_curve(journal: Journal) -> pd.DataFrame:
    df = pd.read_sql("SELECT date, equity, benchmark FROM marks WHERE strategy='MOM30' ORDER BY date", journal.db)
    if df.empty:
        return df
    df["strategy_return"] = df.equity / df.equity.iloc[0] - 1
    df["nifty_return"] = df.benchmark / df.benchmark.iloc[0] - 1
    df["drawdown"] = df.equity / df.equity.cummax() - 1
    return df
