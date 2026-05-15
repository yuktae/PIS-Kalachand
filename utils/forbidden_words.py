"""
Forbidden-words storage and category-merge logic.

Storage shape on disk: { "<category>": [entry, ...], ..., "__global__": [...] }

Each entry is either:
  (legacy)  "experience"                       — auto-coerced on read
  (current) { "word": "experience",
              "replace_with": "feature",       — optional, empty == delete
              "severity": "block" | "warn",    — defaults to "block"
              "reason": "SEO filler",          — optional, ≤80 chars
              "added_by": "Alice Wang",        — optional metadata
              "added_at": "2026-05-12T..." }   — ISO-8601 UTC

The reserved category key "__global__" applies to every product regardless
of its Magento category — used for site-wide bans (legal, brand voice).

This module owns the JSON file shape, normalization, and the per-category
+ global merge. AI-side enforcement (scrub/lint) lives in utils.ai_generation.
"""

import os
import json

from flask import current_app


GLOBAL_CATEGORY_KEY = '__global__'

VALID_SEVERITIES = ('block', 'warn')


def _forbidden_words_file():
    return os.path.join(current_app.config['BASE_DIR'], 'data', 'forbidden_words.json')


def _normalize_word_entry(raw):
    """Coerce a string OR dict into the canonical entry shape.

    Returns None if the input has no usable `word` value so callers can drop
    malformed entries without surfacing them to the UI."""
    if isinstance(raw, str):
        word = raw.strip().lower()
        if not word:
            return None
        return {'word': word, 'replace_with': '', 'severity': 'block'}
    if not isinstance(raw, dict):
        return None
    word = (raw.get('word') or '').strip().lower()
    if not word:
        return None
    severity = raw.get('severity', 'block')
    if severity not in VALID_SEVERITIES:
        severity = 'block'
    entry = {
        'word':         word,
        'replace_with': (raw.get('replace_with') or '').strip(),
        'severity':     severity,
    }
    # Optional governance fields — only carry them through if present so the
    # JSON file stays compact for entries that don't need them.
    for opt_key in ('reason', 'added_by', 'added_at'):
        val = raw.get(opt_key)
        if val:
            entry[opt_key] = str(val).strip()[:120] if opt_key == 'reason' else str(val).strip()
    return entry


def _normalize_category_list(raw_list):
    """Run every entry in a category through _normalize_word_entry and drop
    duplicates (keyed by lowercase word) preserving first-seen order."""
    if not isinstance(raw_list, list):
        return []
    seen = set()
    out = []
    for raw in raw_list:
        entry = _normalize_word_entry(raw)
        if entry is None:
            continue
        if entry['word'] in seen:
            continue
        seen.add(entry['word'])
        out.append(entry)
    return out


def load_forbidden_words():
    """Return the full forbidden-words map with every entry normalized.

    Always returns a dict; the legacy `{ cat: [str, ...] }` shape is upgraded
    on the fly so existing callers see the new object shape transparently."""
    try:
        with open(_forbidden_words_file(), 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {cat: _normalize_category_list(words) for cat, words in raw.items()}


def save_forbidden_words(data):
    """Persist the forbidden-words map. Strips empty categories and runs
    every entry through normalization so the file is always self-consistent
    on disk."""
    cleaned = {}
    for cat, words in (data or {}).items():
        normalized = _normalize_category_list(words)
        if normalized:
            cleaned[cat] = normalized
    fw_file = _forbidden_words_file()
    os.makedirs(os.path.dirname(fw_file), exist_ok=True)
    with open(fw_file, 'w', encoding='utf-8') as f:
        json.dump(cleaned, f, indent=2, ensure_ascii=False)


def get_forbidden_words_for_category(category_3):
    """Return the normalized entry list for a category, MERGED with the
    site-wide __global__ list. Per-category entries win on word collisions
    so a category-specific replacement overrides a global one."""
    data = load_forbidden_words()
    cat_entries = list(data.get(category_3) or []) if category_3 else []
    global_entries = list(data.get(GLOBAL_CATEGORY_KEY) or [])
    by_word = {}
    for e in global_entries:
        by_word[e['word']] = e
    for e in cat_entries:
        by_word[e['word']] = e   # category overrides global
    return list(by_word.values())


def get_forbidden_words_flat(category_3=None):
    """Return just the word strings for a category (+global) — convenience
    helper for legacy callers that only need the list of banned tokens."""
    entries = get_forbidden_words_for_category(category_3) if category_3 \
              else _all_entries_combined()
    return [e['word'] for e in entries]


def _all_entries_combined():
    """Flatten every category into a single deduped entry list. Used by
    callers that don't know the product category yet (e.g. legacy paths)."""
    data = load_forbidden_words()
    by_word = {}
    # Iterate __global__ first so per-category replacements override.
    for cat in [GLOBAL_CATEGORY_KEY] + [k for k in data if k != GLOBAL_CATEGORY_KEY]:
        for e in data.get(cat) or []:
            by_word[e['word']] = e
    return list(by_word.values())
