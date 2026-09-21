"""Tests for the invariants that protect capital.

These are not coverage-driven. Each test guards a specific failure mode that
would cause real financial loss if it regressed:

* A missing price coerced to ``0.0`` would produce a confident, wrong lot count.
* A credential reaching a log file is an unrecoverable disclosure.
* A risk ceiling above the charter limit would authorise oversized positions.
* Stale data passing the freshness gate would allow signals on dead prices.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest

from config.settings import Settings
from core.cache_manager import MarketCache
from core.data_fetcher import _pct_change, _to_float, _to_int
from core.exceptions import CacheStaleError
from core.market_hours import IST, classify_session, is_market_open, poll_interval_seconds
from utils.formatters import fmt_indian_digits, fmt_pct, fmt_price
from utils.logging_config import RedactionFilter


class TestNoFabricatedData:
    """A missing value must never become a number."""

    @pytest.mark.parametrize(
        "raw",
        ["", " ", "-", "--", "NA", "N/A", "null", "None", "nan", None, "abc", "12.3.4"],
    )
    def test_unparseable_input_returns_none_not_zero(self, raw: object) -> None:
        """Unusable input yields None, so it cannot be mistaken for a real price."""
        assert _to_float(raw) is None
        assert _to_int(raw) is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("24500.50", 24500.5), ("1,24,500.50", 124500.5), (0, 0.0), ("0", 0.0), (-5.5, -5.5)],
    )
    def test_valid_numbers_parse(self, raw: object, expected: float) -> None:
        """Genuine values parse, including a real zero."""
        assert _to_float(raw) == expected

    def test_real_zero_is_preserved(self) -> None:
        """A true zero is data and must survive; only absence becomes None."""
        assert _to_float("0") == 0.0
        assert _to_float("0") is not None

    def test_booleans_rejected(self) -> None:
        """bool is an int subclass in Python; treating True as 1.0 would be wrong."""
        assert _to_float(True) is None
        assert _to_float(False) is None

    def test_nan_rejected(self) -> None:
        """NaN propagates silently through arithmetic, so it is refused."""
        assert _to_float(float("nan")) is None

    def test_pct_change_needs_both_operands(self) -> None:
        """Percent change is None unless both inputs exist and the base is non-zero."""
        assert _pct_change(None, 100.0) is None
        assert _pct_change(100.0, None) is None
        assert _pct_change(100.0, 0.0) is None
        assert _pct_change(101.0, 100.0) == 1.0

    def test_formatters_render_absence_explicitly(self) -> None:
        """Formatted output must say 'no data', never '0.00'."""
        assert fmt_price(None) == "no data"
        assert fmt_pct(None) == "no data"
        assert fmt_price(0.0) == "0.00"

    def test_partial_quote_renders_missing_fields_as_no_data(self) -> None:
        """A quote with a price but no OHLC must not show zeros for the gaps.

        This is the realistic failure mode: the broker returns LTP but omits
        depth or previous close. Rendering those as 0.00 would look like a real
        limit-down print.
        """
        from utils.formatters import format_market

        rendered = "\n".join(
            format_market(
                {
                    "NIFTY": {
                        "display_name": "NIFTY 50",
                        "ltp": 24500.0,
                        "open": None,
                        "high": None,
                        "low": None,
                        "previous_close": None,
                        "change": None,
                        "change_pct": None,
                        "volume": None,
                    }
                }
            )
        )
        # The real price is shown with Indian grouping.
        assert "24,500.00" in rendered
        # Every absent field is named as absent. Asserting on the whole OHLC line
        # rather than on "0.00" matters, because a legitimate price such as
        # 24,500.00 contains "0.00" as a substring.
        assert "O no data  H no data  L no data  PC no data" in rendered
        assert "Volume no data" in rendered


class TestCredentialRedaction:
    """Credentials must not reach a log record."""

    @pytest.fixture
    def filt(self) -> RedactionFilter:
        return RedactionFilter()

    def _record(self, message: str, *args: object) -> logging.LogRecord:
        return logging.LogRecord("t", logging.INFO, __file__, 1, message, args, None)

    @pytest.mark.parametrize(
        "message",
        [
            "password=hunter2secret",
            "password: hunter2secret",
            'password="hunter2secret"',
            "api_key=abcd1234efgh",
            "totp_secret=JBSWY3DPEHPK3PXP",
            "Authorization: Bearer abcdef123456789",
            'jwtToken: "eyJhbGciOiJIUzI1NiJ9.payload.signature"',
        ],
    )
    def test_credential_patterns_are_scrubbed(self, filt: RedactionFilter, message: str) -> None:
        """Key-value credential shapes are redacted."""
        record = self._record(message)
        filt.filter(record)
        rendered = record.getMessage()
        assert "***REDACTED***" in rendered
        for leak in ("hunter2secret", "abcd1234efgh", "JBSWY3DPEHPK3PXP", "abcdef123456789"):
            assert leak not in rendered

    def test_registered_literal_scrubbed_without_a_key(self, filt: RedactionFilter) -> None:
        """A bare secret with no identifying key is still removed."""
        filt.register_secret("SuperSecretApiKey123")
        record = self._record("Connecting with SuperSecretApiKey123 now")
        filt.filter(record)
        assert "SuperSecretApiKey123" not in record.getMessage()

    def test_lazy_format_args_are_scrubbed(self, filt: RedactionFilter) -> None:
        """%s-style args are the most common leak path and must be covered."""
        filt.register_secret("LeakedValue987")
        record = self._record("token is %s", "LeakedValue987")
        filt.filter(record)
        assert "LeakedValue987" not in record.getMessage()

    def test_proxy_url_password_scrubbed(self, filt: RedactionFilter) -> None:
        """Credentials embedded in a URL are removed but the host is kept."""
        record = self._record("proxy https://user:MyProxyPass@proxy.example.com:9293")
        filt.filter(record)
        rendered = record.getMessage()
        assert "MyProxyPass" not in rendered
        assert "proxy.example.com" in rendered

    def test_short_values_ignored(self, filt: RedactionFilter) -> None:
        """Very short literals would corrupt unrelated text, so they are skipped."""
        filt.register_secret("ab")
        record = self._record("about to fetch data")
        filt.filter(record)
        assert record.getMessage() == "about to fetch data"


class TestRiskCeilings:
    """Charter ceilings must be enforced at startup, not at trade time."""

    BASE = {
        "ANGEL_API_KEY": "k",
        "ANGEL_CLIENT_ID": "c",
        "ANGEL_PASSWORD": "p",
        "ANGEL_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
        "_env_file": None,
    }

    def test_within_limits_accepted(self) -> None:
        """A compliant configuration loads."""
        settings = Settings(
            **self.BASE, RISK_PER_TRADE_PCT=1.0, MAX_DAILY_LOSS_PCT=2.0, MAX_WEEKLY_LOSS_PCT=5.0
        )
        assert settings.risk_amount_per_trade == 1000.0

    def test_per_trade_ceiling_enforced(self) -> None:
        """Above 2% per trade is rejected."""
        with pytest.raises(ValueError, match="per-trade ceiling"):
            Settings(**self.BASE, RISK_PER_TRADE_PCT=2.5)

    def test_daily_ceiling_enforced(self) -> None:
        """Above 5% daily is rejected."""
        with pytest.raises(ValueError, match="daily ceiling"):
            Settings(**self.BASE, MAX_DAILY_LOSS_PCT=6.0, MAX_WEEKLY_LOSS_PCT=8.0)

    def test_weekly_ceiling_enforced(self) -> None:
        """Above 10% weekly is rejected."""
        with pytest.raises(ValueError, match="weekly ceiling"):
            Settings(**self.BASE, MAX_WEEKLY_LOSS_PCT=15.0)

    def test_incoherent_limits_rejected(self) -> None:
        """A single trade must not be able to breach the daily cap."""
        with pytest.raises(ValueError, match="single"):
            Settings(**self.BASE, RISK_PER_TRADE_PCT=1.5, MAX_DAILY_LOSS_PCT=1.0)

    def test_live_mode_requires_acknowledgement(self) -> None:
        """LIVE without the explicit flag is refused."""
        with pytest.raises(ValueError, match="LIVE_TRADING_ACKNOWLEDGED"):
            Settings(**self.BASE, TRADING_MODE="LIVE", LIVE_TRADING_ACKNOWLEDGED=False)

    def test_micro_account_ceiling_widens_below_threshold(self) -> None:
        """A ₹20,000 account cannot express the standard 2% ceiling and trade.

        The two-tier exception must actually accept the values a small
        account needs, not just theoretically permit them.
        """
        settings = Settings(
            **self.BASE,
            TOTAL_CAPITAL=20_000,
            RISK_PER_TRADE_PCT=10.0,
            MAX_DAILY_LOSS_PCT=12.0,
            MAX_WEEKLY_LOSS_PCT=20.0,
            MAX_OPEN_POSITIONS=2,
        )
        assert settings.risk_amount_per_trade == 2000.0

    def test_micro_account_ceiling_is_not_unlimited(self) -> None:
        """The widened tier still has a ceiling of its own."""
        with pytest.raises(ValueError, match="per-trade ceiling"):
            Settings(**self.BASE, TOTAL_CAPITAL=20_000, RISK_PER_TRADE_PCT=16.0)

    def test_micro_account_open_positions_ceiling_enforced(self) -> None:
        """A small account is also capped on concurrent positions."""
        with pytest.raises(ValueError, match="position.*ceiling"):
            Settings(**self.BASE, TOTAL_CAPITAL=20_000, MAX_OPEN_POSITIONS=3)

    def test_ceiling_reverts_to_standard_above_the_micro_threshold(self) -> None:
        """Adding capital past the threshold snaps the ceiling back down."""
        with pytest.raises(ValueError, match="per-trade ceiling"):
            Settings(**self.BASE, TOTAL_CAPITAL=50_001, RISK_PER_TRADE_PCT=10.0)

    def test_trades_per_day_above_per_week_rejected(self) -> None:
        """A daily cap the weekly cap could never actually bind against."""
        with pytest.raises(ValueError, match="MAX_TRADES_PER_WEEK"):
            Settings(**self.BASE, MAX_TRADES_PER_DAY=10, MAX_TRADES_PER_WEEK=5)

    def test_full_auto_rejected_without_live_mode(self) -> None:
        """FULL_AUTO cannot be reached by flipping one setting."""
        with pytest.raises(ValueError, match="TRADING_MODE must be LIVE"):
            Settings(**self.BASE, AUTONOMY_LEVEL="FULL_AUTO")

    def test_full_auto_rejected_without_unlock_token(self) -> None:
        """LIVE + acknowledged is still not enough without the gate's token."""
        with pytest.raises(ValueError, match="FULL_AUTO_UNLOCK_TOKEN"):
            Settings(
                **self.BASE,
                AUTONOMY_LEVEL="FULL_AUTO",
                TRADING_MODE="LIVE",
                LIVE_TRADING_ACKNOWLEDGED=True,
            )

    def test_full_auto_accepted_when_fully_satisfied(self) -> None:
        """Every precondition met: the configuration loads."""
        settings = Settings(
            **self.BASE,
            AUTONOMY_LEVEL="FULL_AUTO",
            TRADING_MODE="LIVE",
            LIVE_TRADING_ACKNOWLEDGED=True,
            FULL_AUTO_UNLOCK_TOKEN="issued-by-gate-script",
        )
        assert settings.AUTONOMY_LEVEL == "FULL_AUTO"

    def test_safe_dump_never_exposes_the_unlock_token(self) -> None:
        """Presence is reported; the token value itself is not."""
        settings = Settings(
            **self.BASE,
            AUTONOMY_LEVEL="FULL_AUTO",
            TRADING_MODE="LIVE",
            LIVE_TRADING_ACKNOWLEDGED=True,
            FULL_AUTO_UNLOCK_TOKEN="super-secret-token-value",
        )
        dumped = settings.safe_dump()
        assert dumped["full_auto_unlock_token_set"] is True
        assert "super-secret-token-value" not in repr(dumped)

    def test_totp_rejects_six_digit_code(self) -> None:
        """Pasting the rotating code instead of the seed is the common setup error."""
        payload = {**self.BASE, "ANGEL_TOTP_SECRET": "123456"}
        with pytest.raises(ValueError, match="6-digit"):
            Settings(**payload)

    def test_totp_rejects_non_base32(self) -> None:
        """Characters outside the base32 alphabet indicate a mistyped seed."""
        payload = {**self.BASE, "ANGEL_TOTP_SECRET": "JBSWY3DPEHPK3PX!"}
        with pytest.raises(ValueError, match="base32"):
            Settings(**payload)

    def test_secrets_absent_from_safe_dump(self) -> None:
        """Redacted export must not leak any credential value."""
        settings = Settings(**self.BASE)
        dumped = repr(settings.safe_dump())
        assert "JBSWY3DPEHPK3PXP" not in dumped
        assert settings.safe_dump()["credentials_present"] is True

    def test_repr_does_not_leak(self) -> None:
        """An unhandled traceback prints repr, so it must be safe."""
        settings = Settings(**self.BASE)
        assert "JBSWY3DPEHPK3PXP" not in repr(settings)


