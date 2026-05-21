"""
Shared Gemini client configuration — currently just the request timeout.

The SDK (google-genai 1.73.x) accepts a per-request timeout via
`HttpOptions(timeout=ms)` passed to `genai.Client(http_options=...)`.
Two gotchas this helper hides from every caller:

  • Units. The SDK field is documented as MILLISECONDS, but we expose the
    knob to operators as SECONDS via GEMINI_TIMEOUT_SECONDS — seconds are
    the unit ops/SREs reason in, and it matches the way `requests`,
    httpx, and gunicorn document their timeouts. Conversion happens here.

  • Bad input. Empty string, "abc", "0", "-1", floats like "60.5" — all
    of these have shown up in real env files. We accept anything sensible
    and clamp out the absurd values rather than crashing client creation
    at import time.

Kept Flask-free so it can be unit-tested without an app context.
"""
from __future__ import annotations

import logging
import os
from typing import Final

from google.genai import types as _genai_types

logger = logging.getLogger(__name__)

_ENV_KEY: Final[str] = "GEMINI_TIMEOUT_SECONDS"
_DEFAULT_SECONDS: Final[int] = 600   # 10 minutes — long enough for big PDFs.
# Lower clamp keeps "0" or "-1" from yielding an instantly-timing-out client
# (worse than no timeout at all). Upper clamp keeps a typo'd "60000" from
# parking a worker for 16 hours.
_MIN_SECONDS: Final[int] = 5
_MAX_SECONDS: Final[int] = 3600       # 1 hour


def _resolve_seconds() -> int:
    """Read GEMINI_TIMEOUT_SECONDS, validate it, return a sane int."""
    raw = os.getenv(_ENV_KEY)
    if raw is None or raw.strip() == "":
        return _DEFAULT_SECONDS

    try:
        # Accept "60", "60.0", "  60 " — but not "abc".
        secs = int(float(raw.strip()))
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r is not a number; falling back to %ds.",
            _ENV_KEY, raw, _DEFAULT_SECONDS,
        )
        return _DEFAULT_SECONDS

    if secs < _MIN_SECONDS:
        logger.warning(
            "%s=%ds is below the %ds floor; clamping up.",
            _ENV_KEY, secs, _MIN_SECONDS,
        )
        return _MIN_SECONDS
    if secs > _MAX_SECONDS:
        logger.warning(
            "%s=%ds is above the %ds ceiling; clamping down.",
            _ENV_KEY, secs, _MAX_SECONDS,
        )
        return _MAX_SECONDS
    return secs


def gemini_timeout_seconds() -> int:
    """Public accessor for the resolved timeout in seconds. Useful for
    logging or for code paths that want to pass the same timeout to other
    libraries (e.g. httpx wrappers around the Gemini Files API)."""
    return _resolve_seconds()


def gemini_http_options() -> _genai_types.HttpOptions:
    """Return an SDK-ready `HttpOptions` carrying the configured timeout
    in MILLISECONDS, as the SDK requires.

    Pass directly to `genai.Client(http_options=gemini_http_options())`.
    """
    secs = _resolve_seconds()
    return _genai_types.HttpOptions(timeout=secs * 1000)