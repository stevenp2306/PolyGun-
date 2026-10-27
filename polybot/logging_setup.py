"""Logging with secret redaction and size-based rotation."""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

# Patterns that look like secrets even if they were never registered.
_PATTERNS = [
    re.compile(r"0x[0-9a-fA-F]{64}"),  # EVM private keys / raw 32-byte secrets
    re.compile(r"(?i)(api[_-]?key|secret|passphrase|password|token|private[_-]?key)(\s*[=:]\s*)([^\s,;&\"']+)"),
    re.compile(r"(?i)(apiKey=)([^&\s]+)"),  # query-string API keys (e.g. odds provider)
    re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}"),  # telegram bot tokens inside URLs
]

_registered: set[str] = set()


def register_secrets(values: list[str]) -> None:
    for v in values:
        if v and len(v) >= 6:
            _registered.add(v)


def redact(text: str) -> str:
    if not text:
        return text
    for secret in sorted(_registered, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, "***REDACTED***")
    text = _PATTERNS[0].sub("0x***REDACTED***", text)
    text = _PATTERNS[1].sub(lambda m: f"{m.group(1)}{m.group(2)}***REDACTED***", text)
    text = _PATTERNS[2].sub(lambda m: f"{m.group(1)}***REDACTED***", text)
    text = _PATTERNS[3].sub("bot***REDACTED***", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = None
        if record.exc_info and record.exc_info[1] is not None:
            # Format the traceback now so it can be redacted too.
            formatted = logging.Formatter().formatException(record.exc_info)
            record.msg = f"{record.msg}\n{redact(formatted)}"
            record.exc_info = None
            record.exc_text = None
        return True


def setup_logging(log_dir: Path, level: str = "INFO", secrets: list[str] | None = None) -> None:
    register_secrets(secrets or [])
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(RedactingFilter())
    root.addHandler(console)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fileh = logging.handlers.RotatingFileHandler(
            log_dir / "polybot.log", maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8"
        )
        fileh.setFormatter(fmt)
        fileh.addFilter(RedactingFilter())
        root.addHandler(fileh)
    except OSError as e:  # read-only filesystem etc.
        root.warning("file logging disabled: %s", e)
    # third-party HTTP clients may log full URLs (which can contain API keys)
    for noisy in ("httpx", "httpcore", "websockets", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
        logging.getLogger(noisy).addFilter(RedactingFilter())
