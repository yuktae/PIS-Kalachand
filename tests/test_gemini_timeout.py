"""
Gemini timeout tests.

Two layers:

  • Pure helper — `utils.gemini_settings.gemini_http_options()` reads
    GEMINI_TIMEOUT_SECONDS, validates / clamps it, and returns an
    `HttpOptions` whose `.timeout` field is in MILLISECONDS (the unit the
    SDK requires). These tests just twiddle env vars and assert the
    returned HttpOptions shape.

  • Integration — `genai.Client` is monkeypatched with a recording fake
    so we can verify that each module's `_get_client()` actually hands
    the SDK an `http_options` carrying the configured timeout. No real
    Gemini calls and no network — by design, this regression-tests
    *only* the wiring.
"""
from __future__ import annotations

import importlib

import pytest

from utils import gemini_settings


# ── helper-level ────────────────────────────────────────────────────────────


def test_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("GEMINI_TIMEOUT_SECONDS", raising=False)
    opts = gemini_settings.gemini_http_options()
    assert opts.timeout == 600 * 1000


def test_default_when_env_empty(monkeypatch):
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "")
    assert gemini_settings.gemini_http_options().timeout == 600 * 1000


def test_explicit_value_is_used_and_converted_to_ms(monkeypatch):
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "120")
    assert gemini_settings.gemini_http_options().timeout == 120 * 1000


def test_whitespace_and_float_strings_accepted(monkeypatch):
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "  90.5  ")
    # int(float(...)) → 90 → 90_000 ms
    assert gemini_settings.gemini_http_options().timeout == 90 * 1000


def test_garbage_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "five hundred")
    assert gemini_settings.gemini_http_options().timeout == 600 * 1000


def test_below_floor_is_clamped_up(monkeypatch):
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "0")
    # Floor is 5s — otherwise a typo'd "0" produces a client that times
    # out before it can send the first byte.
    assert gemini_settings.gemini_http_options().timeout == 5 * 1000


def test_negative_is_clamped_up(monkeypatch):
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "-30")
    assert gemini_settings.gemini_http_options().timeout == 5 * 1000


def test_above_ceiling_is_clamped_down(monkeypatch):
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "999999")
    # Ceiling is 3600s — keeps a typo'd "60000" from parking a worker
    # for 16 hours.
    assert gemini_settings.gemini_http_options().timeout == 3600 * 1000


def test_gemini_timeout_seconds_accessor(monkeypatch):
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "42")
    assert gemini_settings.gemini_timeout_seconds() == 42


# ── integration: each module's _get_client() hands HttpOptions to SDK ──────


class _RecordingFakeClient:
    """Stand-in for `genai.Client` — records every constructor kwarg so
    tests can assert which options were passed."""

    instances: list["_RecordingFakeClient"] = []

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        _RecordingFakeClient.instances.append(self)


@pytest.fixture
def fake_genai_client(monkeypatch):
    """Patch `google.genai.Client` everywhere it has been imported so
    that any `_get_client()` we exercise hands its kwargs to our fake
    instead of constructing a real SDK client (which would try to read
    GOOGLE_API_KEY etc.)."""
    from google import genai as _genai
    monkeypatch.setattr(_genai, "Client", _RecordingFakeClient)
    _RecordingFakeClient.instances.clear()
    return _RecordingFakeClient


def _clear_thread_local(module):
    """Each module's `_get_client()` caches on a `threading.local()`. To
    force a fresh `genai.Client(...)` call in this test process, blow
    away whatever's cached."""
    tl = getattr(module, "_thread_local", None)
    if tl is not None and hasattr(tl, "client"):
        del tl.client


@pytest.mark.parametrize("module_path", [
    "utils.ai_generation",
    "utils.single_wizard",
    "utils.bulk_wizard",
])
def test_get_client_passes_http_options_with_timeout(
    fake_genai_client, monkeypatch, module_path,
):
    """Every wired module must construct genai.Client with an
    `http_options` argument whose timeout matches GEMINI_TIMEOUT_SECONDS
    (converted to ms). This is the load-bearing assertion of Phase 2."""
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "75")

    module = importlib.import_module(module_path)
    _clear_thread_local(module)

    client = module._get_client()
    assert isinstance(client, _RecordingFakeClient), (
        f"{module_path}._get_client() did not go through the fake — "
        "the monkeypatch on google.genai.Client missed."
    )

    kwargs = client.init_kwargs
    assert "http_options" in kwargs, (
        f"{module_path}._get_client() omitted http_options=... — "
        "timeout is not actually being applied."
    )
    http_opts = kwargs["http_options"]
    assert http_opts.timeout == 75 * 1000, (
        f"{module_path}._get_client() set timeout={http_opts.timeout}ms; "
        "expected 75000ms (75s)."
    )