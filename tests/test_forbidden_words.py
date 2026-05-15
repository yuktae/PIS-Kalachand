"""
Tests for utils.forbidden_words — storage shape, normalization, and the
per-category + __global__ merge. These also exercise the helpers.py
re-export shim so a refactor that breaks the shim doesn't go unnoticed.

API-endpoint coverage is in test_admin.py-style integration form here
because the routes live in blueprints/api.py and require auth.
"""
import json
import os

import pytest

from utils.forbidden_words import (
    GLOBAL_CATEGORY_KEY,
    VALID_SEVERITIES,
    load_forbidden_words,
    save_forbidden_words,
    get_forbidden_words_for_category,
    get_forbidden_words_flat,
    _normalize_word_entry,
    _normalize_category_list,
)


@pytest.fixture
def tmp_fw_file(app, tmp_path, monkeypatch):
    """Redirect the forbidden-words JSON file to a per-test temp path so
    tests can't stomp on the real data/forbidden_words.json."""
    fake_data_dir = tmp_path / "data"
    fake_data_dir.mkdir()
    monkeypatch.setitem(app.config, "BASE_DIR", str(tmp_path))
    return fake_data_dir / "forbidden_words.json"


# ── Normalizer unit tests (no DB / no file IO) ──────────────────────────────


def test_normalize_legacy_string_entry_becomes_dict():
    out = _normalize_word_entry("Experience")
    assert out == {"word": "experience", "replace_with": "", "severity": "block"}


def test_normalize_strips_empty_string():
    assert _normalize_word_entry("") is None
    assert _normalize_word_entry("   ") is None


def test_normalize_invalid_severity_falls_back_to_block():
    out = _normalize_word_entry({"word": "premium", "severity": "nuclear"})
    assert out is not None and out["severity"] == "block"


def test_normalize_carries_optional_governance_fields():
    out = _normalize_word_entry({
        "word":         "lorem",
        "replace_with": "real text",
        "severity":     "warn",
        "reason":       "placeholder filler",
        "added_by":     "Alice",
        "added_at":     "2026-05-01T10:00:00Z",
    })
    assert out is not None
    assert out["reason"] == "placeholder filler"
    assert out["added_by"] == "Alice"
    assert out["added_at"] == "2026-05-01T10:00:00Z"


def test_normalize_truncates_long_reason_to_120_chars():
    out = _normalize_word_entry({"word": "x", "reason": "y" * 500})
    assert out is not None and len(out["reason"]) == 120


def test_normalize_category_list_dedupes_by_lowercase_word():
    raw = ["foo", "Foo", {"word": "FOO", "replace_with": "bar"}, "baz"]
    cleaned = _normalize_category_list(raw)
    words = [e["word"] for e in cleaned]
    assert words == ["foo", "baz"]


# ── load / save round-trip ──────────────────────────────────────────────────


def test_load_returns_empty_dict_when_file_missing(app, tmp_fw_file):
    assert not tmp_fw_file.exists()
    with app.app_context():
        assert load_forbidden_words() == {}


def test_load_returns_empty_dict_on_corrupt_json(app, tmp_fw_file):
    tmp_fw_file.write_text("{not valid json", encoding="utf-8")
    with app.app_context():
        assert load_forbidden_words() == {}


def test_save_then_load_round_trips(app, tmp_fw_file):
    payload = {
        "Lighting": [{"word": "premium", "replace_with": "high-end",
                      "severity": "block"}],
        GLOBAL_CATEGORY_KEY: [{"word": "synergy", "severity": "warn",
                               "replace_with": ""}],
    }
    with app.app_context():
        save_forbidden_words(payload)
        loaded = load_forbidden_words()
    assert "Lighting" in loaded
    assert loaded["Lighting"][0]["word"] == "premium"
    assert loaded[GLOBAL_CATEGORY_KEY][0]["word"] == "synergy"
    assert loaded[GLOBAL_CATEGORY_KEY][0]["severity"] == "warn"


