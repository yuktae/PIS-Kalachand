"""
Tests for utils.api_metering — the wrapper that writes an ApiCallLog row
for every Gemini / search call and rolls the cost up into Job aggregates.

We never hit the real Gemini API. The `fake_gemini` fixture (see
conftest.py) is a record-only stand-in passed via the `client=` kwarg.
"""
from decimal import Decimal

from model import ApiCallLog, Job
from utils import api_metering


# ── job_scope ContextVar ────────────────────────────────────────────────────


def test_current_job_id_defaults_to_none():
    assert api_metering.current_job_id() is None


def test_job_scope_sets_and_unsets_job_id():
    assert api_metering.current_job_id() is None
    with api_metering.job_scope("ABCD1234"):
        assert api_metering.current_job_id() == "ABCD1234"
    assert api_metering.current_job_id() is None


def test_nested_job_scope_restores_outer_on_exit():
    with api_metering.job_scope("OUTER"):
        with api_metering.job_scope("INNER"):
            assert api_metering.current_job_id() == "INNER"
        assert api_metering.current_job_id() == "OUTER"


# ── gemini_call writes ApiCallLog rows ──────────────────────────────────────


def test_gemini_call_writes_one_log_row_per_call(app, db, fake_gemini):
    with app.app_context():
        api_metering.gemini_call(
            prompt_id="unit_test",
            model="gemini-2.5-flash",
            contents=["hello"],
            client=fake_gemini,
        )
        rows = ApiCallLog.query.all()
    assert len(rows) == 1
    assert rows[0].prompt_id == "unit_test"
    assert rows[0].provider == "gemini"
    assert rows[0].model == "gemini-2.5-flash"


def test_gemini_call_records_token_usage_from_response(app, db, fake_gemini):
    fake_gemini._response.usage_metadata.prompt_token_count = 1_000_000
    fake_gemini._response.usage_metadata.candidates_token_count = 1_000_000
    fake_gemini._response.usage_metadata.cached_content_token_count = 0

    with app.app_context():
        api_metering.gemini_call(
            prompt_id="cost_check",
            model="gemini-2.5-flash",
            contents=["x"],
            client=fake_gemini,
        )
        row = ApiCallLog.query.one()
    assert row.input_tokens == 1_000_000
    assert row.output_tokens == 1_000_000
    # 1M input @ $0.075/1M + 1M output @ $0.30/1M = $0.375
    assert Decimal(row.cost_usd) == Decimal("0.375000")


def test_gemini_call_attributes_to_active_job_scope(app, db, fake_gemini):
    with app.app_context():
        job = Job(id="JOB123", status="processing", model_name="X")
        db.session.add(job)
        db.session.commit()

        with api_metering.job_scope("JOB123"):
            api_metering.gemini_call(
                prompt_id="attributed",
                model="gemini-2.5-flash",
                contents=["x"],
                client=fake_gemini,
            )

        row = ApiCallLog.query.one()
        refreshed_job = Job.query.get("JOB123")
    assert row.job_id == "JOB123"
    # Job aggregates should also have been bumped.
    assert refreshed_job is not None
    assert refreshed_job.total_calls == 1
    assert Decimal(refreshed_job.total_cost_usd) > Decimal("0")


def test_gemini_call_with_no_job_scope_logs_with_null_job_id(app, db, fake_gemini):
    with app.app_context():
        api_metering.gemini_call(
            prompt_id="orphan",
            model="gemini-2.5-flash",
            contents=["x"],
            client=fake_gemini,
        )
        row = ApiCallLog.query.one()
    assert row.job_id is None


# ── Image model billing ─────────────────────────────────────────────────────


def test_image_model_is_billed_per_image_not_per_token(app, db, fake_gemini):
    with app.app_context():
        api_metering.gemini_call(
            prompt_id="retouch",
            model="gemini-2.5-flash-image",
            contents=["x"],
            image_count_hint=3,
            client=fake_gemini,
        )
        row = ApiCallLog.query.one()
    # 3 images @ $0.039 each = $0.117. Tokens should be zero.
    assert row.input_tokens == 0
    assert row.output_tokens == 0
    assert row.image_count == 3
    assert Decimal(row.cost_usd) == Decimal("0.117000")


# ── log_search_call ─────────────────────────────────────────────────────────


def test_log_search_call_records_provider_and_query_count(app, db):
    with app.app_context():
        api_metering.log_search_call(
            provider="brave_search",
            query_count=4,
            latency_ms=120,
        )
        row = ApiCallLog.query.one()
    assert row.provider == "brave_search"
    assert row.query_count == 4
    assert row.latency_ms == 120
    # 4 queries @ $3/1000 = $0.012
    assert Decimal(row.cost_usd) == Decimal("0.012000")


def test_log_search_call_for_free_provider_records_zero_cost(app, db):
    with app.app_context():
        api_metering.log_search_call(provider="duckduckgo", query_count=10)
        row = ApiCallLog.query.one()
    assert Decimal(row.cost_usd) == Decimal("0E-6")


# ── Resilience ──────────────────────────────────────────────────────────────


def test_gemini_call_failure_still_logs_an_error_row(app, db, fake_gemini):
    """If the wrapped Gemini call raises, the wrapper must re-raise BUT
    still write a log row with the error captured. Otherwise spend goes
    invisible during outages."""
    class _Boom:
        models = None  # placeholder, replaced below
        def __init__(self):
            self.models = self
        def generate_content(self, **_kwargs):
            raise RuntimeError("API outage")

    boom = _Boom()
    with app.app_context():
        try:
            api_metering.gemini_call(
                prompt_id="error_path",
                model="gemini-2.5-flash",
                contents=["x"],
                client=boom,
            )
        except RuntimeError:
            pass
        row = ApiCallLog.query.one()
    assert row.error is not None
    assert "API outage" in row.error
