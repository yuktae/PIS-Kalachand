# Test Results — Full Suite (Phase 1.3)

**Date:** 2026-04-27
**Environment:** Python 3.12.6 · pytest 8.0.0 · pytest-flask 1.3.0 · PostgreSQL 15 (local, port 5499)

---

## Summary

| | |
|---|---|
| **Total** | 43 |
| **Passed** | 43 |
| **Failed** | 0 |
| **Skipped** | 0 |
| **Duration** | 7.55s |

---

## test_admin_security.py — Admin Security (8 tests)

> Requires PostgreSQL. Tests purge access control and admin API protection.

| Test | Status |
|---|---|
| `test_purge_blocked_for_unauthenticated` | PASSED |
| `test_purge_blocked_for_marketing_user` | PASSED |
| `test_purge_blocked_for_director_user` | PASSED |
| `test_purge_rejected_with_missing_confirm_text` | PASSED |
| `test_purge_rejected_with_wrong_confirm_text` | PASSED |
| `test_purge_rejected_with_lowercase_delete` | PASSED |
| `test_create_user_api_blocked_for_marketing` | PASSED |
| `test_reset_all_prompts_blocked_for_director` | PASSED |

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

## test_workflow.py — Workflow & Helpers (18 tests)

> Requires PostgreSQL. Tests product model, version snapshots, diff logic, normalization, and JSONB validation.

| Test | Status |
|---|---|
| `test_product_default_stage_is_marketing_draft` | PASSED |
| `test_product_soft_delete_leaves_record` | PASSED |
| `test_first_snapshot_is_major` | PASSED |
| `test_minor_snapshot_increments_version` | PASSED |
| `test_shallow_diff_detects_changed_key` | PASSED |
| `test_shallow_diff_empty_when_identical` | PASSED |
| `test_shallow_diff_detects_new_key` | PASSED |
| `test_shallow_diff_detects_removed_key` | PASSED |
| `test_normalize_fills_all_required_keys` | PASSED |
| `test_normalize_preserves_existing_values` | PASSED |
| `test_normalize_handles_none_input` | PASSED |
| `test_validate_pis_data_passes_for_valid_structure` | PASSED |
| `test_validate_pis_data_warns_on_missing_keys` | PASSED |
| `test_validate_pis_data_rejects_non_dict` | PASSED |
| `test_validate_pis_data_warns_on_wrong_type` | PASSED |
| `test_validate_spec_data_passes_for_valid_structure` | PASSED |
| `test_validate_spec_data_warns_on_missing_keys` | PASSED |
| `test_validate_spec_data_rejects_non_dict` | PASSED |

---

## Warnings

- `datetime.datetime.utcnow()` is deprecated in Python 3.12. Affects `model.py` default timestamps and one test assertion in `test_workflow.py`. No functional impact — scheduled for a future cleanup.

---

## Notes

- **Rate limiter disabled in tests:** Flask-Limiter 3.x stores `enabled` as a plain instance attribute. `limiter.enabled = False` is set in the session-scoped `app` fixture so limits do not interfere across tests.
- **Stale user cleanup:** A session-scoped autouse fixture (`_scrub_stale_test_users`) deletes known test usernames at the start of every run, preventing `UniqueViolation` errors from previously interrupted runs.

---

## How to Run

```bash
# Run all tests
python -m pytest tests/ -v

# Run only DB-independent tests (no PostgreSQL needed)
python -m pytest tests/test_utils.py -v
```

---

## Raw Terminal Output