def test_save_drops_empty_categories(app, tmp_fw_file):
    with app.app_context():
        save_forbidden_words({"Empty": [], "Useful": [{"word": "buzz"}]})
        loaded = load_forbidden_words()
    assert "Empty" not in loaded
    assert "Useful" in loaded


def test_legacy_string_list_on_disk_is_upgraded_on_read(app, tmp_fw_file):
    tmp_fw_file.write_text(json.dumps({"Audio": ["junk", "fluff"]}),
                           encoding="utf-8")
    with app.app_context():
        loaded = load_forbidden_words()
    assert isinstance(loaded["Audio"][0], dict)
    assert loaded["Audio"][0]["word"] == "junk"
    assert loaded["Audio"][0]["severity"] == "block"


# ── Category + global merge ─────────────────────────────────────────────────


def test_category_entries_override_global_on_same_word(app, tmp_fw_file):
    with app.app_context():
        save_forbidden_words({
            GLOBAL_CATEGORY_KEY: [{"word": "fast",
                                   "replace_with": "global-replacement"}],
            "Lighting": [{"word": "fast",
                          "replace_with": "category-replacement"}],
        })
        merged = get_forbidden_words_for_category("Lighting")
    fast = next(e for e in merged if e["word"] == "fast")
    assert fast["replace_with"] == "category-replacement"


def test_global_only_words_show_up_for_any_category(app, tmp_fw_file):
    with app.app_context():
        save_forbidden_words({
            GLOBAL_CATEGORY_KEY: [{"word": "synergy", "severity": "warn"}],
        })
        merged = get_forbidden_words_for_category("Lighting")
    assert [e["word"] for e in merged] == ["synergy"]


def test_flat_helper_returns_just_words(app, tmp_fw_file):
    with app.app_context():
        save_forbidden_words({"Lighting": [{"word": "premium"}],
                              GLOBAL_CATEGORY_KEY: [{"word": "buzz"}]})
        flat = get_forbidden_words_flat("Lighting")
    assert set(flat) == {"premium", "buzz"}


# ── Re-export shim ──────────────────────────────────────────────────────────


def test_helpers_reexport_is_identity():
    """The Tier-3 split moved storage out of helpers.py but left a shim. If
    that shim ever rebinds to a wrapper instead of re-exporting the real
    callable, callers will subtly diverge. Guard against that."""
    import helpers
    import utils.forbidden_words as fw
    assert helpers.load_forbidden_words is fw.load_forbidden_words
    assert helpers.save_forbidden_words is fw.save_forbidden_words
    assert helpers.GLOBAL_CATEGORY_KEY == fw.GLOBAL_CATEGORY_KEY
    assert helpers.VALID_SEVERITIES == fw.VALID_SEVERITIES


# ── API endpoint coverage (admin auth) ──────────────────────────────────────


def test_api_get_forbidden_words_requires_login(client):
    resp = client.get("/api/forbidden_words", follow_redirects=False)
    assert resp.status_code in (301, 302)


def test_api_add_and_remove_forbidden_word_round_trip(
        admin_client, app, tmp_fw_file):
    add = admin_client.post(
        "/api/forbidden_words",
        data=json.dumps({"category": "Lighting", "word": "premium",
                         "replace_with": "high-end", "severity": "block"}),
        content_type="application/json",
    )
    assert add.status_code == 200, add.data
    body = json.loads(add.data)
    assert body["ok"] is True
    words = [e["word"] for e in body["words"]]
    assert "premium" in words

    remove = admin_client.delete(
        "/api/forbidden_words",
        data=json.dumps({"category": "Lighting", "word": "premium"}),
        content_type="application/json",
    )
    assert remove.status_code == 200, remove.data
    body = json.loads(remove.data)
    assert "premium" not in [e["word"] for e in body["words"]]


def test_api_rejects_invalid_severity(admin_client, tmp_fw_file):
    resp = admin_client.post(
        "/api/forbidden_words",
        data=json.dumps({"category": "Lighting", "word": "x",
                         "severity": "explosive"}),
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_valid_severities_constant_matches_documentation():
    # Anchored constant; if you add a new severity, update the UI and the
    # AI prompt block alongside this test.
    assert VALID_SEVERITIES == ("block", "warn")