class TestStalenessGate:
    """Analysis must be blocked on stale data."""

    def test_empty_cache_blocks_analysis(self) -> None:
        """A cache that never updated is not fresh."""
        cache = MarketCache(stale_threshold=60)
        assert cache.is_stale
        assert cache.age_seconds is None
        with pytest.raises(CacheStaleError):
            cache.assert_fresh()

    def test_fresh_cache_permits_analysis(self) -> None:
        """A just-updated cache passes the gate."""
        cache = MarketCache(stale_threshold=60)
        cache.mark_update(success=True)
        cache.assert_fresh()
        assert not cache.is_stale

    def test_aged_cache_blocks_analysis(self) -> None:
        """Data past the threshold raises rather than being served to analysis."""
        cache = MarketCache(stale_threshold=30)
        cache.mark_update(success=True)
        cache._last_success = datetime.now(IST) - timedelta(seconds=120)
        assert cache.is_stale
        with pytest.raises(CacheStaleError) as caught:
            cache.assert_fresh()
        assert caught.value.age_seconds is not None
        assert caught.value.age_seconds > 30

    def test_status_promotes_to_stale(self) -> None:
        """A connected cache reports STALE once its data ages out."""
        cache = MarketCache(stale_threshold=10)
        cache.mark_update(success=True)
        cache.set_status("CONNECTED")
        assert cache.status == "CONNECTED"
        cache._last_success = datetime.now(IST) - timedelta(seconds=60)
        assert cache.status == "STALE"

    def test_failed_section_preserves_previous_value(self) -> None:
        """One failing section must not blank the others."""
        cache = MarketCache()
        cache.update_section("market", {"NIFTY": {"ltp": 24500.0}})
        cache.update_section("market", None, error="quote batch failed")
        snapshot = cache.snapshot()
        assert snapshot["market"]["NIFTY"]["ltp"] == 24500.0
        assert "market" in snapshot["metadata"]["section_errors"]

    def test_snapshot_is_isolated_from_cache(self) -> None:
        """A caller mutating a snapshot must not corrupt cached state."""
        cache = MarketCache()
        cache.update_section("market", {"NIFTY": {"ltp": 24500.0}})
        snapshot = cache.snapshot()
        snapshot["market"]["NIFTY"]["ltp"] = 99999.0
        assert cache.snapshot()["market"]["NIFTY"]["ltp"] == 24500.0


