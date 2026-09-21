"""Typed application configuration loaded from the environment.

Every tunable lives here. Nothing else in the codebase reads ``os.environ``
directly, so there is exactly one place where a credential can enter the
process and exactly one place where risk ceilings are enforced.

Security notes:
    * ``SecretStr`` wraps every credential. Its ``__repr__`` renders
      ``**********``, so accidental logging or an unhandled traceback cannot
      leak the value. Unwrapping requires an explicit
      ``.get_secret_value()`` call, which is easy to grep for.
    * :meth:`Settings.safe_dump` produces a redacted dict suitable for logs
      and for the ``/health`` endpoint.

Example:
    >>> from config.settings import get_settings
    >>> settings = get_settings()
    >>> settings.CACHE_UPDATE_INTERVAL
    5
    >>> "ANGEL_API_KEY" in settings.safe_dump()
    False
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Repository-relative root: <repo>/trading_system
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Charter ceilings. These are hard limits, not defaults, and are enforced at
# startup so a typo in .env cannot silently authorise oversized risk.
MAX_ALLOWED_RISK_PER_TRADE_PCT = 2.0
MAX_ALLOWED_DAILY_LOSS_PCT = 5.0
MAX_ALLOWED_WEEKLY_LOSS_PCT = 10.0
MAX_ALLOWED_OPEN_POSITIONS = 20

# Two-tier exception for a small account. Below MICRO_ACCOUNT_CAPITAL_THRESHOLD
# rupees, the standard percentage ceilings above are unworkable: on ₹20,000,
# a 2% per-trade budget (₹400) is smaller than the minimum possible loss on a
# single NIFTY or SENSEX option position, so the account could never place a
# trade. The ceiling widens automatically for a declared micro-account and
# automatically snaps back to the standard, stricter ceiling the moment
# TOTAL_CAPITAL crosses the threshold — nobody has to remember to tighten it
# back down after adding capital.
MICRO_ACCOUNT_CAPITAL_THRESHOLD = 50_000.0
MICRO_ACCOUNT_MAX_RISK_PER_TRADE_PCT = 15.0
MICRO_ACCOUNT_MAX_DAILY_LOSS_PCT = 20.0
MICRO_ACCOUNT_MAX_WEEKLY_LOSS_PCT = 30.0
MICRO_ACCOUNT_MAX_OPEN_POSITIONS = 2

TradingMode = Literal["ANALYSIS", "PAPER", "LIVE"]
RiskProfile = Literal["CONSERVATIVE", "MODERATE", "AGGRESSIVE"]
TradingStyle = Literal["SCALPING", "INTRADAY", "SWING", "POSITIONAL"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
# MANUAL: analyse/score only, no order access (today's behaviour).
# SEMI_AUTO: engine proposes a trade; a human approves each entry; exits
#            (stop/target/time) are then managed automatically.
# FULL_AUTO: engine also enters without per-trade approval. Gated separately
#            by _enforce_full_auto_gate — see that validator for what must be
#            true before this value is even accepted.
AutonomyLevel = Literal["MANUAL", "SEMI_AUTO", "FULL_AUTO"]


class ConfigurationError(Exception):
    """Raised when configuration is missing, malformed, or unsafe.

    Deliberately defined here rather than in :mod:`core.exceptions` so that
    ``config`` has no intra-project imports and can be loaded first during
    startup.
    """


class Settings(BaseSettings):
    """Validated runtime configuration.

    Construct via :func:`get_settings` so the ``.env`` file is parsed once per
    process.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # -- Angel One credentials ------------------------------------------
    ANGEL_API_KEY: SecretStr = Field(default=SecretStr(""))
    ANGEL_CLIENT_ID: SecretStr = Field(default=SecretStr(""))
    ANGEL_PASSWORD: SecretStr = Field(default=SecretStr(""))
    ANGEL_TOTP_SECRET: SecretStr = Field(default=SecretStr(""))

    # -- Server ---------------------------------------------------------
    API_HOST: str = "127.0.0.1"
    API_PORT: int = Field(default=8000, ge=1, le=65535)
    API_AUTH_KEY: SecretStr = Field(default=SecretStr(""))
    CORS_ORIGINS: str = ""

    # -- Cache ----------------------------------------------------------
    CACHE_UPDATE_INTERVAL: int = Field(default=5, ge=1, le=300)
    CACHE_STALE_THRESHOLD: int = Field(default=60, ge=5, le=3600)
    CACHE_CANDLE_BARS: int = Field(default=200, ge=20, le=2000)

    # -- Rate limits (requests/second) ----------------------------------
    RATE_LIMIT_QUOTE: float = Field(default=1.0, gt=0, le=50)
    RATE_LIMIT_LTP: float = Field(default=8.0, gt=0, le=50)
    RATE_LIMIT_CANDLE: float = Field(default=3.0, gt=0, le=50)
    RATE_LIMIT_ACCOUNT: float = Field(default=1.0, gt=0, le=50)
    RATE_LIMIT_ORDER: float = Field(default=8.0, gt=0, le=50)

    # -- Logging --------------------------------------------------------
    LOG_LEVEL: LogLevel = "INFO"
    LOG_CONSOLE: bool = True
    LOG_DIR: str = "logs"

    # -- Data storage -----------------------------------------------------
    DATA_DIR: str = "data"

    # -- Risk -----------------------------------------------------------
    TOTAL_CAPITAL: float = Field(default=100_000.0, gt=0)
    RISK_PER_TRADE_PCT: float = Field(default=1.0, gt=0)
    MAX_DAILY_LOSS_PCT: float = Field(default=2.0, gt=0)
    MAX_WEEKLY_LOSS_PCT: float = Field(default=5.0, gt=0)
    MAX_OPEN_POSITIONS: int = Field(default=3, ge=1, le=20)
    RISK_PROFILE: RiskProfile = "MODERATE"
    TRADING_STYLE: TradingStyle = "INTRADAY"

    # -- Option pricing -------------------------------------------------
    # Used to solve implied volatility and Greeks. Configurable because the rate
    # moves: a stale rate biases IV, and at longer tenors the bias is not small.
    # Roughly the 91-day T-bill yield is the conventional choice for INR.
    RISK_FREE_RATE: float = Field(default=0.065, ge=0.0, le=0.25)

    # -- Signal generation (Phase 4A) -----------------------------------
    # Thresholds for the spread candidate engine. Defaults are deliberately
    # conservative: this layer exists to reject, and a permissive default would
    # make it produce candidates that cannot actually be traded.
    SIGNAL_MIN_CHAIN_COVERAGE: float = Field(default=0.85, ge=0.0, le=1.0)
    # Percent, matching every other _PCT field in this file. 8.0 means eight
    # percent of the midpoint, not 0.08 percent.
    SIGNAL_MAX_BID_ASK_SPREAD_PCT: float = Field(default=8.0, gt=0.0, le=100.0)
    SIGNAL_MIN_RISK_REWARD: float = Field(default=1.5, gt=0.0, le=50.0)
    SIGNAL_MIN_DAYS_TO_EXPIRY: float = Field(default=2.0, ge=0.0, le=365.0)
    SIGNAL_MAX_DAYS_TO_EXPIRY: float = Field(default=14.0, gt=0.0, le=365.0)
    SIGNAL_MAX_COST_DRAG_PCT: float = Field(default=50.0, gt=0.0, le=100.0)
    SIGNAL_MAX_WIDTH_STEPS: int = Field(default=4, ge=1, le=20)
    SIGNAL_PRICE_SOURCE: str = "conservative"
    SIGNAL_MIN_LEG_VOLUME: int | None = Field(default=None, ge=0)
    SIGNAL_MIN_LEG_OPEN_INTEREST: int | None = Field(default=None, ge=0)
    # Gate on ever presenting an unbacktested structure as tradeable. Phase 4A has
    # no recorded option history to validate against, so this stays false and the
    # engine's best action never becomes BUY or SELL.
    SIGNAL_ENABLE_UNBACKTESTED: bool = False

    # -- Mode -----------------------------------------------------------
    TRADING_MODE: TradingMode = "ANALYSIS"
    LIVE_TRADING_ACKNOWLEDGED: bool = False
    AUTONOMY_LEVEL: AutonomyLevel = "MANUAL"
    # Only meaningful once AUTONOMY_LEVEL=FULL_AUTO is requested. Written by
    # the Phase-5/6 promotion gate (research/ml or execution gate script)
    # after its own pass criteria are met, never hand-typed. Compared as an
    # opaque string, not parsed, so its format is whatever the gate script
    # chooses.
    FULL_AUTO_UNLOCK_TOKEN: SecretStr = Field(default=SecretStr(""))

    # -- Trade-frequency limits ------------------------------------------
    # Independent of, and in addition to, the rupee loss caps above: a
    # strategy can stay inside its daily/weekly loss budget while still
    # over-trading (more entries than the edge can support, each one paying
    # brokerage). These bound entry *count*, not loss amount.
    MAX_TRADES_PER_DAY: int = Field(default=5, ge=1, le=50)
    MAX_TRADES_PER_WEEK: int = Field(default=15, ge=1, le=200)
    # Consecutive *losing* trades before the kill switch trips. The charter's
    # default halt point is 5; a smaller account with fewer, larger positions
    # should not wait that long to stop and reassess.
    MAX_CONSECUTIVE_LOSSES: int = Field(default=5, ge=1, le=20)

    # -- Order placement & compliance ------------------------------------
    # Kept well under the SEBI 2025 retail-algo threshold (10 orders/second)
    # that separates unregistered self-use from a registered algo provider.
    # This is a hard client-side throttle, not merely a target.
    ORDER_RATE_LIMIT_PER_SEC: float = Field(default=2.0, gt=0, le=9.0)
    # Prefix stamped onto every order's broker tag so every order this system
    # places is identifiable in the order book and in the audit export.
    ORDER_TAG_PREFIX: str = "ALGOTRD"

    # -- KPI (reporting only — see docstring on WEEKLY_TARGET_PCT) -------
    # A target shown on the weekly report, nothing more. It must never be
    # read by any sizing, entry-frequency, or risk-check code path: doing so
    # would turn "we're behind this week" into "take a bigger/worse trade to
    # catch up", which is precisely the revenge-trading pattern the charter's
    # behavioural safeguards exist to prevent. 0 disables the KPI line.
    WEEKLY_TARGET_PCT: float = Field(default=0.0, ge=0.0, le=100.0)
    # Comma-separated underlyings in *preference order* — first tried first
    # when more than one produces a qualifying candidate on the same pass.
    # Empty means "no preference", i.e. whatever config.instruments lists.
    PREFERRED_UNDERLYINGS: str = ""

    # -- Static IP / proxy ----------------------------------------------
    ANGEL_HTTPS_PROXY: SecretStr = Field(default=SecretStr(""))
    REGISTERED_STATIC_IP: str = ""

    # -- Upstox credentials (historical-data research only, never live trading) --
    UPSTOX_CLIENT_ID: SecretStr = Field(default=SecretStr(""))
    UPSTOX_CLIENT_SECRET: SecretStr = Field(default=SecretStr(""))
    UPSTOX_ACCESS_TOKEN: SecretStr = Field(default=SecretStr(""))
    UPSTOX_REDIRECT_URI: str = Field(default="")  # not secret, just a URL

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("LOG_LEVEL", mode="before")
    @classmethod
    def _upper_log_level(cls, v: Any) -> Any:
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("TRADING_MODE", "RISK_PROFILE", "TRADING_STYLE", mode="before")
    @classmethod
    def _upper_enum(cls, v: Any) -> Any:
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("ANGEL_TOTP_SECRET")
    @classmethod
    def _validate_totp_shape(cls, v: SecretStr) -> SecretStr:
        """Reject a 6-digit code pasted where the base32 secret belongs.

        This is the single most common setup mistake, and it fails at login
        with an opaque broker error. Catching it here saves real debugging
        time. The check is structural only; the secret itself is never logged.
        """
        raw = v.get_secret_value().strip().replace(" ", "").upper()
        if not raw:
            return v
        if raw.isdigit():
            raise ValueError(
                "ANGEL_TOTP_SECRET looks like a 6-digit rotating code. It must be "
                "the BASE32 seed behind the authenticator QR code (letters A-Z and "
                "digits 2-7). Re-enrol TOTP and choose 'enter key manually'."
            )
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=")
        if not set(raw) <= allowed:
            raise ValueError(
                "ANGEL_TOTP_SECRET contains characters outside the base32 alphabet "
                "(A-Z, 2-7). Check for a truncated or mistyped secret."
            )
        if len(raw) < 16:
            raise ValueError(
                f"ANGEL_TOTP_SECRET is only {len(raw)} characters; base32 TOTP seeds "
                "are normally 16 or more. It looks truncated."
            )
        return SecretStr(raw)

    @field_validator("API_HOST")
    @classmethod
    def _validate_host(cls, v: str) -> str:
        host = v.strip()
        if not host:
            raise ValueError("API_HOST must not be empty")
        return host

    @field_validator("SIGNAL_PRICE_SOURCE")
    @classmethod
    def _validate_price_source(cls, v: str) -> str:
        """Canonicalise the signal pricing mode at startup.

        Imported lazily to keep ``config`` free of an import-time dependency on
        ``analysis``. Validated here rather than at request time so a typo in
        ``.env`` fails on boot instead of silently changing every price in a
        response.
        """
        from analysis.spread_pricer import normalise_price_mode

        return normalise_price_mode(v)

    @model_validator(mode="after")
    def _enforce_signal_coherence(self) -> Settings:
        """Refuse an expiry window that can never admit a candidate."""
        if self.SIGNAL_MIN_DAYS_TO_EXPIRY > self.SIGNAL_MAX_DAYS_TO_EXPIRY:
            raise ValueError(
                f"SIGNAL_MIN_DAYS_TO_EXPIRY={self.SIGNAL_MIN_DAYS_TO_EXPIRY} exceeds "
                f"SIGNAL_MAX_DAYS_TO_EXPIRY={self.SIGNAL_MAX_DAYS_TO_EXPIRY}, so the "
                "expiry window is empty and every candidate would be rejected on time "
                "to expiry regardless of its merits."
            )
        return self

    @property
    def _is_micro_account(self) -> bool:
        """True when TOTAL_CAPITAL qualifies for the widened micro-account
        ceiling. See the constants' docstring comment near the top of this
        module for why this tier exists.
        """
        return self.TOTAL_CAPITAL <= MICRO_ACCOUNT_CAPITAL_THRESHOLD

    @property
    def _effective_risk_ceilings(self) -> tuple[float, float, float, int]:
        """(per-trade %, daily %, weekly %, max open positions) ceiling
        actually in force for this configuration's TOTAL_CAPITAL.
        """
        if self._is_micro_account:
            return (
                MICRO_ACCOUNT_MAX_RISK_PER_TRADE_PCT,
                MICRO_ACCOUNT_MAX_DAILY_LOSS_PCT,
                MICRO_ACCOUNT_MAX_WEEKLY_LOSS_PCT,
                MICRO_ACCOUNT_MAX_OPEN_POSITIONS,
            )
        return (
            MAX_ALLOWED_RISK_PER_TRADE_PCT,
            MAX_ALLOWED_DAILY_LOSS_PCT,
            MAX_ALLOWED_WEEKLY_LOSS_PCT,
            MAX_ALLOWED_OPEN_POSITIONS,
        )

    @model_validator(mode="after")
    def _enforce_risk_ceilings(self) -> Settings:
        """Refuse to start with risk settings above the charter ceilings.

        The ceiling itself depends on TOTAL_CAPITAL: see
        ``_effective_risk_ceilings`` and the micro-account constants above.
        Above the micro-account threshold this is exactly the original
        single-tier check.
        """
        risk_ceiling, daily_ceiling, weekly_ceiling, positions_ceiling = (
            self._effective_risk_ceilings
        )
        breaches: list[str] = []
        if self.RISK_PER_TRADE_PCT > risk_ceiling:
            breaches.append(
                f"RISK_PER_TRADE_PCT={self.RISK_PER_TRADE_PCT} exceeds the "
                f"{risk_ceiling}% per-trade ceiling"
            )
        if self.MAX_DAILY_LOSS_PCT > daily_ceiling:
            breaches.append(
                f"MAX_DAILY_LOSS_PCT={self.MAX_DAILY_LOSS_PCT} exceeds the "
                f"{daily_ceiling}% daily ceiling"
            )
        if self.MAX_WEEKLY_LOSS_PCT > weekly_ceiling:
            breaches.append(
                f"MAX_WEEKLY_LOSS_PCT={self.MAX_WEEKLY_LOSS_PCT} exceeds the "
                f"{weekly_ceiling}% weekly ceiling"
            )
        if self.MAX_OPEN_POSITIONS > positions_ceiling:
            breaches.append(
                f"MAX_OPEN_POSITIONS={self.MAX_OPEN_POSITIONS} exceeds the "
                f"{positions_ceiling}-position ceiling"
            )
        if self.RISK_PER_TRADE_PCT > self.MAX_DAILY_LOSS_PCT:
            breaches.append(
                "RISK_PER_TRADE_PCT exceeds MAX_DAILY_LOSS_PCT, so a single "
                "losing trade would breach the daily cap"
            )
        if self.MAX_DAILY_LOSS_PCT > self.MAX_WEEKLY_LOSS_PCT:
            breaches.append("MAX_DAILY_LOSS_PCT exceeds MAX_WEEKLY_LOSS_PCT")
        if breaches:
            raise ValueError("Risk configuration rejected: " + "; ".join(breaches))
        return self

    @model_validator(mode="after")
    def _enforce_trade_frequency_coherence(self) -> Settings:
        """A daily cap above the weekly cap could never bind."""
        if self.MAX_TRADES_PER_DAY > self.MAX_TRADES_PER_WEEK:
            raise ValueError(
                f"MAX_TRADES_PER_DAY={self.MAX_TRADES_PER_DAY} exceeds "
                f"MAX_TRADES_PER_WEEK={self.MAX_TRADES_PER_WEEK}; five trading "
                "days could then exceed the weekly count on the daily limit "
                "alone, so the weekly limit would never actually bind."
            )
        return self

    @model_validator(mode="after")
    def _enforce_full_auto_gate(self) -> Settings:
        """FULL_AUTO must never be reachable by flipping one setting.

        It requires LIVE mode, the existing live-trading acknowledgement, and
        a non-empty unlock token. The token's *validity* (was it actually
        issued by a passing promotion gate, for this account, still current)
        is checked at runtime by the execution layer, not here — Settings has
        no business reading the gate's state file. This validator only
        refuses the configuration shape that could never be legitimate.
        """
        if self.AUTONOMY_LEVEL == "FULL_AUTO":
            problems: list[str] = []
            if self.TRADING_MODE != "LIVE":
                problems.append("TRADING_MODE must be LIVE")
            if not self.LIVE_TRADING_ACKNOWLEDGED:
                problems.append("LIVE_TRADING_ACKNOWLEDGED must be true")
            if not self.FULL_AUTO_UNLOCK_TOKEN.get_secret_value().strip():
                problems.append("FULL_AUTO_UNLOCK_TOKEN must be set")
            if problems:
                raise ValueError(
                    "AUTONOMY_LEVEL=FULL_AUTO requires: " + "; ".join(problems)
                )
        return self

    @model_validator(mode="after")
    def _enforce_live_trading_gate(self) -> Settings:
        """LIVE mode requires an explicit acknowledgement flag."""
        if self.TRADING_MODE == "LIVE" and not self.LIVE_TRADING_ACKNOWLEDGED:
            raise ValueError(
                "TRADING_MODE=LIVE requires LIVE_TRADING_ACKNOWLEDGED=true. "
                "Live execution ships in Phase 6; Phases 1-5 gates must pass first."
            )
        return self

    @model_validator(mode="after")
    def _warn_on_exposed_binding(self) -> Settings:
        """Flag network exposure without authentication.

        Not fatal, because a deliberate LAN bind behind a reverse proxy is a
        legitimate deployment. But it is loud, because the default endpoints
        return cash balances and open positions.
        """
        public_bind = self.API_HOST not in {"127.0.0.1", "localhost", "::1"}
        if public_bind and not self.API_AUTH_KEY.get_secret_value():
            logger.critical(
                "SECURITY: API bound to %s with no API_AUTH_KEY. Account balances, "
                "positions and order history are readable by anyone who can reach "
                "this host. Set API_AUTH_KEY or bind to 127.0.0.1.",
                self.API_HOST,
            )
        if "*" in self.cors_origin_list and self.API_AUTH_KEY.get_secret_value():
            logger.warning(
                "CORS_ORIGINS='*' with API_AUTH_KEY set: any origin may attempt "
                "authenticated requests. Prefer an explicit origin list."
            )
        return self

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def credentials_present(self) -> bool:
        """True when all four Angel One credentials are non-empty."""
        return all(
            (
                self.ANGEL_API_KEY.get_secret_value().strip(),
                self.ANGEL_CLIENT_ID.get_secret_value().strip(),
                self.ANGEL_PASSWORD.get_secret_value().strip(),
                self.ANGEL_TOTP_SECRET.get_secret_value().strip(),
            )
        )

    @property
    def missing_credentials(self) -> list[str]:
        """Names of unset credential variables, for actionable error messages."""
        fields = {
            "ANGEL_API_KEY": self.ANGEL_API_KEY,
            "ANGEL_CLIENT_ID": self.ANGEL_CLIENT_ID,
            "ANGEL_PASSWORD": self.ANGEL_PASSWORD,
            "ANGEL_TOTP_SECRET": self.ANGEL_TOTP_SECRET,
        }
        return [name for name, value in fields.items() if not value.get_secret_value().strip()]

    @property
    def upstox_credentials_configured(self) -> bool:
        """True only when all three Upstox credentials are non-empty.

        Used to short-circuit :class:`core.upstox_client.UpstoxClient` calls to
        ``UPSTOX_AUTH_UNAVAILABLE`` rather than attempting a doomed request.
        Upstox is a historical-data source for research only; nothing in this
        property gates Angel One, which remains the live-trading broker.
        """
        return all(
            (
                self.UPSTOX_CLIENT_ID.get_secret_value().strip(),
                self.UPSTOX_CLIENT_SECRET.get_secret_value().strip(),
                self.UPSTOX_ACCESS_TOKEN.get_secret_value().strip(),
            )
        )

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS_ORIGINS parsed into a list; empty list disables CORS."""
        raw = self.CORS_ORIGINS.strip()
        if not raw:
            return []
        return [origin.strip() for origin in raw.split(",") if origin.strip()]

    @property
    def auth_enabled(self) -> bool:
        """True when an X-API-Key is required on protected endpoints."""
        return bool(self.API_AUTH_KEY.get_secret_value().strip())

    @property
    def preferred_underlyings_list(self) -> list[str]:
        """PREFERRED_UNDERLYINGS parsed into an ordered list, first = tried
        first. Empty list means no preference.
        """
        raw = self.PREFERRED_UNDERLYINGS.strip()
        if not raw:
            return []
        return [u.strip().upper() for u in raw.split(",") if u.strip()]

    @property
    def log_dir_path(self) -> Path:
        """Absolute log directory, created on access."""
        path = Path(self.LOG_DIR)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def data_dir_path(self) -> Path:
        """Absolute data directory for the instrument master cache and the
        recorder database. Respects ``DATA_DIR`` exactly like ``log_dir_path``
        respects ``LOG_DIR``, so tests (and any future deployment target with
        its own data volume) can isolate it from the real project directory.
        """
        path = Path(self.DATA_DIR)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def proxies(self) -> dict[str, str]:
        """Requests-style proxy mapping, empty when no proxy is configured."""
        proxy = self.ANGEL_HTTPS_PROXY.get_secret_value().strip()
        return {"https": proxy, "http": proxy} if proxy else {}

    @property
    def risk_amount_per_trade(self) -> float:
        """Rupee risk budget for a single trade."""
        return round(self.TOTAL_CAPITAL * self.RISK_PER_TRADE_PCT / 100.0, 2)

    @property
    def max_daily_loss_amount(self) -> float:
        """Rupee daily loss cap."""
        return round(self.TOTAL_CAPITAL * self.MAX_DAILY_LOSS_PCT / 100.0, 2)

    @property
    def max_weekly_loss_amount(self) -> float:
        """Rupee weekly loss cap."""
        return round(self.TOTAL_CAPITAL * self.MAX_WEEKLY_LOSS_PCT / 100.0, 2)

    # ------------------------------------------------------------------
    # Redacted export
    # ------------------------------------------------------------------

    def safe_dump(self) -> dict[str, Any]:
        """Return configuration with every secret removed.

        Safe for logs and HTTP responses. Credential *presence* is reported as
        a boolean so operators can diagnose setup without seeing values.

        Returns:
            Redacted configuration mapping.
        """
        return {
            "credentials_present": self.credentials_present,
            "missing_credentials": self.missing_credentials,
            "api_host": self.API_HOST,
            "api_port": self.API_PORT,
            "auth_enabled": self.auth_enabled,
            "cors_origins": self.cors_origin_list,
            "cache_update_interval": self.CACHE_UPDATE_INTERVAL,
            "cache_stale_threshold": self.CACHE_STALE_THRESHOLD,
            "cache_candle_bars": self.CACHE_CANDLE_BARS,
            "log_level": self.LOG_LEVEL,
            "trading_mode": self.TRADING_MODE,
            "live_trading_acknowledged": self.LIVE_TRADING_ACKNOWLEDGED,
            "autonomy_level": self.AUTONOMY_LEVEL,
            "full_auto_unlock_token_set": bool(
                self.FULL_AUTO_UNLOCK_TOKEN.get_secret_value().strip()
            ),
            "risk": {
                "total_capital": self.TOTAL_CAPITAL,
                "is_micro_account": self._is_micro_account,
                "risk_per_trade_pct": self.RISK_PER_TRADE_PCT,
                "risk_amount_per_trade": self.risk_amount_per_trade,
                "max_daily_loss_pct": self.MAX_DAILY_LOSS_PCT,
                "max_daily_loss_amount": self.max_daily_loss_amount,
                "max_weekly_loss_pct": self.MAX_WEEKLY_LOSS_PCT,
                "max_weekly_loss_amount": self.max_weekly_loss_amount,
                "max_open_positions": self.MAX_OPEN_POSITIONS,
                "max_trades_per_day": self.MAX_TRADES_PER_DAY,
                "max_trades_per_week": self.MAX_TRADES_PER_WEEK,
                "max_consecutive_losses": self.MAX_CONSECUTIVE_LOSSES,
                "risk_profile": self.RISK_PROFILE,
                "trading_style": self.TRADING_STYLE,
            },
            "kpi": {
                "weekly_target_pct": self.WEEKLY_TARGET_PCT,
                "preferred_underlyings": self.preferred_underlyings_list,
            },
            "compliance": {
                "order_rate_limit_per_sec": self.ORDER_RATE_LIMIT_PER_SEC,
                "order_tag_prefix": self.ORDER_TAG_PREFIX,
            },
            "option_pricing": {
                "risk_free_rate": self.RISK_FREE_RATE,
            },
            "signals": {
                "min_chain_coverage": self.SIGNAL_MIN_CHAIN_COVERAGE,
                "max_bid_ask_spread_pct": self.SIGNAL_MAX_BID_ASK_SPREAD_PCT,
                "min_risk_reward": self.SIGNAL_MIN_RISK_REWARD,
                "min_days_to_expiry": self.SIGNAL_MIN_DAYS_TO_EXPIRY,
                "max_days_to_expiry": self.SIGNAL_MAX_DAYS_TO_EXPIRY,
                "max_cost_drag_pct": self.SIGNAL_MAX_COST_DRAG_PCT,
                "max_width_steps": self.SIGNAL_MAX_WIDTH_STEPS,
                "price_source": self.SIGNAL_PRICE_SOURCE,
                "min_leg_volume": self.SIGNAL_MIN_LEG_VOLUME,
                "min_leg_open_interest": self.SIGNAL_MIN_LEG_OPEN_INTEREST,
                "enable_unbacktested": self.SIGNAL_ENABLE_UNBACKTESTED,
            },
            "rate_limits": {
                "quote": self.RATE_LIMIT_QUOTE,
                "ltp": self.RATE_LIMIT_LTP,
                "candle": self.RATE_LIMIT_CANDLE,
                "account": self.RATE_LIMIT_ACCOUNT,
                "order": self.RATE_LIMIT_ORDER,
            },
            "proxy_configured": bool(self.proxies),
            "registered_static_ip": self.REGISTERED_STATIC_IP or None,
            "upstox_credentials_configured": self.upstox_credentials_configured,
            "upstox_redirect_uri": self.UPSTOX_REDIRECT_URI or None,
        }

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"Settings(mode={self.TRADING_MODE}, credentials_present={self.credentials_present})"

    __str__ = __repr__


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache configuration for the process lifetime.

    Returns:
        The validated :class:`Settings` singleton.

    Raises:
        ConfigurationError: If validation fails. The message lists every
            problem found and never contains a secret value.

    Example:
        >>> settings = get_settings()
        >>> settings.TRADING_MODE in {"ANALYSIS", "PAPER", "LIVE"}
        True
    """
    try:
        return Settings()
    except Exception as exc:  # noqa: BLE001 - surfaced as ConfigurationError
        raise ConfigurationError(
            "Configuration invalid. Copy .env.example to .env and review the " f"following: {exc}"
        ) from exc


def reset_settings_cache() -> None:
    """Clear the cached singleton. Test-support only.

    Tolerates ``get_settings`` having been monkeypatched to a plain function,
    which is the normal case in tests and would otherwise raise
    :class:`AttributeError` during teardown.
    """
    clear = getattr(get_settings, "cache_clear", None)
    if callable(clear):
        clear()
