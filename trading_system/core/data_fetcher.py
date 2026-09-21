"""Retrieval and normalisation of market, account, and derivatives data.

Contract for every public function in this module: **never raise, never
invent.** Failures are logged and reported as ``None`` or an empty collection.
That is Charter Principle 8 expressed in code, and it is why numeric coercion
here returns ``None`` rather than ``0.0`` on failure.

The distinction matters more than it first appears. A price of ``0.0`` flows
through a position sizer and produces a confident, catastrophically wrong lot
count. ``None`` propagates as absence and the sizer refuses to act. So
:func:`_to_float` has no zero default, and callers must handle ``None``.

SmartAPI returns every numeric field as a string, and option strikes in the
instrument master are scaled by 100. Both are normalised here so nothing
downstream deals with broker encoding quirks.

Example:
    >>> from core.data_fetcher import DataFetcher
    >>> fetcher = DataFetcher(client, master)        # doctest: +SKIP
    >>> quote = fetcher.fetch_ltp("NIFTY")          # doctest: +SKIP
    >>> quote["ltp"]                                # doctest: +SKIP
    24500.5
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any, Literal

import pandas as pd

from config.instruments import (
    CORE_INSTRUMENTS,
    MAX_DAYS_PER_CANDLE_REQUEST,
    MAX_TOKENS_PER_QUOTE_REQUEST,
    VALID_INTERVALS,
    Instrument,
    InstrumentMaster,
    get_instrument,
    normalise_interval,
)
from core.angel_client import AngelClient
from core.market_hours import IST, now_ist, previous_trading_day, session_bounds

logger = logging.getLogger(__name__)

QuoteMode = Literal["LTP", "OHLC", "FULL"]

# Candle rows arrive as [timestamp, open, high, low, close, volume].
CANDLE_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


def _to_float(value: Any) -> float | None:
    """Coerce a broker field to float, or ``None`` when not numeric.

    There is deliberately no zero default. A missing price must stay missing so
    that downstream arithmetic cannot silently treat it as a real quote.

    Args:
        value: Raw field from a broker response.

    Returns:
        The parsed float, or ``None``.

    Example:
        >>> _to_float("24500.50")
        24500.5
        >>> _to_float("") is None
        True
        >>> _to_float("-") is None
        True
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        result = float(value)
        return None if result != result else result  # reject NaN
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "NA", "N/A", "null", "None", "nan"}:
        return None
    try:
        result = float(text)
    except (TypeError, ValueError):
        return None
    return None if result != result else result


def _to_int(value: Any) -> int | None:
    """Coerce a broker field to int, or ``None`` when not numeric.

    Args:
        value: Raw field from a broker response.

    Returns:
        The parsed integer, or ``None``.
    """
    parsed = _to_float(value)
    return None if parsed is None else int(parsed)


def _pct_change(ltp: float | None, close: float | None) -> float | None:
    """Percent change of ``ltp`` against a previous close.

    Args:
        ltp: Current price.
        close: Previous close.

    Returns:
        Percent change rounded to two decimals, or ``None`` when either input
        is missing or the close is zero.
    """
    if ltp is None or close is None or close == 0:
        return None
    return round((ltp - close) / close * 100.0, 2)


