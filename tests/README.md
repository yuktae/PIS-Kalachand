# Tests

## Running

```powershell
# 1. Create the test database (one time)
createdb -U postgres pis_system_test

# 2. Run the full suite
venv\Scripts\python.exe -m pytest

# Run a single file
venv\Scripts\python.exe -m pytest tests\test_pricing.py

# Run a single test, verbose
venv\Scripts\python.exe -m pytest tests\test_auth.py::test_login_with_valid_credentials_sets_session -v
```

## Environment

By default the suite expects Postgres at
`postgresql://postgres:postgres@localhost:5432/pis_system_test`.
Override with `TEST_DATABASE_URL` in your environment if your local
Postgres listens elsewhere.

The suite will **fail loudly** (not skip) if Postgres is unreachable —
this is intentional. A green CI run that silently skipped half the suite
was the worst footgun in the old test setup.

## Fixtures (in conftest.py)

| fixture | scope | what it gives you |
|---|---|---|
| `app` | session | the Flask app with `TESTING=True`, CSRF off |
| `client` | function | a fresh test client (unauthenticated) |
| `admin_client` / `marketing_client` / `director_client` / `web_client` | function | a test client already signed in as that role |
| `db` | function | the SQLAlchemy `db` object inside an app context |
| `make_user(role=..., email=..., ...)` | function | factory that returns a committed `User` |
| `fake_gemini` | function | a record-only stand-in for `genai.Client()` |
| `mock_gemini` | function | patches `_get_client()` so any `gemini_call(...)` w/out `client=` uses the fake |
| `block_http` | function | makes any real `requests.get/post` raise so accidental network use is caught |
| `reset_db` (autouse) | function | `TRUNCATE ... RESTART IDENTITY CASCADE` before each test |

## Conventions

- **Never hit real APIs.** Pass `client=fake_gemini` into `gemini_call(...)`,
  or pull the `mock_gemini` fixture which patches the default lookup.
- **Tests own their users.** The autouse `reset_db` fixture truncates
  everything; use `make_user(...)` or the role-specific signed-in client
  fixtures.
- **One assertion concept per test.** If a test name has "and" in it,
  consider splitting.
