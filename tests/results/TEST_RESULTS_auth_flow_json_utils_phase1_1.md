# Test Results — Auth Flow & JSON Utility Tests (Phase 1.1)

**Date:** 2026-04-25
**Environment:** Python 3.12.6 · pytest 8.0.0 · pytest-flask 1.3.0 · PostgreSQL 15 (Docker)

---

## Summary

| | |
|---|---|
| **Total** | 17 |
| **Passed** | 17 |
| **Failed** | 0 |
| **Skipped** | 0 |
| **Duration** | 3.57s |

---

## test_auth.py — Auth Flow (6 tests)

> Requires PostgreSQL. Skipped automatically when the database is not reachable.

| Test | Status |
|---|---|
| `test_login_marketing_redirects_to_dashboard` | PASSED |
| `test_login_director_redirects_to_dashboard` | PASSED |
| `test_login_wrong_password_shows_error` | PASSED |
| `test_login_unknown_email_shows_error` | PASSED |
| `test_login_inactive_user_shows_error` | PASSED |
| `test_logout_clears_session` | PASSED |

---

## test_utils.py — Utility Functions (11 tests)

> No DB or network dependencies. Always run.

| Test | Status |
|---|---|
| `test_parses_plain_json_object` | PASSED |
| `test_parses_plain_json_list` | PASSED |
| `test_strips_markdown_code_fence` | PASSED |
| `test_strips_markdown_code_fence_no_lang` | PASSED |
| `test_repairs_trailing_comma_in_object` | PASSED |
| `test_repairs_trailing_comma_in_object_array` | PASSED |
| `test_extracts_completed_objects_from_truncated_list` | PASSED |
| `test_ignores_leading_text_before_json` | PASSED |
| `test_returns_fallback_on_empty_string` | PASSED |
| `test_returns_fallback_on_none` | PASSED |
| `test_returns_fallback_on_invalid_json` | PASSED |

---

## Warnings

- `datetime.datetime.utcnow()` is deprecated in Python 3.12. Affects `model.py` default timestamps. No functional impact — scheduled for a future cleanup.

---

## How to Run

```bash
# Start PostgreSQL (Docker)
docker start pis-postgres

# Run all tests
python -m pytest tests/ -v
```

---

## Raw Terminal Output

```
============================= test session starts =============================
platform win32 -- Python 3.12.6, pytest-8.0.0, pluggy-1.6.0 -- C:\Users\yukta\ConnectEd\backend\venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: c:\Users\yukta\PIS-JKalachand
plugins: anyio-4.12.1, flask-1.3.0
collecting ... collected 17 items

tests/test_auth.py::test_login_marketing_redirects_to_dashboard PASSED   [  5%]
tests/test_auth.py::test_login_director_redirects_to_dashboard PASSED    [ 11%]
tests/test_auth.py::test_login_wrong_password_shows_error PASSED         [ 17%]
tests/test_auth.py::test_login_unknown_email_shows_error PASSED          [ 23%]
tests/test_auth.py::test_login_inactive_user_shows_error PASSED          [ 29%]
tests/test_auth.py::test_logout_clears_session PASSED                    [ 35%]
tests/test_utils.py::test_parses_plain_json_object PASSED                [ 41%]
tests/test_utils.py::test_parses_plain_json_list PASSED                  [ 47%]
tests/test_utils.py::test_strips_markdown_code_fence PASSED              [ 52%]
tests/test_utils.py::test_strips_markdown_code_fence_no_lang PASSED      [ 58%]
tests/test_utils.py::test_repairs_trailing_comma_in_object PASSED        [ 64%]
tests/test_utils.py::test_repairs_trailing_comma_in_object_array PASSED  [ 70%]
tests/test_utils.py::test_extracts_completed_objects_from_truncated_list PASSED [ 76%]
tests/test_utils.py::test_ignores_leading_text_before_json PASSED        [ 82%]
tests/test_utils.py::test_returns_fallback_on_empty_string PASSED        [ 88%]
tests/test_utils.py::test_returns_fallback_on_none PASSED                [ 94%]
tests/test_utils.py::test_returns_fallback_on_invalid_json PASSED        [100%]

============================== warnings summary ===============================
tests/test_auth.py::test_login_marketing_redirects_to_dashboard
tests/test_auth.py::test_login_director_redirects_to_dashboard
tests/test_auth.py::test_login_wrong_password_shows_error
tests/test_auth.py::test_login_inactive_user_shows_error
tests/test_auth.py::test_logout_clears_session
  C:\Users\yukta\ConnectEd\backend\venv\Lib\site-packages\sqlalchemy\sql\schema.py:3596: DeprecationWarning: datetime.datetime.utcnow() is deprecated and scheduled for removal in a future version. Use timezone-aware objects to represent datetimes in UTC: datetime.datetime.now(datetime.UTC).
    return util.wrap_callable(lambda ctx: fn(), fn)  # type: ignore

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
======================= 17 passed, 5 warnings in 3.57s ========================
```
