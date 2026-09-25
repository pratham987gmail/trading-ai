"""Credit-spread (30-45 DTE) replay on the live recorder's real bid/ask data.

The Iron Condor / credit-spread idea was never actually tested in this
project: the legacy ``option_chains.db`` data only reached 7-10 DTE and had
no bid/ask, so the chain_replay attempt tested a different, shorter trade.
The live recorder (``scripts/record_chains.py`` -> ``data/options.db``) now
captures 7 weekly expiries deep, every 5 minutes, with real bid/ask. This
study replays the textbook structure on that data and is meant to be re-run
weekly as the sample grows. Until it has ``MIN_CLOSED_TRADES`` closed trades
its verdict is ``INSUFFICIENT_SAMPLE``, whatever the numbers look like.

Rules (one put spread and one call spread per expiry, per underlying):
    Entry   first snapshot at/after 09:45 IST with DTE in [30, 45] whose
            quotes are usable. Short strike ~1 standard deviation OTM, where
            1 SD = ATM straddle mid / 0.8 (the standard straddle-to-move
            approximation; no IV or greeks are recorded, so this uses only
            real quoted prices). Long wing ``width_steps`` strikes further.
    Fills   sell at the bid, buy at the ask, on entry and exit: real
            crossing prices, so no extra slippage is added on top.
    Exits   50% of credit captured, loss of 2x credit, or DTE <= 21,
            whichever comes first, checked at every recorded snapshot.
    Costs   ``backtest.costs.CostModel`` per leg per side (4 orders round trip).

Known limitations, reported in the output rather than hidden:
    * Recording gaps (sleep / network drops) mean exits are checked late
      when snapshots are missing; the largest gap per trade is reported.
    * Strikes outside the recorded range cannot be traded; such entries are
      counted as skipped, not silently moved closer to the money.
    * Days recorded before 2026-09-25 only captured +-20 strikes, so many
      early far-dated entries will be skipped for that reason.

Run:
    python -m research.backtest.credit_spread_live_study
    python -m research.backtest.credit_spread_live_study --width-steps 2
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backtest.costs import CostModel  # noqa: E402
from config.settings import get_settings  # noqa: E402

ENTRY_DTE_MIN = 30
ENTRY_DTE_MAX = 45
ENTRY_NOT_BEFORE = "09:45"
TIME_EXIT_DTE = 21
TAKE_PROFIT_FRACTION = 0.5
STOP_LOSS_MULTIPLE = 2.0
STRADDLE_TO_SD = 0.8
MIN_CLOSED_TRADES = 30
GATE_MIN_PROFIT_FACTOR = 1.3

Side = Literal["PUT", "CALL"]


@dataclass
class Trade:
    underlying: str
    expiry: str
    side: Side
    entry_time: str
    entry_dte: int
    spot: float
    one_sd_move: float
    short_strike: float
    long_strike: float
    width: float
    quantity: int
    credit: float
    max_loss_rupees: float
    fits_budget: bool
    exit_time: str | None = None
    exit_reason: str | None = None
    close_cost: float | None = None
    gross_pnl: float | None = None
    costs: float | None = None
    net_pnl: float | None = None
    max_gap_minutes: float = 0.0
    status: str = "OPEN"


@dataclass
class Snapshot:
    snapshot_id: int
    captured_at: datetime
    dte: int
    spot: float
    atm_strike: float
    quotes: dict[tuple[float, str], tuple[float | None, float | None, int]] = field(default_factory=dict)


def _load(con: sqlite3.Connection, underlying: str) -> dict[str, list[Snapshot]]:
    rows = con.execute(
        "SELECT snapshot_id, captured_at, trade_date, expiry, expiry_date, spot, atm_strike "
        "FROM snapshots WHERE underlying = ? AND spot IS NOT NULL ORDER BY captured_at",
        (underlying,),
    ).fetchall()
    by_expiry: dict[str, list[Snapshot]] = {}
    by_id: dict[int, Snapshot] = {}
    for sid, captured, trade_date, expiry, expiry_date, spot, atm in rows:
        dte = (date.fromisoformat(expiry_date) - date.fromisoformat(trade_date)).days
        snap = Snapshot(sid, datetime.fromisoformat(captured), dte, float(spot), float(atm or 0))
        by_expiry.setdefault(expiry, []).append(snap)
        by_id[sid] = snap
    if not by_id:
        return by_expiry
    placeholders = ",".join("?" * len(by_id))
    for sid, strike, opt, bid, ask, lot in con.execute(
        f"SELECT snapshot_id, strike, option_type, bid, ask, lot_size FROM quotes WHERE snapshot_id IN ({placeholders})",
        list(by_id),
    ):
        by_id[sid].quotes[(float(strike), opt)] = (
            float(bid) if bid and bid > 0 else None,
            float(ask) if ask and ask > 0 else None,
            int(lot or 0),
        )
    return by_expiry


def _strike_step(snap: Snapshot) -> float | None:
    strikes = sorted({k[0] for k in snap.quotes})
    diffs = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    return min(diffs) if diffs else None


def _one_sd_move(snap: Snapshot) -> float | None:
    ce = snap.quotes.get((snap.atm_strike, "CE"))
    pe = snap.quotes.get((snap.atm_strike, "PE"))
    if not ce or not pe or None in (ce[0], ce[1], pe[0], pe[1]):
        return None
    return ((ce[0] + ce[1]) / 2 + (pe[0] + pe[1]) / 2) / STRADDLE_TO_SD


def _try_entry(
    snap: Snapshot, underlying: str, expiry: str, side: Side, width_steps: int, budget: float
) -> tuple[Trade | None, str | None]:
    """Return (trade, None) on entry, or (None, skip_reason)."""
    sd = _one_sd_move(snap)
    step = _strike_step(snap)
    if sd is None or step is None:
        return None, "NO_ATM_QUOTES"
    opt = "PE" if side == "PUT" else "CE"
    strikes = sorted({k[0] for k in snap.quotes if k[1] == opt})
    if side == "PUT":
        candidates = [s for s in strikes if s <= snap.spot - sd]
        if not candidates:
            return None, "STRIKE_OUT_OF_RECORDED_RANGE"
        short = max(candidates)
        long_ = short - width_steps * step
    else:
        candidates = [s for s in strikes if s >= snap.spot + sd]
        if not candidates:
            return None, "STRIKE_OUT_OF_RECORDED_RANGE"
        short = min(candidates)
        long_ = short + width_steps * step
    sq, lq = snap.quotes.get((short, opt)), snap.quotes.get((long_, opt))
    if lq is None:
        return None, "STRIKE_OUT_OF_RECORDED_RANGE"
    if sq is None or sq[0] is None or lq[1] is None:
        return None, "NO_BID_ASK"
    credit = sq[0] - lq[1]
    if credit <= 0:
        return None, "NO_CREDIT_AT_BID_ASK"
    qty = sq[2] or lq[2]
    width = abs(short - long_)
    max_loss = (width - credit) * qty
    return (
        Trade(
            underlying=underlying,
            expiry=expiry,
            side=side,
            entry_time=snap.captured_at.isoformat(timespec="minutes"),
            entry_dte=snap.dte,
            spot=snap.spot,
            one_sd_move=round(sd, 2),
            short_strike=short,
            long_strike=long_,
            width=width,
            quantity=qty,
            credit=round(credit, 2),
            max_loss_rupees=round(max_loss, 2),
            fits_budget=max_loss <= budget,
        ),
        None,
    )


def _close_cost(snap: Snapshot, trade: Trade) -> float | None:
    opt = "PE" if trade.side == "PUT" else "CE"
    sq, lq = snap.quotes.get((trade.short_strike, opt)), snap.quotes.get((trade.long_strike, opt))
    if not sq or not lq or sq[1] is None or lq[0] is None:
        return None
    return sq[1] - lq[0]


def replay(con: sqlite3.Connection, underlying: str, width_steps: int, budget: float, model: CostModel):
    trades: list[Trade] = []
    skips: Counter[str] = Counter()
    for expiry, snaps in _load(con, underlying).items():
        for side in ("PUT", "CALL"):
            trade: Trade | None = None
            entry_legs: tuple[float, float] | None = None
            last_time: datetime | None = None
            last_reason: str | None = None
            for snap in snaps:
                if trade is None:
                    if not (ENTRY_DTE_MIN <= snap.dte <= ENTRY_DTE_MAX):
                        continue
                    if snap.captured_at.strftime("%H:%M") < ENTRY_NOT_BEFORE:
                        continue
                    trade, last_reason = _try_entry(snap, underlying, expiry, side, width_steps, budget)
                    if trade is not None:
                        opt = "PE" if side == "PUT" else "CE"
                        entry_legs = (
                            snap.quotes[(trade.short_strike, opt)][0],
                            snap.quotes[(trade.long_strike, opt)][1],
                        )
                        last_time = snap.captured_at
                    continue

                if last_time is not None:
                    gap = (snap.captured_at - last_time).total_seconds() / 60
                    if snap.captured_at.date() == last_time.date():
                        trade.max_gap_minutes = max(trade.max_gap_minutes, round(gap, 1))
                last_time = snap.captured_at

                cost = _close_cost(snap, trade)
                if cost is None:
                    continue
                reason = None
                if cost <= trade.credit * (1 - TAKE_PROFIT_FRACTION):
                    reason = "TAKE_PROFIT_50PCT"
                elif cost >= trade.credit * (1 + STOP_LOSS_MULTIPLE):
                    reason = "STOP_2X_CREDIT"
                elif snap.dte <= TIME_EXIT_DTE:
                    reason = "TIME_EXIT_21DTE"
                if reason:
                    opt = "PE" if side == "PUT" else "CE"
                    exit_short_ask = snap.quotes[(trade.short_strike, opt)][1]
                    exit_long_bid = snap.quotes[(trade.long_strike, opt)][0]
                    q = trade.quantity
                    costs = sum(
                        model.calculate("OPTION", s, p, q).total
                        for s, p in (
                            ("SELL", entry_legs[0]),
                            ("BUY", entry_legs[1]),
                            ("BUY", exit_short_ask),
                            ("SELL", exit_long_bid),
                        )
                    )
                    trade.close_cost = round(cost, 2)
                    trade.exit_time = snap.captured_at.isoformat(timespec="minutes")
                    trade.exit_reason = reason
                    trade.gross_pnl = round((trade.credit - cost) * q, 2)
                    trade.costs = round(costs, 2)
                    trade.net_pnl = round(trade.gross_pnl - costs, 2)
                    trade.status = "CLOSED"
                    break

            if trade is None:
                if last_reason:
                    skips[last_reason] += 1
                continue
            if trade.status == "OPEN":
                mark = next((c for s in reversed(snaps) if (c := _close_cost(s, trade)) is not None), None)
                if mark is not None:
                    trade.close_cost = round(mark, 2)
                    trade.gross_pnl = round((trade.credit - mark) * trade.quantity, 2)
            trades.append(trade)
    return trades, skips


def _summary(closed: list[Trade]) -> dict[str, Any]:
    if not closed:
        return {"closed_trades": 0}
    pnl = [t.net_pnl for t in closed]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    gross_loss = -sum(losses)
    return {
        "closed_trades": len(closed),
        "win_rate_pct": round(100 * len(wins) / len(closed), 1),
        "net_pnl": round(sum(pnl), 2),
        "expectancy_per_trade": round(sum(pnl) / len(closed), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "worst_trade": round(min(pnl), 2),
        "profit_factor": round(sum(wins) / gross_loss, 2) if gross_loss > 0 else None,
        "total_costs": round(sum(t.costs for t in closed), 2),
        "exit_reasons": dict(Counter(t.exit_reason for t in closed)),
    }


def _verdict(s: dict[str, Any]) -> str:
    n = s.get("closed_trades", 0)
    if n < MIN_CLOSED_TRADES:
        return f"INSUFFICIENT_SAMPLE ({n}/{MIN_CLOSED_TRADES} closed trades)"
    pf = s.get("profit_factor")
    if s["expectancy_per_trade"] > 0 and pf is not None and pf >= GATE_MIN_PROFIT_FACTOR:
        return "PASS"
    return "FAIL"


def run(db_path: Path, width_steps: int) -> dict[str, Any]:
    settings = get_settings()
    budget = settings.TOTAL_CAPITAL * settings.RISK_PER_TRADE_PCT / 100
    model = CostModel()
    con = sqlite3.connect(db_path)
    try:
        all_trades: list[Trade] = []
        skips: Counter[str] = Counter()
        coverage = {}
        for u in ("NIFTY", "SENSEX"):
            t, s = replay(con, u, width_steps, budget, model)
            all_trades += t
            skips += s
            coverage[u] = con.execute(
                "SELECT COUNT(DISTINCT trade_date), COUNT(DISTINCT cycle_id) FROM snapshots WHERE underlying = ?", (u,)
            ).fetchone()
    finally:
        con.close()

    closed = [t for t in all_trades if t.status == "CLOSED"]
    overall = _summary(closed)
    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "width_steps": width_steps,
        "per_trade_budget_rupees": budget,
        "data_coverage": {u: {"trading_days": d, "capture_cycles": c} for u, (d, c) in coverage.items()},
        "verdict": _verdict(overall),
        "overall": overall,
        "budget_fitting_only": _summary([t for t in closed if t.fits_budget]),
        "by_underlying_side": {
            f"{u}_{sd}": _summary([t for t in closed if t.underlying == u and t.side == sd])
            for u in ("NIFTY", "SENSEX")
            for sd in ("PUT", "CALL")
        },
        "open_positions": [asdict(t) for t in all_trades if t.status == "OPEN"],
        "skipped_entries": dict(skips),
        "trades": [asdict(t) for t in closed],
    }
    return result


def _markdown(results: list[dict[str, Any]]) -> str:
    lines = [f"# Credit-spread live replay -- {results[0]['generated_at'][:10]}", ""]
    cov = results[0]["data_coverage"]
    lines.append(
        "Data: "
        + ", ".join(f"{u} {v['trading_days']} days / {v['capture_cycles']} cycles" for u, v in cov.items())
    )
    lines.append("")
    for r in results:
        o = r["overall"]
        lines.append(f"## Width {r['width_steps']} strike step(s): **{r['verdict']}**")
        if o.get("closed_trades"):
            lines.append(
                f"- Closed {o['closed_trades']}, win rate {o['win_rate_pct']}%, net Rs {o['net_pnl']}, "
                f"expectancy Rs {o['expectancy_per_trade']}/trade, profit factor {o['profit_factor']}, "
                f"worst Rs {o['worst_trade']}, costs Rs {o['total_costs']}"
            )
            lines.append(f"- Exits: {o['exit_reasons']}")
        else:
            lines.append("- No closed trades yet.")
        b = r["budget_fitting_only"]
        lines.append(
            f"- Within Rs {r['per_trade_budget_rupees']:.0f} per-trade budget: {b.get('closed_trades', 0)} closed"
            + (f", net Rs {b['net_pnl']}, PF {b['profit_factor']}" if b.get("closed_trades") else "")
        )
        lines.append(f"- Open positions: {len(r['open_positions'])}; skipped entries: {r['skipped_entries']}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "options.db"))
    parser.add_argument("--width-steps", type=int, nargs="+", default=[1, 2])
    args = parser.parse_args(argv)

    results = [run(Path(args.db), w) for w in args.width_steps]

    out_dir = PROJECT_ROOT / "research" / "reports" / "credit_spread_live"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    (out_dir / f"{stamp}.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    md = _markdown(results)
    (out_dir / f"{stamp}.md").write_text(md, encoding="utf-8")
    (out_dir / "latest.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
