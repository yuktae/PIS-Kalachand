"""
External API metering — wraps every Gemini and search call so we capture
token counts, latency, cost, and an attributable (job_id, prompt_id)
pair. The admin "AI Job Activity" panel reads from the resulting
ApiCallLog table.

How it's used by callers:

  Gemini text call (replaces `_get_client().models.generate_content(...)`):

      response = gemini_call(
          prompt_id="proforma_extraction",
          model=_MODEL,
          contents=[...],
          config=...,
      )

  Gemini image call (e.g. nano-banana retouch):

      response = gemini_call(
          prompt_id="image_retouch",
          model=_NANO_BANANA_MODEL,
          contents=[...],
          image_count_hint=1,   # how many images the call should produce
      )

  Search call (after the search has run, you know how many results came back):

      log_search_call(provider="brave_search", query_count=1, latency_ms=ms)

  Worker boot (so calls inside the worker get attributed to this job):

      with job_scope(job_id):
          ... do the work ...

Calls outside any job_scope are still logged (job_id = NULL) so spend
from verify-marketing fixes / compare exports / ad-hoc paths is visible.

Failures inside the metering layer NEVER raise — the wrapped call must
not break because logging broke.
"""
from __future__ import annotations

import os
import time
import contextvars
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Iterator

from . import pricing


# ContextVar so background workers can set the job once at the top and
# every call below it inherits the attribution without plumbing kwargs.
_current_job_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_job_id", default=None
)


@contextmanager
def job_scope(job_id: str | None) -> Iterator[None]:
    """Attach all metered calls inside this block to `job_id`."""
    token = _current_job_id.set(job_id)
    try:
        yield
    finally:
        _current_job_id.reset(token)


def set_current_job(job_id: str | None) -> None:
    """Imperative variant for places where a `with` block is awkward
    (top of a worker function that runs to completion)."""
    _current_job_id.set(job_id)


def current_job_id() -> str | None:
    return _current_job_id.get()


# ── Logging primitives ──────────────────────────────────────────────────────

def _safe_get_usage(response: Any) -> tuple[int, int, int]:
    """Pull (input, output, cached) token counts off a Gemini response.
    Returns zeros on any extraction failure — never raises."""
    try:
        usage = getattr(response, "usage_metadata", None)
        if not usage:
            return 0, 0, 0
        in_tok  = int(getattr(usage, "prompt_token_count", 0) or 0)
        out_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
        cached  = int(getattr(usage, "cached_content_token_count", 0) or 0)
        return in_tok, out_tok, cached
    except Exception:
        return 0, 0, 0


def _bump_job_aggregates(job_id: str | None, cost_usd: Decimal) -> None:
    """Increment Job.total_cost_usd / total_calls. Silent on failure."""
    if not job_id:
        return
    try:
        from model import db, Job
        # Use a single UPDATE so we don't load + write the full row.
        db.session.execute(
            db.text(
                "UPDATE job SET total_cost_usd = COALESCE(total_cost_usd, 0) + :c, "
                "total_calls = COALESCE(total_calls, 0) + 1 WHERE id = :jid"
            ),
            {"c": cost_usd, "jid": job_id},
        )
        db.session.commit()
    except Exception:
        try:
            from model import db
            db.session.rollback()
        except Exception:
            pass


def _write_log_row(**fields) -> None:
    """Insert one ApiCallLog row. Silent on failure (logging must
    never break the caller)."""
    try:
        from model import db, ApiCallLog
        row = ApiCallLog(**fields)
        db.session.add(row)
        db.session.commit()
    except Exception as e:
        try:
            from model import db
            db.session.rollback()
        except Exception:
            pass
        if os.getenv("API_METERING_DEBUG", "").lower() in ("1", "true", "yes"):
            print(f"[api_metering] log write failed: {e}")


# ── Public call wrappers ────────────────────────────────────────────────────

def gemini_call(*, prompt_id: str, model: str,
                contents: Any = None, config: Any = None,
                image_count_hint: int = 0,
                client: Any = None) -> Any:
    """Call Gemini's generate_content and log token usage + cost.

    Returns the same response object generate_content returned, so callers
    are drop-in compatible with the previous `_get_client().models.generate_content(...)`
    pattern.

    `client` is optional — if omitted, the caller's module-local
    `_get_client()` is used by importing it lazily; but in practice every
    call site already has a client, so we accept it explicitly to avoid
    circular imports.
    """
    if client is None:
        # Lazy import — every utils module exposes a `_get_client()`. We
        # don't want a hard import dependency.
        from .ai_generation import _get_client as _default_client
        client = _default_client()

    started = time.monotonic()
    response = None
    error_text = None
    try:
        kwargs: dict[str, Any] = {"model": model}
        if contents is not None:
            kwargs["contents"] = contents
        if config is not None:
            kwargs["config"] = config
        response = client.models.generate_content(**kwargs)
        return response
    except Exception as e:
        error_text = f"{type(e).__name__}: {str(e)[:160]}"
        raise
    finally:
        latency_ms = int((time.monotonic() - started) * 1000)

        if pricing.is_image_model(model):
            # Image model — bill per produced image. We trust the hint
            # rather than introspecting the response payload.
            count = image_count_hint or 1
            cost = pricing.cost_for_image_call(model, count) if response else Decimal("0")
            in_t, out_t, cached_t = 0, 0, 0
            img_count = count
        else:
            in_t, out_t, cached_t = _safe_get_usage(response) if response else (0, 0, 0)
            cost = pricing.cost_for_text_call(model, in_t, out_t, cached_t) if response else Decimal("0")
            img_count = 0

        jid = current_job_id()
        _write_log_row(
            job_id=jid,
            prompt_id=prompt_id,
            provider="gemini",
            model=model,
            input_tokens=in_t,
            output_tokens=out_t,
            cached_tokens=cached_t,
            image_count=img_count,
            query_count=0,
            latency_ms=latency_ms,
            cost_usd=cost,
            error=error_text,
        )
        _bump_job_aggregates(jid, cost)


def log_search_call(*, provider: str, query_count: int = 1,
                    latency_ms: int = 0, error: str | None = None,
                    prompt_id: str | None = None) -> None:
    """Log a non-Gemini search call (Google CSE, Brave, DuckDuckGo,
    plain web scraper). Free providers contribute call count + latency
    but $0.00 cost."""
    cost = pricing.cost_for_search_call(provider, query_count)
    jid = current_job_id()
    _write_log_row(
        job_id=jid,
        prompt_id=prompt_id,
        provider=provider,
        model=None,
        input_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        image_count=0,
        query_count=query_count,
        latency_ms=latency_ms,
        cost_usd=cost,
        error=error,
    )
    _bump_job_aggregates(jid, cost)
