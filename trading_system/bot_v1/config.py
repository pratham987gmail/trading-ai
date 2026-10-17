"""Bot configuration. Defaults are the exact research parameters; change them only
with a new validation run, never because of recent results."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Mode = Literal["PAPER", "LIVE"]


@dataclass(frozen=True)
class MomentumParams:
    universe_size: int = 200            # NIFTY 200 constituents
    top_n: int = 15                     # locked 17 Oct 2026: top 15 beat top 30 (momentum_ic_results.md)
    lookback_short: int = 189           # ~9 months (locked on pre-2022 data: 9/12 pick; sandbox_2022_split_results.md)
    lookback_long: int = 252            # ~12 months
    vol_window: int = 252               # 1-year daily volatility for risk adjustment
    trend_sma: int = 200                # NIFTY 200-day average trend filter
    rebalance_months: tuple[int, ...] = (6, 12)   # full re-rank at June and December month-ends
    min_history: int = 252
    max_daily_move_flag: float = 0.20   # |1-day return| above this -> possible split/bonus, exclude & flag


@dataclass(frozen=True)
class CondorParams:
    min_vix: float = 18.0
    short_delta: float = 0.15
    wing_sigma: float = 1.0             # variant "sigma": wing width = 1 x sigma(points) to expiry
    wing_points: float = 100.0          # variant "points100": fixed 100-pt wings (the other AI's walk-forward pick)
    entry_days_before_expiry: int = 1   # trading days
    lots: int = 1


@dataclass(frozen=True)
class BotConfig:
    mode: Mode = "PAPER"
    capital: float = 100_000.0
    algo_name: str = "SCALEEDGE_MOM30_V1"          # sent as X-Algo-Name on LIVE orders
    order_tag: str = "BOTV1"
    order_host: str = "https://api-hft.upstox.com"  # Upstox order host; verify against your app docs
    api_host: str = "https://api.upstox.com"
    registered_static_ips: tuple[str, ...] = ()     # primary + backup; LIVE refuses to run without a match
    max_orders_per_second: float = 2.0              # far below the 10/s registration threshold
    limit_buffer_pct: float = 0.002                 # buy limit = LTP*(1+b), sell = LTP*(1-b)
    daily_loss_cap_short_term: float = 1_000.0      # applies to short-term sleeve only (shadow condor is P&L-only)
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("BOTV1_DATA", "data/bot_v1")))
    momentum: MomentumParams = field(default_factory=MomentumParams)
    condor: CondorParams = field(default_factory=CondorParams)

    @property
    def journal_path(self) -> Path:
        return self.data_dir / "journal.sqlite"


def load_config() -> BotConfig:
    """Environment overrides: BOTV1_MODE, BOTV1_CAPITAL, BOTV1_STATIC_IPS (comma separated)."""
    mode = os.environ.get("BOTV1_MODE", "PAPER").upper()
    if mode not in ("PAPER", "LIVE"):
        raise ValueError(f"BOTV1_MODE must be PAPER or LIVE, got {mode!r}")
    ips = tuple(x.strip() for x in os.environ.get("BOTV1_STATIC_IPS", "").split(",") if x.strip())
    cap = float(os.environ.get("BOTV1_CAPITAL", "100000"))
    return BotConfig(mode=mode, capital=cap, registered_static_ips=ips)
