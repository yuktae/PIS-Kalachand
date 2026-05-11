"""
Unit tests for the unified image-extraction pipeline helpers.

These tests cover the pure SKU-matching functions that have no external
dependencies (no DB, no Gemini, no filesystem) and therefore always run.
"""
import pytest


# ── _normalise_sku ────────────────────────────────────────────────────────────

def test_normalise_sku_strips_separators():
    from utils.image_pipeline import _normalise_sku
    assert _normalise_sku("XDY60.120060-OAK-W") == "xdy60120060oakw"
    assert _normalise_sku("XDY60/120060 OAK W") == "xdy60120060oakw"


def test_normalise_sku_handles_empty():
    from utils.image_pipeline import _normalise_sku
    assert _normalise_sku("") == ""
    assert _normalise_sku(None) == ""  # type: ignore[arg-type]


def test_normalise_sku_lowercases():
    from utils.image_pipeline import _normalise_sku
    assert _normalise_sku("ABC123") == "abc123"


# ── _score_sku_match ─────────────────────────────────────────────────────────

def test_score_exact_match():
    from utils.image_pipeline import _score_sku_match
    assert _score_sku_match("abc123", "abc123") == 1.0


def test_score_substring_match():
    from utils.image_pipeline import _score_sku_match
    score = _score_sku_match("abc123", "abc123xyz")
    # shorter is substring of longer → 0.85 * len_ratio
    assert 0.5 < score < 1.0


def test_score_difflib_fallback():
    from utils.image_pipeline import _score_sku_match
    # High similarity but not exact/substring → difflib path
    score = _score_sku_match("abc1234", "abc1235")
    assert score > 0.0


def test_score_no_match():
    from utils.image_pipeline import _score_sku_match
    assert _score_sku_match("aaaaaaa", "zzzzzzz") == 0.0


def test_score_short_strings_return_zero():
    from utils.image_pipeline import _score_sku_match
    # Strings shorter than 3 chars are rejected
    assert _score_sku_match("ab", "ab") == 0.0


# ── _build_match_targets ──────────────────────────────────────────────────────

def test_build_match_targets_singleton():
    from utils.image_pipeline import _build_match_targets
    drafts = [{"id": 1, "kind": "singleton", "model_number": "SKU-001", "name": "Widget"}]
    targets = _build_match_targets(drafts)
    assert len(targets) == 1
    assert targets[0]["draft_id"] == 1
    assert "SKU-001" in targets[0]["search_strings"]


def test_build_match_targets_variants():
    from utils.image_pipeline import _build_match_targets
    drafts = [{
        "id": 2, "kind": "variants",
        "variants": [
            {"model_number": "SKU-A", "label": "Oak"},
            {"model_number": "SKU-B", "label": "Walnut"},
        ],
    }]
    targets = _build_match_targets(drafts)
    assert len(targets) == 2
    skus = {t["variant_sku"] for t in targets}
    assert skus == {"SKU-A", "SKU-B"}


def test_build_match_targets_skips_no_id():
    from utils.image_pipeline import _build_match_targets
    drafts = [{"kind": "singleton", "model_number": "X"}]  # no id
    assert _build_match_targets(drafts) == []


# ── _classify_assignment ─────────────────────────────────────────────────────

def test_classify_unambiguous():
    from utils.image_pipeline import _classify_assignment
    matches = [(1, "SKU-A", 0.95), (2, "SKU-B", 0.40)]
    tier, assign = _classify_assignment(matches)
    assert tier == "unambiguous"
    assert len(assign) == 1
    assert assign[0][0] == 1


def test_classify_tie():
    from utils.image_pipeline import _classify_assignment
    # Both high scores, gap < _UNAMBIGUOUS_GAP (0.20)
    matches = [(1, "SKU-A", 0.80), (2, "SKU-B", 0.75)]
    tier, assign = _classify_assignment(matches)
    assert tier == "tie"
    assert len(assign) == 2


def test_classify_weak():
    from utils.image_pipeline import _classify_assignment
    matches = [(1, "SKU-A", 0.50)]
    tier, assign = _classify_assignment(matches)
    assert tier == "weak"
    assert len(assign) == 1


def test_classify_orphan_empty():
    from utils.image_pipeline import _classify_assignment
    tier, assign = _classify_assignment([])
    assert tier == "orphan"
    assert assign == []


def test_classify_orphan_low_score():
    from utils.image_pipeline import _classify_assignment
    matches = [(1, "SKU-A", 0.20)]
    tier, assign = _classify_assignment(matches)
    assert tier == "orphan"


# ── _fuzzy_match_region ───────────────────────────────────────────────────────

def test_fuzzy_match_region_finds_exact():
    from utils.image_pipeline import _fuzzy_match_region
    targets = [{"draft_id": 1, "variant_sku": "SKU001",
                "search_strings": ["SKU001", "Widget"]}]
    results = _fuzzy_match_region("SKU001", targets)
    assert results
    assert results[0][0] == 1
    assert results[0][2] == 1.0


def test_fuzzy_match_region_empty_text():
    from utils.image_pipeline import _fuzzy_match_region
    targets = [{"draft_id": 1, "variant_sku": "", "search_strings": ["SKU001"]}]
    assert _fuzzy_match_region("", targets) == []
    assert _fuzzy_match_region("ab", targets) == []   # too short


def test_fuzzy_match_region_sorted_descending():
    from utils.image_pipeline import _fuzzy_match_region
    targets = [
        {"draft_id": 1, "variant_sku": "A", "search_strings": ["abc123def"]},
        {"draft_id": 2, "variant_sku": "B", "search_strings": ["abc123"]},
    ]
    results = _fuzzy_match_region("abc123", targets)
    # draft 2 exact match should score higher than draft 1 substring
    assert results[0][0] == 2
    assert results[0][2] > results[1][2]
