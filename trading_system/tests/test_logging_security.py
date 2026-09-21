"""Tests for third-party logger security.

These tests guard against the regression that actually happened: on
2026-09-08, ``SmartConnect.__init__`` (in the third-party SmartAPI SDK) called
``logzero.logfile(path, loglevel=ERROR)`` on session construction, which
attached a brand-new file handler directly to logzero's own logger. That
handler was never tagged with this repository's :data:`RedactionFilter`, so a
live bearer JWT, API key, and MAC address reached plaintext disk the next time
the SDK logged a failed request.

:mod:`tests.test_safety_invariants` already covers the filter's own pattern
matching in isolation (``TestCredentialRedaction``). This file covers the part
that actually failed: whether a *third-party logger*, with handlers this
repository does not control and that can be attached after the fact, is
provably unable to leak a credential to a file this repository owns.
"""

from __future__ import annotations

import logging
import time

import pytest

from utils.logging_config import (
    RedactionFilter,
    redaction_filter,
    register_secrets,
    secure_third_party_loggers,
    setup_logging,
)

FAKE_JWT = "eyJhbGciOiJIUzUxMiJ9.eyJmYWtlIjoicGF5bG9hZCJ9.c2lnbmF0dXJlLXBvcnRpb24tZm9yLXRlc3Rpbmc"
FAKE_API_KEY = "OVrdNfkx"
FAKE_MAC = "94:bb:43:1b:dc:90"
FAKE_PASSWORD = "hunter2-correct-battery-staple"


@pytest.fixture(autouse=True)
def _clean_loggers():
    """Isolate the loggers this module manipulates.

    ``secure_third_party_loggers`` attaches a filter idempotently and never
    removes one, so a leftover filter or handler from a previous test (or from
    the frozen ``redaction_filter`` singleton's registered literals) must not
    leak between tests.
    """
    touched = ("logzero_default", "logzero", "SmartApi", "smartapi", "some.sdk.logger")
    saved_filters = {name: list(logging.getLogger(name).filters) for name in touched}
    saved_handlers = {name: list(logging.getLogger(name).handlers) for name in touched}
    saved_literals = set(redaction_filter._literals)
    yield
    for name in touched:
        logger = logging.getLogger(name)
        logger.filters = saved_filters[name]
        for handler in list(logger.handlers):
            if handler not in saved_handlers[name]:
                logger.removeHandler(handler)
                handler.close()
    redaction_filter._literals = saved_literals


