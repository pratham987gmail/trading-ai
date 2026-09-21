"""Structured logging with mandatory credential redaction.

Two design decisions worth stating explicitly.

**Redaction is a filter, not a convention.** Relying on developers to remember
not to log a password fails eventually. :class:`RedactionFilter` is attached to
every handler and rewrites records in flight, so a credential cannot reach a log
file even from a third-party library or an unhandled traceback. The SmartAPI SDK
in particular logs request bodies at DEBUG level, and those bodies contain the
password and TOTP.

**Three sinks, different retention needs.** ``app.log`` is the full stream,
``errors.log`` isolates WARNING and above for fast triage, and ``trades.log``
is an append-only audit of order and fill events that must survive log rotation
pressure from chatty debug output.

Example:
    >>> from utils.logging_config import setup_logging, get_logger
    >>> setup_logging(log_level="INFO", console=True)   # doctest: +SKIP
    >>> logger = get_logger(__name__)
    >>> logger.info("password=hunter2")   # written as password=***REDACTED***
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5

REDACTION_PLACEHOLDER = "***REDACTED***"

#: Patterns matched against every formatted log record.
#:
#: Each entry captures the key and separator in group 1 and the secret value in
#: the remainder, so the substitution keeps the key visible for debugging while
#: destroying the value. Ordering matters: JSON-quoted forms are handled before
#: bare ``key=value`` forms.
_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # JSON / dict style: "password": "secret"
    (
        re.compile(
            r'(["\']?(?:password|pwd|pin|mpin|totp|totp_secret|api_key|apikey'
            r"|secret|token|access_token|refresh_token|jwt|jwttoken|feedtoken"
            r'|authorization|auth_key|client_secret|private_key)["\']?\s*[:=]\s*)'
            r'["\']([^"\']{2,})["\']',
            re.IGNORECASE,
        ),
        r"\1" + f'"{REDACTION_PLACEHOLDER}"',
    ),
    # Bearer tokens in headers.
    #
    # This must precede the bare key=value rule below. Otherwise
    # "Authorization: Bearer <token>" matches the bare rule first, which treats
    # the literal word "Bearer" as the secret and leaves the actual token in
    # place. Ordering here is a correctness requirement, not a style choice.
    (
        re.compile(r"\b(Bearer\s+)([A-Za-z0-9._\-]{8,})", re.IGNORECASE),
        r"\1" + REDACTION_PLACEHOLDER,
    ),
    # Any JWT-shaped string, wherever it appears.
    (
        re.compile(r"\beyJ[A-Za-z0-9._\-]{10,}"),
        REDACTION_PLACEHOLDER,
    ),
    # Bare style: password=secret  /  api_key: secret
    #
    # The lookaheads stop this rule from re-processing a value the Bearer rule
    # has already handled, or from redacting the placeholder itself.
    (
        re.compile(
            r"\b((?:password|pwd|pin|mpin|totp|totp_secret|api_key|apikey"
            r"|secret|token|access_token|refresh_token|jwt|jwttoken|feedtoken"
            r"|authorization|auth_key|client_secret|private_key)\s*[:=]\s*)"
            r"(?!Bearer\b)(?!\*\*\*REDACTED)"
            r"([^\s,;}\)\"']{2,})",
            re.IGNORECASE,
        ),
        r"\1" + REDACTION_PLACEHOLDER,
    ),
    # Credentials embedded in a proxy or database URL.
    (
        re.compile(r"(://[^:/\s]+:)([^@/\s]+)(@)"),
        r"\1" + REDACTION_PLACEHOLDER + r"\3",
    ),
)


class RedactionFilter(logging.Filter):
    """Strips credential-shaped substrings from log records.

    Applied to every handler. The filter mutates ``record.msg`` and clears
    ``record.args`` once the message has been rendered, because leaving args in
    place would let a downstream formatter re-expand the unredacted values.

    Registered secret literals are matched verbatim in addition to the pattern
    rules, which covers values that carry no identifying key, such as a raw
    API key logged on its own.
    """

    def __init__(self, name: str = "") -> None:
        """Initialise the filter.

        Args:
            name: Standard :class:`logging.Filter` name.
        """
        super().__init__(name)
        self._literals: set[str] = set()

    def register_secret(self, value: str) -> None:
        """Register a literal value to redact wherever it appears.

        Args:
            value: The secret. Values shorter than 4 characters are ignored,
                since matching them would corrupt unrelated text.
        """
        cleaned = (value or "").strip()
        if len(cleaned) >= 4:
            self._literals.add(cleaned)

    def _scrub(self, text: str) -> str:
        """Apply literal and pattern redaction to a string."""
        for literal in self._literals:
            if literal in text:
                text = text.replace(literal, REDACTION_PLACEHOLDER)
        for pattern, replacement in _REDACTION_PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact the record in place.

        Args:
            record: The record being emitted.

        Returns:
            Always True; records are modified, never dropped.
        """
        try:
            rendered = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string must not kill logging
            rendered = str(record.msg)

        scrubbed = self._scrub(rendered)
        if scrubbed != rendered or record.args:
            record.msg = scrubbed
            record.args = ()

        if record.exc_text:
            record.exc_text = self._scrub(record.exc_text)

        return True


