"""Regression test: the collector's actual startup path never logs a credential.

``tests/test_logging_security.py`` proves the redaction filter's own pattern
matching in isolation. ``tests/test_angel_client.py`` proves
``AngelClient.connect()``'s path is redacted, in isolation from the recorder.

Neither drives the sequence ``scripts/record_chains.py`` actually runs at
startup: ``register_secrets()`` with the real configured values, then
``AngelClient.connect()`` (which calls ``secure_third_party_loggers()``
defensively right before constructing ``SmartConnect`` -- see
``core/angel_client.py``), followed by the recorder actually producing log
output during a capture cycle. This test drives exactly that sequence, with a
fake ``SmartApi`` module standing in for the broker SDK (same technique as
``tests/test_angel_client.py``) so no real broker is ever contacted, and
inspects every log record produced -- plus the on-disk log file -- for a fake
credential planted specifically to be leaked if any step here skipped
redaction.
"""

from __future__ import annotations

import logging
import sys
import types

import pytest

from config.settings import Settings
from core.angel_client import AngelClient
from core.chain_recorder import ChainRecorder, RecorderConfig
from core.option_store import OptionChainStore
from core.replay_fetcher import ReplayFetcher
from utils.logging_config import register_secrets, setup_logging

#: Deliberately shaped like real Angel One values but never real. Long and
#: distinctive enough that an accidental substring match elsewhere is not a
#: concern, and long enough to pass RedactionFilter.register_secret's 4-char
#: floor.
CANARY_API_KEY = "CANARY-API-KEY-7f3a9c21"
CANARY_PASSWORD = "CANARY-PASSWORD-hunter2xyz"
CANARY_CLIENT_ID = "CANARYCLIENT01"
#: A syntactically valid base32 TOTP seed (RFC 4648 test vector); Settings'
#: own validator accepts it and the fake SDK never checks it.
VALID_TOTP_SEED = "JBSWY3DPEHPK3PXP"


class LeakingFakeSmartConnect:
    """Reproduces the actual 2026-09-08 incident: the SDK logs the raw request,
    credentials included, through its own logger. Standing in for
    ``SmartConnect`` here means this test fails loudly if
    ``secure_third_party_loggers()`` is ever skipped on the collector's
    startup path, rather than passing vacuously because nothing tried to leak
    anything.
    """

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.proxies = None
        logging.getLogger("logzero_default").error("SDK init with api_key=%s", api_key)

    def generateSession(self, client_id: str, password: str, totp: str) -> dict:
        logging.getLogger("logzero_default").error(
            "login attempt client_id=%s password=%s totp=%s", client_id, password, totp
        )
        return {
            "status": True,
            "data": {"jwtToken": "fake.jwt.token", "feedToken": "fake-feed", "refreshToken": "fake-refresh"},
        }

    def terminateSession(self, client_id: str) -> None:
        pass


@pytest.fixture
def fake_smartapi_module(monkeypatch) -> None:
    """Install the leaking fake so `from SmartApi import SmartConnect` succeeds."""
    module = types.ModuleType("SmartApi")
    module.SmartConnect = LeakingFakeSmartConnect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "SmartApi", module)


@pytest.fixture(autouse=True)
def _clean_third_party_loggers():
    """Isolate the loggers this module (and secure_third_party_loggers) touches."""
    touched = ("logzero_default", "logzero", "SmartApi", "smartapi")
    saved_filters = {name: list(logging.getLogger(name).filters) for name in touched}
    saved_handlers = {name: list(logging.getLogger(name).handlers) for name in touched}
    yield
    for name in touched:
        logger = logging.getLogger(name)
        logger.filters = saved_filters[name]
        for handler in list(logger.handlers):
            if handler not in saved_handlers[name]:
                logger.removeHandler(handler)
                handler.close()


