"""Replay the frozen no-VIX-filter condor (and its 8 sibling variants) on the LIVE recorded quotes
from the Azure VM (Angel One recorder, real bid/ask), through the bot's own plan_condor/charges.

Entry: first recorder snapshot at/after 15:05 IST on each expiry eve (latest earlier snapshot if the
recorder stopped before 15:05); legs filled at real bid (sell) / ask (buy). Strikes use the India VIX
15:05 5-minute close as vol, exactly as the live scan does. Settlement: index close on expiry day.

  python live_replay_novix.py <azure options.db> <expired_options_5m.db (for VIX + index closes)>
"""
from __future__ import annotations

import sqlite3
import sys
from dataclasses import replace
from datetime import datetime

import numpy as np
import pandas as pd

from bot_v1 import condor as C
from bot_v1.config import CondorParams
from bot_v1.report import condor_charges_points

FROZEN = "d15_w1.5"                                   # frozen pick from condor_novix_split_results.md
VARIANTS = {f"d{int(d * 100)}_w{w:g}": (d, w) for d in (0.10, 0.15, 0.20) for w in (0.5, 1.0, 1.5)}
IDX = {"NIFTY": "NSE_INDEX|Nifty 50", "SENSEX": "BSE_INDEX|SENSEX"}
VIX = "NSE_INDEX|India VIX"


def ro(path):
    return sqlite3.connect(f"file:{path}?immutable=1", uri=True)


def main(live_db, hist_db):
    h = ro(hist_db)
    daily = pd.read_sql("SELECT key, d, c FROM index_daily", h).pivot(index="d", columns="key", values="c")
    vix5 = pd.read_sql("SELECT ts, c FROM index_5m WHERE key=?", h, params=(VIX,)).set_index("ts").c
    sessions = sorted(daily.index)
    L = ro(live_db)
    snaps = pd.read_sql("SELECT snapshot_id, captured_at, trade_date, underlying, expiry_date, spot FROM snapshots "
                        "WHERE underlying IN ('NIFTY','SENSEX')", L)
    rows = []
    for (day, und), g in snaps.groupby(["trade_date", "underlying"]):
        if day not in sessions or sessions.index(day) + 1 >= len(sessions):
            continue
        nxt = sessions[sessions.index(day) + 1]
        g = g[g.expiry_date == nxt]
        if g.empty:
            continue
        after = g[g.captured_at.str[11:16] >= "15:05"]
        snap = (after.sort_values("captured_at").iloc[0] if len(after) else g.sort_values("captured_at").iloc[-1])
        t = snap.captured_at[11:16]
        if not ("14:50" <= t <= "15:20"):              # rule enters ~15:05; no usable snapshot near it -> skip
            rows.append(dict(eve=day, und=und, variant="ALL", note=f"no snapshot near 15:05 (nearest {t})"))
            continue
        bar = f"{day}T15:05:00" if t >= "15:05" else f"{day}T{t[:3]}{int(t[3:]) // 5 * 5:02d}:00"
        vix = float(vix5.get(bar, np.nan))
        if not np.isfinite(vix):
            continue
        q = pd.read_sql("SELECT strike, option_type, bid, ask, lot_size FROM quotes WHERE snapshot_id=?", L,
                        params=(int(snap.snapshot_id),))
        lot = int(q.lot_size.dropna().iloc[0])
        piv = q.pivot_table(index="strike", columns="option_type", values=["bid", "ask"])
        chain = [dict(strike=float(k), ce_bid=r[("bid", "CE")], ce_ask=r[("ask", "CE")],
                      pe_bid=r[("bid", "PE")], pe_ask=r[("ask", "PE")]) for k, r in piv.iterrows()]
        chain = [c for c in chain if all(np.isfinite(v) and v > 0 for k, v in c.items() if k != "strike")]
        cap = datetime.fromisoformat(snap.captured_at).replace(tzinfo=None)
        dte = (datetime.fromisoformat(nxt + "T15:30:00") - cap).total_seconds() / 86400
        settle = float(daily.at[nxt, IDX[und]])
        for name, (d, w) in VARIANTS.items():
            p = replace(CondorParams(), min_vix=0.0, short_delta=d, wing_sigma=w)
            try:
                plan = C.plan_condor(chain, float(snap.spot), vix, nxt, dte, lot, p, wing_mode="sigma")
            except C.NoTrade as e:
                rows.append(dict(eve=day, und=und, variant=name, note=str(e)))
                continue
            pd_ = plan.to_dict()
            res = C.settle(pd_, settle)
            cost = condor_charges_points(pd_)
            net = res["pnl_pts"] - cost
            rows.append(dict(eve=day, und=und, variant=name, entry=t, vix=vix, spot=float(snap.spot), settle=settle,
                             move_pct=100 * (settle / float(snap.spot) - 1),
                             strikes=" / ".join(f"{l.side[0]}{l.strike:g}{l.kind}" for l in plan.legs),
                             credit=plan.credit_real, spread_cost=plan.credit_mid - plan.credit_real,
                             max_loss=plan.max_loss_pts, net_pts=net, r=net / plan.max_loss_pts, rupees=net * lot, lot=lot))
    df = pd.DataFrame(rows)
    df.to_csv("live_replay_novix.csv", index=False)
    pd.set_option("display.width", 250)
    f = df[df.variant == FROZEN]
    print("FROZEN PICK", FROZEN, "on live recorded quotes:")
    print(f[["eve", "und", "entry", "vix", "move_pct", "strikes", "credit", "spread_cost", "max_loss", "net_pts", "r", "rupees"]]
          .to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    s = df.dropna(subset=["r"]).groupby("variant").agg(n=("r", "size"), win=("r", lambda x: (x > 0).mean()),
                                                       avg_r=("r", "mean"), rupees=("rupees", "sum"))
    print(s.sort_values("avg_r", ascending=False).to_string(float_format=lambda x: f"{x:,.3f}"))
    print(df[df.variant == "ALL"][["eve", "und", "note"]].to_string(index=False))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
