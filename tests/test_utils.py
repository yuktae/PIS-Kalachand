"""Tests for core utility functions that have no DB or network dependencies."""
from utils.json_utils import safe_json_loads


# ── safe_json_loads ────────────────────────────────────────────────────────────

def test_parses_plain_json_object():
    result = safe_json_loads('{"key": "value"}')
    assert result == {"key": "value"}


def test_parses_plain_json_list():
    result = safe_json_loads('[1, 2, 3]')
    assert result == [1, 2, 3]


def test_strips_markdown_code_fence():
    result = safe_json_loads('```json\n{"a": 1}\n```')
    assert result == {"a": 1}


def test_strips_markdown_code_fence_no_lang():
    result = safe_json_loads('```\n{"b": 2}\n```')
    assert result == {"b": 2}


def test_repairs_trailing_comma_in_object():
    result = safe_json_loads('{"x": 1, "y": 2,}')
    assert result == {"x": 1, "y": 2}


def test_repairs_trailing_comma_in_object_array():
    # _parse_truncated_list extracts complete {…} objects, so trailing commas
    # between/after objects are handled correctly.
    result = safe_json_loads('[{"a": 1}, {"b": 2},]')
    assert result == [{"a": 1}, {"b": 2}]


def test_extracts_completed_objects_from_truncated_list():
    truncated = '[{"a": 1}, {"a": 2}, {"a"'
    result = safe_json_loads(truncated)
    assert result == [{"a": 1}, {"a": 2}]


def test_ignores_leading_text_before_json():
    result = safe_json_loads('Here is the result:\n{"ok": true}')
    assert result == {"ok": True}


def test_returns_fallback_on_empty_string():
    assert safe_json_loads('', fallback=[]) == []


def test_returns_fallback_on_none():
    assert safe_json_loads(None, fallback=None) is None


def test_returns_fallback_on_invalid_json():
    assert safe_json_loads('not json at all', fallback={}) == {}
