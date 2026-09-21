"""Feature provenance: what every feature is, before any number is computed.

Every feature the engine can produce is declared here first, as a
:class:`FeatureSpec`, and only then computed in
:mod:`research.features.engine`. The point of doing it in this order is that
``uses_future_data`` becomes a claim that can be asserted (see
``tests/research/test_feature_leakage.py::test_registry_never_claims_future_data``)
rather than a comment someone could quietly falsify while editing the engine.

This mirrors ``research/data/models.py``'s ``DatasetVersion``/provenance
approach: declare the shape and guarantee of a piece of research data
separately from the code that produces it, so an audit can check the
declaration against the registry without re-deriving every formula.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

FeatureGroup = Literal[
    "price", "technical", "volume", "volatility", "options", "time", "regime"
]


@dataclass(frozen=True)
class FeatureSpec:
    """Declares one feature's identity, lookback, and no-lookahead guarantee.

    Attributes:
        name: Column name the engine emits.
        group: Which of the seven feature groups this belongs to.
        lookback_bars: How many trailing bars (including the current one) the
            feature's window spans. ``1`` for a feature that only reads the
            current bar (e.g. ``gap``), ``0`` for one that reads no bars at
            all (e.g. a pass-through of an out-of-band parameter like
            ``time_to_expiry_days``, which reads the bar's own timestamp
            rather than a price window).
        uses_future_data: Must always be ``False``. Asserted, not just
            documented — see the registry-wide leakage test. There is no
            legitimate feature in this engine that reads a timestamp after
            the bar it is computed for.
        description: Why this feature exists / what it captures.
        source_columns: Input columns the feature is derived from.
    """

    name: str
    group: FeatureGroup
    lookback_bars: int
    uses_future_data: bool
    description: str
    source_columns: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.uses_future_data:
            raise ValueError(
                f"FeatureSpec '{self.name}' declares uses_future_data=True. No feature "
                "in this engine is permitted to read data timestamped after the bar "
                "it is computed for; this is a hard invariant, not a default."
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "group": self.group,
            "lookback_bars": self.lookback_bars,
            "uses_future_data": self.uses_future_data,
            "description": self.description,
            "source_columns": list(self.source_columns),
        }


def _spec(
    name: str,
    group: FeatureGroup,
    lookback_bars: int,
    description: str,
    source_columns: tuple[str, ...],
) -> FeatureSpec:
    return FeatureSpec(
        name=name,
        group=group,
        lookback_bars=lookback_bars,
        uses_future_data=False,
        description=description,
        source_columns=source_columns,
    )


#: Every feature :class:`research.features.engine.FeatureEngine` can produce.
#: Populated once at import time; the engine's compute functions must not
#: emit a column absent from this registry (checked by the engine test suite).
FEATURE_REGISTRY: dict[str, FeatureSpec] = {}


def _register(spec: FeatureSpec) -> FeatureSpec:
    FEATURE_REGISTRY[spec.name] = spec
    return spec


# ----------------------------------------------------------------------
# Price
# ----------------------------------------------------------------------
_register(_spec("returns", "price", 2, "Bar-over-bar close return.", ("close",)))
_register(_spec("log_returns", "price", 2, "Bar-over-bar log return.", ("close",)))
_register(
    _spec(
        "momentum_10",
        "price",
        11,
        "Percentage change in close over the trailing 10 bars.",
        ("close",),
    )
)
_register(
    _spec("high_low_range", "price", 1, "This bar's high minus low.", ("high", "low"))
)
_register(
    _spec(
        "gap",
        "price",
        2,
        "This bar's open minus the previous bar's close.",
        ("open", "close"),
    )
)
_register(
    _spec(
        "dist_from_prev_high",
        "price",
        2,
        "Close minus the previous bar's high.",
        ("close", "high"),
    )
)
_register(
    _spec(
        "dist_from_prev_low",
        "price",
        2,
        "Close minus the previous bar's low.",
        ("close", "low"),
    )
)

# ----------------------------------------------------------------------
# Technical (via analysis/indicators.py)
# ----------------------------------------------------------------------
_register(_spec("rsi_14", "technical", 15, "Wilder RSI, 14-period.", ("close",)))
_register(_spec("macd", "technical", 26, "MACD line (12/26 EMA).", ("close",)))
_register(_spec("macd_signal", "technical", 35, "9-period EMA of the MACD line.", ("close",)))
_register(_spec("macd_hist", "technical", 35, "MACD minus signal.", ("close",)))
_register(
    _spec("atr_14", "technical", 15, "Wilder-smoothed average true range, 14-period.", ("high", "low", "close"))
)
_register(
    _spec("adx_14", "technical", 28, "Average directional index, 14-period.", ("high", "low", "close"))
)
_register(_spec("bb_upper", "technical", 20, "Bollinger upper band, 20-period.", ("close",)))
_register(_spec("bb_lower", "technical", 20, "Bollinger lower band, 20-period.", ("close",)))
_register(
    _spec("bb_width_pct", "technical", 20, "Bollinger band width as % of the middle band.", ("close",))
)
_register(
    _spec(
        "vwap",
        "technical",
        0,
        "Session volume-weighted average price. Needs volume and a DatetimeIndex.",
        ("high", "low", "close", "volume"),
    )
)
_register(
    _spec(
        "vwap_deviation",
        "technical",
        0,
        "Close minus VWAP, as a fraction of VWAP.",
        ("high", "low", "close", "volume"),
    )
)

# ----------------------------------------------------------------------
# Volume
# ----------------------------------------------------------------------
_register(_spec("volume", "volume", 1, "This bar's raw volume.", ("volume",)))
_register(
    _spec(
        "relative_volume",
        "volume",
        20,
        "This bar's volume vs its trailing 20-bar average.",
        ("volume",),
    )
)
_register(
    _spec(
        "volume_percentile",
        "volume",
        100,
        "This bar's volume rank within the trailing 100 bars.",
        ("volume",),
    )
)
_register(
    _spec(
        "volume_acceleration",
        "volume",
        2,
        "Bar-over-bar rate of change of volume.",
        ("volume",),
    )
)

# ----------------------------------------------------------------------
# Volatility
# ----------------------------------------------------------------------
_register(
    _spec(
        "realized_volatility",
        "volatility",
        20,
        "Rolling stdev of returns over 20 bars (annualization-agnostic).",
        ("close",),
    )
)
_register(
    _spec(
        "atr_percentile",
        "volatility",
        100,
        "ATR(14)'s rank within its trailing 100-bar history.",
        ("high", "low", "close"),
    )
)
_register(
    _spec(
        "range_expansion",
        "volatility",
        20,
        "Flag: this bar's true range exceeds 1.5x its trailing 20-bar ATR.",
        ("high", "low", "close"),
    )
)
_register(
    _spec(
        "range_compression",
        "volatility",
        20,
        "Flag: this bar's true range is below 0.5x its trailing 20-bar ATR.",
        ("high", "low", "close"),
    )
)

# ----------------------------------------------------------------------
# Options (only computed when the source columns are present on the frame)
# ----------------------------------------------------------------------
_register(_spec("oi", "options", 1, "Open interest, pass-through.", ("open_interest",)))
_register(
    _spec("oi_change", "options", 2, "Bar-over-bar change in open interest.", ("open_interest",))
)
_register(
    _spec(
        "pcr",
        "options",
        0,
        "Put-call ratio placeholder. A single-leg frame carries only one side "
        "(CE or PE) of the chain, so PCR cannot be derived here — it needs "
        "both legs simultaneously and is computed separately wherever "
        "chain-level (both CE and PE) data is available, not by this "
        "per-leg engine. Always NaN/skipped at this level.",
        (),
    )
)
_register(_spec("iv", "options", 1, "Implied volatility, pass-through.", ("iv",)))
_register(_spec("iv_change", "options", 2, "Bar-over-bar change in IV.", ("iv",)))
_register(_spec("delta", "options", 1, "Option delta, pass-through.", ("delta",)))
_register(_spec("gamma", "options", 1, "Option gamma, pass-through.", ("gamma",)))
_register(_spec("theta", "options", 1, "Option theta, pass-through.", ("theta",)))
_register(_spec("vega", "options", 1, "Option vega, pass-through.", ("vega",)))
_register(
    _spec(
        "bid_ask_spread",
        "options",
        1,
        "Ask minus bid.",
        ("bid", "ask"),
    )
)

# ----------------------------------------------------------------------
# Time (needs a DatetimeIndex)
# ----------------------------------------------------------------------
_register(_spec("minute_of_day", "time", 0, "Minutes since midnight of the bar's own timestamp.", ()))
_register(_spec("day_of_week", "time", 0, "Day of week (0=Monday) of the bar's own timestamp.", ()))
_register(
    _spec(
        "time_to_expiry_days",
        "time",
        0,
        "Calendar days from this bar's own timestamp to the given expiry date. "
        "Only computed when an expiry_date is passed to compute()/compute_frame(); "
        "never derived from any other bar.",
        (),
    )
)

# ----------------------------------------------------------------------
# Regime (only computed when a `regime` column is present)
# ----------------------------------------------------------------------
_register(_spec("regime", "regime", 1, "Raw regime label, pass-through.", ("regime",)))
_register(
    _spec(
        "regime_is_known",
        "regime",
        1,
        "Whether a non-null regime label was available for this bar.",
        ("regime",),
    )
)


def provenance_report() -> list[dict[str, Any]]:
    """Return every registered feature's provenance, for audit.

    Returns:
        One dict per :class:`FeatureSpec` in :data:`FEATURE_REGISTRY`.
    """
    return [spec.as_dict() for spec in FEATURE_REGISTRY.values()]