class TestSessionClock:
    """Session detection drives poll rate and candle fetching."""

    def test_weekend_is_closed(self) -> None:
        """Saturday is not a session."""
        saturday = datetime(2026, 8, 22, 11, 0, tzinfo=IST)
        assert not is_market_open(saturday)
        assert classify_session(saturday) == "CLOSED_WEEKEND"

    def test_known_holiday_is_closed(self) -> None:
        """Republic Day 2026 falls on a Monday, so the holiday rule is what fires.

        The date matters: a holiday that lands on a weekend is reported as
        CLOSED_WEEKEND, since the weekday check runs first. Picking a weekend
        holiday here would assert nothing about holiday handling.
        """
        holiday = datetime(2026, 1, 26, 11, 0, tzinfo=IST)
        assert holiday.weekday() < 5, "test date must be a weekday"
        assert not is_market_open(holiday)
        assert classify_session(holiday) == "CLOSED_HOLIDAY"

    def test_session_boundaries(self) -> None:
        """09:15 and 15:30 are inclusive; 09:14 and 15:31 are not."""
        day = datetime(2026, 8, 20, tzinfo=IST)  # a Thursday, not a listed holiday
        assert is_market_open(day.replace(hour=9, minute=15))
        assert is_market_open(day.replace(hour=15, minute=30))
        assert not is_market_open(day.replace(hour=9, minute=14))
        assert not is_market_open(day.replace(hour=15, minute=31))

    def test_pre_open_classified(self) -> None:
        """The 09:00-09:15 call auction is distinct from the continuous session."""
        moment = datetime(2026, 8, 20, 9, 5, tzinfo=IST)
        assert classify_session(moment) == "PRE_OPEN"
        assert not is_market_open(moment)

    def test_poll_rate_follows_session(self) -> None:
        """Fast polling during the session, slow outside it."""
        assert poll_interval_seconds(5, 60, datetime(2026, 8, 20, 11, 0, tzinfo=IST)) == 5
        assert poll_interval_seconds(5, 60, datetime(2026, 8, 22, 11, 0, tzinfo=IST)) == 60


class TestIndianNumberFormat:
    """Figures are cross-checked against a broker terminal, so grouping matters."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (100000, "1,00,000.00"),
            (1000, "1,000.00"),
            (999, "999.00"),
            (12345678.5, "1,23,45,678.50"),
            (-4500, "-4,500.00"),
            (0, "0.00"),
        ],
    )
    def test_lakh_crore_grouping(self, value: float, expected: str) -> None:
        """Grouping follows the Indian convention, not the Western one."""
        assert fmt_indian_digits(value) == expected
