"""Versioned, per-component transaction cost model (Phase 3.5 cost audit).

**Migration note (read this before touching this file or backtest/costs.py):**
This module is a NEW, additive cost model. It does **not** replace
:mod:`backtest.costs` and nothing in that module has been modified as part of
this audit. ``backtest.costs.CostModel``, ``CostBreakdown``,
``DEFAULT_COST_MODEL`` and ``ZERO_COST_MODEL`` are unchanged and remain what
every existing consumer imports: ``backtest/engine.py``,
``backtest/broker_simulator.py``, ``analysis/spread_pricer.py``,
``analysis/spread_selector.py``, ``research/backtest/options_backtest.py``,
``research/ml/targets.py``, ``research/strategies/option_selection.py``, and
their tests. That surface is wide enough that an in-place rewrite of
``backtest/costs.py`` risked silently breaking every one of those call sites
at once; a parallel, versioned module that existing code does not import
lets each caller migrate deliberately, one at a time, once the components it
needs are actually verified. This was a considered choice, not an oversight.

**Why "versioned per component" instead of one flat dataclass of floats:**
``backtest.costs.CostModel`` bundles every rate into a single, un-sourced
set of float fields. Its own docstring says the numbers need verification
against a real contract note -- a check that had never actually been done,
only asserted in a comment. ``CostComponent`` forces every rate to carry its
own citation, effective date, and a ``verified`` flag, so "this number is
real" and "this number is a placeholder nobody has checked" cannot be
confused by reading the same-looking float in both cases.

**What was verified this session (2026-09-20), and how:**
Primary-source government pages (incometax.gov.in, sebi.gov.in,
nseindia.com circular pages) either 404'd or timed out for automated
fetching in this session. Verification therefore rests on three independent
broker/tax-portal published-schedule pages fetched this session -- Angel
One's pricing page, Zerodha's charges page, and ClearTax's STT reference --
which *converge* on the same current figures for STT, NSE/BSE exchange
transaction charges, SEBI charges, GST, and options stamp duty. Convergence
across three independently operated, commercially liable schedules is
meaningfully stronger evidence than one repo comment, but it is still not a
primary circular text, and futures stamp duty could not be resolved because
those same sources *disagree* with each other (see the component below and
the human-verification checklist in PHASE_3_5_COST_AUDIT_REPORT.md).

**Notable finding:** the existing ``backtest/costs.py`` defaults
(STT 0.10% on options, 0.02% on futures) are themselves now stale. All three
sources fetched this session report STT at 0.15% (options) and 0.05%
(futures), with ClearTax explicitly citing a Budget 2026 STT revision
effective 2026-04-01 -- a revision the 2025-26-vintage comments in
``backtest/costs.py`` predate. This is exactly the failure mode that
module's own docstring warned about ("rates change by Finance Act... verify
before treating any result as a deployment decision"), now confirmed to have
actually happened again.

See ``PHASE_3_5_COST_AUDIT_REPORT.md`` for the full inventory, every
component's sourcing, and the complete human-verification checklist.

**Phase 3.6 Section F additions (2026-09-21), additive only -- nothing
below removes or overwrites a Phase 3.5 citation:**

- ``stt_option_sell`` and ``stt_future_sell`` gained a second, independent
  citation layer: multiple 2026 tax/broker portals (ClearTax, TaxGuru,
  ICICI Direct, Outlook Money, 5paisa, Arihant Plus, ITRnGST, a Deloitte
  briefing) now describe the SAME 0.15%/0.05% figures as enacted, not
  merely proposed, and TaxGuru's article names an exact legislative
  citation: **Finance Bill 2026, Clause 143, amending Section 98 of the
  Finance (No.2) Act, 2004, the Table against serial number 4, entries
  (a)/(b)/(c)**. Multiple independent secondary sources (Taxmann, EY
  India, gazettetracker.com, CAalley, Sambad English) converge on
  **Presidential assent 30 March 2026** and a gazette identifier
  ``CG-DL-E-31032026-271439``. An attempt was made to fetch the actual
  gazette PDF this session at
  ``https://egazette.gov.in/WriteReadData/2026/271439.pdf`` -- unlike
  Phase 3.5's flat 404s, this URL DID resolve and download (1.5MB PDF), a
  genuine step closer to primary-source access, but its text could not be
  extracted either by the fetch tool's own content processor (reported as
  garbled/undecodable) or by a manual PDF content-stream decompression
  attempt in this session (no readable "Securities Transaction"/"section
  98" text was recovered). **This therefore remains SECONDARY
  corroboration, not a confirmed primary-source read** -- the gazette
  exists and is now locatable by exact citation, but its text has not
  actually been read by anyone in this project. ``verified`` stays
  ``True`` for these two components (the secondary convergence bar Phase
  3.5 set is still met, now by a wider and more specific set of sources),
  but the distinction between "converging secondary sources" and "read
  primary text" is preserved explicitly rather than blurred.
- **New component added:** ``stt_option_exercise`` -- STT on an option
  that is exercised/assigned at expiry, charged on INTRINSIC VALUE at
  settlement, not on the original premium. This is a real, distinct rule
  (confirmed via Zerodha's own bulletin on the September 2019 change) that
  Phase 3.5's audit did not cover at all. Historically (pre-1 Sept 2019)
  this was charged on the full contract/settlement value at 0.125%, which
  could exceed the option's entire premium for a deep-ITM exercise (the
  "STT trap"); since 1 Sept 2019 the base changed to intrinsic value only,
  and per the same Finance Bill 2026 Clause 143 (Table serial 4(b)) the
  rate rises from 0.125% to 0.15% alongside the other F&O STT rates
  effective 2026-04-01. See ``PHASE_3_6_SECTION_F_COST_MIGRATION.md`` for
  the fetch trail.
- **Stamp duty (futures buy side) -- Phase 3.5's 20x conflict investigated
  further:** Zerodha's own "Uniform Stamp Duty" explainer (z-connect) and
  independent aggregators (Chittorgarh, Accelpix) converge on **futures
  buy-side stamp duty = 0.002%, options buy-side (premium) = 0.003%**,
  both under the Indian Stamp Act, 1899, made uniform across all states
  effective 1 July 2020 (2019-20 Union Budget amendment). This resolves
  the hypothesis Section F set out to investigate: Zerodha's original
  0.002% figure is the current one, and Angel One's conflicting 0.0001%
  reads as a stale or mistyped figure, not a genuinely different current
  rate -- but this conclusion rests on secondary sources (a broker's own
  explainer plus two aggregator sites), not a direct read of the Indian
  Stamp Act schedule or a government notification, and is labeled as such.
  ``stamp_duty_future_buy`` is promoted from ``verified=False`` to
  ``verified=True`` on this basis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from backtest.costs import CostBreakdown
from backtest.slippage_model_v2 import SlippageModelV2

logger = logging.getLogger(__name__)

InstrumentType = Literal["OPTION", "FUTURE", "EQUITY_INTRADAY", "EQUITY_DELIVERY"]
Side = Literal["BUY", "SELL"]
Exchange = Literal["NSE", "BSE"]


class UnverifiedCostComponentError(RuntimeError):
    """Raised when a calculation needs a component marked ``verified=False``
    and the caller supplied no override.

    This is the "fail loudly rather than silently produce an optimistic
    number" requirement made concrete: an unverified statutory rate must
    never quietly flow into a P&L figure. A caller who has genuinely checked
    the rate themselves (e.g. against a real contract note) can supply an
    explicit override to ``CostModelV2`` to proceed -- see its docstring.
    """


@dataclass(frozen=True)
class CostComponent:
    """One sourced (or explicitly unsourced) rate in the cost model.

    Attributes:
        name: Stable identifier used for lookup, e.g. ``"stt_option_sell"``
            or ``"exchange_transaction_charge_option_NSE"``.
        value: The rate. ``None`` when ``verified`` is ``False`` and no
            number could be responsibly populated -- callers must either
            supply an override or accept the raised
            :class:`UnverifiedCostComponentError`.
        unit: ``"pct_of_premium"``, ``"pct_of_notional"``,
            ``"flat_per_order"``, or ``"pct_of_turnover"``.
        side: ``"BUY"``, ``"SELL"``, or ``"BOTH"``.
        base: What the rate is applied to: ``"premium"``, ``"notional"``,
            ``"turnover"``, or ``"brokerage_plus_exchange"`` (GST's base).
        exchange: ``"NSE"``, ``"BSE"``, or ``"BOTH"`` (statutory taxes set by
            central Finance Act apply identically to both and are recorded
            as ``"BOTH"`` rather than duplicated).
        instrument_type: ``"OPTION"``, ``"FUTURE"``, ``"EQUITY_INTRADAY"``,
            or ``"EQUITY_DELIVERY"``.
        source: Citation -- a circular number, a URL, "Angel One published
            schedule as of <date>", or ``"UNVERIFIED"``.
        effective_date: ISO date the rate took effect, or ``None`` if
            unknown.
        verified: ``True`` only if ``source`` is a real, checkable citation
            (or a converging set of them) actually consulted this session --
            never ``True`` just because a plausible number already existed
            in the repo.
    """

    name: str
    value: float | None
    unit: str
    side: str
    base: str
    exchange: str
    instrument_type: str
    source: str
    effective_date: str | None
    verified: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "side": self.side,
            "base": self.base,
            "exchange": self.exchange,
            "instrument_type": self.instrument_type,
            "source": self.source,
            "effective_date": self.effective_date,
            "verified": self.verified,
        }


@dataclass(frozen=True)
class CostModelVersion:
    """An immutable, named, dated set of cost components.

    Mirrors the versioning style already used by
    :class:`research.data.models.DatasetVersion` (a stable ``version_id``,
    a free-form ``notes`` field, ``created_at``, ``as_dict``/``from_dict``)
    rather than inventing a different convention for costs.

    Attributes:
        version_id: e.g. ``"COST_MODEL_v2_2026_09"``.
        components: The full set of components in this version.
        created_at: When this version was assembled.
        notes: Free-form provenance detail -- what was checked, what
            wasn't, and why.
    """

    version_id: str
    components: tuple[CostComponent, ...]
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "components": [c.as_dict() for c in self.components],
            "created_at": self.created_at,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CostModelVersion:
        return cls(
            version_id=data["version_id"],
            components=tuple(CostComponent(**c) for c in data["components"]),
            created_at=data.get("created_at", datetime.now().isoformat()),
            notes=data.get("notes", ""),
        )

    def unverified_components(self) -> list[CostComponent]:
        """Every component that would raise :class:`UnverifiedCostComponentError`
        if used without an override."""
        return [c for c in self.components if not c.verified]

    def by_name(self) -> dict[str, CostComponent]:
        return {c.name: c for c in self.components}


def _exchange_charge_name(instrument_type: str, exchange: str) -> str:
    base = "equity" if instrument_type in ("EQUITY_INTRADAY", "EQUITY_DELIVERY") else instrument_type.lower()
    return f"exchange_transaction_charge_{base}_{exchange}"


def build_default_version() -> CostModelVersion:
    """Build ``COST_MODEL_v2_2026_09`` from this session's verification pass.

    Starting point was ``backtest/costs.py``'s existing defaults, but **no**
    value here was carried forward as ``verified=True`` solely because it
    was already in the repo. Each was re-checked against Angel One's
    pricing page, Zerodha's charges page, and ClearTax's STT reference
    (fetched 2026-09-20); see the module docstring and
    ``PHASE_3_5_COST_AUDIT_REPORT.md`` for exactly what converged, what
    conflicted, and what could not be reached at all.
    """
    fetched = "Angel One (angelone.in/pricing), Zerodha (zerodha.com/charges), and ClearTax (cleartax.in) published schedules, fetched 2026-09-20; converge on this figure"
    stt_source = fetched + (
        ". ClearTax additionally cites a Budget 2026 STT revision effective 2026-04-01, "
        "which is why this figure differs from backtest/costs.py's 0.10%/0.02% defaults."
    )
    # Phase 3.6 Section F (2026-09-21): second, independent citation layer.
    # SECONDARY corroboration (broker/tax-portal pages restating the Bill),
    # NOT a confirmed read of primary gazette text -- see module docstring
    # for the exact egazette.gov.in fetch attempt and why it did not yield
    # readable text.
    stt_source_p36 = stt_source + (
        " Phase 3.6 Section F (2026-09-21) re-checked via WebSearch/WebFetch: "
        "ClearTax, TaxGuru, ICICI Direct, Outlook Money, 5paisa, Arihant Plus, "
        "ITRnGST, and a Deloitte FPI briefing all independently describe the same "
        "0.15%/0.05% figures as enacted (not merely proposed). TaxGuru names the "
        "exact legislative citation: Finance Bill 2026, Clause 143, amending "
        "Section 98 of the Finance (No.2) Act, 2004, Table against serial number 4 "
        "(entries a/c). Presidential assent 30 March 2026 and gazette id "
        "CG-DL-E-31032026-271439 are corroborated by Taxmann, EY India, "
        "gazettetracker.com, CAalley, and Sambad English. The gazette PDF itself "
        "(https://egazette.gov.in/WriteReadData/2026/271439.pdf) was fetched this "
        "session -- it resolved (1.5MB download, unlike Phase 3.5's 404s) but its "
        "text could not be extracted by either the fetch tool's content processor "
        "or a manual PDF-stream decompression attempt, so this is SECONDARY "
        "corroboration only, not a primary-source read. See "
        "PHASE_3_6_SECTION_F_COST_MIGRATION.md for the full fetch trail."
    )
    components = [
        CostComponent(
            name="brokerage",
            value=20.0,
            unit="flat_per_order",
            side="BOTH",
            base="turnover",
            exchange="BOTH",
            instrument_type="OPTION",
            source=(
                "Angel One published pricing page, fetched 2026-09-20: flat Rs 20 per "
                "executed order (discount-broker plan). NOTE: Angel One's page also "
                "advertises a promotional 'free up to Rs 500 for the first 30 days' "
                "tier that is NOT modelled here -- it is a temporary onboarding offer, "
                "not a standing rate."
            ),
            effective_date=None,
            verified=True,
        ),
        CostComponent(
            name="stt_option_sell",
            value=0.15,
            unit="pct_of_premium",
            side="SELL",
            base="premium",
            exchange="BOTH",
            instrument_type="OPTION",
            source=stt_source_p36,
            effective_date="2026-04-01",
            verified=True,
        ),
        CostComponent(
            name="stt_option_exercise",
            value=0.15,
            unit="pct_of_intrinsic_value",
            side="BOTH",
            base="intrinsic_value_at_exercise",
            exchange="BOTH",
            instrument_type="OPTION",
            source=(
                "NEW in Phase 3.6 Section F (2026-09-21) -- not covered by Phase 3.5's "
                "audit at all. STT on an EXERCISED/ASSIGNED option is charged on "
                "INTRINSIC VALUE AT SETTLEMENT (|settlement price - strike| x quantity), "
                "NOT on the original premium -- a distinct rule from stt_option_sell "
                "above, confirmed via Zerodha's own bulletin "
                "(zerodha.com/marketintel/bulletin/230019/no-more-stt-trap-on-exercised-"
                "options-from-today): pre-1-Sept-2019, exercise STT was charged on the "
                "FULL contract/settlement value at 0.125%, which could exceed the "
                "option's entire premium for a deep-ITM exercise (the 'STT trap'); "
                "since 1 Sept 2019 the base is intrinsic value only. Rate corroborated "
                "at 0.15% (up from 0.125%) effective 2026-04-01 by ClearTax and TaxGuru, "
                "the latter citing Finance Bill 2026 Clause 143, amending Section 98 of "
                "the Finance (No.2) Act 2004, Table serial number 4(b) -- same "
                "SECONDARY-corroboration caveat as stt_option_sell/stt_future_sell "
                "above (gazette PDF fetched but not machine-readable this session). "
                "This system's current backtest paths (BuyAndHold on a single long "
                "option, no expiry-exercise path modelled) do not currently exercise "
                "this component in practice, but it is recorded here so a future "
                "exercise-at-expiry path does not have to invent it from scratch or, "
                "worse, silently reuse stt_option_sell's premium-based rate against "
                "the wrong base."
            ),
            effective_date="2026-04-01",
            verified=True,
        ),
        CostComponent(
            name="stt_future_sell",
            value=0.05,
            unit="pct_of_notional",
            side="SELL",
            base="notional",
            exchange="BOTH",
            instrument_type="FUTURE",
            source=stt_source_p36
            + " (Angel One's page also returned a conflicting Rs 0 figure for futures STT "
            "in this session's fetch, which reads as an extraction artifact given it "
            "contradicts both Zerodha and ClearTax and contradicts STT having existed "
            "on futures since inception; Zerodha+ClearTax's 0.05% is used).",
            effective_date="2026-04-01",
            verified=True,
        ),
        CostComponent(
            name="exchange_transaction_charge_option_NSE",
            value=0.0355,
            unit="pct_of_premium",
            side="BOTH",
            base="premium",
            exchange="NSE",
            instrument_type="OPTION",
            source=fetched + " (Angel One: 0.0355299%, Zerodha: 0.03553%).",
            effective_date="2024-10-01",
            verified=True,
        ),
        CostComponent(
            name="exchange_transaction_charge_option_BSE",
            value=0.0325,
            unit="pct_of_premium",
            side="BOTH",
            base="premium",
            exchange="BSE",
            instrument_type="OPTION",
            source=fetched + " (Angel One and Zerodha both quote 0.0325% for BSE index options, "
            "distinct from NSE's 0.0355% -- confirms NSE/BSE separation is real, not assumed).",
            effective_date="2024-10-01",
            verified=True,
        ),
        CostComponent(
            name="exchange_transaction_charge_future_NSE",
            value=0.00183,
            unit="pct_of_notional",
            side="BOTH",
            base="notional",
            exchange="NSE",
            instrument_type="FUTURE",
            source=fetched + " (Angel One: 0.0018299%, Zerodha: 0.00183%).",
            effective_date="2024-10-01",
            verified=True,
        ),
        CostComponent(
            name="exchange_transaction_charge_future_BSE",
            value=0.0,
            unit="pct_of_notional",
            side="BOTH",
            base="notional",
            exchange="BSE",
            instrument_type="FUTURE",
            source=fetched + " (both Angel One and Zerodha quote 0% for BSE index futures).",
            effective_date=None,
            verified=True,
        ),
        CostComponent(
            name="gst",
            value=18.0,
            unit="pct_of_turnover",
            side="BOTH",
            base="brokerage_plus_exchange",
            exchange="BOTH",
            instrument_type="OPTION",
            source=fetched + " (both quote 18% GST on brokerage + exchange charges).",
            effective_date=None,
            verified=True,
        ),
        CostComponent(
            name="sebi_charges",
            value=0.0001,
            unit="pct_of_turnover",
            side="BOTH",
            base="turnover",
            exchange="BOTH",
            instrument_type="OPTION",
            source=fetched + " (both quote Rs 10 per crore = 0.0001%).",
            effective_date=None,
            verified=True,
        ),
        CostComponent(
            name="stamp_duty_option_buy",
            value=0.003,
            unit="pct_of_premium",
            side="BUY",
            base="premium",
            exchange="BOTH",
            instrument_type="OPTION",
            source=fetched + " (both quote 0.003% on premium value, buy side).",
            effective_date=None,
            verified=True,
        ),
        CostComponent(
            name="stamp_duty_future_buy",
            value=0.002,
            unit="pct_of_notional",
            side="BUY",
            base="notional",
            exchange="BOTH",
            instrument_type="FUTURE",
            source=(
                "Phase 3.5 (2026-09-20): UNVERIFIED - Angel One's pricing page quoted "
                "0.0001% on turnover; Zerodha's charges page quoted 0.002% on turnover "
                "for the same line item, a 20x conflict, not guessed between. "
                "Phase 3.6 Section F (2026-09-21) investigated the hypothesis that this "
                "is the post-July-2020 uniform Indian Stamp Act, 1899 regime (futures "
                "0.002% buy-side, options 0.003% buy-side on premium), with Angel One's "
                "figure being stale/mistyped: Zerodha's own 'Uniform Stamp Duty' "
                "explainer (zerodha.com/z-connect/general/uniform-stamp-duty) states "
                "stamp duty has been charged uniformly across all states since "
                "1 July 2020 per the 2019-20 Union Budget amendment, and independent "
                "aggregators (chittorgarh.com, accelpix.com brokerage calculators) both "
                "separately state futures=0.002% buy-side, options=0.003% buy-side on "
                "premium -- matching this model's stamp_duty_option_buy exactly and "
                "resolving the conflict in Zerodha's favour. This is SECONDARY "
                "corroboration (a broker's own explainer plus two independent "
                "aggregator sites), NOT a direct read of the Indian Stamp Act schedule "
                "or a government/exchange circular naming the rate -- promoted to "
                "verified=True on that basis, per the same 'two-or-more independent, "
                "commercially-liable sources converge' bar Phase 3.5 used for the "
                "other F&O components, not a stronger bar than that."
            ),
            effective_date="2020-07-01",
            verified=True,
        ),
        # Equity components: not part of this system's traded instruments
        # (NIFTY/BANKNIFTY/SENSEX options), so not fetched/checked this
        # session. Carried as explicit placeholders rather than silently
        # omitted, so a future caller adding equity support sees them as
        # unverified rather than assuming they were checked.
        CostComponent(
            name="stt_equity_delivery",
            value=None,
            unit="pct_of_turnover",
            side="BOTH",
            base="turnover",
            exchange="BOTH",
            instrument_type="EQUITY_DELIVERY",
            source="UNVERIFIED - not checked this session (out of scope: system trades options, not equity cash).",
            effective_date=None,
            verified=False,
        ),
        CostComponent(
            name="stt_equity_intraday_sell",
            value=None,
            unit="pct_of_turnover",
            side="SELL",
            base="turnover",
            exchange="BOTH",
            instrument_type="EQUITY_INTRADAY",
            source="UNVERIFIED - not checked this session (out of scope: system trades options, not equity cash).",
            effective_date=None,
            verified=False,
        ),
        CostComponent(
            name="exchange_transaction_charge_equity_NSE",
            value=None,
            unit="pct_of_turnover",
            side="BOTH",
            base="turnover",
            exchange="NSE",
            instrument_type="EQUITY_DELIVERY",
            source="UNVERIFIED - not checked this session (out of scope: system trades options, not equity cash).",
            effective_date=None,
            verified=False,
        ),
        CostComponent(
            name="exchange_transaction_charge_equity_BSE",
            value=None,
            unit="pct_of_turnover",
            side="BOTH",
            base="turnover",
            exchange="BSE",
            instrument_type="EQUITY_DELIVERY",
            source="UNVERIFIED - not checked this session (out of scope: system trades options, not equity cash).",
            effective_date=None,
            verified=False,
        ),
        CostComponent(
            name="stamp_duty_equity_delivery_buy",
            value=None,
            unit="pct_of_turnover",
            side="BUY",
            base="turnover",
            exchange="BOTH",
            instrument_type="EQUITY_DELIVERY",
            source="UNVERIFIED - not checked this session (out of scope: system trades options, not equity cash).",
            effective_date=None,
            verified=False,
        ),
        CostComponent(
            name="stamp_duty_equity_intraday_buy",
            value=None,
            unit="pct_of_turnover",
            side="BUY",
            base="turnover",
            exchange="BOTH",
            instrument_type="EQUITY_INTRADAY",
            source="UNVERIFIED - not checked this session (out of scope: system trades options, not equity cash).",
            effective_date=None,
            verified=False,
        ),
    ]

    return CostModelVersion(
        version_id="COST_MODEL_v2_2026_09",
        components=tuple(components),
        notes=(
            "Starting point was backtest/costs.py's existing defaults, but none were "
            "carried forward as verified=True without independent re-checking this "
            "session. See PHASE_3_5_COST_AUDIT_REPORT.md for the full trail."
        ),
    )


#: The default version assembled from this session's verification pass.
DEFAULT_COST_MODEL_VERSION = build_default_version()


class CostModelV2:
    """Computes :class:`~backtest.costs.CostBreakdown` from a versioned,
    per-component :class:`CostModelVersion`.

    Reuses ``backtest.costs.CostBreakdown`` directly -- it is not
    reimplemented or subclassed -- so anything already built to consume that
    shape (``backtest/engine.py``, ``analysis/spread_pricer.py``, trade
    logs, reports) can accept a ``CostModelV2``-produced breakdown without
    change.

    Raises :class:`UnverifiedCostComponentError` from :meth:`calculate` (and
    therefore :meth:`round_trip`) if computing the requested leg would need
    a component whose ``verified`` flag is ``False`` and no override was
    supplied for it. This is deliberate: it is safer for a caller's
    backtest to crash than for it to silently price the trade with an
    unsourced number.

    Args:
        version: The versioned component set to use.
        overrides: ``{component_name: value}`` for components the caller has
            verified out-of-band (e.g. against a real contract note). Using
            an override logs a warning identifying it as caller-supplied and
            not sourced by this module.
    """

    def __init__(self, version: CostModelVersion, *, overrides: dict[str, float] | None = None) -> None:
        self.version = version
        self._overrides = dict(overrides or {})
        self._by_name = version.by_name()
        # For duck-type compatibility with backtest/engine.py's FillEngine,
        # which calls cost_model.apply_slippage(...) directly. This is a
        # convenience wrapper only, NOT part of the sourced/versioned
        # statutory model above -- slippage has no regulatory citation and
        # belongs entirely to backtest.slippage_model_v2 (Part B3 of the
        # cost audit). It uses that module's conservative flat floor
        # (moneyness=0, time_of_day="MID", no observed spread/liquidity),
        # matching backtest.costs.CostModel.apply_slippage's flat-percentage
        # shape so the two models can be swapped in the same engine for an
        # apples-to-apples comparison.
        self._slippage_model = SlippageModelV2()

    def apply_slippage(self, price: float, side: Side, instrument_type: InstrumentType) -> float:
        """Flat conservative slippage, for engine duck-type compatibility.

        See the note in :meth:`__init__`: this is a modelling convenience,
        not a sourced statutory figure, and is documented separately in
        :mod:`backtest.slippage_model_v2`.
        """
        quote = self._slippage_model.quote(
            moneyness=0.0,
            time_of_day="MID",
            spread_pct=None,
            order_qty=0.0,
            available_liquidity=None,
        )
        pct = (quote.slippage_pct or 0.0) / 100.0
        adjusted = price * (1 + pct) if side == "BUY" else price * (1 - pct)
        return max(round(adjusted, 2), 0.05)

    def as_dict(self) -> dict[str, Any]:
        """Report the configured version, for engine duck-type compatibility
        and for inclusion in a backtest report (mirrors
        ``backtest.costs.CostModel.as_dict``'s role, not its exact shape)."""
        unverified = self.version.unverified_components()
        return {
            "cost_model_version": self.version.version_id,
            "overrides_in_use": dict(self._overrides),
            "unverified_components": [c.name for c in unverified],
            "assumptions": [
                f"Cost model version '{self.version.version_id}'. See "
                "PHASE_3_5_COST_AUDIT_REPORT.md for full sourcing.",
                (
                    f"{len(unverified)} component(s) unverified: "
                    f"{', '.join(c.name for c in unverified)}."
                    if unverified
                    else "All components in this version are verified."
                ),
            ]
            + (
                [
                    f"Override in use for '{name}' = {value} (caller-supplied, not "
                    "sourced by this module)."
                    for name, value in self._overrides.items()
                ]
                if self._overrides
                else []
            ),
        }

    def _resolve(self, name: str) -> float:
        comp = self._by_name.get(name)
        if comp is None:
            raise KeyError(
                f"Unknown cost component '{name}' in version '{self.version.version_id}'"
            )
        if comp.verified and comp.value is not None:
            return comp.value
        if name in self._overrides:
            value = self._overrides[name]
            logger.warning(
                "CostModelV2(%s): component '%s' is unverified (source=%r); using "
                "caller-supplied override value=%s. This override is NOT sourced by "
                "this module -- it is the caller's responsibility to have verified it "
                "(e.g. against a real contract note) before relying on it.",
                self.version.version_id,
                name,
                comp.source,
                value,
            )
            return value
        raise UnverifiedCostComponentError(
            f"Cost component '{name}' is unverified (source={comp.source!r}) and no "
            f"override was supplied to CostModelV2 for version '{self.version.version_id}'. "
            "Refusing to silently use an unsourced number. Either supply an explicit "
            "override you have personally verified, or resolve the item in "
            "PHASE_3_5_COST_AUDIT_REPORT.md's human-verification checklist."
        )

    def calculate(
        self,
        instrument_type: InstrumentType,
        side: Side,
        price: float,
        quantity: int,
        *,
        exchange: Exchange = "NSE",
        slippage_amount: float = 0.0,
    ) -> CostBreakdown:
        """Compute all statutory/broker costs for one order leg.

        Mirrors ``backtest.costs.CostModel.calculate``'s arithmetic exactly
        (same turnover base, same sell-side-only STT, same buy-side-only
        stamp duty, same GST base of brokerage+exchange charges), but every
        rate is looked up from a sourced, versioned component instead of a
        bare dataclass field.

        Args:
            instrument_type: Instrument category.
            side: ``BUY`` or ``SELL``.
            price: Executed price per unit (premium for options).
            quantity: Units, already multiplied out by lot size.
            exchange: ``NSE`` or ``BSE`` -- selects the exchange-specific
                transaction-charge component. STT, GST, SEBI charges, and
                stamp duty are set by central Finance Act / uniform
                schedule and do not vary by exchange in this model (see
                each component's ``exchange="BOTH"``).
            slippage_amount: Rupee slippage already incurred, recorded for
                reporting only -- this model does not compute slippage
                itself; see :mod:`backtest.slippage_model_v2`.

        Returns:
            An itemised :class:`~backtest.costs.CostBreakdown`.

        Raises:
            UnverifiedCostComponentError: If an unverified component would
                be needed and no override was supplied.
        """
        if quantity <= 0 or price <= 0:
            return CostBreakdown()

        turnover = price * quantity
        brokerage = self._resolve("brokerage")
        stt = 0.0
        exch = 0.0
        stamp = 0.0

        if instrument_type == "OPTION":
            if side == "SELL":
                stt = turnover * self._resolve("stt_option_sell") / 100.0
            exch = turnover * self._resolve(f"exchange_transaction_charge_option_{exchange}") / 100.0
            if side == "BUY":
                stamp = turnover * self._resolve("stamp_duty_option_buy") / 100.0

        elif instrument_type == "FUTURE":
            if side == "SELL":
                stt = turnover * self._resolve("stt_future_sell") / 100.0
            exch = turnover * self._resolve(f"exchange_transaction_charge_future_{exchange}") / 100.0
            if side == "BUY":
                stamp = turnover * self._resolve("stamp_duty_future_buy") / 100.0

        elif instrument_type == "EQUITY_DELIVERY":
            stt = turnover * self._resolve("stt_equity_delivery") / 100.0
            exch = turnover * self._resolve(f"exchange_transaction_charge_equity_{exchange}") / 100.0
            if side == "BUY":
                stamp = turnover * self._resolve("stamp_duty_equity_delivery_buy") / 100.0

        else:  # EQUITY_INTRADAY
            if side == "SELL":
                stt = turnover * self._resolve("stt_equity_intraday_sell") / 100.0
            exch = turnover * self._resolve(f"exchange_transaction_charge_equity_{exchange}") / 100.0
            if side == "BUY":
                stamp = turnover * self._resolve("stamp_duty_equity_intraday_buy") / 100.0

        sebi = turnover * self._resolve("sebi_charges") / 100.0
        gst = (brokerage + exch) * self._resolve("gst") / 100.0

        return CostBreakdown(
            brokerage=brokerage,
            stt=stt,
            exchange_charges=exch,
            gst=gst,
            sebi_charges=sebi,
            stamp_duty=stamp,
            slippage=slippage_amount,
            turnover=turnover,
        )

    def round_trip(
        self,
        instrument_type: InstrumentType,
        entry_price: float,
        exit_price: float,
        quantity: int,
        direction: Literal["LONG", "SHORT"] = "LONG",
        *,
        exchange: Exchange = "NSE",
    ) -> CostBreakdown:
        """Combined entry+exit costs. Same semantics as
        ``backtest.costs.CostModel.round_trip``."""
        entry_side: Side = "BUY" if direction == "LONG" else "SELL"
        exit_side: Side = "SELL" if direction == "LONG" else "BUY"
        return self.calculate(
            instrument_type, entry_side, entry_price, quantity, exchange=exchange
        ) + self.calculate(instrument_type, exit_side, exit_price, quantity, exchange=exchange)