#: Process-wide filter instance, so credentials can be registered once at
#: startup and apply to every handler thereafter.
redaction_filter = RedactionFilter()


class JsonFormatter(logging.Formatter):
    """Renders records as single-line JSON for log shipping."""

    def format(self, record: logging.LogRecord) -> str:
        """Format the record as a JSON object.

        Args:
            record: The record to render.

        Returns:
            A single-line JSON string.
        """
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, DATE_FORMAT),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = record.stack_info

        standard = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)
        standard.update({"message", "asctime", "taskName"})
        for key, value in record.__dict__.items():
            if key not in standard and not key.startswith("_"):
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)

        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(
    log_level: str = "INFO",
    log_dir: Path | str = "logs",
    console: bool = True,
) -> logging.Logger:
    """Configure process-wide logging. Idempotent.

    Installs three rotating file handlers plus an optional console handler,
    each carrying :data:`redaction_filter`.

    Args:
        log_level: Threshold name, e.g. ``"INFO"``.
        log_dir: Directory for log files. Created when absent.
        console: Emit human-readable lines to stdout when True, JSON when
            False.

    Returns:
        The configured root logger.

    Example:
        >>> logger = setup_logging(log_level="DEBUG", console=True)  # doctest: +SKIP
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, log_level.strip().upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    # Idempotency: drop handlers this function installed previously.
    for handler in list(root.handlers):
        if getattr(handler, "_trading_system_handler", False):
            root.removeHandler(handler)
            handler.close()

    text_formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    json_formatter = JsonFormatter()

    def _tag(handler: logging.Handler) -> logging.Handler:
        handler._trading_system_handler = True  # type: ignore[attr-defined]
        handler.addFilter(redaction_filter)
        return handler

    app_handler = logging.handlers.RotatingFileHandler(
        log_path / "app.log", maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    app_handler.setLevel(level)
    app_handler.setFormatter(text_formatter)
    root.addHandler(_tag(app_handler))

    error_handler = logging.handlers.RotatingFileHandler(
        log_path / "errors.log", maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(text_formatter)
    root.addHandler(_tag(error_handler))

    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(text_formatter)
        root.addHandler(_tag(console_handler))
    else:
        json_handler = logging.StreamHandler(sys.stdout)
        json_handler.setLevel(level)
        json_handler.setFormatter(json_formatter)
        root.addHandler(_tag(json_handler))

    # Dedicated audit trail. propagate=False keeps it out of app.log so it is
    # not rotated away by unrelated debug volume.
    trade_logger = logging.getLogger("trades")
    trade_logger.setLevel(logging.INFO)
    trade_logger.propagate = False
    for handler in list(trade_logger.handlers):
        trade_logger.removeHandler(handler)
        handler.close()
    trade_handler = logging.handlers.RotatingFileHandler(
        log_path / "trades.log", maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    trade_handler.setFormatter(json_formatter)
    trade_logger.addHandler(_tag(trade_handler))

    _secure_third_party_loggers(level)

    logging.getLogger(__name__).info(
        "Logging initialised: level=%s dir=%s console=%s", log_level.upper(), log_path, console
    )
    return root


#: Logger names the SmartAPI SDK and its dependencies log through, directly or
#: via logzero. ``logzero_default`` is the one that matters most: it is
#: logzero's real default logger name (``logzero.logger.name``), not
#: ``"logzero"`` as the name might suggest, and it is what
#: ``SmartConnect.__init__`` logs credentials-in-headers exceptions through.
_THIRD_PARTY_LOGGER_NAMES: tuple[str, ...] = (
    "urllib3",
    "requests",
    "SmartApi",
    "smartapi",
    "logzero",
    "logzero_default",
    "asyncio",
)


def _secure_third_party_loggers(app_level: int = logging.INFO) -> None:
    """Redact and quiet every logger the SmartAPI SDK writes through.

    ``SmartConnect.__init__`` calls ``logzero.logfile(path, loglevel=ERROR)`` on
    every session it opens. That call attaches a **new** file handler directly
    to the ``logzero_default`` logger, bypassing this module's own handlers (and
    therefore :data:`redaction_filter`) entirely — this is how a live bearer
    token previously reached a plaintext log file. A handler-level fix cannot
    close this, because the SDK can and does attach new handlers after this
    function runs.

    Attaching :data:`redaction_filter` to the **logger** instead of to a
    handler closes it for good: a logger-level filter runs once in
    ``Logger.handle()``, before Python's logging module dispatches the record to
    *any* of that logger's handlers — including ones attached later, by code
    this repository does not control. This function is therefore safe, and
    cheap, to call more than once and from more than one place; both
    :func:`setup_logging` and :mod:`core.angel_client` call it, so protection
    does not depend on import order or on every entry point remembering to call
    :func:`setup_logging` first.

    Args:
        app_level: The application's configured level. Third-party loggers are
            held at INFO or above even when the app runs at DEBUG, since
            ``urllib3`` and the SDK log full request payloads at DEBUG and the
            volume would bury application logs. The redaction filter does not
            depend on this floor: it applies at every level, including ERROR.
    """
    floor = max(app_level, logging.INFO)
    for name in _THIRD_PARTY_LOGGER_NAMES:
        logger = logging.getLogger(name)
        logger.setLevel(floor)
        if not any(isinstance(f, RedactionFilter) for f in logger.filters):
            logger.addFilter(redaction_filter)
    # Access logs duplicate our own request logging.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


#: Public name, for callers outside this module (``core.angel_client`` calls
#: this defensively right before constructing a broker session, so protection
#: does not depend on every entry point having called :func:`setup_logging`
#: first).
secure_third_party_loggers = _secure_third_party_loggers

#: Backward-compatible alias for the previous, narrower-named function.
_quieten_third_party = _secure_third_party_loggers


def register_secrets(*values: str) -> None:
    """Register literal secrets for redaction across all handlers.

    Call once at startup with the loaded credentials, before any client is
    constructed.

    Args:
        *values: Secret strings. Empty or very short values are ignored.

    Example:
        >>> register_secrets("my-api-key", "my-totp-seed")
    """
    for value in values:
        redaction_filter.register_secret(value)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger.

    Args:
        name: Usually ``__name__``.

    Returns:
        The named logger.
    """
    return logging.getLogger(name)


def get_trade_logger() -> logging.Logger:
    """Return the append-only trade audit logger.

    Returns:
        The ``trades`` logger, which writes JSON lines to ``trades.log``.
    """
    return logging.getLogger("trades")
