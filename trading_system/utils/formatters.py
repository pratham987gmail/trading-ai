"""Human and AI readable text rendering of cache snapshots.

The ``/text`` endpoint exists so a snapshot can be pasted into an analysis
session without the reader having to parse JSON. Two rules govern this module.

**Missing data renders as an explicit marker, never as a plausible number.** A
absent price becomes ``no data``, never ``0.00``. Formatted output is exactly
where a fabricated zero would be most convincing and most dangerous, so
:func:`fmt_price` and friends return :data:`NO_DATA` for ``None``.

**Staleness is stated at the top.** A reader who skims past a warning buried at
the bottom may act on minutes-old prices, so the freshness banner is placed
before any price.

Indian digit grouping (``1,00,000`` rather than ``100,000``) is implemented in
:func:`fmt_inr` because that is how the figures will be cross-checked against a
broker terminal.

Example:
    >>> fmt_inr(100000)
    '1,00,000.00'
    >>> fmt_price(None)
    'no data'
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

NO_DATA = "no data"
WIDTH = 66


def fmt_indian_digits(value: float, decimals: int = 2) -> str:
    """Group a number using the Indian lakh/crore convention.

    Args:
        value: Number to format.
        decimals: Decimal places.

    Returns:
        Grouped string, e.g. ``"1,00,000.00"``.

    Example:
        >>> fmt_indian_digits(12345678.5)
        '1,23,45,678.50'
        >>> fmt_indian_digits(-4500)
        '-4,500.00'
    """
    negative = value < 0
    fixed = f"{abs(value):.{decimals}f}"
    whole, _, fraction = fixed.partition(".")

    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join([*groups, tail])

    result = f"{whole}.{fraction}" if fraction else whole
    return f"-{result}" if negative else result


def fmt_inr(value: float | None, decimals: int = 2) -> str:
    """Format a rupee amount, or :data:`NO_DATA` when absent.

    Args:
        value: Amount.
        decimals: Decimal places.

    Returns:
        Grouped amount without a currency symbol, or ``"no data"``.
    """
    return NO_DATA if value is None else fmt_indian_digits(float(value), decimals)


def fmt_price(value: float | None, decimals: int = 2) -> str:
    """Format a price, or :data:`NO_DATA` when absent.

    Args:
        value: Price.
        decimals: Decimal places.

    Returns:
        Grouped price, or ``"no data"``.
    """
    return NO_DATA if value is None else fmt_indian_digits(float(value), decimals)


def fmt_pct(value: float | None, decimals: int = 2, signed: bool = True) -> str:
    """Format a percentage, or :data:`NO_DATA` when absent.

    Args:
        value: Percentage value.
        decimals: Decimal places.
        signed: Prefix positive values with ``+``.

    Returns:
        Formatted percentage, or ``"no data"``.

    Example:
        >>> fmt_pct(0.75)
        '+0.75%'
        >>> fmt_pct(None)
        'no data'
    """
    if value is None:
        return NO_DATA
    sign = "+" if signed and value > 0 else ""
    return f"{sign}{value:.{decimals}f}%"


def fmt_int(value: int | None) -> str:
    """Format an integer with Indian grouping, or :data:`NO_DATA`.

    Args:
        value: Integer value.

    Returns:
        Grouped integer, or ``"no data"``.
    """
    return NO_DATA if value is None else fmt_indian_digits(float(value), 0)


def _rule(char: str = "-") -> str:
    """Return a horizontal rule of the standard width."""
    return char * WIDTH


def _banner(title: str, subtitle: str | None = None) -> list[str]:
    """Build a boxed heading.

    Args:
        title: Heading text.
        subtitle: Optional second line.

    Returns:
        Lines forming the banner.
    """
    inner = WIDTH - 2
    lines = ["+" + "=" * inner + "+", "|" + title.center(inner) + "|"]
    if subtitle:
        lines.append("|" + subtitle.center(inner) + "|")
    lines.append("+" + "=" * inner + "+")
    return lines


def _section(title: str) -> list[str]:
    """Build a section heading.

    Args:
        title: Section name.

    Returns:
        Lines forming the heading.
    """
    return ["", title.upper(), _rule()]


def format_freshness(metadata: dict[str, Any], market_status: dict[str, Any]) -> list[str]:
    """Render the data freshness and session banner.

    Placed first in every snapshot so a reader cannot miss stale data.

    Args:
        metadata: Cache metadata from :meth:`MarketCache.metadata`.
        market_status: Session status from :func:`get_market_status`.

    Returns:
        Lines describing status, age, and session.
    """
    status = metadata.get("status", "UNKNOWN")
    age = metadata.get("age_seconds")
    stale = metadata.get("is_stale", True)
    threshold = metadata.get("stale_threshold_seconds", 60)

    lines = _section("data status")
    lines.append(f"  Cache status    : {status}")
    lines.append(f"  Data age        : {f'{age:.1f}s' if age is not None else 'never updated'}")
    lines.append(f"  Session         : {market_status.get('session', 'UNKNOWN')}")
    lines.append(f"  Market open     : {'yes' if market_status.get('is_open') else 'no'}")

    if not market_status.get("holiday_data_available", True):
        lines.append(
            "  NOTE            : no holiday list for this year; trading-day check "
            "is weekday-only"
        )

    if stale:
        lines.append("")
        lines.append(f"  *** STALE DATA - older than {threshold}s ***")
        lines.append("  Do not generate signals or size positions from this snapshot.")
        lines.append("  Charter: signal generation is blocked on stale data.")

    errors = metadata.get("section_errors") or {}
    if errors:
        lines.append("")
        lines.append("  Section errors:")
        for name, message in errors.items():
            lines.append(f"    - {name}: {message[:120]}")

    return lines


def format_market(market: dict[str, Any]) -> list[str]:
    """Render the index quote table.

    Args:
        market: Mapping of instrument shorthand to quote.

    Returns:
        Lines forming the market section.
    """
    lines = _section("market data")
    if not market:
        lines.append("  No market data available.")
        return lines

    lines.append(f"  {'INSTRUMENT':<20}{'LTP':>13}{'CHANGE':>12}{'CHG%':>10}")
    lines.append(f"  {_rule()[:55]}")

    for quote in market.values():
        name = str(quote.get("display_name") or quote.get("symbol") or "?")[:19]
        lines.append(
            f"  {name:<20}{fmt_price(quote.get('ltp')):>13}"
            f"{fmt_price(quote.get('change')):>12}{fmt_pct(quote.get('change_pct')):>10}"
        )

    lines.append("")
    for quote in market.values():
        name = str(quote.get("display_name") or quote.get("symbol") or "?")
        lines.append(f"  {name}")
        lines.append(
            f"    O {fmt_price(quote.get('open'))}  H {fmt_price(quote.get('high'))}  "
            f"L {fmt_price(quote.get('low'))}  PC {fmt_price(quote.get('previous_close'))}"
        )
        detail = (
            f"    Volume {fmt_int(quote.get('volume'))}  "
            f"OI {fmt_int(quote.get('open_interest'))}"
        )
        if quote.get("lot_size"):
            detail += f"  Lot {quote['lot_size']}"
        lines.append(detail)
        if quote.get("bid") is not None or quote.get("ask") is not None:
            lines.append(
                f"    Bid {fmt_price(quote.get('bid'))} x {fmt_int(quote.get('bid_qty'))}   "
                f"Ask {fmt_price(quote.get('ask'))} x {fmt_int(quote.get('ask_qty'))}   "
                f"Spread {fmt_price(quote.get('spread'))}"
            )

    return lines


def format_account(account: dict[str, Any], capital: float | None = None) -> list[str]:
    """Render funds and margin.

    Args:
        account: Funds mapping from :meth:`DataFetcher.fetch_funds`.
        capital: Configured capital, shown for comparison against broker cash.

    Returns:
        Lines forming the account section.
    """
    lines = _section("account")
    if not account:
        lines.append("  No account data available.")
        return lines

    lines.append(f"  Available cash  : {fmt_inr(account.get('available_cash'))}")
    lines.append(f"  Used margin     : {fmt_inr(account.get('used_margin'))}")
    lines.append(f"  Net             : {fmt_inr(account.get('net'))}")
    lines.append(f"  Collateral      : {fmt_inr(account.get('collateral'))}")
    lines.append(f"  M2M unrealised  : {fmt_inr(account.get('m2m_unrealised'))}")
    lines.append(f"  M2M realised    : {fmt_inr(account.get('m2m_realised'))}")

    if capital is not None:
        lines.append(f"  Configured cap. : {fmt_inr(capital)}")
        broker_cash = account.get("available_cash")
        if broker_cash is not None and abs(broker_cash - capital) > 0.01 * capital:
            lines.append(
                "  NOTE            : broker cash differs from TOTAL_CAPITAL by more "
                "than 1%; sizing uses TOTAL_CAPITAL"
            )

    return lines


def format_risk(risk: dict[str, Any]) -> list[str]:
    """Render the configured risk envelope.

    Args:
        risk: Risk block from :meth:`Settings.safe_dump`.

    Returns:
        Lines forming the risk section.
    """
    lines = _section("risk parameters")
    lines.append(f"  Capital         : {fmt_inr(risk.get('total_capital'))}")
    lines.append(
        f"  Risk per trade  : {risk.get('risk_per_trade_pct')}%  "
        f"({fmt_inr(risk.get('risk_amount_per_trade'))})"
    )
    lines.append(
        f"  Max daily loss  : {risk.get('max_daily_loss_pct')}%  "
        f"({fmt_inr(risk.get('max_daily_loss_amount'))})"
    )
    lines.append(
        f"  Max weekly loss : {risk.get('max_weekly_loss_pct')}%  "
        f"({fmt_inr(risk.get('max_weekly_loss_amount'))})"
    )
    lines.append(f"  Max positions   : {risk.get('max_open_positions')}")
    lines.append(f"  Risk profile    : {risk.get('risk_profile')}")
    lines.append(f"  Trading style   : {risk.get('trading_style')}")
    return lines


def format_positions(positions: list[dict[str, Any]]) -> list[str]:
    """Render open positions with aggregate P&L.

    Args:
        positions: Normalised positions.

    Returns:
        Lines forming the positions section.
    """
    lines = _section("open positions")
    live = [p for p in positions if p.get("is_open")]

    if not live:
        lines.append("  Flat. No open positions.")
        return lines

    lines.append(f"  {'SYMBOL':<26}{'QTY':>7}{'AVG':>11}{'LTP':>11}{'P&L':>11}")
    lines.append(f"  {_rule()[:64]}")

    total = 0.0
    have_total = False
    for position in live:
        pnl = position.get("pnl")
        if pnl is not None:
            total += pnl
            have_total = True
        lines.append(
            f"  {str(position.get('symbol') or '?')[:25]:<26}"
            f"{position.get('net_quantity') or 0:>7}"
            f"{fmt_price(position.get('net_avg_price')):>11}"
            f"{fmt_price(position.get('ltp')):>11}"
            f"{fmt_price(pnl):>11}"
        )

    lines.append(f"  {_rule()[:64]}")
    lines.append(
        f"  {'TOTAL':<26}{'':>7}{'':>11}{'':>11}{fmt_price(total if have_total else None):>11}"
    )
    return lines


def format_orders(orders: list[dict[str, Any]]) -> list[str]:
    """Render the order book, open orders first.

    Args:
        orders: Normalised orders.

    Returns:
        Lines forming the orders section.
    """
    lines = _section("orders today")
    if not orders:
        lines.append("  No orders placed today.")
        return lines

    pending = {"open", "pending", "trigger pending", "open pending", "modified"}
    ordered = sorted(orders, key=lambda o: str(o.get("status", "")).lower() not in pending)

    for order in ordered[:20]:
        status = str(order.get("status") or "?").upper()
        lines.append(
            f"  {str(order.get('symbol') or '?')[:24]:<25}"
            f"{str(order.get('transaction_type') or '?'):<5}"
            f"{order.get('quantity') or 0:>6}  @ {fmt_price(order.get('price')):>10}  {status}"
        )
        if order.get("rejection_reason"):
            lines.append(f"      reason: {order['rejection_reason'][:80]}")

    if len(ordered) > 20:
        lines.append(f"  ... and {len(ordered) - 20} more")

    return lines


def format_levels(levels: dict[str, Any]) -> list[str]:
    """Render previous-session levels and floor pivots.

    Args:
        levels: Mapping of instrument shorthand to level data.

    Returns:
        Lines forming the levels section.
    """
    lines = _section("previous session levels")
    if not levels:
        lines.append("  Not computed yet.")
        return lines

    for symbol, data in levels.items():
        pivots = data.get("pivots") or {}
        lines.append(f"  {symbol} ({data.get('date')})")
        lines.append(
            f"    H {fmt_price(data.get('high'))}  L {fmt_price(data.get('low'))}  "
            f"C {fmt_price(data.get('close'))}  Range {fmt_price(data.get('range'))}"
        )
        lines.append(
            f"    Pivot {fmt_price(pivots.get('pivot'))}   "
            f"R1 {fmt_price(pivots.get('r1'))}  R2 {fmt_price(pivots.get('r2'))}  "
            f"R3 {fmt_price(pivots.get('r3'))}"
        )
        lines.append(
            f"    {'':<12}   S1 {fmt_price(pivots.get('s1'))}  "
            f"S2 {fmt_price(pivots.get('s2'))}  S3 {fmt_price(pivots.get('s3'))}"
        )

    return lines


def format_candles(candles: dict[str, list[dict[str, Any]]], bars: int = 5) -> list[str]:
    """Render the most recent bars of each cached series.

    Args:
        candles: Mapping of ``"SYMBOL:INTERVAL"`` to candle records.
        bars: Bars to show per series.

    Returns:
        Lines forming the candles section.
    """
    lines = _section("recent candles")
    if not candles:
        lines.append("  No candle data cached.")
        return lines

    for key, series in candles.items():
        if not series:
            continue
        lines.append(f"  {key}  (last {min(bars, len(series))} of {len(series)} bars)")
        for bar in series[-bars:]:
            stamp = str(bar.get("timestamp") or "")[11:16]
            lines.append(
                f"    {stamp}  O {fmt_price(bar.get('open'))}  H {fmt_price(bar.get('high'))}  "
                f"L {fmt_price(bar.get('low'))}  C {fmt_price(bar.get('close'))}  "
                f"V {fmt_int(bar.get('volume'))}"
            )

    return lines


def format_snapshot(
    snapshot: dict[str, Any],
    config: dict[str, Any] | None = None,
    include_candles: bool = True,
) -> str:
    """Render a full cache snapshot as plain text.

    Args:
        snapshot: Output of :meth:`MarketCache.snapshot`.
        config: Redacted settings from :meth:`Settings.safe_dump`, used for the
            risk and mode sections.
        include_candles: Include the candle section when present.

    Returns:
        The formatted report.

    Example:
        >>> text = format_snapshot({"metadata": {}, "market_status": {}})
        >>> "DATA STATUS" in text
        True
    """
    metadata = snapshot.get("metadata") or {}
    market_status = snapshot.get("market_status") or {}

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = (config or {}).get("trading_mode", "ANALYSIS")

    lines = _banner("LIVE TRADING DATA SNAPSHOT", f"{stamp} IST   MODE: {mode}")
    lines += format_freshness(metadata, market_status)
    lines += format_market(snapshot.get("market") or {})

    capital = ((config or {}).get("risk") or {}).get("total_capital")
    lines += format_account(snapshot.get("account") or {}, capital)

    if config and config.get("risk"):
        lines += format_risk(config["risk"])

    lines += format_positions(snapshot.get("positions") or [])
    lines += format_orders(snapshot.get("orders") or [])
    lines += format_levels(snapshot.get("levels") or {})

    if include_candles and snapshot.get("candles"):
        lines += format_candles(snapshot["candles"])

    lines.append("")
    lines.append(_rule("="))

    if metadata.get("is_stale"):
        lines.append("STATUS: STALE DATA - NOT SUITABLE FOR ANALYSIS")
    elif metadata.get("status") == "CONNECTED":
        lines.append("STATUS: DATA CURRENT")
    else:
        lines.append(f"STATUS: {metadata.get('status', 'UNKNOWN')} - VERIFY BEFORE USE")

    lines.append("REMINDER: this is raw data only. No signal, score, or recommendation is")
    lines.append("implied. Default state is NO TRADE until analysis is performed.")
    lines.append(_rule("="))

    return "\n".join(lines)
