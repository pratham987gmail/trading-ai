"""Upstox instrument master discovery.

Do not hard-code instrument keys. Upstox publishes the full mapping from
human-readable contract descriptions (underlying, expiry, strike, option type)
to its opaque ``instrument_key`` strings as a daily JSON file, refreshed
around 6am IST, and it needs no authentication to fetch.

The real published shape (verified 2026-09-20 against the live NSE.json.gz and
BSE.json.gz files, not assumed from documentation) differs from a naive guess
in one notable way: there is no ``option_type`` field. CE/PE/FUT/INDEX all
live in a single ``instrument_type`` field alongside every other instrument
kind, and the underlying is described via ``underlying_symbol`` (and, for
options, ``underlying_key`` -- pointing directly at the underlying's own
instrument key) rather than a single generic ``underlying`` field. This module
normalises those into the columns described in the integration brief
(``option_type``, ``name``) so callers do not need to know the raw field
names, while still exposing the raw ``instrument_type``/``underlying_symbol``
columns for anything that needs them.

Example:
    >>> from pathlib import Path
    >>> from core.upstox_instruments import download_instrument_master, load_instrument_master
    >>> path = download_instrument_master()                # doctest: +SKIP
    >>> frame = load_instrument_master(path)                # doctest: +SKIP
    >>> "instrument_key" in frame.columns                    # doctest: +SKIP
    True
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

#: Public, unauthenticated instrument master URLs. "complete" covers every
#: exchange in one (larger) file; per-exchange files are smaller and are what
#: this module's default parameters use.
INSTRUMENT_MASTER_URLS: dict[str, str] = {
    "COMPLETE": "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz",
    "NSE": "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",
    "BSE": "https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz",
}

#: Columns guaranteed present on the returned DataFrame, per the integration
#: brief. Populated with None/NaN when the raw record lacks the underlying
#: field (e.g. an equity record has no strike_price or expiry).
_OUTPUT_COLUMNS = [
    "instrument_key",
    "trading_symbol",
    "exchange",
    "segment",
    "expiry",
    "strike_price",
    "option_type",
    "lot_size",
    "name",
    "underlying_symbol",
    "instrument_type",
]

# Index segments confirmed against the real downloaded files: NSE carries
# Nifty 50 / Nifty Bank / etc under NSE_INDEX, BSE carries SENSEX under
# BSE_INDEX. Verified empirically rather than assumed.
_INDEX_SEGMENTS = ("NSE_INDEX", "BSE_INDEX")

# Aliases a caller might reasonably type for an index underlying, mapped to
# the substring that appears in the real file's `name` field. Verified
# against the live file: NSE_INDEX|Nifty 50 has name="Nifty 50" and
# trading_symbol="NIFTY"; NSE_INDEX|Nifty Bank has trading_symbol="BANKNIFTY";
# BSE_INDEX|SENSEX has name="BSE SENSEX" and trading_symbol="SENSEX".
_INDEX_ALIASES: dict[str, str] = {
    "NIFTY": "NIFTY",
    "NIFTY50": "NIFTY",
    "NIFTY 50": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "SENSEX": "SENSEX",
}


class InstrumentMasterError(Exception):
    """Raised when the instrument master cannot be obtained by any means.

    Only raised when there is truly nothing to serve: no fresh download and
    no usable cache. Never raised merely because the cache is stale, since a
    stale cache is still served (with a logged warning) in preference to
    fabricating data.
    """


def _cache_path(cache_dir: Path, exchange: str) -> Path:
    return cache_dir / f"{exchange.upper()}.json.gz"


def download_instrument_master(
    exchange: str = "NSE",
    *,
    cache_dir: Path | None = None,
    max_age_hours: int = 20,
    timeout: float = 30.0,
) -> Path:
    """Download (or reuse a fresh cache of) the public instrument master file.

    Upstox refreshes these files around 6am IST daily, so a cache younger than
    ``max_age_hours`` is reused without a network call. On a download failure,
    an existing cache (however old) is served with a logged warning rather
    than raising, since a same-day list going stale by some hours is a far
    smaller problem than research code failing outright, or worse, silently
    fabricating instrument keys. Only when there is no download AND no cache
    does this raise :class:`InstrumentMasterError`.

    Args:
        exchange: One of the keys in :data:`INSTRUMENT_MASTER_URLS`
            (case-insensitive), e.g. ``"NSE"``, ``"BSE"``, ``"COMPLETE"``.
        cache_dir: Directory to cache into. Defaults to
            ``Settings().data_dir_path / "upstox_instruments"``.
        max_age_hours: Cache freshness window in hours.
        timeout: Download timeout in seconds.

    Returns:
        Local path to the cached (gzip JSON) file.

    Raises:
        InstrumentMasterError: No cache exists and the download failed, or
            ``exchange`` is not a recognised key.
    """
    key = exchange.upper()
    if key not in INSTRUMENT_MASTER_URLS:
        raise InstrumentMasterError(
            f"Unknown instrument master exchange {exchange!r}; expected one of "
            f"{sorted(INSTRUMENT_MASTER_URLS)}"
        )

    if cache_dir is None:
        from config.settings import get_settings

        cache_dir = get_settings().data_dir_path / "upstox_instruments"
    cache_dir.mkdir(parents=True, exist_ok=True)

    path = _cache_path(cache_dir, key)

    if path.exists():
        age_hours = (time.time() - path.stat().st_mtime) / 3600.0
        if age_hours < max_age_hours:
            logger.debug("Upstox instrument master cache for %s is fresh (%.1fh old)", key, age_hours)
            return path

    url = INSTRUMENT_MASTER_URLS[key]
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        content = response.content
        # Validate it actually decompresses/parses before committing to disk,
        # so a truncated or corrupted download never silently replaces a good
        # cache.
        gzip.decompress(content)
    except Exception as exc:  # noqa: BLE001 - any failure falls back to cache
        if path.exists():
            logger.warning(
                "Upstox instrument master download for %s failed (%s); serving existing cache from %s",
                key,
                type(exc).__name__,
                path,
            )
            return path
        raise InstrumentMasterError(
            f"Could not download the Upstox instrument master for {key} and no "
            f"cache exists at {path}: {type(exc).__name__}: {exc}"
        ) from exc

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_bytes(content)
    tmp_path.replace(path)
    logger.info("Upstox instrument master for %s downloaded (%d bytes)", key, len(content))
    return path


def load_instrument_master(path: Path) -> pd.DataFrame:
    """Parse a cached instrument master file into a normalised DataFrame.

    Args:
        path: Local gzip JSON file, as returned by
            :func:`download_instrument_master`.

    Returns:
        DataFrame with at least the columns in :data:`_OUTPUT_COLUMNS`.
        ``option_type`` is ``instrument_type`` narrowed to ``CE``/``PE``
        (``None`` for every other instrument kind), since the raw file has no
        such field of its own. ``expiry`` is left as the raw epoch-millisecond
        integer Upstox publishes; callers needing a date should convert
        explicitly rather than this module guessing a timezone.

    Raises:
        InstrumentMasterError: The file cannot be read or does not parse as
            the expected list of instrument records.
    """
    try:
        raw_bytes = path.read_bytes()
        try:
            decompressed = gzip.decompress(raw_bytes)
        except OSError:
            # Tolerate a pre-decompressed file (e.g. a test fixture written
            # as plain JSON), rather than requiring every caller to gzip it.
            decompressed = raw_bytes
        records = json.loads(decompressed)
    except Exception as exc:  # noqa: BLE001 - surfaced as a clear typed error
        raise InstrumentMasterError(f"Could not parse instrument master at {path}: {exc}") from exc

    if not isinstance(records, list):
        raise InstrumentMasterError(
            f"Instrument master at {path} is {type(records).__name__}, expected a list of records"
        )

    frame = pd.DataFrame.from_records(records)
    if frame.empty:
        return pd.DataFrame(columns=_OUTPUT_COLUMNS)

    if "instrument_type" in frame.columns:
        frame["option_type"] = frame["instrument_type"].where(frame["instrument_type"].isin(["CE", "PE"]))
    else:
        frame["option_type"] = None

    for column in _OUTPUT_COLUMNS:
        if column not in frame.columns:
            frame[column] = None

    ordered = [c for c in _OUTPUT_COLUMNS if c in frame.columns]
    remaining = [c for c in frame.columns if c not in ordered]
    return frame[ordered + remaining]


def resolve_instrument_key(
    frame: pd.DataFrame,
    *,
    underlying: str,
    segment: str | None = None,
    expiry: str | None = None,
    strike: float | None = None,
    option_type: str | None = None,
) -> str | None:
    """Look up the exact instrument key for a described contract.

    Never fabricates a plausible-looking key: returns ``None`` when nothing
    matches, and raises when more than one row matches, since silently
    picking one of several ambiguous candidates is a worse failure mode than
    stopping.

    Args:
        frame: A DataFrame as returned by :func:`load_instrument_master`.
        underlying: Matched case-insensitively against ``name``,
            ``underlying_symbol``, and ``trading_symbol`` (whichever the row
            has); a substring match, since ``name`` values like
            ``"MIDCPNIFTY"`` and ``trading_symbol`` values like
            ``"MIDCPNIFTY 15225 PE 27 OCT 26"`` both need to match a plain
            ``"MIDCPNIFTY"`` query.
        segment: Exact match against ``segment``, e.g. ``"NSE_FO"``.
        expiry: ``YYYY-MM-DD``; matched against the raw epoch-millisecond
            ``expiry`` column by converting it to a date first.
        strike: Exact match against ``strike_price``.
        option_type: ``"CE"`` or ``"PE"``, exact match.

    Returns:
        The unique matching ``instrument_key``, or ``None`` if no row matches.

    Raises:
        ValueError: More than one row matches the given filters.
    """
    if frame.empty:
        return None

    mask = pd.Series(True, index=frame.index)

    underlying_upper = underlying.strip().upper()
    name_col = frame.get("name", pd.Series("", index=frame.index)).fillna("").astype(str).str.upper()
    underlying_col = (
        frame.get("underlying_symbol", pd.Series("", index=frame.index)).fillna("").astype(str).str.upper()
    )
    symbol_col = (
        frame.get("trading_symbol", pd.Series("", index=frame.index)).fillna("").astype(str).str.upper()
    )
    underlying_mask = (
        name_col.str.contains(underlying_upper, regex=False)
        | underlying_col.str.contains(underlying_upper, regex=False)
        | symbol_col.str.contains(underlying_upper, regex=False)
    )
    mask &= underlying_mask

    if segment is not None:
        mask &= frame.get("segment", pd.Series(None, index=frame.index)) == segment

    if expiry is not None and "expiry" in frame.columns:
        target_date = pd.to_datetime(expiry).date()
        expiry_dates = pd.to_datetime(frame["expiry"], unit="ms", errors="coerce").dt.date
        mask &= expiry_dates == target_date

    if strike is not None and "strike_price" in frame.columns:
        mask &= pd.to_numeric(frame["strike_price"], errors="coerce") == float(strike)

    if option_type is not None and "option_type" in frame.columns:
        mask &= frame["option_type"] == option_type.strip().upper()

    matches = frame.loc[mask]
    if matches.empty:
        return None
    if len(matches) > 1:
        keys = matches["instrument_key"].tolist()
        raise ValueError(
            f"Ambiguous instrument match for underlying={underlying!r} segment={segment!r} "
            f"expiry={expiry!r} strike={strike!r} option_type={option_type!r}: "
            f"{len(matches)} candidates {keys[:5]}{'...' if len(keys) > 5 else ''}. "
            "Narrow the filters instead of guessing."
        )
    return str(matches.iloc[0]["instrument_key"])


def resolve_underlying_index_key(frame: pd.DataFrame, underlying: str) -> str | None:
    """Resolve an index instrument key for NIFTY/BANKNIFTY/SENSEX-style underlyings.

    Restricted to ``NSE_INDEX``/``BSE_INDEX`` segments (verified against the
    real downloaded files: NSE.json.gz carries ``NSE_INDEX|Nifty 50`` with
    ``trading_symbol="NIFTY"`` and ``NSE_INDEX|Nifty Bank`` with
    ``trading_symbol="BANKNIFTY"``; BSE.json.gz carries ``BSE_INDEX|SENSEX``).

    Args:
        frame: A DataFrame as returned by :func:`load_instrument_master`,
            loaded from either the NSE or BSE master (or ``COMPLETE``).
        underlying: e.g. ``"NIFTY"``, ``"BANKNIFTY"``, ``"SENSEX"``. Also
            accepts common variants such as ``"NIFTY 50"``.

    Returns:
        The unique matching ``instrument_key``, or ``None`` if not found.

    Raises:
        ValueError: More than one index row matches.
    """
    if frame.empty or "segment" not in frame.columns:
        return None

    canonical = _INDEX_ALIASES.get(underlying.strip().upper(), underlying.strip().upper())
    index_frame = frame[frame["segment"].isin(_INDEX_SEGMENTS)]
    if index_frame.empty:
        return None

    symbol_col = (
        index_frame.get("trading_symbol", pd.Series("", index=index_frame.index))
        .fillna("")
        .astype(str)
        .str.upper()
    )
    matches = index_frame.loc[symbol_col == canonical]
    if matches.empty:
        return None
    if len(matches) > 1:
        keys = matches["instrument_key"].tolist()
        raise ValueError(
            f"Ambiguous index match for underlying={underlying!r}: {len(matches)} candidates {keys}. "
            "Narrow the query instead of guessing."
        )
    return str(matches.iloc[0]["instrument_key"])
