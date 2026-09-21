"""Phase 1 edge check: replay the rule-based spread engine over *real*
recorded option chains, before any execution code is written.

Data source: ``recorder.storage.ChainStorage`` reading ``data/option_chains.db``
(the legacy recorder — 216k NIFTY chain snapshots across ~357 trading days,
Sep 2024 - Sep 2026). This is the only underlying with any meaningful
recorded chain history on this machine; SENSEX has zero recorded chains, so
this replay cannot validate the SENSEX-first preference yet (see the module
docstring in the resulting report for that caveat, always stated up front).

Method, deliberately simple for a first-pass answer rather than a
production backtester:

1. Build a real intraday OHLC candle series for the underlying purely from
   the ``spot_price`` recorded alongside every chain snapshot (no separate
   OHLC history exists for this period), so ``analysis.technical`` has real
   trend/structure context, never synthetic data.
2. Once per trading day, at the first snapshot at/after 10:00 IST (skips
   the opening 45 minutes, per the charter's default caution), run the same
   candidate pipeline ``analysis.signal_engine.generate()`` uses
   (``_generate_relaxed_liquidity`` below) against the real recorded chain
   for whichever expiry falls in the configured DTE window, with one
   deliberate relaxation from the live gate: it does not require a two-sided
   (bid+ask) quote, because this dataset never has one — see point 4.
   Structures are restricted to debit verticals and long options (no iron
   condors: four legs of brokerage does not clear on a small account; no
   credit verticals: not implemented in this codebase yet).
3. If a candidate is accepted (passes every existing risk/liquidity gate)
   and no position is already open, treat it as entered at its priced
   ``net_debit``.
4. Walk forward one trading day at a time (same ~10:00 snapshot) pricing
   the position to market from the REAL recorded LTP on the same strikes.
   **This dataset has no bid/ask anywhere** — a direct scan of all 20.8M
   recorded strike rows found 100% NULL on every bid/ask column, only
   LTP/OI/volume were ever captured — so entry and exit both run at
   ``SIGNAL_PRICE_SOURCE="ltp"``, and every fill pays an explicit slippage
   cost (``backtest.costs.CostModel.apply_slippage``) rather than pretending
   an LTP is a crossable two-sided quote. Exit on target, stop, or DTE<=1
   time-exit.
5. Every entry and every exit pays real Indian F&O costs plus that
   slippage, computed leg-by-leg via ``backtest.costs.CostModel`` — never
   intrinsic value, never a hypothetical fill.

Run:
    python -m research.backtest.chain_replay
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from analysis.liquidity_filters import LiquidityLimits
from analysis.signal_engine import (
    build_market_context,
    build_option_context,
    chain_coverage,
    limits_from_settings,
    resolve_lot_size,
    truncate_to,
)
from analysis.signal_models import OptionChain, SpreadCandidate
from analysis.spread_selector import enumerate_candidates, rank
from analysis.setup_scoring import score_all
from backtest.costs import CostModel
from config.instruments import CORE_INSTRUMENTS
from config.settings import Settings
from recorder.storage import ChainStorage

# Structures actually implemented and defined-risk enough for a micro-account.
# No IRON_CONDOR (four legs of brokerage), no credit verticals (not built).
REPLAY_STRATEGIES = ("BULL_CALL_SPREAD", "BEAR_PUT_SPREAD", "LONG_CALL", "LONG_PUT")

ENTRY_CHECK_TIME = "10:00"  # IST, first snapshot at/after this each day
DTE_MIN = 3.0
DTE_MAX = 7.0
TARGET_R_MULTIPLE = 1.5  # profit target, in units of max_loss_after_costs
STOP_FRACTION_OF_DEBIT = 0.5  # stop when 50% of the debit paid is lost
TIME_EXIT_DTE = 1.0  # force-close at or below this many days to expiry


def _generate_relaxed_liquidity(
    underlying: str,
    chain_dict: dict[str, Any],
    candles: pd.DataFrame,
    settings: Settings,
    moment: datetime,
    strategies: tuple[str, ...],
    max_candidates: int = 1,
) -> tuple[list[SpreadCandidate], str | None]:
    """Runs the same pipeline as ``analysis.signal_engine.generate()``, with
    one deliberate difference: ``LiquidityLimits.require_two_sided=False``.

    ``generate()`` cannot be used as-is for this dataset: it hard-requires a
    bid AND an ask on every leg it will even consider, and this legacy
    recorder captured neither (see the module docstring). Volume and open
    interest floors — the parts of the liquidity gate this data CAN still
    check — are left exactly as configured. Everything else (risk budget,
    cost-drag, R:R, DTE window, coverage) is unchanged from ``generate()``.

    Returns live ``SpreadCandidate`` objects (not the ``.as_dict()`` the
    real report uses), ranked best first, plus a reason string when nothing
    could even be evaluated (no reason means "evaluated, nothing survived").
    """
    mode = "ltp"
    budget = settings.risk_amount_per_trade
    risk_limits, _default_liquidity = limits_from_settings(settings, budget)
    liquidity_limits = LiquidityLimits(
        max_spread_pct=settings.SIGNAL_MAX_BID_ASK_SPREAD_PCT,
        min_volume=settings.SIGNAL_MIN_LEG_VOLUME,
        min_open_interest=settings.SIGNAL_MIN_LEG_OPEN_INTEREST,
        require_two_sided=False,
    )
    model = CostModel()

    option_context = build_option_context(
        chain_dict, source="recorded", rate=settings.RISK_FREE_RATE, price_mode=mode, moment=moment
    )
    if option_context.spot is None:
        return [], "no spot price on the recorded chain"

    _recorded, _expected, ratio = chain_coverage(chain_dict)
    if ratio is None or ratio < settings.SIGNAL_MIN_CHAIN_COVERAGE:
        return [], f"chain coverage {ratio} below {settings.SIGNAL_MIN_CHAIN_COVERAGE}"

    lot_size = resolve_lot_size(chain_dict)
    if lot_size is None:
        return [], "no lot size on the recorded chain"

    frame = truncate_to(candles, moment)
    market_context = build_market_context(frame, symbol=underlying, interval=None, spot=option_context.spot)

    candidates = enumerate_candidates(
        chain_dict,
        risk_limits,
        strategies=strategies,
        price_mode=mode,
        lots=1,
        max_width_steps=settings.SIGNAL_MAX_WIDTH_STEPS,
        cost_model=model,
        liquidity_limits=liquidity_limits,
        context=market_context,
        moment=moment,
    )
    score_all(candidates, market_context, option_context, None)
    survivors = rank(candidates)
    return survivors[:max_candidates], None


@dataclass
class ReplayTrade:
    entry_date: date
    strategy: str
    direction: str
    expiry: str
    entry_dte: float | None
    quantity: int
    net_debit_per_unit: float
    max_loss_after_costs: float
    entry_score: float | None
    exit_date: date | None = None
    exit_reason: str | None = None
    exit_value_per_unit: float | None = None
    round_trip_costs: float | None = None
    net_pnl: float | None = None
    legs: list[dict[str, Any]] = field(default_factory=list)


def _lot_size(underlying: str) -> int:
    spec = CORE_INSTRUMENTS.get(underlying.upper())
    if spec is None:
        raise ValueError(f"No instrument spec for {underlying}; cannot size a replay trade")
    return int(spec.lot_size)


def _replay_settings(capital: float, risk_pct: float, max_moneyness_pct: float | None = None) -> Settings:
    """Isolated settings for the replay: real .env never consulted."""
    return Settings(
        ANGEL_API_KEY="k",
        ANGEL_CLIENT_ID="c",
        ANGEL_PASSWORD="p",
        ANGEL_TOTP_SECRET="JBSWY3DPEHPK3PXP",
        _env_file=None,
        TOTAL_CAPITAL=capital,
        RISK_PER_TRADE_PCT=risk_pct,
        MAX_DAILY_LOSS_PCT=min(risk_pct * 1.2, 20.0),
        MAX_WEEKLY_LOSS_PCT=min(risk_pct * 2.0, 30.0),
        MAX_OPEN_POSITIONS=1,
        SIGNAL_MIN_DAYS_TO_EXPIRY=DTE_MIN,
        SIGNAL_MAX_DAYS_TO_EXPIRY=DTE_MAX,
        # RISK_TOO_FAR_FROM_MONEY (analysis/risk_filters.py): the shipped
        # gate this finding motivated. None reproduces the unmodified
        # engine exactly.
        SIGNAL_MAX_MONEYNESS_PCT=max_moneyness_pct,
        # No bid/ask anywhere in this legacy dataset (see chain_replay.py's
        # module docstring) — LTP is the only price available, and this mode
        # is what makes spread_pricer add slippage as an explicit cost
        # rather than pricing a two-sided quote that does not exist here.
        SIGNAL_PRICE_SOURCE="ltp",
        # The default 0.85 whole-chain coverage gate is calibrated for a
        # live FULL-mode quote batch, where a low ratio means a dropped
        # network call. Here it measures something different and expected:
        # ~30% of legs, concentrated in deep OTM/illiquid strikes, simply
        # never traded that minute so LTP is null. Lowered so a replay isn't
        # blocked chain-wide by strikes far from anything a narrow vertical
        # would ever use; the liquidity/OI gates on the legs actually
        # selected are unchanged and still apply per candidate.
        SIGNAL_MIN_CHAIN_COVERAGE=0.5,
    )


def build_spot_candles(db_path: str, symbol: str, freq: str = "15min") -> pd.DataFrame:
    """Real intraday OHLC built from every recorded ``spot_price`` tick.

    Multiple chain rows can share one timestamp (one per expiry recorded at
    that instant); the spot price is identical across them, so a plain
    ``MIN`` collapses duplicates without averaging away any real variation.
    """
    con = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(
            "SELECT timestamp, MIN(spot_price) AS spot "
            "FROM recorded_chains WHERE symbol = ? GROUP BY timestamp ORDER BY timestamp",
            con,
            params=(symbol,),
        )
    finally:
        con.close()
    if df.empty:
        return df
    # Mixed format: most rows are "YYYY-MM-DD HH:MM:SS", a minority carry
    # fractional seconds ("...HH:MM:SS.ffffff").
    idx = pd.to_datetime(df["timestamp"], format="mixed").dt.tz_localize("Asia/Kolkata")
    series = pd.Series(df["spot"].to_numpy(), index=idx, name="close")
    ohlc = series.resample(freq).ohlc().dropna(how="all")
    ohlc["volume"] = 0  # not recorded; technical indicators here don't need it
    ohlc.index.name = "timestamp"
    return ohlc.dropna(subset=["close"])


def _trade_dates(db_path: str, symbol: str) -> list[date]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT DISTINCT DATE(timestamp) FROM recorded_chains WHERE symbol = ? ORDER BY 1",
            (symbol,),
        ).fetchall()
    finally:
        con.close()
    return [datetime.strptime(r[0], "%Y-%m-%d").date() for r in rows]


def _chain_dict(chain: OptionChain, lot_size: int, exchange: str) -> dict[str, Any]:
    """Convert a recorded ``OptionChain`` into the dict shape
    ``analysis.signal_engine.generate()`` expects (the same shape
    ``core.data_fetcher.fetch_option_chain`` produces live).
    """
    strikes: list[dict[str, Any]] = []
    for s in chain.strikes:
        row: dict[str, Any] = {"strike": float(s.strike), "call": None, "put": None}
        for side, prefix in (("call", "call_"), ("put", "put_")):
            ltp = getattr(s, f"{prefix}ltp")
            if ltp is None:
                continue
            # This legacy recorder (recorder.storage.ChainStorage) never
            # captured bid/ask depth, only LTP/OI/volume, confirmed by a
            # direct scan of the whole 20.8M-row recorded_strikes table
            # (100% NULL on every bid/ask column). bid/ask stay None, and
            # signal_engine/spread_pricer must run with SIGNAL_PRICE_SOURCE
            # ="ltp" against this chain, which is exactly the mode they
            # already support for a one-sided book — including adding
            # slippage as an explicit cost, since an LTP is not a two-sided
            # quote and nothing here should look tighter than it really is.
            row[side] = {
                "symbol": None,
                "token": None,
                "ltp": ltp,
                "bid": None,
                "ask": None,
                # Depth (bid_qty/ask_qty) was not captured by this legacy
                # recorder. Left None rather than invented; liquidity gates
                # that need size fall back to volume/OI, which were captured.
                "bid_qty": None,
                "ask_qty": None,
                "volume": getattr(s, f"{prefix}volume"),
                "open_interest": getattr(s, f"{prefix}oi"),
                "previous_close": None,
                "change_pct": None,
                "exchange_timestamp": None,
                "lot_size": lot_size,
            }
        strikes.append(row)
    expiry_str = chain.expiry.strftime("%d%b%Y").upper()
    atm = min((s.strike for s in chain.strikes), key=lambda k: abs(k - chain.spot_price), default=None)
    return {
        "underlying": chain.symbol,
        "expiry": expiry_str,
        "exchange": exchange,
        "spot": chain.spot_price,
        "atm_strike": atm,
        "strike_count": len(strikes),
        "strikes": strikes,
        "totals": {},
        "note": "Legacy recorded chain (recorder.storage.ChainStorage); no bid/ask depth captured.",
        "fetched_at": chain.timestamp.isoformat(),
    }


def _leg_quote(chain_dict: dict[str, Any], strike: float, option_type: Literal["CE", "PE"]) -> dict[str, Any] | None:
    side = "call" if option_type == "CE" else "put"
    for row in chain_dict["strikes"]:
        if abs(row["strike"] - strike) < 1e-6:
            return row.get(side)
    return None


def _mark_to_market(legs: list[dict[str, Any]], chain_dict: dict[str, Any]) -> float | None:
    """Value of closing the position right now, per unit, at LTP.

    No bid/ask exists in this data (see the note in ``_chain_dict``), so
    this cannot be role-aware the way a real order book would be. It is raw
    LTP for every leg regardless of role; the cost of not being able to
    cross a real spread is charged separately as slippage in
    ``_round_trip_cost_with_slippage``, never silently absorbed into a
    friendlier fill price here.
    """
    total = 0.0
    for leg in legs:
        quote = _leg_quote(chain_dict, leg["strike"], leg["type"])
        if quote is None or quote["ltp"] is None:
            return None
        price = quote["ltp"]
        total += price if leg["role"] == "LONG" else -price
    return total


def _round_trip_cost_with_slippage(
    cost_model: CostModel, entry_price: float, exit_price: float, quantity: int, direction: Literal["LONG", "SHORT"]
) -> float:
    """Round-trip cost including slippage, mirroring
    ``analysis.spread_pricer.estimate_costs`` exactly (mode in
    {"mid","ltp","bid","ask"} always prices slippage) so the entry-time
    candidate score and this replay's realised P&L use the same assumption
    about what an unquoted book actually costs to cross.
    """
    entry_side: Literal["BUY", "SELL"] = "BUY" if direction == "LONG" else "SELL"
    exit_side: Literal["BUY", "SELL"] = "SELL" if direction == "LONG" else "BUY"
    total = 0.0
    for side, price in ((entry_side, entry_price), (exit_side, exit_price)):
        adjusted = cost_model.apply_slippage(price, side, "OPTION")
        slippage_amount = abs(adjusted - price) * quantity
        total += cost_model.calculate("OPTION", side, price, quantity, slippage_amount=slippage_amount).total
    return total


def _entry_legs(candidate: SpreadCandidate) -> list[dict[str, Any]]:
    """Normalise a live ``SpreadCandidate``'s legs into the plain-dict shape
    the rest of this module (``_mark_to_market``, ``_leg_quote``, the cost
    loop) works with.
    """
    raw_legs = [candidate.long_leg]
    if candidate.short_leg is not None:
        raw_legs.append(candidate.short_leg)
    return [
        {
            "role": leg.role,
            "strike": leg.strike,
            "type": leg.option_type,
            "price": leg.price_used,
            "_exit_price": None,
        }
        for leg in raw_legs
    ]


def run_replay(
    underlying: str = "NIFTY",
    db_path: str | None = None,
    capital: float = 20_000.0,
    risk_pct: float = 10.0,
    start: date | None = None,
    end: date | None = None,
    max_moneyness_pct: float | None = None,
) -> dict[str, Any]:
    """Run the day-by-day replay and return trades plus summary metrics.

    Args:
        max_moneyness_pct: When set, only consider a candidate whose long
            (bought) leg sits within this fraction of spot — e.g. ``0.02``
            keeps the near leg within 2%. Passed straight through to
            ``Settings.SIGNAL_MAX_MONEYNESS_PCT``, which drives the real,
            shipped ``RISK_TOO_FAR_FROM_MONEY`` gate
            (``analysis/risk_filters.py``) — this replay exercises the
            production code path, not a stand-in for it. ``None`` (the
            default) reproduces exactly what the shipped engine would
            select today, with no moneyness constraint.
    """
    project_root = Path(__file__).resolve().parent.parent.parent
    db_path = db_path or str(project_root / "data" / "option_chains.db")
    lot_size = _lot_size(underlying)
    exchange = CORE_INSTRUMENTS[underlying.upper()].fo_exchange
    settings = _replay_settings(capital, risk_pct, max_moneyness_pct)
    cost_model = CostModel()
    store = ChainStorage(db_path)

    candles = build_spot_candles(db_path, underlying)
    if candles.empty:
        return {"error": f"No recorded spot data for {underlying} in {db_path}"}

    dates = _trade_dates(db_path, underlying)
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]

    trades: list[ReplayTrade] = []
    open_trade: ReplayTrade | None = None
    open_legs: list[dict[str, Any]] | None = None
    skipped_days_no_expiry = 0

    for day in dates:
        day_chains = store.get_chains_on_date(underlying, day)
        if not day_chains:
            continue

        # -- Manage an open position first -----------------------------
        if open_trade is not None:
            # Nearest snapshot at/after the check time for the SAME expiry.
            candidates_today = sorted(
                (c for c in day_chains if c.expiry.strftime("%d%b%Y").upper() == open_trade.expiry),
                key=lambda c: c.timestamp,
            )
            snap = next(
                (c for c in candidates_today if c.timestamp.strftime("%H:%M") >= ENTRY_CHECK_TIME),
                candidates_today[0] if candidates_today else None,
            )
            dte = (open_trade.expiry and (
                datetime.strptime(open_trade.expiry, "%d%b%Y").date() - day
            ).days)

            # Fresh mark for today, and the per-leg prices that produced it
            # (needed for the round-trip cost calc regardless of whether we
            # exit today). Both come from the SAME snapshot, so an exit's
            # cost and its P&L are always computed from one consistent quote.
            exit_value = None
            if snap is not None:
                chain_dict = _chain_dict(snap, lot_size, exchange)
                exit_value = _mark_to_market(open_legs, chain_dict)
                if exit_value is not None:
                    for leg in open_legs:
                        quote = _leg_quote(chain_dict, leg["strike"], leg["type"])
                        if quote:
                            leg["_exit_price"] = quote["ltp"]
                    open_trade.exit_value_per_unit = exit_value

            exit_reason = None
            if exit_value is not None:
                pnl_gross = (exit_value - open_trade.net_debit_per_unit) * open_trade.quantity
                if pnl_gross >= TARGET_R_MULTIPLE * open_trade.max_loss_after_costs:
                    exit_reason = "TARGET"
                elif pnl_gross <= -STOP_FRACTION_OF_DEBIT * (
                    open_trade.net_debit_per_unit * open_trade.quantity
                ):
                    exit_reason = "STOP"
            if exit_reason is None and dte is not None and dte <= TIME_EXIT_DTE:
                exit_reason = "TIME_DTE"
            if exit_reason is None and dte is not None and dte < 0:
                exit_reason = "EXPIRED_UNMARKED"  # ran off the end of the expiry with no clean mark

            if exit_reason is not None:
                if exit_value is None:
                    # Forced exit with no fresh quote today: use the last
                    # value we could mark (and the leg prices that produced
                    # it, already stored on the legs), never invent a price.
                    exit_value = open_trade.exit_value_per_unit
                total_costs = 0.0
                for leg in open_legs:
                    exit_price = leg.get("_exit_price")
                    if exit_price is None:
                        continue
                    direction: Literal["LONG", "SHORT"] = "LONG" if leg["role"] == "LONG" else "SHORT"
                    total_costs += _round_trip_cost_with_slippage(
                        cost_model, leg["price"], exit_price, open_trade.quantity, direction
                    )
                open_trade.exit_date = day
                open_trade.exit_reason = exit_reason
                open_trade.exit_value_per_unit = exit_value
                open_trade.round_trip_costs = total_costs
                if exit_value is not None:
                    gross = (exit_value - open_trade.net_debit_per_unit) * open_trade.quantity
                    open_trade.net_pnl = gross - total_costs
                trades.append(open_trade)
                open_trade = None
                open_legs = None
            continue  # one position at a time; no new entry while one is open

        # -- No open position: look for an entry ------------------------
        eligible = [
            c
            for c in day_chains
            if DTE_MIN <= (c.expiry - day).days <= DTE_MAX
        ]
        if not eligible:
            skipped_days_no_expiry += 1
            continue
        eligible.sort(key=lambda c: (c.timestamp.strftime("%H:%M") < ENTRY_CHECK_TIME, c.timestamp))
        snap = next((c for c in eligible if c.timestamp.strftime("%H:%M") >= ENTRY_CHECK_TIME), eligible[0])

        chain_dict = _chain_dict(snap, lot_size, exchange)
        moment = snap.timestamp
        # RISK_TOO_FAR_FROM_MONEY (via settings.SIGNAL_MAX_MONEYNESS_PCT,
        # when set) already filters within enumerate_candidates/
        # check_candidate, so the top-ranked survivor is already
        # moneyness-compliant; no post-filter needed here.
        accepted, _reason = _generate_relaxed_liquidity(
            underlying, chain_dict, candles, settings, moment, REPLAY_STRATEGIES, max_candidates=1
        )
        if not accepted:
            continue
        top = accepted[0]
        if top.net_debit is None:
            continue  # a credit/unbounded structure slipped through; skip

        legs = _entry_legs(top)
        quantity = top.quantity
        net_debit_per_unit = top.net_debit / quantity if quantity else top.net_debit

        open_trade = ReplayTrade(
            entry_date=day,
            strategy=top.strategy,
            direction=top.direction,
            expiry=top.expiry,
            entry_dte=top.days_to_expiry,
            quantity=quantity,
            net_debit_per_unit=net_debit_per_unit,
            max_loss_after_costs=float(top.max_loss_after_costs or 0.0),
            entry_score=top.score,
        )
        open_legs = legs

    result = _summarise(underlying, capital, risk_pct, trades, skipped_days_no_expiry, len(dates))
    result["max_moneyness_pct"] = max_moneyness_pct
    return result


def _summarise(
    underlying: str,
    capital: float,
    risk_pct: float,
    trades: list[ReplayTrade],
    skipped_days_no_expiry: int,
    total_days: int,
) -> dict[str, Any]:
    closed = [t for t in trades if t.net_pnl is not None]
    wins = [t for t in closed if t.net_pnl > 0]
    losses = [t for t in closed if t.net_pnl <= 0]
    gross_profit = sum(t.net_pnl for t in wins)
    gross_loss = -sum(t.net_pnl for t in losses)
    total_costs = sum(t.round_trip_costs or 0.0 for t in closed)
    net_pnl = sum(t.net_pnl for t in closed)

    equity = capital
    peak = capital
    max_dd = 0.0
    max_dd_pct = 0.0
    for t in closed:
        equity += t.net_pnl
        peak = max(peak, equity)
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = (dd / peak * 100.0) if peak else 0.0

    return {
        "underlying": underlying,
        "capital": capital,
        "risk_pct": risk_pct,
        "total_trading_days_in_data": total_days,
        "days_with_no_eligible_expiry": skipped_days_no_expiry,
        "trades_closed": len(closed),
        "trades_open_at_end": len(trades) - len(closed),
        "win_rate_pct": round(100.0 * len(wins) / len(closed), 1) if closed else None,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "total_round_trip_costs": round(total_costs, 2),
        "net_pnl": round(net_pnl, 2),
        "avg_net_pnl_per_trade": round(net_pnl / len(closed), 2) if closed else None,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "max_drawdown_rupees": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "cost_drag_pct_of_gross_profit": (
            round(100.0 * total_costs / gross_profit, 1) if gross_profit > 0 else None
        ),
        "trades": [
            {
                "entry_date": t.entry_date.isoformat(),
                "exit_date": t.exit_date.isoformat() if t.exit_date else None,
                "strategy": t.strategy,
                "direction": t.direction,
                "expiry": t.expiry,
                "exit_reason": t.exit_reason,
                "quantity": t.quantity,
                "net_debit_per_unit": round(t.net_debit_per_unit, 2),
                "exit_value_per_unit": (
                    round(t.exit_value_per_unit, 2) if t.exit_value_per_unit is not None else None
                ),
                "round_trip_costs": round(t.round_trip_costs, 2) if t.round_trip_costs is not None else None,
                "net_pnl": round(t.net_pnl, 2) if t.net_pnl is not None else None,
                "entry_score": t.entry_score,
            }
            for t in trades
        ],
    }


if __name__ == "__main__":
    import json
    import sys

    result = run_replay()
    print(json.dumps({k: v for k, v in result.items() if k != "trades"}, indent=2, default=str))
    out_dir = Path(__file__).resolve().parent.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "chain_replay_NIFTY.json"
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nFull trade log written to {out_path}", file=sys.stderr)