class TestThirdPartyLoggerIsSecured:
    """The exact failure mode: a handler attached after securing runs."""

    def test_handler_attached_after_securing_still_gets_redacted(self, tmp_path) -> None:
        """Reproduces the incident: logfile() is called *after* startup secures the logger.

        This is the property a handler-level fix cannot provide: the SDK
        attaches its file handler lazily, inside ``SmartConnect.__init__``,
        which runs long after ``setup_logging()``. A filter attached to a
        handler this repository creates would never see that handler. A
        filter attached to the *logger* sees every handler the logger ever
        has, including ones attached later by code this repository does not
        control.
        """
        setup_logging(log_level="INFO", log_dir=str(tmp_path / "logs"), console=False)
        secure_third_party_loggers()

        sdk_logger = logging.getLogger("logzero_default")

        # Simulate logzero.logfile(): a handler attached well after securing.
        late_log = tmp_path / "sdk_late.log"
        late_handler = logging.FileHandler(late_log, encoding="utf-8")
        late_handler.setLevel(logging.ERROR)
        sdk_logger.addHandler(late_handler)

        sdk_logger.error(
            "POST failed. Headers: %s",
            {"Authorization": f"Bearer {FAKE_JWT}", "X-PrivateKey": FAKE_API_KEY},
        )
        late_handler.flush()

        content = late_log.read_text()
        assert FAKE_JWT not in content
        assert "REDACTED" in content

    def test_logzero_default_is_the_real_default_logger_name(self) -> None:
        """Pin the name so a future refactor cannot silently unpin it.

        The previous version of this module secured a logger literally named
        ``"logzero"``, which does not exist as logzero configures it; the real
        name is ``"logzero_default"``. That mismatch is exactly why the
        incident's handler went unfiltered.
        """
        import logzero

        assert logzero.logger.name == "logzero_default"

    def test_securing_is_idempotent(self, tmp_path) -> None:
        """Calling it from every entry point that might construct a broker
        session must not accumulate duplicate filters or duplicate redaction.
        """
        setup_logging(log_level="INFO", log_dir=str(tmp_path / "logs"), console=False)
        for _ in range(5):
            secure_third_party_loggers()

        sdk_logger = logging.getLogger("logzero_default")
        count = sum(1 for f in sdk_logger.filters if isinstance(f, RedactionFilter))
        assert count == 1

    def test_secured_before_any_setup_logging_call(self, tmp_path) -> None:
        """core.angel_client calls this directly, in case a script never calls
        setup_logging() at all. It must work standalone.
        """
        logger = logging.getLogger("SmartApi")
        secure_third_party_loggers()

        out = tmp_path / "standalone.log"
        handler = logging.FileHandler(out, encoding="utf-8")
        logger.addHandler(handler)
        logger.error("password=%s", FAKE_PASSWORD)
        handler.flush()

        content = out.read_text()
        assert FAKE_PASSWORD not in content
        logger.removeHandler(handler)
        handler.close()


class TestFullIncidentReplay:
    """End-to-end: the literal sequence of calls that produced the 2026-09-08 leak."""

    def test_smartconnect_style_logfile_call_cannot_leak(self, tmp_path, monkeypatch) -> None:
        """Reconstructs SmartConnect.__init__'s own logging setup and proves a
        subsequent error carrying a real-shaped Authorization header, API key,
        and MAC address cannot reach the resulting file unredacted.
        """
        logzero = pytest.importorskip("logzero")

        monkeypatch.chdir(tmp_path)
        setup_logging(log_level="INFO", log_dir="logs", console=False)
        register_secrets(FAKE_API_KEY, FAKE_MAC)
        secure_third_party_loggers()

        # Exactly what SmartConnect.__init__ does (SmartApi/smartConnect.py):
        log_folder = "logs/" + time.strftime("%Y-%m-%d")
        import os

        os.makedirs(log_folder, exist_ok=True)
        log_path = os.path.join(log_folder, "app.log")
        logzero.logfile(log_path, loglevel=logging.ERROR)

        headers = {
            "Authorization": f"Bearer {FAKE_JWT}",
            "X-PrivateKey": FAKE_API_KEY,
            "X-MACAddress": FAKE_MAC,
        }
        logzero.logger.error("Error occurred while making a POST request. Headers: %s", headers)

        content = open(log_path, encoding="utf-8").read()
        assert FAKE_JWT not in content
        assert FAKE_API_KEY not in content
        assert FAKE_MAC not in content
        assert "REDACTED" in content


class TestRepositoryLoggerStillRedacts:
    """The baseline the incident tests build on: this module's own path."""

    @pytest.mark.parametrize(
        "message",
        [
            'Authorization: Bearer %s' % FAKE_JWT,
            "api_key=%s" % FAKE_API_KEY,
            'password: "%s"' % FAKE_PASSWORD,
            "refresh_token=%s" % FAKE_JWT,
        ],
    )
    def test_repository_logger_redacts_every_credential_shape(
        self, tmp_path, message: str
    ) -> None:
        setup_logging(log_level="INFO", log_dir=str(tmp_path / "logs"), console=False)
        logger = logging.getLogger("tests.repo_logger_check")
        logger.error(message)

        content = (tmp_path / "logs" / "app.log").read_text()
        assert FAKE_JWT not in content
        assert FAKE_PASSWORD not in content