```
============================= test session starts =============================
platform win32 -- Python 3.12.6, pytest-8.0.0, pluggy-1.6.0 -- C:\Users\yukta\ConnectEd\backend\venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: C:\Users\yukta\PIS-JKalachand
plugins: anyio-4.12.1, flask-1.3.0
collecting ... collected 43 items

tests/test_admin_security.py::test_purge_blocked_for_unauthenticated PASSED [  2%]
tests/test_admin_security.py::test_purge_blocked_for_marketing_user PASSED [  4%]
tests/test_admin_security.py::test_purge_blocked_for_director_user PASSED [  6%]
tests/test_admin_security.py::test_purge_rejected_with_missing_confirm_text PASSED [  9%]
tests/test_admin_security.py::test_purge_rejected_with_wrong_confirm_text PASSED [ 11%]
tests/test_admin_security.py::test_purge_rejected_with_lowercase_delete PASSED [ 13%]
tests/test_admin_security.py::test_create_user_api_blocked_for_marketing PASSED [ 16%]
tests/test_admin_security.py::test_reset_all_prompts_blocked_for_director PASSED [ 18%]
tests/test_auth.py::test_login_marketing_redirects_to_dashboard PASSED   [ 20%]
tests/test_auth.py::test_login_director_redirects_to_dashboard PASSED    [ 23%]
tests/test_auth.py::test_login_wrong_password_shows_error PASSED         [ 25%]
tests/test_auth.py::test_login_unknown_email_shows_error PASSED          [ 27%]
tests/test_auth.py::test_login_inactive_user_shows_error PASSED          [ 30%]
tests/test_auth.py::test_logout_clears_session PASSED                    [ 32%]
tests/test_utils.py::test_parses_plain_json_object PASSED                [ 34%]
tests/test_utils.py::test_parses_plain_json_list PASSED                  [ 37%]
tests/test_utils.py::test_strips_markdown_code_fence PASSED              [ 39%]
tests/test_utils.py::test_strips_markdown_code_fence_no_lang PASSED      [ 41%]
tests/test_utils.py::test_repairs_trailing_comma_in_object PASSED        [ 44%]
tests/test_utils.py::test_repairs_trailing_comma_in_object_array PASSED  [ 46%]
tests/test_utils.py::test_extracts_completed_objects_from_truncated_list PASSED [ 48%]
tests/test_utils.py::test_ignores_leading_text_before_json PASSED        [ 51%]
tests/test_utils.py::test_returns_fallback_on_empty_string PASSED        [ 53%]
tests/test_utils.py::test_returns_fallback_on_none PASSED                [ 55%]
tests/test_utils.py::test_returns_fallback_on_invalid_json PASSED        [ 58%]
tests/test_workflow.py::test_product_default_stage_is_marketing_draft PASSED [ 60%]
tests/test_workflow.py::test_product_soft_delete_leaves_record PASSED    [ 62%]
tests/test_workflow.py::test_first_snapshot_is_major PASSED              [ 65%]
tests/test_workflow.py::test_minor_snapshot_increments_version PASSED    [ 67%]
tests/test_workflow.py::test_shallow_diff_detects_changed_key PASSED     [ 69%]
tests/test_workflow.py::test_shallow_diff_empty_when_identical PASSED    [ 72%]
tests/test_workflow.py::test_shallow_diff_detects_new_key PASSED         [ 74%]
tests/test_workflow.py::test_shallow_diff_detects_removed_key PASSED     [ 76%]
tests/test_workflow.py::test_normalize_fills_all_required_keys PASSED    [ 79%]
tests/test_workflow.py::test_normalize_preserves_existing_values PASSED  [ 81%]
tests/test_workflow.py::test_normalize_handles_none_input PASSED         [ 83%]
tests/test_workflow.py::test_validate_pis_data_passes_for_valid_structure PASSED [ 86%]
tests/test_workflow.py::test_validate_pis_data_warns_on_missing_keys PASSED [ 88%]
tests/test_workflow.py::test_validate_pis_data_rejects_non_dict PASSED   [ 90%]
tests/test_workflow.py::test_validate_pis_data_warns_on_wrong_type PASSED [ 93%]
tests/test_workflow.py::test_validate_spec_data_passes_for_valid_structure PASSED [ 95%]
tests/test_workflow.py::test_validate_spec_data_warns_on_missing_keys PASSED [ 97%]
tests/test_workflow.py::test_validate_spec_data_rejects_non_dict PASSED  [100%]

============================== warnings summary ===============================
tests/test_admin_security.py: 7 warnings
tests/test_auth.py: 5 warnings
tests/test_workflow.py: 7 warnings
  C:\Users\yukta\ConnectEd\backend\venv\Lib\site-packages\sqlalchemy\sql\schema.py:3596: DeprecationWarning: datetime.datetime.utcnow() is deprecated and scheduled for removal in a future version. Use timezone-aware objects to represent datetimes in UTC: datetime.datetime.now(datetime.UTC).
    return util.wrap_callable(lambda ctx: fn(), fn)  # type: ignore

tests/test_workflow.py::test_product_soft_delete_leaves_record
  c:\Users\yukta\PIS-JKalachand\tests\test_workflow.py:55: DeprecationWarning: datetime.datetime.utcnow() is deprecated and scheduled for removal in a future version. Use timezone-aware objects to represent datetimes in UTC: datetime.datetime.now(datetime.UTC).
    return util.wrap_callable(lambda ctx: fn(), fn)  # type: ignore

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
======================= 43 passed, 20 warnings in 7.55s =======================
```
