"""
Shared pytest fixtures.

Design choices:

  • Real Postgres, no SQLite. The app uses JSONB, GIN indexes, and Postgres
    advisory locks — SQLite would mask schema bugs. If Postgres isn't
    reachable, the suite FAILS FAST in `_postgres_required` rather than
    silently skipping (the previous suite's biggest blind spot).

  • One app/engine per session. Per-test isolation is done with TRUNCATE
    ... RESTART IDENTITY CASCADE before each test — slightly slower than a
    SAVEPOINT but immune to the Flask-SQLAlchemy session-binding gotchas
    that bit the old conftest.

  • CSRF is disabled in the test config so each test doesn't have to scrape
    a token out of the login form first.

  • External APIs (Gemini, requests, search) are NEVER hit. The
    `mock_gemini` and `block_http` fixtures wire that up; tests that need
    them opt in explicitly.

Environment knobs (read once at import time):
  TEST_DATABASE_URL  Defaults to localhost pis_system_test. Override for CI.
  SECRET_KEY         Defaulted here so `create_app()` doesn't print the
                     "no SECRET_KEY" warning during every test run.
"""
from __future__ import annotations

import os

# Set env BEFORE importing the app factory so create_app() picks these up.
os.environ.setdefault(
    "TEST_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/pis_system_test",
)
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ.setdefault("SECRET_KEY", "test-secret-not-for-production")
# Force the limiter to in-memory so tests don't need Redis. The default
# already falls back to memory:// but be explicit.
os.environ["REDIS_URL"] = "memory://"

import pytest  # noqa: E402

from app import create_app  # noqa: E402
from model import db as _db, User  # noqa: E402


# ── App / DB ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def app():
    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    return application


@pytest.fixture(scope="session", autouse=True)
def _postgres_required(app):
    """Hard-fail the whole session if Postgres isn't reachable. We never
    want a misconfigured CI to skip silently and report green."""
    with app.app_context():
        try:
            _db.session.execute(_db.text("SELECT 1"))
            _db.session.commit()
        except Exception as e:
            pytest.fail(
                f"Postgres at {app.config['SQLALCHEMY_DATABASE_URI']} is "
                f"unreachable. Create the test DB or set TEST_DATABASE_URL. "
                f"Underlying error: {e}",
                pytrace=False,
            )


@pytest.fixture(autouse=True)
def reset_db(app):
    """Wipe every table before each test. TRUNCATE ... CASCADE handles the
    FK ordering for us, RESTART IDENTITY keeps autoincrement IDs predictable
    across tests."""
    with app.app_context():
        table_names = [t.name for t in reversed(_db.metadata.sorted_tables)]
        if table_names:
            quoted = ", ".join(f'"{n}"' for n in table_names)
            _db.session.execute(
                _db.text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE")
            )
            _db.session.commit()
    yield


@pytest.fixture
def db(app):
    """Convenience handle on the SQLAlchemy `db` object, scoped to the
    app context. Use this when a test needs to add rows directly."""
    with app.app_context():
        yield _db


@pytest.fixture
def client(app):
    return app.test_client()


# ── User factories ──────────────────────────────────────────────────────────


def _make_user(role: str = "marketing", *, email: str | None = None,
               username: str | None = None, password: str = "pw-test-1234",
               is_active: bool = True) -> User:
    """Create + commit a User and return it. Caller owns the app context."""
    email = email or f"{role}@test.local"
    username = username or role
    u = User(
        username=username, email=email, role=role,
        display_name=username.capitalize(), is_active=is_active,
    )
    u.set_password(password)
    _db.session.add(u)
    _db.session.commit()
    return u


@pytest.fixture
def make_user(app):
    """Factory fixture: `make_user(role='admin')` returns a fresh User."""
    def _factory(role: str = "marketing", **kwargs) -> User:
        with app.app_context():
            return _make_user(role=role, **kwargs)
    return _factory


def _login_client(client, app, *, role: str, password: str = "pw-test-1234") -> User:
    """Create a user of `role` (if not present) and sign in the test client."""
    with app.app_context():
        existing = User.query.filter_by(role=role).first()
        if existing is None:
            existing = _make_user(role=role, password=password)
        email = existing.email
    resp = client.post("/login", data={"email": email, "password": password},
                       follow_redirects=False)
    assert resp.status_code in (301, 302), (
        f"Expected redirect after login, got {resp.status_code}: {resp.data[:200]!r}"
    )
    return existing


@pytest.fixture
def admin_client(client, app):
    _login_client(client, app, role="admin")
    return client


@pytest.fixture
def marketing_client(client, app):
    _login_client(client, app, role="marketing")
    return client


@pytest.fixture
def director_client(client, app):
    _login_client(client, app, role="director")
    return client


@pytest.fixture
def web_client(client, app):
    _login_client(client, app, role="web")
    return client


# ── External-service mocks ──────────────────────────────────────────────────


class _FakeUsageMetadata:
    def __init__(self, in_tok: int, out_tok: int, cached: int = 0):
        self.prompt_token_count = in_tok
        self.candidates_token_count = out_tok
        self.cached_content_token_count = cached


class FakeGeminiResponse:
    """Minimal stand-in for a `generate_content` return value. Has the two
    attributes the metering layer reads (`usage_metadata`) and the one
    most callers read (`text`)."""

    def __init__(self, text: str = "{}", *, in_tok: int = 100,
                 out_tok: int = 50, cached: int = 0):
        self.text = text
        self.usage_metadata = _FakeUsageMetadata(in_tok, out_tok, cached)


class FakeGeminiClient:
    """A drop-in for `genai.Client()` that records every call instead of
    hitting the network. Pass to `gemini_call(client=fake)` or use the
    `mock_gemini` fixture which patches the default client lookup."""

    def __init__(self, response: FakeGeminiResponse | None = None):
        self._response = response or FakeGeminiResponse()
        self.calls: list[dict] = []
        self.models = self  # so .models.generate_content() resolves

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


@pytest.fixture
def fake_gemini():
    """Returns a FakeGeminiClient. Pass it explicitly into `gemini_call(...)`
    via the `client=` kwarg, OR use `mock_gemini` to patch the default
    client-factory globally."""
    return FakeGeminiClient()


@pytest.fixture
def mock_gemini(monkeypatch, fake_gemini):
    """Patch `utils.ai_generation._get_client` so any `gemini_call(...)`
    that omits its `client=` kwarg gets the fake."""
    import utils.ai_generation as ai_gen
    monkeypatch.setattr(ai_gen, "_get_client", lambda: fake_gemini)
    return fake_gemini


@pytest.fixture
def block_http(monkeypatch):
    """Fail any test that accidentally tries to make a real HTTP request.
    Opt-in: tests that need network mocking pull this fixture and then
    monkeypatch specific URLs themselves."""
    def _refuse(*args, **kwargs):
        raise RuntimeError(
            "Real HTTP call attempted during tests. Mock the response "
            "explicitly with monkeypatch."
        )
    import requests
    monkeypatch.setattr(requests, "get", _refuse)
    monkeypatch.setattr(requests, "post", _refuse)