class DataFetcher:
    """Reads data from Angel One and normalises it.

    Args:
        client: Connected or degraded :class:`~core.angel_client.AngelClient`.
        master: Loaded :class:`~config.instruments.InstrumentMaster` used for
            token resolution and contract discovery.
    """

    def __init__(self, client: AngelClient, master: InstrumentMaster) -> None:
        """Initialise the fetcher."""
        self.client = client
        self.master = master

    # ------------------------------------------------------------------
    # Token resolution
    # ------------------------------------------------------------------

    def resolve(self, symbol: str, exchange: str | None = None) -> Instrument | None:
        """Resolve a symbol to an :class:`Instrument`.

        Core index shorthands are checked first, then the instrument master.

        Args:
            symbol: Shorthand (``"NIFTY"``), trading symbol (``"RELIANCE-EQ"``),
                or underlying name.
            exchange: Exchange to search when falling back to the master.

        Returns:
            The instrument, or ``None`` when unresolvable.
        """
        core = get_instrument(symbol)
        if core:
            return core

        segment = (exchange or "NSE").strip().upper()
        token = self.master.get_token(symbol, segment)
        if not token:
            logger.warning("Could not resolve '%s' on %s", symbol, segment)
            return None

        lot_size = self.master.get_lot_size(symbol, "NFO") or 1
        cleaned = symbol.strip().upper()
        return Instrument(
            key=cleaned,
            symbol=cleaned,
            token=token,
            exchange=segment,
            lot_size=lot_size,
            display_name=cleaned,
        )

    # ------------------------------------------------------------------
    # Quotes
    # ------------------------------------------------------------------

    def fetch_ltp(self, symbol: str, exchange: str | None = None) -> dict[str, Any] | None:
        """Fetch last traded price and the day's OHLC for one instrument.

        Args:
            symbol: Instrument shorthand or trading symbol.
            exchange: Exchange override.

        Returns:
            Mapping with ``symbol``, ``token``, ``exchange``, ``ltp``, ``open``,
            ``high``, ``low``, ``previous_close``, ``change``, ``change_pct``,
            and ``fetched_at``. ``None`` when unavailable.
        """
        instrument = self.resolve(symbol, exchange)
        if instrument is None:
            return None

        response = self.client.call(
            "ltpData",
            instrument.exchange,
            instrument.symbol,
            instrument.token,
            limiter="ltp",
        )
        if response is None:
            return None

        data = response.get("data") or {}
        if not data:
            logger.warning("Empty LTP payload for %s", instrument.key)
            return None

        ltp = _to_float(data.get("ltp"))
        if ltp is None:
            logger.warning("LTP missing or unparseable for %s; reporting no data", instrument.key)
            return None

        previous_close = _to_float(data.get("close"))
        return {
            "symbol": instrument.key,
            "display_name": instrument.display_name,
            "token": instrument.token,
            "exchange": instrument.exchange,
            "ltp": ltp,
            "open": _to_float(data.get("open")),
            "high": _to_float(data.get("high")),
            "low": _to_float(data.get("low")),
            "previous_close": previous_close,
            "change": round(ltp - previous_close, 2) if previous_close is not None else None,
            "change_pct": _pct_change(ltp, previous_close),
            "fetched_at": now_ist().isoformat(),
        }

    def fetch_quotes(
        self,
        instruments: Iterable[Instrument],
        mode: QuoteMode = "FULL",
    ) -> dict[str, dict[str, Any]]:
        """Batch-fetch quotes, grouped by exchange.

        ``getMarketData`` accepts up to 50 tokens per request and is the
        efficient path for an option chain, where a per-strike LTP call would
        exhaust the rate limit immediately.

        Args:
            instruments: Instruments to quote.
            mode: ``"LTP"``, ``"OHLC"``, or ``"FULL"``. ``FULL`` adds depth,
                open interest, and the upper/lower circuit bands.

        Returns:
            Mapping of token to normalised quote. Tokens that failed are absent
            rather than present with null values.
        """
        instruments = list(instruments)
        if not instruments:
            return {}

        by_exchange: dict[str, list[str]] = {}
        for instrument in instruments:
            by_exchange.setdefault(instrument.exchange, []).append(instrument.token)

        quotes: dict[str, dict[str, Any]] = {}

        for exchange, tokens in by_exchange.items():
            for start in range(0, len(tokens), MAX_TOKENS_PER_QUOTE_REQUEST):
                chunk = tokens[start : start + MAX_TOKENS_PER_QUOTE_REQUEST]
                response = self.client.call(
                    "getMarketData", mode, {exchange: chunk}, limiter="quote"
                )
                if response is None:
                    logger.warning(
                        "Quote batch failed for %s (%d tokens); those tokens are omitted",
                        exchange,
                        len(chunk),
                    )
                    continue

                payload = response.get("data") or {}
                for row in payload.get("fetched") or []:
                    parsed = self._parse_quote_row(row)
                    if parsed:
                        quotes[parsed["token"]] = parsed

                for row in payload.get("unfetched") or []:
                    logger.debug(
                        "Token unfetched: %s (%s)",
                        row.get("symbolToken"),
                        row.get("message") or row.get("errorCode"),
                    )

        return quotes

    @staticmethod
    def _parse_quote_row(row: dict[str, Any]) -> dict[str, Any] | None:
        """Normalise one ``getMarketData`` row.

        Args:
            row: Raw row from the ``fetched`` list.

        Returns:
            Normalised quote, or ``None`` when the row has no usable price.
        """
        token = str(row.get("symbolToken") or "").strip()
        ltp = _to_float(row.get("ltp"))
        if not token or ltp is None:
            return None

        previous_close = _to_float(row.get("close"))
        depth = row.get("depth") or {}
        best_bid = (depth.get("buy") or [{}])[0] if depth.get("buy") else {}
        best_ask = (depth.get("sell") or [{}])[0] if depth.get("sell") else {}

        bid = _to_float(best_bid.get("price"))
        ask = _to_float(best_ask.get("price"))
        spread = round(ask - bid, 2) if bid is not None and ask is not None else None
        spread_pct = (
            round((ask - bid) / ltp * 100.0, 3)
            if bid is not None and ask is not None and ltp
            else None
        )

        return {
            "token": token,
            "symbol": str(row.get("tradingSymbol") or "").strip(),
            "exchange": str(row.get("exchange") or "").strip(),
            "ltp": ltp,
            "open": _to_float(row.get("open")),
            "high": _to_float(row.get("high")),
            "low": _to_float(row.get("low")),
            "previous_close": previous_close,
            "change": round(ltp - previous_close, 2) if previous_close is not None else None,
            "change_pct": _pct_change(ltp, previous_close),
            "volume": _to_int(row.get("tradeVolume")),
            "open_interest": _to_int(row.get("opnInterest")),
            "oi_change_pct": _to_float(row.get("percentChange")),
            "average_price": _to_float(row.get("avgPrice")),
            "upper_circuit": _to_float(row.get("upperCircuit")),
            "lower_circuit": _to_float(row.get("lowerCircuit")),
            "week_52_high": _to_float(row.get("52WeekHigh")),
            "week_52_low": _to_float(row.get("52WeekLow")),
            "total_buy_quantity": _to_int(row.get("totBuyQuan")),
            "total_sell_quantity": _to_int(row.get("totSellQuan")),
            "bid": bid,
            "ask": ask,
            "bid_qty": _to_int(best_bid.get("quantity")),
            "ask_qty": _to_int(best_ask.get("quantity")),
            "spread": spread,
            "spread_pct": spread_pct,
            "exchange_timestamp": row.get("exchFeedTime"),
            "fetched_at": now_ist().isoformat(),
        }

    def fetch_index_snapshot(self, keys: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Fetch a batch snapshot for named core instruments.

        Args:
            keys: Core instrument shorthands, e.g. ``("NIFTY", "BANKNIFTY")``.

        Returns:
            Mapping of shorthand to quote. Failed instruments are omitted.
        """
        wanted = [CORE_INSTRUMENTS[k] for k in keys if k in CORE_INSTRUMENTS]
        if not wanted:
            return {}

        by_token = self.fetch_quotes(wanted, mode="FULL")
        snapshot: dict[str, dict[str, Any]] = {}
        for instrument in wanted:
            quote = by_token.get(instrument.token)
            if quote is None:
                logger.debug("No quote returned for %s", instrument.key)
                continue
            snapshot[instrument.key] = {
                **quote,
                "symbol": instrument.key,
                "display_name": instrument.display_name,
                "lot_size": instrument.lot_size,
            }
        return snapshot

    # ------------------------------------------------------------------
    # Candles
    # ------------------------------------------------------------------

    def fetch_candles(
        self,
        symbol: str,
        interval: str = "FIVE_MINUTE",
        bars: int = 200,
        exchange: str | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ) -> pd.DataFrame | None:
        """Fetch historical OHLCV candles.

        When an explicit range is not supplied, a lookback window is derived
        from ``bars`` and the interval, inflated to absorb weekends, holidays,
        and the fact that a trading day holds a fixed number of intraday bars.

        Args:
            symbol: Instrument shorthand or trading symbol.
            interval: SmartAPI interval name or alias (``"5min"``).
            bars: Approximate number of bars wanted.
            exchange: Exchange override.
            from_date: Explicit range start.
            to_date: Explicit range end.

        Returns:
            DataFrame indexed by IST timestamp with ``open``, ``high``, ``low``,
            ``close``, ``volume``. ``None`` when unavailable, empty frame when
            the broker legitimately has no bars for the window.
        """
        canonical = normalise_interval(interval)
        if canonical is None:
            logger.error("Unknown interval '%s'. Valid: %s", interval, ", ".join(VALID_INTERVALS))
            return None

        instrument = self.resolve(symbol, exchange)
        if instrument is None:
            return None

        end = to_date or now_ist()
        if from_date:
            start = from_date
        else:
            minutes_per_bar = VALID_INTERVALS[canonical]
            if canonical == "ONE_DAY":
                calendar_days = int(bars * 1.5) + 7
            else:
                bars_per_day = max(1, 375 // minutes_per_bar)
                trading_days = max(1, -(-bars // bars_per_day))
                calendar_days = int(trading_days * 1.6) + 5
            start = end - timedelta(days=calendar_days)

        max_span = MAX_DAYS_PER_CANDLE_REQUEST.get(canonical, 100)
        frames: list[pd.DataFrame] = []
        window_start = start

        while window_start < end:
            window_end = min(window_start + timedelta(days=max_span), end)
            chunk = self._fetch_candle_chunk(instrument, canonical, window_start, window_end)
            if chunk is not None and not chunk.empty:
                frames.append(chunk)
            window_start = window_end + timedelta(minutes=1)

        if not frames:
            logger.debug(
                "No candles returned for %s %s between %s and %s",
                instrument.key,
                canonical,
                start.date(),
                end.date(),
            )
            return pd.DataFrame(columns=list(CANDLE_COLUMNS[1:]))

        combined = pd.concat(frames)
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        return combined.tail(bars) if bars > 0 else combined

    def _fetch_candle_chunk(
        self,
        instrument: Instrument,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame | None:
        """Fetch one within-limit candle window.

        Args:
            instrument: Resolved instrument.
            interval: Canonical SmartAPI interval name.
            start: Window start.
            end: Window end.

        Returns:
            Parsed frame, or ``None`` on failure.
        """
        params = {
            "exchange": instrument.exchange,
            "symboltoken": instrument.token,
            "interval": interval,
            "fromdate": start.strftime("%Y-%m-%d %H:%M"),
            "todate": end.strftime("%Y-%m-%d %H:%M"),
        }

        response = self.client.call("getCandleData", params, limiter="candle")
        if response is None:
            return None

        rows = response.get("data") or []
        if not rows:
            return pd.DataFrame(columns=list(CANDLE_COLUMNS[1:]))

        try:
            frame = pd.DataFrame(rows, columns=list(CANDLE_COLUMNS))
        except ValueError as exc:
            logger.error("Candle payload shape unexpected for %s: %s", instrument.key, exc)
            return None

        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce", utc=True)
        frame = frame.dropna(subset=["timestamp"])
        if frame.empty:
            return pd.DataFrame(columns=list(CANDLE_COLUMNS[1:]))

        frame["timestamp"] = frame["timestamp"].dt.tz_convert(IST)
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

        # A bar without a close is unusable; drop rather than fill.
        frame = frame.dropna(subset=["open", "high", "low", "close"])
        return frame.set_index("timestamp").sort_index()

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def fetch_funds(self) -> dict[str, Any] | None:
        """Fetch funds, margin, and available cash.

        Returns:
            Mapping with ``available_cash``, ``used_margin``, ``net``,
            ``collateral``, and ``opening_balance``. ``None`` when unavailable.
        """
        response = self.client.call("rmsLimit", limiter="account")
        if response is None:
            return None

        data = response.get("data") or {}
        if not data:
            logger.warning("Empty funds payload")
            return None

        return {
            "available_cash": _to_float(data.get("availablecash")),
            "available_intraday_payin": _to_float(data.get("availableintradaypayin")),
            "available_limit_margin": _to_float(data.get("availablelimitmargin")),
            "net": _to_float(data.get("net")),
            "used_margin": _to_float(data.get("utiliseddebits")),
            "used_span": _to_float(data.get("utilisedspan")),
            "used_exposure": _to_float(data.get("utilisedexposure")),
            "used_option_premium": _to_float(data.get("utilisedoptionpremium")),
            "used_payout": _to_float(data.get("utilisedpayout")),
            "collateral": _to_float(data.get("collateral")),
            "m2m_unrealised": _to_float(data.get("m2munrealized")),
            "m2m_realised": _to_float(data.get("m2mrealized")),
            "fetched_at": now_ist().isoformat(),
        }

    def fetch_positions(self) -> list[dict[str, Any]]:
        """Fetch open positions.

        Returns:
            List of normalised positions. Empty when flat, and also empty when
            the call fails, so pair this with client state on ``/health`` to
            distinguish the two.
        """
        response = self.client.call("position", limiter="account")
        if response is None:
            return []

        rows = response.get("data") or []
        if not isinstance(rows, list):
            logger.warning("Positions payload was %s, expected a list", type(rows).__name__)
            return []

        positions: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            net_qty = _to_int(row.get("netqty")) or 0
            positions.append(
                {
                    "symbol": str(row.get("tradingsymbol") or "").strip(),
                    "token": str(row.get("symboltoken") or "").strip(),
                    "exchange": str(row.get("exchange") or "").strip(),
                    "product_type": str(row.get("producttype") or "").strip(),
                    "net_quantity": net_qty,
                    "buy_quantity": _to_int(row.get("buyqty")),
                    "sell_quantity": _to_int(row.get("sellqty")),
                    "buy_avg_price": _to_float(row.get("buyavgprice")),
                    "sell_avg_price": _to_float(row.get("sellavgprice")),
                    "net_avg_price": _to_float(row.get("netprice")),
                    "ltp": _to_float(row.get("ltp")),
                    "close_price": _to_float(row.get("close")),
                    "realised_pnl": _to_float(row.get("realised")),
                    "unrealised_pnl": _to_float(row.get("unrealised")),
                    "pnl": _to_float(row.get("pnl")),
                    "lot_size": _to_int(row.get("lotsize")),
                    "direction": "LONG" if net_qty > 0 else "SHORT" if net_qty < 0 else "FLAT",
                    "is_open": net_qty != 0,
                }
            )
        return positions

    def fetch_holdings(self) -> list[dict[str, Any]]:
        """Fetch delivery holdings.

        Returns:
            List of normalised holdings; empty when none or on failure.
        """
        response = self.client.call("holding", limiter="account")
        if response is None:
            return []

        rows = response.get("data") or []
        if not isinstance(rows, list):
            return []

        return [
            {
                "symbol": str(row.get("tradingsymbol") or "").strip(),
                "token": str(row.get("symboltoken") or "").strip(),
                "exchange": str(row.get("exchange") or "").strip(),
                "quantity": _to_int(row.get("quantity")),
                "average_price": _to_float(row.get("averageprice")),
                "ltp": _to_float(row.get("ltp")),
                "pnl": _to_float(row.get("profitandloss")),
                "pnl_pct": _to_float(row.get("pnlpercentage")),
            }
            for row in rows
            if isinstance(row, dict)
        ]

    def fetch_orders(self) -> list[dict[str, Any]]:
        """Fetch the day's order book.

        Returns:
            List of normalised orders; empty when none or on failure.
        """
        response = self.client.call("orderBook", limiter="account")
        if response is None:
            return []

        rows = response.get("data") or []
        if not isinstance(rows, list):
            return []

        return [
            {
                "order_id": str(row.get("orderid") or "").strip(),
                "symbol": str(row.get("tradingsymbol") or "").strip(),
                "token": str(row.get("symboltoken") or "").strip(),
                "exchange": str(row.get("exchange") or "").strip(),
                "transaction_type": str(row.get("transactiontype") or "").strip(),
                "order_type": str(row.get("ordertype") or "").strip(),
                "product_type": str(row.get("producttype") or "").strip(),
                "status": str(row.get("status") or "").strip(),
                "quantity": _to_int(row.get("quantity")),
                "filled_quantity": _to_int(row.get("filledshares")),
                "pending_quantity": _to_int(row.get("unfilledshares")),
                "price": _to_float(row.get("price")),
                "trigger_price": _to_float(row.get("triggerprice")),
                "average_price": _to_float(row.get("averageprice")),
                "order_time": row.get("updatetime") or row.get("exchtime"),
                "rejection_reason": str(row.get("text") or "").strip() or None,
            }
            for row in rows
            if isinstance(row, dict)
        ]

    def fetch_trades(self) -> list[dict[str, Any]]:
        """Fetch the day's executed trades.

        Returns:
            List of normalised fills; empty when none or on failure.
        """
        response = self.client.call("tradeBook", limiter="account")
        if response is None:
            return []

        rows = response.get("data") or []
        if not isinstance(rows, list):
            return []

        return [
            {
                "order_id": str(row.get("orderid") or "").strip(),
                "symbol": str(row.get("tradingsymbol") or "").strip(),
                "exchange": str(row.get("exchange") or "").strip(),
                "transaction_type": str(row.get("transactiontype") or "").strip(),
                "product_type": str(row.get("producttype") or "").strip(),
                "quantity": _to_int(row.get("fillsize")),
                "price": _to_float(row.get("fillprice")),
                "trade_value": _to_float(row.get("tradevalue")),
                "fill_time": row.get("filltime"),
            }
            for row in rows
            if isinstance(row, dict)
        ]

    # ------------------------------------------------------------------
    # Derivatives
    # ------------------------------------------------------------------

    def fetch_option_chain(
        self,
        underlying: str,
        expiry: str | None = None,
        strike_count: int = 20,
        exchange: str = "NFO",
    ) -> dict[str, Any] | None:
        """Assemble an option chain around the at-the-money strike.

        SmartAPI exposes no option-chain endpoint. The chain is built by
        selecting contracts from the instrument master, narrowing to strikes
        near spot, then batch-quoting those tokens. Narrowing first is what
        keeps this inside the rate limit: a full NIFTY expiry can carry several
        hundred strikes, and quoting all of them is neither necessary nor
        permitted at these limits.

        Open interest and volume come from the ``FULL`` quote mode. Implied
        volatility and Greeks are **not** returned by this endpoint and are
        therefore absent rather than estimated; they arrive in Phase 3 from
        either the option-Greek endpoint or a local Black-Scholes solver.

        Args:
            underlying: Underlying name (``"NIFTY"``, ``"BANKNIFTY"``).
            expiry: Expiry as ``DDMMMYYYY``. Nearest expiry when ``None``.
            strike_count: Strikes to include on each side of spot.
            exchange: Derivatives segment.

        Returns:
            Mapping with ``underlying``, ``expiry``, ``spot``, ``atm_strike``,
            ``strikes``, and ``totals``. ``None`` when the chain cannot be
            built.
        """
        if not self.master.is_loaded:
            logger.error("Option chain needs the instrument master, which is not loaded")
            return None

        key = underlying.strip().upper()

        if not expiry:
            expiries = self.master.get_expiries(key, exchange)
            if not expiries:
                logger.warning("No unexpired option contracts found for %s", key)
                return None
            expiry = expiries[0]

        contracts = self.master.get_option_contracts(key, expiry, exchange)
        if contracts.empty:
            logger.warning("No contracts for %s expiry %s", key, expiry)
            return None

        spot_quote = self.fetch_ltp(key)
        if spot_quote is None:
            logger.warning("Cannot build %s chain: spot price unavailable", key)
            return None
        spot = spot_quote["ltp"]

        available = sorted(contracts["strike"].unique())
        if not available:
            return None
        atm = min(available, key=lambda strike: abs(strike - spot))
        atm_index = available.index(atm)

        low = max(0, atm_index - strike_count)
        high = min(len(available), atm_index + strike_count + 1)
        selected = set(available[low:high])

        narrowed = contracts[contracts["strike"].isin(selected)]
        if narrowed.empty:
            return None

        to_quote = [
            Instrument(
                key=str(row["symbol"]),
                symbol=str(row["symbol"]),
                token=str(row["token"]),
                exchange=exchange,
                lot_size=int(row["lotsize"]),
            )
            for _, row in narrowed.iterrows()
        ]

        quotes = self.fetch_quotes(to_quote, mode="FULL")
        if not quotes:
            logger.warning("Chain quotes failed entirely for %s %s", key, expiry)
            return None

        rows: dict[float, dict[str, Any]] = {}
        for _, contract in narrowed.iterrows():
            strike = float(contract["strike"])
            side = str(contract["option_type"]).lower()
            quote = quotes.get(str(contract["token"]))
            entry = rows.setdefault(strike, {"strike": strike, "call": None, "put": None})
            if quote is None:
                continue
            entry["call" if side == "ce" else "put"] = {
                "symbol": str(contract["symbol"]),
                "token": str(contract["token"]),
                "ltp": quote.get("ltp"),
                "bid": quote.get("bid"),
                "ask": quote.get("ask"),
                # Size at the touch, not just the price. A two-leg spread is only
                # fillable at the quoted mid if there is quantity behind both
                # legs, so a chain recorded without depth cannot answer the one
                # question that matters for a small-size spread strategy.
                "bid_qty": quote.get("bid_qty"),
                "ask_qty": quote.get("ask_qty"),
                "spread": quote.get("spread"),
                "spread_pct": quote.get("spread_pct"),
                "volume": quote.get("volume"),
                "open_interest": quote.get("open_interest"),
                "previous_close": quote.get("previous_close"),
                "change_pct": quote.get("change_pct"),
                # The only broker-side clock in the payload. Without it a
                # recorder cannot distinguish a frozen feed from an unchanged
                # market, because our own capture time always advances.
                "exchange_timestamp": quote.get("exchange_timestamp"),
                "lot_size": int(contract["lotsize"]),
            }

        ordered = [rows[strike] for strike in sorted(rows)]

        call_oi = sum(
            r["call"]["open_interest"]
            for r in ordered
            if r["call"] and r["call"].get("open_interest")
        )
        put_oi = sum(
            r["put"]["open_interest"] for r in ordered if r["put"] and r["put"].get("open_interest")
        )
        call_volume = sum(
            r["call"]["volume"] for r in ordered if r["call"] and r["call"].get("volume")
        )
        put_volume = sum(r["put"]["volume"] for r in ordered if r["put"] and r["put"].get("volume"))

        return {
            "underlying": key,
            "expiry": expiry,
            "exchange": exchange,
            "spot": spot,
            "atm_strike": atm,
            "strike_count": len(ordered),
            "strikes": ordered,
            "totals": {
                "call_oi": call_oi or None,
                "put_oi": put_oi or None,
                "call_volume": call_volume or None,
                "put_volume": put_volume or None,
                # PCR is a raw ratio here. Interpretation is Phase 3's job.
                "pcr_oi": round(put_oi / call_oi, 3) if call_oi else None,
                "pcr_volume": round(put_volume / call_volume, 3) if call_volume else None,
            },
            "note": (
                "IV and Greeks are not provided by the quote endpoint and are "
                "deliberately absent rather than estimated. They arrive in Phase 3."
            ),
            "fetched_at": now_ist().isoformat(),
        }

    def fetch_expiries(self, underlying: str, exchange: str = "NFO") -> list[str]:
        """List unexpired option expiries for an underlying.

        Args:
            underlying: Underlying name.
            exchange: Derivatives segment.

        Returns:
            Expiry strings, soonest first; empty when none found.
        """
        return self.master.get_expiries(underlying, exchange)

    def fetch_futures_quote(self, underlying: str, exchange: str = "NFO") -> dict[str, Any] | None:
        """Quote the near-month future and compute its basis against spot.

        Basis sign is a sentiment input: premium is normal carry, discount
        signals selling pressure in the futures segment.

        Args:
            underlying: Underlying name.
            exchange: Derivatives segment.

        Returns:
            Mapping with the futures quote, ``spot``, ``basis``, and
            ``basis_pct``. ``None`` when unavailable.
        """
        contracts = self.master.get_futures_contracts(underlying, exchange)
        if contracts.empty:
            logger.warning("No futures contracts found for %s", underlying)
            return None

        near = contracts.iloc[0]
        instrument = Instrument(
            key=str(near["symbol"]),
            symbol=str(near["symbol"]),
            token=str(near["token"]),
            exchange=exchange,
            lot_size=int(near["lotsize"]),
        )

        quotes = self.fetch_quotes([instrument], mode="FULL")
        quote = quotes.get(instrument.token)
        if quote is None:
            return None

        spot_quote = self.fetch_ltp(underlying)
        spot = spot_quote["ltp"] if spot_quote else None
        futures_price = quote.get("ltp")

        basis = (
            round(futures_price - spot, 2)
            if spot is not None and futures_price is not None
            else None
        )

        return {
            "underlying": underlying.strip().upper(),
            "symbol": instrument.symbol,
            "token": instrument.token,
            "expiry": str(near["expiry"]),
            "lot_size": instrument.lot_size,
            **{k: v for k, v in quote.items() if k not in {"token", "symbol", "exchange"}},
            "spot": spot,
            "basis": basis,
            "basis_pct": (round(basis / spot * 100.0, 3) if basis is not None and spot else None),
            "structure": (
                None
                if basis is None
                else "PREMIUM" if basis > 0 else "DISCOUNT" if basis < 0 else "FLAT"
            ),
        }

    # ------------------------------------------------------------------
    # Derived reference levels
    # ------------------------------------------------------------------

    def fetch_previous_day_levels(self, symbol: str) -> dict[str, Any] | None:
        """Fetch the previous session's high, low, close, and pivots.

        Standard floor pivots are computed here because they are pure
        arithmetic on published OHLC, not analysis. Interpretation stays in
        Phase 2.

        Args:
            symbol: Instrument shorthand or trading symbol.

        Returns:
            Mapping with previous OHLC and pivot levels, or ``None``.
        """
        previous = previous_trading_day()
        start, _ = session_bounds(previous)
        end = start.replace(hour=23, minute=59)

        candles = self.fetch_candles(
            symbol, interval="ONE_DAY", bars=5, from_date=start - timedelta(days=7), to_date=end
        )
        if candles is None or candles.empty:
            return None

        row = candles.iloc[-1]
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        pivot = (high + low + close) / 3.0

        return {
            "symbol": symbol.strip().upper(),
            "date": candles.index[-1].date().isoformat(),
            "open": float(row["open"]),
            "high": high,
            "low": low,
            "close": close,
            "range": round(high - low, 2),
            "pivots": {
                "pivot": round(pivot, 2),
                "r1": round(2 * pivot - low, 2),
                "r2": round(pivot + (high - low), 2),
                "r3": round(high + 2 * (pivot - low), 2),
                "s1": round(2 * pivot - high, 2),
                "s2": round(pivot - (high - low), 2),
                "s3": round(low - 2 * (high - pivot), 2),
            },
        }
