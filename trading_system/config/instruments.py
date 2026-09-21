"""Instrument definitions, token maps, and the SmartAPI instrument master.

Angel One identifies every tradable instrument by a numeric ``symboltoken``
that is unique per exchange. Index tokens are stable and hardcoded below;
everything else is resolved from the daily instrument master file.

The master is a single ~10MB JSON document published at a public URL that
needs no authentication:
https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json

It is refreshed by Angel One each trading day. Contract tokens for F&O change
on expiry rollover, so a stale master silently yields dead tokens. This module
therefore treats a master older than one calendar day as expired.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import requests

logger = logging.getLogger(__name__)

INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)

MASTER_FILENAME = "instrument_master.json"
MASTER_PARQUET = "instrument_master.parquet"
DOWNLOAD_TIMEOUT_SECONDS = 120

OptionType = Literal["CE", "PE"]


@dataclass(frozen=True)
class Instrument:
    """A tradable instrument.

    Attributes:
        key: Internal shorthand, e.g. ``"NIFTY"``.
        symbol: Exact SmartAPI trading symbol, e.g. ``"Nifty 50"``.
        token: SmartAPI ``symboltoken`` as a string.
        exchange: Exchange code for quote calls (``NSE``, ``NFO``, ...).
        lot_size: Derivatives lot size; 1 for cash/index spot.
        fo_exchange: Exchange hosting this underlying's derivatives.
        display_name: Human-readable label for output.
    """

    key: str
    symbol: str
    token: str
    exchange: str
    lot_size: int = 1
    fo_exchange: str = "NFO"
    display_name: str = ""

    def __post_init__(self) -> None:
        if not self.display_name:
            object.__setattr__(self, "display_name", self.key)


# ---------------------------------------------------------------------------
# Index spot tokens
# ---------------------------------------------------------------------------
# These are index *spot* series on the NSE segment. Verified against the
# instrument master; `verify_core_tokens()` re-checks them at runtime so a
# broker-side change surfaces as a warning rather than silently wrong prices.
#
# Lot sizes change by SEBI/exchange circular (they were revised upward during
# 2024-25). `refresh_lot_sizes_from_master()` overwrites these from the live
# master, which is authoritative. Treat the literals as fallbacks only.

CORE_INSTRUMENTS: dict[str, Instrument] = {
    "NIFTY": Instrument(
        key="NIFTY",
        symbol="Nifty 50",
        token="99926000",
        exchange="NSE",
        lot_size=75,
        display_name="NIFTY 50",
    ),
    "BANKNIFTY": Instrument(
        key="BANKNIFTY",
        symbol="Nifty Bank",
        token="99926009",
        exchange="NSE",
        lot_size=35,
        display_name="NIFTY BANK",
    ),
    "FINNIFTY": Instrument(
        key="FINNIFTY",
        symbol="Nifty Fin Service",
        token="99926037",
        exchange="NSE",
        lot_size=65,
        display_name="NIFTY FIN SERVICE",
    ),
    "MIDCPNIFTY": Instrument(
        key="MIDCPNIFTY",
        symbol="NIFTY MID SELECT",
        token="99926074",
        exchange="NSE",
        lot_size=140,
        display_name="NIFTY MIDCAP SELECT",
    ),
    "INDIAVIX": Instrument(
        key="INDIAVIX",
        symbol="India VIX",
        token="99926017",
        exchange="NSE",
        lot_size=1,
        display_name="INDIA VIX",
    ),
    "SENSEX": Instrument(
        key="SENSEX",
        symbol="SENSEX",
        token="99919000",
        exchange="BSE",
        lot_size=20,
        fo_exchange="BFO",
        display_name="BSE SENSEX",
    ),
}

# Instruments the cache keeps warm. VIX has no F&O and is quote-only.
DEFAULT_WATCHLIST: tuple[str, ...] = ("NIFTY", "BANKNIFTY", "FINNIFTY", "INDIAVIX")

# Candle series pre-warmed by the cache: (instrument key, interval).
DEFAULT_CANDLE_SERIES: tuple[tuple[str, str], ...] = (
    ("NIFTY", "FIVE_MINUTE"),
    ("NIFTY", "FIFTEEN_MINUTE"),
    ("BANKNIFTY", "FIVE_MINUTE"),
    ("BANKNIFTY", "FIFTEEN_MINUTE"),
)

# SmartAPI historical-candle interval names and their minute equivalents.
VALID_INTERVALS: dict[str, int] = {
    "ONE_MINUTE": 1,
    "THREE_MINUTE": 3,
    "FIVE_MINUTE": 5,
    "TEN_MINUTE": 10,
    "FIFTEEN_MINUTE": 15,
    "THIRTY_MINUTE": 30,
    "ONE_HOUR": 60,
    "ONE_DAY": 375,  # one NSE equity session
}

# Convenience aliases accepted on the /candles endpoint.
INTERVAL_ALIASES: dict[str, str] = {
    "1m": "ONE_MINUTE",
    "1min": "ONE_MINUTE",
    "3m": "THREE_MINUTE",
    "3min": "THREE_MINUTE",
    "5m": "FIVE_MINUTE",
    "5min": "FIVE_MINUTE",
    "10m": "TEN_MINUTE",
    "10min": "TEN_MINUTE",
    "15m": "FIFTEEN_MINUTE",
    "15min": "FIFTEEN_MINUTE",
    "30m": "THIRTY_MINUTE",
    "30min": "THIRTY_MINUTE",
    "1h": "ONE_HOUR",
    "1hr": "ONE_HOUR",
    "60m": "ONE_HOUR",
    "1d": "ONE_DAY",
    "day": "ONE_DAY",
    "daily": "ONE_DAY",
}

# Angel One's documented per-request candle ceilings, by interval. Requests
# beyond these are rejected broker-side, so the fetcher must chunk.
MAX_DAYS_PER_CANDLE_REQUEST: dict[str, int] = {
    "ONE_MINUTE": 30,
    "THREE_MINUTE": 60,
    "FIVE_MINUTE": 100,
    "TEN_MINUTE": 100,
    "FIFTEEN_MINUTE": 200,
    "THIRTY_MINUTE": 200,
    "ONE_HOUR": 400,
    "ONE_DAY": 2000,
}

EXCHANGES: frozenset[str] = frozenset(
    {"NSE", "BSE", "NFO", "BFO", "MCX", "CDS", "NCDEX", "NCO", "BCO"}
)

# getMarketData accepts at most 50 tokens per request.
MAX_TOKENS_PER_QUOTE_REQUEST = 50


def normalise_interval(interval: str) -> str | None:
    """Map a user-supplied interval to a SmartAPI interval name.

    Args:
        interval: Either a canonical name (``"FIVE_MINUTE"``) or an alias
            (``"5min"``, ``"5m"``). Case-insensitive.

    Returns:
        The canonical interval name, or ``None`` if unrecognised.

    Example:
        >>> normalise_interval("15min")
        'FIFTEEN_MINUTE'
        >>> normalise_interval("nonsense") is None
        True
    """
    if not interval:
        return None
    candidate = interval.strip()
    upper = candidate.upper()
    if upper in VALID_INTERVALS:
        return upper
    return INTERVAL_ALIASES.get(candidate.lower())


def get_instrument(key: str) -> Instrument | None:
    """Look up a core instrument by shorthand key.

    Args:
        key: Instrument shorthand, case-insensitive (``"nifty"``).

    Returns:
        The :class:`Instrument`, or ``None`` if not a core instrument.
    """
    return CORE_INSTRUMENTS.get(key.strip().upper()) if key else None


class InstrumentMaster:
    """Loader and query interface for the SmartAPI instrument master.

    Holds the master as a :class:`pandas.DataFrame` and exposes lookups for
    token resolution, expiry discovery, and option-chain construction.

    The master is cached to disk as Parquet after first download because
    parsing 10MB of JSON on every start costs seconds; Parquet reloads in
    milliseconds. If ``pyarrow`` is unavailable the JSON path is used, which
    is slower but functionally identical.

    Example:
        >>> master = InstrumentMaster(Path("data"))
        >>> master.load()          # doctest: +SKIP
        True
        >>> master.get_token("RELIANCE", "NSE")   # doctest: +SKIP
        '2885'
    """

    #: Master columns as published, normalised to lowercase on load.
    COLUMNS = (
        "token",
        "symbol",
        "name",
        "expiry",
        "strike",
        "lotsize",
        "instrumenttype",
        "exch_seg",
        "tick_size",
    )

    def __init__(self, data_dir: Path) -> None:
        """Initialise the master.

        Args:
            data_dir: Directory for the cached master files.
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._df: pd.DataFrame | None = None
        self._loaded_at: datetime | None = None
        self._source: str = "none"

    # -- state ----------------------------------------------------------

    @property
    def json_path(self) -> Path:
        """Path to the raw JSON master on disk."""
        return self.data_dir / MASTER_FILENAME

    @property
    def parquet_path(self) -> Path:
        """Path to the Parquet fast-load cache."""
        return self.data_dir / MASTER_PARQUET

    @property
    def is_loaded(self) -> bool:
        """True when a non-empty master is in memory."""
        return self._df is not None and not self._df.empty

    @property
    def row_count(self) -> int:
        """Number of instruments loaded."""
        return 0 if self._df is None else len(self._df)

    @property
    def dataframe(self) -> pd.DataFrame:
        """The loaded master, or an empty frame when unavailable.

        A copy is not made; callers must not mutate the result.
        """
        return self._df if self._df is not None else pd.DataFrame(columns=list(self.COLUMNS))

    def status(self) -> dict[str, Any]:
        """Summarise loader state for health endpoints.

        Returns:
            Mapping with load state, row count, source, age, and staleness.
        """
        return {
            "loaded": self.is_loaded,
            "rows": self.row_count,
            "source": self._source,
            "loaded_at": self._loaded_at.isoformat() if self._loaded_at else None,
            "file_date": self._file_date().isoformat() if self._file_date() else None,
            "expired": self.is_expired(),
        }

    def _file_date(self) -> date | None:
        """Modification date of the on-disk master, if present."""
        for path in (self.parquet_path, self.json_path):
            if path.exists():
                return datetime.fromtimestamp(path.stat().st_mtime).date()
        return None

    def is_expired(self) -> bool:
        """True when the cached master predates today.

        F&O tokens change on expiry rollover, so a master from a previous day
        can contain tokens that no longer resolve.
        """
        file_date = self._file_date()
        return file_date is None or file_date < date.today()

    # -- acquisition ----------------------------------------------------

    def download(self, timeout: int = DOWNLOAD_TIMEOUT_SECONDS) -> bool:
        """Download the instrument master to disk.

        The endpoint is public and unauthenticated; no credentials are sent.
        Writes to a temporary file and renames, so an interrupted download
        cannot leave a truncated master in place.

        Args:
            timeout: Total request timeout in seconds.

        Returns:
            True on success, False on any failure.

        Raises:
            Never raises.
        """
        tmp_path = self.json_path.with_suffix(".json.tmp")
        try:
            logger.info("Downloading instrument master from Angel One")
            response = requests.get(
                INSTRUMENT_MASTER_URL,
                timeout=timeout,
                headers={"User-Agent": "trading-system/1.0"},
            )
            response.raise_for_status()

            payload = response.json()
            if not isinstance(payload, list) or not payload:
                logger.error(
                    "Instrument master payload malformed: expected a non-empty list, got %s",
                    type(payload).__name__,
                )
                return False

            tmp_path.write_text(json.dumps(payload), encoding="utf-8")
            tmp_path.replace(self.json_path)

            # Invalidate the Parquet cache so it is rebuilt from this JSON.
            self.parquet_path.unlink(missing_ok=True)

            logger.info("Instrument master downloaded: %d instruments", len(payload))
            return True

        except requests.Timeout:
            logger.error("Instrument master download timed out after %ss", timeout)
        except requests.RequestException as exc:
            logger.error("Instrument master download failed: %s", exc)
        except (ValueError, OSError) as exc:
            logger.error("Instrument master could not be written: %s", exc)
        finally:
            tmp_path.unlink(missing_ok=True)
        return False

    def load(self, download_if_missing: bool = True, force_download: bool = False) -> bool:
        """Load the master into memory, downloading when required.

        Args:
            download_if_missing: Download when no local copy exists or the
                local copy has expired.
            force_download: Download unconditionally.

        Returns:
            True when a non-empty master is in memory.

        Raises:
            Never raises.
        """
        try:
            should_download = force_download or (download_if_missing and self.is_expired())
            if should_download and not self.download() and not self.json_path.exists():
                logger.error("No instrument master available: download failed, no local copy")
                return False

            if self.parquet_path.exists() and not force_download:
                try:
                    self._df = pd.read_parquet(self.parquet_path)
                    self._source = "parquet"
                    self._loaded_at = datetime.now()
                    logger.info("Instrument master loaded from Parquet: %d rows", len(self._df))
                    return self.is_loaded
                except Exception as exc:  # noqa: BLE001 - fall back to JSON
                    logger.warning("Parquet cache unreadable (%s); rebuilding from JSON", exc)
                    self.parquet_path.unlink(missing_ok=True)

            if not self.json_path.exists():
                logger.error("Instrument master JSON missing at %s", self.json_path)
                return False

            raw = json.loads(self.json_path.read_text(encoding="utf-8"))
            self._df = self._normalise(pd.DataFrame(raw))
            self._source = "json"
            self._loaded_at = datetime.now()

            try:
                self._df.to_parquet(self.parquet_path, index=False)
            except Exception as exc:  # noqa: BLE001 - optional optimisation
                logger.debug("Parquet cache not written (%s); pyarrow may be absent", exc)

            logger.info("Instrument master loaded from JSON: %d rows", len(self._df))
            return self.is_loaded

        except json.JSONDecodeError as exc:
            logger.error("Instrument master JSON is corrupt: %s", exc)
            self.json_path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - never raise during startup
            logger.error("Instrument master load failed: %s", exc, exc_info=True)
        return False

    @staticmethod
    def _normalise(df: pd.DataFrame) -> pd.DataFrame:
        """Coerce raw master rows into stable dtypes.

        The published JSON encodes every field as a string, including strike
        and lot size, and strike is scaled by 100 (``2450000`` means 24500.0).

        Args:
            df: Raw frame built from the master JSON.

        Returns:
            Normalised frame with lowercase columns and numeric types.
        """
        df.columns = [c.strip().lower() for c in df.columns]

        for column in ("token", "symbol", "name", "exch_seg", "instrumenttype"):
            if column in df.columns:
                df[column] = df[column].astype(str).str.strip()
            else:
                df[column] = ""

        # Strike arrives multiplied by 100.
        if "strike" in df.columns:
            df["strike"] = pd.to_numeric(df["strike"], errors="coerce").fillna(0.0) / 100.0
        else:
            df["strike"] = 0.0

        if "lotsize" in df.columns:
            df["lotsize"] = pd.to_numeric(df["lotsize"], errors="coerce").fillna(1).astype("int64")
        else:
            df["lotsize"] = 1

        if "tick_size" in df.columns:
            df["tick_size"] = pd.to_numeric(df["tick_size"], errors="coerce").fillna(0.0) / 100.0
        else:
            df["tick_size"] = 0.0

        # Expiry is "DDMMMYYYY" (e.g. "28AUG2025") and empty for cash.
        df["expiry"] = (
            df.get("expiry", pd.Series([""] * len(df))).astype(str).str.strip().str.upper()
        )
        df["expiry_date"] = pd.to_datetime(df["expiry"], format="%d%b%Y", errors="coerce")

        df["name_upper"] = df["name"].str.upper()
        df["symbol_upper"] = df["symbol"].str.upper()

        return df

    # -- queries --------------------------------------------------------

    def get_token(self, symbol: str, exchange: str = "NSE") -> str | None:
        """Resolve a trading symbol to its SmartAPI token.

        Matches the exact trading symbol first, then the underlying name.
        For NSE cash the ``-EQ`` suffixed series is preferred, since that is
        the deliverable segment.

        Args:
            symbol: Trading symbol or underlying name (``"RELIANCE"``,
                ``"RELIANCE-EQ"``, ``"Nifty 50"``).
            exchange: Exchange segment to search.

        Returns:
            Token string, or ``None`` when not found.

        Example:
            >>> master.get_token("INFY", "NSE")   # doctest: +SKIP
            '1594'
        """
        if not self.is_loaded or not symbol:
            return None

        df = self.dataframe
        needle = symbol.strip().upper()
        segment = df[df["exch_seg"] == exchange.strip().upper()]
        if segment.empty:
            return None

        exact = segment[segment["symbol_upper"] == needle]
        if not exact.empty:
            return str(exact.iloc[0]["token"])

        if exchange.strip().upper() == "NSE":
            eq_series = segment[segment["symbol_upper"] == f"{needle}-EQ"]
            if not eq_series.empty:
                return str(eq_series.iloc[0]["token"])

        by_name = segment[segment["name_upper"] == needle]
        if not by_name.empty:
            # Prefer cash/index rows over derivatives when the name is ambiguous.
            spot = by_name[by_name["expiry"] == ""]
            chosen = spot if not spot.empty else by_name
            return str(chosen.iloc[0]["token"])

        return None

    def search(
        self, query: str, exchange: str | None = None, limit: int = 25
    ) -> list[dict[str, Any]]:
        """Substring search across symbols and underlying names.

        Args:
            query: Search text, minimum two characters.
            exchange: Restrict to one exchange segment when given.
            limit: Maximum rows returned, capped at 200.

        Returns:
            List of matches, exact hits first. Empty when nothing matches.
        """
        if not self.is_loaded or not query or len(query.strip()) < 2:
            return []

        needle = query.strip().upper()
        df = self.dataframe
        if exchange:
            df = df[df["exch_seg"] == exchange.strip().upper()]
        if df.empty:
            return []

        matches = df[
            df["symbol_upper"].str.contains(needle, regex=False, na=False)
            | df["name_upper"].str.contains(needle, regex=False, na=False)
        ]
        if matches.empty:
            return []

        # Exact matches float to the top.
        matches = matches.assign(
            _rank=(matches["symbol_upper"] != needle).astype(int)
            + (matches["name_upper"] != needle).astype(int)
        ).sort_values(["_rank", "symbol_upper"])

        limit = max(1, min(int(limit), 200))
        return [
            {
                "token": str(row["token"]),
                "symbol": str(row["symbol"]),
                "name": str(row["name"]),
                "exchange": str(row["exch_seg"]),
                "instrument_type": str(row["instrumenttype"]),
                "expiry": str(row["expiry"]) or None,
                "strike": float(row["strike"]) or None,
                "lot_size": int(row["lotsize"]),
            }
            for _, row in matches.head(limit).iterrows()
        ]

    def get_expiries(self, underlying: str, exchange: str = "NFO") -> list[str]:
        """List available option expiries for an underlying, soonest first.

        Past expiries are filtered out.

        Args:
            underlying: Underlying name as it appears in the master
                (``"NIFTY"``, ``"BANKNIFTY"``, ``"RELIANCE"``).
            exchange: Derivatives segment (``"NFO"`` or ``"BFO"``).

        Returns:
            Expiry strings in ``DDMMMYYYY`` form, e.g. ``["28AUG2025", ...]``.
        """
        if not self.is_loaded or not underlying:
            return []

        df = self.dataframe
        options = df[
            (df["exch_seg"] == exchange.strip().upper())
            & (df["name_upper"] == underlying.strip().upper())
            & (df["instrumenttype"].str.upper().isin(["OPTIDX", "OPTSTK"]))
            & df["expiry_date"].notna()
        ]
        if options.empty:
            return []

        today = pd.Timestamp(date.today())
        future = options[options["expiry_date"] >= today]
        if future.empty:
            return []

        ordered = future[["expiry", "expiry_date"]].drop_duplicates().sort_values("expiry_date")
        return ordered["expiry"].tolist()

    def get_option_contracts(
        self,
        underlying: str,
        expiry: str,
        exchange: str = "NFO",
        option_type: OptionType | None = None,
    ) -> pd.DataFrame:
        """Return option contracts for one underlying and expiry.

        This is the basis of option-chain construction: SmartAPI has no
        option-chain endpoint, so the chain is assembled by selecting these
        contracts and batch-quoting their tokens.

        Args:
            underlying: Underlying name (``"NIFTY"``).
            expiry: Expiry in ``DDMMMYYYY`` form (``"28AUG2025"``).
            exchange: Derivatives segment.
            option_type: Restrict to ``"CE"`` or ``"PE"``; both when ``None``.

        Returns:
            Frame with ``token``, ``symbol``, ``strike``, ``option_type``,
            ``lotsize``, sorted by strike. Empty when nothing matches.
        """
        if not self.is_loaded or not underlying or not expiry:
            return pd.DataFrame()

        df = self.dataframe
        contracts = df[
            (df["exch_seg"] == exchange.strip().upper())
            & (df["name_upper"] == underlying.strip().upper())
            & (df["expiry"] == expiry.strip().upper())
            & (df["instrumenttype"].str.upper().isin(["OPTIDX", "OPTSTK"]))
        ].copy()

        if contracts.empty:
            return pd.DataFrame()

        # CE/PE is the trading symbol suffix, e.g. NIFTY28AUG2524500CE.
        contracts["option_type"] = contracts["symbol_upper"].str[-2:]
        contracts = contracts[contracts["option_type"].isin(["CE", "PE"])]

        if option_type:
            contracts = contracts[contracts["option_type"] == option_type.upper()]

        return (
            contracts[["token", "symbol", "strike", "option_type", "lotsize", "expiry"]]
            .sort_values(["strike", "option_type"])
            .reset_index(drop=True)
        )

    def get_futures_contracts(self, underlying: str, exchange: str = "NFO") -> pd.DataFrame:
        """Return unexpired futures contracts for an underlying, nearest first.

        Args:
            underlying: Underlying name.
            exchange: Derivatives segment.

        Returns:
            Frame with ``token``, ``symbol``, ``expiry``, ``lotsize``.
        """
        if not self.is_loaded or not underlying:
            return pd.DataFrame()

        df = self.dataframe
        futures = df[
            (df["exch_seg"] == exchange.strip().upper())
            & (df["name_upper"] == underlying.strip().upper())
            & (df["instrumenttype"].str.upper().isin(["FUTIDX", "FUTSTK"]))
            & df["expiry_date"].notna()
        ]
        if futures.empty:
            return pd.DataFrame()

        today = pd.Timestamp(date.today())
        futures = futures[futures["expiry_date"] >= today]
        if futures.empty:
            return pd.DataFrame()

        return (
            futures[["token", "symbol", "expiry", "expiry_date", "lotsize"]]
            .sort_values("expiry_date")
            .reset_index(drop=True)
        )

    def get_lot_size(self, underlying: str, exchange: str = "NFO") -> int | None:
        """Return the current derivatives lot size for an underlying.

        The master is authoritative; exchange circulars change lot sizes and
        hardcoded values go stale.

        Args:
            underlying: Underlying name.
            exchange: Derivatives segment.

        Returns:
            Lot size, or ``None`` when no derivative contract is found.
        """
        if not self.is_loaded or not underlying:
            return None

        df = self.dataframe
        derivatives = df[
            (df["exch_seg"] == exchange.strip().upper())
            & (df["name_upper"] == underlying.strip().upper())
            & (df["lotsize"] > 0)
            & df["expiry_date"].notna()
        ]
        if derivatives.empty:
            return None

        nearest = derivatives.sort_values("expiry_date").iloc[0]
        return int(nearest["lotsize"])

    # -- integrity ------------------------------------------------------

    def verify_core_tokens(self) -> dict[str, bool]:
        """Check every hardcoded index token against the loaded master.

        A False here means the literal in :data:`CORE_INSTRUMENTS` no longer
        matches the broker's master and index prices would be wrong.

        Returns:
            Mapping of instrument key to verification result. Empty when the
            master is not loaded.
        """
        if not self.is_loaded:
            return {}

        df = self.dataframe
        results: dict[str, bool] = {}
        for key, instrument in CORE_INSTRUMENTS.items():
            match = df[(df["token"] == instrument.token) & (df["exch_seg"] == instrument.exchange)]
            ok = not match.empty
            results[key] = ok
            if not ok:
                logger.warning(
                    "Token verification failed for %s (token=%s, exchange=%s). "
                    "Hardcoded token not present in the instrument master; index "
                    "quotes for this instrument will be unreliable.",
                    key,
                    instrument.token,
                    instrument.exchange,
                )
        return results

    def refresh_lot_sizes(self) -> dict[str, int]:
        """Update :data:`CORE_INSTRUMENTS` lot sizes from the master.

        Lot sizes are revised by exchange circular, so the hardcoded literals go
        stale and must be replaced from the live master.

        Important caveat for position sizing: this reads the **nearest expiry**
        contract. When a lot-size revision is phased in, existing expiries keep
        the old size while newly introduced ones carry the new size, so a single
        per-underlying number is ambiguous during the transition.

        Treat the value returned here as display and sanity-check context only.
        Sizing arithmetic must use the ``lot_size`` attached to the specific
        contract being traded, which :meth:`get_option_contracts` and
        :meth:`get_futures_contracts` both return per row.

        Returns:
            Mapping of instrument key to the nearest-expiry lot size now in
            effect.
        """
        if not self.is_loaded:
            return {}

        applied: dict[str, int] = {}
        for key, instrument in list(CORE_INSTRUMENTS.items()):
            if key == "INDIAVIX":
                continue  # not tradable
            live = self.get_lot_size(key, instrument.fo_exchange)
            if live and live != instrument.lot_size:
                logger.info(
                    "Lot size for %s updated from master: %d -> %d",
                    key,
                    instrument.lot_size,
                    live,
                )
                CORE_INSTRUMENTS[key] = Instrument(
                    key=instrument.key,
                    symbol=instrument.symbol,
                    token=instrument.token,
                    exchange=instrument.exchange,
                    lot_size=live,
                    fo_exchange=instrument.fo_exchange,
                    display_name=instrument.display_name,
                )
            applied[key] = CORE_INSTRUMENTS[key].lot_size
        return applied