def _settings_with_canary_credentials() -> Settings:
    return Settings(
        _env_file=None,
        ANGEL_API_KEY=CANARY_API_KEY,
        ANGEL_CLIENT_ID=CANARY_CLIENT_ID,
        ANGEL_PASSWORD=CANARY_PASSWORD,
        ANGEL_TOTP_SECRET=VALID_TOTP_SEED,
    )


class TestCollectorStartupNeverLeaksCredentials:
    """Drives register_secrets -> AngelClient.connect() -> a replay-mode
    recording cycle -- the collector's real startup shape -- and checks every
    log record and the on-disk log file for the canary credentials.
    """

    def test_full_collector_startup_and_replay_cycle_never_logs_credentials(
        self, fake_smartapi_module, tmp_path, caplog
    ) -> None:
        log_dir = tmp_path / "logs"
        setup_logging(log_level="DEBUG", log_dir=log_dir, console=False)

        settings = _settings_with_canary_credentials()

        # Mirrors scripts/record_chains.py's main() exactly: secrets are
        # registered for redaction before anything touches the broker SDK.
        register_secrets(
            settings.ANGEL_API_KEY.get_secret_value(),
            settings.ANGEL_CLIENT_ID.get_secret_value(),
            settings.ANGEL_PASSWORD.get_secret_value(),
            settings.ANGEL_TOTP_SECRET.get_secret_value(),
        )

        store = OptionChainStore(tmp_path / "replay.db")
        client = AngelClient(settings)
        try:
            with caplog.at_level(logging.DEBUG):
                assert client.connect() is True

                recorder = ChainRecorder(
                    ReplayFetcher(),
                    store,
                    RecorderConfig(underlyings=["NIFTY"], expiries=1, ignore_market_hours=True),
                )
                recorder.record_cycle()
        finally:
            client.disconnect()
            store.close()

        haystack = "\n".join(caplog.messages)
        for secret in (CANARY_API_KEY, CANARY_PASSWORD, CANARY_CLIENT_ID):
            assert secret not in haystack, f"{secret!r} leaked into a log record"

        # The 2026-09-08 incident was specifically a credential reaching a
        # plaintext file a human could open, so the file check matters as
        # much as the in-memory one.
        log_files = list(log_dir.rglob("*.log")) if log_dir.exists() else []
        assert log_files, "expected setup_logging to have created log files"
        for path in log_files:
            content = path.read_text(errors="ignore")
            for secret in (CANARY_API_KEY, CANARY_PASSWORD, CANARY_CLIENT_ID):
                assert secret not in content, f"{secret!r} leaked into {path}"

    def test_the_fake_sdk_actually_attempted_to_leak(self, fake_smartapi_module, tmp_path) -> None:
        """Guards against the test above passing vacuously: confirms the fake
        SDK really did log the raw credential to logzero_default's own
        handlers before redaction is applied, so a clean result above is
        because of redaction and not because nothing was ever logged.
        """
        raw_records: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                raw_records.append(record.getMessage())

        settings = _settings_with_canary_credentials()
        register_secrets(
            settings.ANGEL_API_KEY.get_secret_value(),
            settings.ANGEL_CLIENT_ID.get_secret_value(),
            settings.ANGEL_PASSWORD.get_secret_value(),
            settings.ANGEL_TOTP_SECRET.get_secret_value(),
        )

        # Attach the capture handler BEFORE connect() runs
        # secure_third_party_loggers(), so it sees whatever the logger-level
        # filter lets through -- proving the filter, not handler ordering, is
        # what does the redacting.
        capture_logger = logging.getLogger("logzero_default")
        handler = _Capture()
        capture_logger.addHandler(handler)
        try:
            client = AngelClient(settings)
            assert client.connect() is True
            client.disconnect()
        finally:
            capture_logger.removeHandler(handler)

        assert raw_records, "expected the fake SDK to log at least one record"
        joined = "\n".join(raw_records)
        # The filter mutates record.msg in place before any handler (this one
        # included) ever sees it, so even this direct capture must be clean.
        assert CANARY_PASSWORD not in joined
        assert CANARY_API_KEY not in joined
