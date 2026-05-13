"""
AI content generation utilities for PIS System
Handles all Gemini AI-powered content generation
"""

import json
import os
import time
import re
from typing import Any
from google import genai
from google.genai import types
from .category_classifier import classify_product_category
from .json_utils import safe_json_loads
from .prompt_manager import get_prompt

_MODEL = 'gemini-2.5-flash'
_client = None

def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv('GOOGLE_API_KEY'))
    return _client


def _require_prompt(name: str) -> str:
    """Load a prompt template by name and raise a clear error if it's missing.

    `get_prompt()` returns Optional[str] because a prompt key might not exist
    in the DB or in the default-prompts fallback. Every callsite needs the
    template to be a real string, so we centralise the None guard here."""
    template = get_prompt(name)
    if not template:
        raise RuntimeError(f"Prompt template '{name}' is missing from DB and defaults")
    return template


def _wait_for_file_upload(uf: Any) -> Any:
    """Poll a Gemini file-upload handle until processing completes.

    Wraps the standard `uf.state.name == 'PROCESSING'` pattern with explicit
    None checks: the Google GenAI SDK types `state` and `name` as Optional
    even though they're populated after a successful upload. Returns the
    final handle (refetched on every poll like the original code did).

    Typed as `Any` because the SDK's `File` class isn't always importable
    cleanly and the caller just hands the handle straight back into
    `generate_content(contents=...)`."""
    if uf is None:
        raise RuntimeError("Gemini file upload returned no handle")
    # `state` may be None on first inspection; treat that as "not processing"
    # so we don't deadlock waiting on a status that never arrives.
    while uf.state is not None and uf.state.name == "PROCESSING":
        time.sleep(1)
        file_name = uf.name
        if not file_name:
            # Without a name we can't refetch — break out and trust the handle.
            break
        uf = _get_client().files.get(name=file_name)
        if uf is None:
            raise RuntimeError("Gemini file lookup returned None during processing")
    return uf


def _response_text(response) -> str:
    """Return `response.text` as a real string. The SDK types it Optional
    even though the API always returns *some* text body."""
    return (getattr(response, 'text', None) or '')


def generate_pis_data(file_paths, model_name, url_data) -> dict[str, Any]:
    """Generate single PIS data from uploaded file(s) and/or website data.
    
    Args:
        file_paths: A single file path string, a list of file paths, or empty list/None.
        model_name: The product model name.
        url_data: Scraped website data dict.
    """
    # Normalize file_paths
    if file_paths is None:
        file_paths = []
    elif isinstance(file_paths, str):
        file_paths = [file_paths]
    
    # 1. Upload files to Gemini (if any)
    uploaded_files = []
    for fp in file_paths:
        if fp:
            uf = _get_client().files.upload(file=fp)
            uf = _wait_for_file_upload(uf)
            uploaded_files.append(uf)
    
    # Context Construction
    web_context = ""
    image_candidates_str = ""
    if url_data.get('text'):
        web_context = f"WEBSITE TEXT CONTENT: {url_data['text']}\n\nWEBSITE HTML (Partial): {url_data['html']}"
        candidates = url_data.get('image_candidates', [])
        image_candidates_str = "IMAGE CANDIDATES (Ranked by crawler):\n" + "\n".join([f"- {url}" for url in candidates])

    # Build source description based on what's available
    if uploaded_files and web_context:
        source_instruction = f"Analyze ALL {len(uploaded_files)} uploaded document(s) (Proforma Invoices/Spec Sheets) AND the provided Website Context. Cross-reference information across all sources."
    elif uploaded_files:
        source_instruction = f"Analyze ALL {len(uploaded_files)} uploaded document(s) (Proforma Invoices/Spec Sheets) thoroughly."
    else:
        source_instruction = "Analyze the provided Website Context thoroughly. Extract all product details from the website data."

    # Load prompt from admin-editable prompt manager
    prompt_template = _require_prompt('pis_extraction')
    prompt = prompt_template.format(
        model_name=model_name,
        source_instruction=source_instruction,
        image_candidates_str=image_candidates_str,
        web_context=web_context
    )
    
    # Build content list: prompt + any uploaded files
    content_parts = [prompt] + uploaded_files
    
    response = _get_client().models.generate_content(
        model=_MODEL,
        contents=content_parts,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    # Normalize: AI sometimes returns a list-wrapped single product or null;
    # callers (api.py, marketing.py) treat the return value as a PIS dict and
    # call .get('header_info', ...) on it, so coerce non-dict shapes to {}.
    parsed = safe_json_loads(response.text, fallback={})
    if isinstance(parsed, list):
        parsed = parsed[0] if parsed and isinstance(parsed[0], dict) else {}
    if not isinstance(parsed, dict):
        parsed = {}
    return parsed


# Common English suffix forms appended to a banned root so the scrubber
# catches plurals and simple inflections without pulling in a stemming lib.
# Order matters: longest first so "experienced" matches `ed` before bare word.
_SUFFIX_FORMS = ('ments', 'ment', 'able', 'ings', 'ing', 'ies', 'ied',
                 'ers', 'ed', 'es', 'er', 'ly', 's')


def _normalize_forbidden_entries(forbidden_words):
    """Accept either:
        - list of strings   (legacy)
        - list of dicts     ({word, replace_with, severity, ...})
        - mixed list
    and return a clean list of dict entries with all three required fields.
    Empty / falsy / malformed items are dropped silently."""
    out = []
    seen = set()
    for raw in (forbidden_words or []):
        if isinstance(raw, str):
            word = raw.strip().lower()
            if not word or word in seen:
                continue
            seen.add(word)
            out.append({'word': word, 'replace_with': '', 'severity': 'block'})
        elif isinstance(raw, dict):
            word = (raw.get('word') or '').strip().lower()
            if not word or word in seen:
                continue
            seen.add(word)
            severity = raw.get('severity', 'block')
            if severity not in ('block', 'warn'):
                severity = 'block'
            out.append({
                'word':         word,
                'replace_with': (raw.get('replace_with') or '').strip(),
                'severity':     severity,
            })
    return out


def _compile_forbidden_patterns(entries):
    """Pre-compile one regex per entry that matches the base word plus
    common English suffix forms (plural / -ing / -ed / hyphenated compound).

    Words ending in `e` get a second stem with the trailing `e` dropped
    so we catch the standard English inflection rule (experience →
    experiencing / experienced, not experienceing / experienceed).

    Returns a list of (entry, compiled_regex) pairs."""
    suffix_alt = '|'.join(_SUFFIX_FORMS)
    compiled = []
    for entry in entries:
        word = entry['word']
        stems = [re.escape(word)]
        if len(word) > 2 and word.endswith('e'):
            stems.append(re.escape(word[:-1]))   # experience → experienc
        stem_alt = '|'.join(stems)
        # \b (root|root-e) (suffix)? \b
        # `\b` handles hyphenated compounds because `-` is a non-word char,
        # so "experience-driven" still matches the base form.
        pat = re.compile(
            r'\b(?:' + stem_alt + r')(?:' + suffix_alt + r')?\b',
            re.IGNORECASE,
        )
        compiled.append((entry, pat))
    return compiled


def _scrub_forbidden_words(data, forbidden_words):
    """Recursively scrub forbidden words from every string in a nested
    dict/list. Returns (scrubbed_data, hits_dict).

    Behaviour per entry:
      - severity == 'block'  → match is replaced with `replace_with` (or
                               deleted + whitespace collapsed if empty).
      - severity == 'warn'   → text is left untouched but the hit is counted.

    `forbidden_words` accepts the legacy list-of-strings shape so callers
    that haven't been migrated keep working with default block behaviour."""
    entries = _normalize_forbidden_entries(forbidden_words)
    if not entries:
        return data, {}
    compiled = _compile_forbidden_patterns(entries)
    hits = {}

    def _scrub(node):
        if isinstance(node, str):
            text = node
            for entry, pattern in compiled:
                # Count matches for the hits report regardless of severity.
                matches = pattern.findall(text)
                if not matches:
                    continue
                hits[entry['word']] = hits.get(entry['word'], 0) + len(matches)
                if entry['severity'] == 'warn':
                    # Don't mutate text for warn-level rules — they exist so
                    # the human sees the flag in the UI, not to silently rewrite.
                    continue
                text = pattern.sub(entry['replace_with'], text)
            # Tidy: collapse runs of whitespace, strip stray punctuation that
            # got orphaned when a word was deleted (e.g. " , " → ", ").
            text = re.sub(r'\s+([,.;:!?])', r'\1', text)
            text = re.sub(r'\s{2,}', ' ', text).strip()
            return text
        if isinstance(node, list):
            return [_scrub(item) for item in node]
        if isinstance(node, dict):
            return {k: _scrub(v) for k, v in node.items()}
        return node

    return _scrub(data), hits


def lint_text_for_forbidden(text, forbidden_words):
    """Read-only sibling of _scrub_forbidden_words: returns a hits dict
    describing what forbidden words appear in `text` without mutating it.
    Used by the client-side linter via a small JSON endpoint."""
    if not text or not forbidden_words:
        return {}
    _, hits = _scrub_forbidden_words(text, forbidden_words)
    return hits


def _build_seo_context(pis_data, spec_data=None, categories=None):
    """Pull a clean, prompt-ready set of product-context variables out of the
    PIS / spec_data blobs. Centralised so both the full SpecSheet generation
    and the SEO-only regeneration draw from the same source of truth.

    `categories` (optional) — canonical category dict from the caller. When
    provided, it wins over any legacy JSON shape. Falls back to
    spec_data.categories then pis_data.category_data (the bulk-classifier
    output) — the same precedence get_product_category() applies.

    Every value is coerced to a non-empty string with a safe fallback so the
    prompt template's `str.format(...)` call never raises on missing data.
    """
    header  = (pis_data or {}).get('header_info') or {}
    variants = (pis_data or {}).get('variants') or []
    techspec = (pis_data or {}).get('technical_specifications') or {}

    # Category path — resolve against the same precedence as the canonical
    # helper: explicit caller-supplied dict, then spec_data.categories,
    # then pis_data.category_data. The legacy top-level category_A/B/C
    # references were ghost keys never actually written and are dropped.
    cats_canonical = categories if isinstance(categories, dict) else None
    cats_spec      = (spec_data or {}).get('categories') if isinstance(spec_data, dict) else None
    cats_pis       = (pis_data  or {}).get('category_data') if isinstance(pis_data,  dict) else None
    chosen = cats_canonical or cats_spec or cats_pis or {}
    cat_a = (chosen.get('category_1') or '').strip()
    cat_b = (chosen.get('category_2') or '').strip()
    cat_c = (chosen.get('category_3') or '').strip()
    category_path = ' > '.join([p for p in (cat_a, cat_b, cat_c) if p]) or 'Unknown'

    # Key specs — compact human-readable summary of the most search-relevant
    # technical attributes. Cap at ~6 entries to keep the prompt short.
    SPEC_KEYS_OF_INTEREST = (
        'screen size', 'display', 'resolution', 'capacity', 'volume',
        'power', 'wattage', 'voltage', 'colour', 'color', 'material',
        'energy class', 'energy rating', 'dimensions', 'weight',
    )
    key_spec_pairs = []
    if isinstance(techspec, dict):
        # First: anything matching a search-relevant key (case-insensitive substring).
        for k, v in techspec.items():
            if not v: continue
            k_low = str(k).lower()
            if any(needle in k_low for needle in SPEC_KEYS_OF_INTEREST):
                key_spec_pairs.append(f"{k}: {v}")
            if len(key_spec_pairs) >= 6:
                break
        # Pad with whatever else is available, up to 6 total.
        if len(key_spec_pairs) < 6:
            for k, v in techspec.items():
                if not v: continue
                pair = f"{k}: {v}"
                if pair in key_spec_pairs: continue
                key_spec_pairs.append(pair)
                if len(key_spec_pairs) >= 6:
                    break
    key_specs = '; '.join(key_spec_pairs) if key_spec_pairs else 'n/a'

    # Variant labels — comma-separated colour/size labels, capped.
    variant_labels = ''
    if isinstance(variants, list) and variants:
        labels = []
        for v in variants[:8]:
            if isinstance(v, dict):
                lbl = v.get('label') or v.get('colour') or v.get('color') or v.get('model_number')
                if lbl: labels.append(str(lbl))
        variant_labels = ', '.join(labels) or 'n/a'
    else:
        variant_labels = 'n/a'

    return {
        'brand':          header.get('brand') or 'Unknown',
        'product_name':   header.get('product_name') or 'the product',
        'model_number':   header.get('model_number') or '',
        'category_path':  category_path,
        'key_specs':      key_specs,
        'variant_labels': variant_labels,
    }


def _build_forbidden_instruction(entries):
    """Compose the FORBIDDEN WORDS block injected into the AI prompt.

    When an entry has a `replace_with` value we surface it as guidance
    (e.g. *"experience" → use "feature" instead*) so the model has a
    concrete substitute rather than guessing one of its own."""
    if not entries:
        return ""
    block_lines = []
    warn_lines = []
    for e in entries:
        line = f'"{e["word"]}"'
        if e.get('replace_with'):
            line += f' → use "{e["replace_with"]}" instead'
        if e['severity'] == 'warn':
            warn_lines.append(line)
        else:
            block_lines.append(line)
    parts = []
    if block_lines:
        parts.append(
            "    **FORBIDDEN WORDS — CRITICAL RULE**:\n"
            "    The following words/phrases are STRICTLY FORBIDDEN and MUST NOT appear anywhere in your output.\n"
            "    Do NOT use them in any form (singular, plural, capitalized, hyphenated, etc.):\n"
            "    " + "\n    ".join(block_lines) + "\n"
            "    If you need to express a similar concept, prefer the suggested replacement or rephrase entirely.\n"
        )
    if warn_lines:
        parts.append(
            "    **PREFER NOT TO USE** (use only if no clean alternative exists):\n"
            "    " + "\n    ".join(warn_lines) + "\n"
        )
    return "\n\n" + "\n".join(parts) if parts else ""


def generate_comprehensive_spec_data(pis_data, forbidden_words=None, categories=None):
    """Generate comprehensive spec sheet data from PIS data.

    Args:
        pis_data: The PIS data dict.
        forbidden_words: Optional list of forbidden-word entries (objects with
            keys word/replace_with/severity) OR legacy list of plain strings.
            The post-AI scrubber catches anything the model emits despite the
            in-prompt warning.
        categories: Optional dict (`category_1`/`category_2`/`category_3`) —
            the caller's canonical category. When supplied, lands in
            spec_data['categories'] as-is; the AI classifier is NOT invoked.
            When None and the product has no canonical value, the classifier
            runs once as a last-resort fallback so the SpecSheet still
            classifies a never-seen-before product. After this refactor
            this is the ONLY classifier call in the spec pipeline (bulk
            import is the other).

    Returns:
        spec_data dict. If any forbidden words were caught during scrubbing
        a sentinel key `_forbidden_hits` is attached so the caller can log
        the report and then pop it before persisting.
    """
    # Extract sales arguments for strict prompt
    sales_arguments = pis_data.get('sales_arguments', [])

    fw_entries = _normalize_forbidden_entries(forbidden_words)
    forbidden_instruction = _build_forbidden_instruction(fw_entries)

    # Extra SEO context — brand / category / key specs / variants. Passed as
    # explicit slots so the SEO-tuned prompt can compose smarter titles and
    # descriptions without inferring everything from the sales arguments.
    seo_ctx = _build_seo_context(pis_data, categories=categories)

    # Load prompt from admin-editable prompt manager
    prompt_template = _require_prompt('spec_sheet_generation')
    prompt = prompt_template.format(
        sales_arguments_json=json.dumps(sales_arguments),
        forbidden_instruction=forbidden_instruction,
        **seo_ctx,
    )
    try:
        response = _get_client().models.generate_content(
            model=_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        spec_data = safe_json_loads(response.text, fallback={})
        
        # Ensure we have a dict if fallback was used
        if not isinstance(spec_data, dict): spec_data = {}
        
        # MANDATORY SAFETY NET: Enforce fallback if AI failed
        if (
            not spec_data.get("key_features")
            or not isinstance(spec_data["key_features"], list)
            or len(spec_data["key_features"]) == 0
        ):
            print("⚠️ AI returned empty/invalid key_features, falling back to PIS sales_arguments")
            spec_data["key_features"] = sales_arguments
        
        # --- POST-PROCESSING: Scrub forbidden words from all text fields ---
        # Attach the hits report as a sentinel key so the caller can log a
        # human-readable summary; web.py pops the key before persisting.
        if fw_entries:
            spec_data, hits = _scrub_forbidden_words(spec_data, fw_entries)
            if hits:
                spec_data['_forbidden_hits'] = hits

        # CATEGORY: prefer the caller's canonical value. Only run the AI
        # classifier as a last resort when the product has no category at
        # all (single-import products on their first SpecSheet generation).
        # After this refactor the classifier runs at most ONCE per product
        # — either in bulk enrichment or here, never both.
        if categories and (categories.get('category_1') or '').strip():
            spec_data["categories"] = {
                "category_1": categories.get('category_1', ''),
                "category_2": categories.get('category_2', ''),
                "category_3": categories.get('category_3', ''),
            }
        else:
            try:
                spec_data["categories"] = classify_product_category(pis_data)
            except Exception as e:
                print(f"❌ Category classification error: {e}")
                spec_data["categories"] = {
                    "category_1": "Home & Garden",
                    "category_2": "Home Deco",
                    "category_3": "Lighting",
                }

        return spec_data
        
    except Exception as e:
        print(f"Spec Generation Error: {e}")
        import traceback
        traceback.print_exc()
        
        # HARD GUARANTEE: Always return valid structure with PIS data
        fallback_data = {
            "customer_friendly_description": pis_data.get('seo_data', {}).get('seo_long_description', ''),
            "key_features": sales_arguments,  # Direct 1-to-1 fallback
            "internal_web_keywords": pis_data.get('seo_data', {}).get('generated_keywords', ''),
            "seo": {
                "meta_title": pis_data.get('seo_data', {}).get('meta_title', ''),
                "meta_description": pis_data.get('seo_data', {}).get('meta_description', ''),
                "keywords": pis_data.get('seo_data', {}).get('generated_keywords', '')
            }
        }
        
        # Category: prefer the caller's canonical value; only classify when
        # there's no canonical yet AND the AI happy-path didn't return one.
        if categories and (categories.get('category_1') or '').strip():
            fallback_data["categories"] = {
                "category_1": categories.get('category_1', ''),
                "category_2": categories.get('category_2', ''),
                "category_3": categories.get('category_3', ''),
            }
        else:
            try:
                fallback_data["categories"] = classify_product_category(pis_data)
            except Exception as cat_error:
                print(f"❌ Category classification failed in fallback: {cat_error}")
                fallback_data["categories"] = {
                    "category_1": "Home & Garden",
                    "category_2": "Home Deco",
                    "category_3": "Lighting",
                }

        return fallback_data


def regenerate_seo_only(pis_data, spec_data=None, forbidden_words=None):
    """Regenerate ONLY the SEO metadata (meta_title, meta_description, keywords)
    for an existing SpecSheet — the rest of spec_data is left untouched.

    Used by the "Regenerate SEO" button in the SpecSheet editor so the team can
    iterate on SEO copy without resetting key_features or the customer-facing
    description. Returns a dict with shape {meta_title, meta_description, keywords}.

    Args:
        pis_data:        The PIS data dict.
        spec_data:       The current SpecSheet data (for context + safe fallback).
        forbidden_words: Optional list of forbidden-word entries; the freshly
                         generated SEO block is scrubbed before return so
                         banned words can't slip back in via regeneration.

    On failure, returns the existing SEO block from spec_data (or a sensible
    empty dict) so the caller can show an error without losing data.
    """
    spec_data = spec_data or {}
    ctx = _build_seo_context(pis_data, spec_data)
    # Give the prompt a peek at the current customer-facing description so it
    # can lift the strongest selling phrase rather than guessing.
    current_description = (spec_data.get('customer_friendly_description') or '').strip()
    if len(current_description) > 600:
        current_description = current_description[:600].rsplit(' ', 1)[0] + '…'
    ctx['current_description'] = current_description or 'n/a'

    # Forbidden-words guard for the SEO pass — same instruction format the
    # full SpecSheet generation uses so the model behaves consistently across
    # both pathways.
    fw_entries = _normalize_forbidden_entries(forbidden_words)
    ctx['forbidden_instruction'] = _build_forbidden_instruction(fw_entries)

    existing_seo = spec_data.get('seo') or {}
    fallback = {
        'meta_title':       existing_seo.get('meta_title', ''),
        'meta_description': existing_seo.get('meta_description', ''),
        'keywords':         existing_seo.get('keywords', ''),
    }

    try:
        prompt_template = _require_prompt('seo_regeneration')
        # The legacy seo_regeneration template doesn't reference
        # {forbidden_instruction}; tolerate that by stripping unknown slots.
        try:
            prompt = prompt_template.format(**ctx)
        except KeyError:
            ctx_minus_fw = {k: v for k, v in ctx.items() if k != 'forbidden_instruction'}
            prompt = prompt_template.format(**ctx_minus_fw)
            # Append the rule as a trailing block so the guidance still lands.
            if ctx['forbidden_instruction']:
                prompt += "\n" + ctx['forbidden_instruction']

        response = _get_client().models.generate_content(
            model=_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        result = safe_json_loads(response.text, fallback={})
        if not isinstance(result, dict):
            result = {}
        seo = {
            'meta_title':       (result.get('meta_title') or existing_seo.get('meta_title') or '').strip(),
            'meta_description': (result.get('meta_description') or existing_seo.get('meta_description') or '').strip(),
            'keywords':         (result.get('keywords') or existing_seo.get('keywords') or '').strip(),
        }
        # Scrub the regenerated SEO block so banned words can't sneak back in.
        if fw_entries:
            seo, _hits = _scrub_forbidden_words(seo, fw_entries)
        return seo
    except Exception as e:
        print(f"SEO Regeneration Error: {e}")
        import traceback
        traceback.print_exc()
        return fallback


# ══════════════════════════════════════════════════════════════════════════
# Phase 2 — Unified Proforma Import
# ══════════════════════════════════════════════════════════════════════════

# Phase 2.4: every extraction now starts with this Mauritius retail context
# so the AI doesn't have to be told the same local norms on every call.
_MAURITIUS_DEFAULT_CONTEXT = (
    "MAURITIUS RETAIL CONTEXT (always applies — use as background knowledge):\n"
    "- Mains electricity is 240V / 50Hz; plug type G (UK 3-pin).\n"
    "- Standard local warranty for major appliances: 2 years parts, 1 year labour.\n"
    "- The market is bilingual French / English — proforma documents may mix\n"
    "  the two. Translate every narrative field to English in the output.\n"
    "- Local pricing is in MUR (Mauritian Rupee, written `Rs` or `Rs.`). A\n"
    "  proforma in USD/EUR/CNY is supplier-side wholesale, not retail; keep\n"
    "  the original currency in `price_estimate` so reviewers can flag it.\n"
)


# Brand-context library: extra context injected into the prompt for
# local/Mauritian brands whose specs are scarce online. Keep keys lower-cased.
BRAND_CONTEXT_LIBRARY = {
    "belair": (
        "BelAir is a Mauritian premium-tier home appliance brand. Online "
        "documentation is sparse, so when you cannot find a documented spec, "
        "deduce the standard premium-grade spec for this product type "
        "(e.g. inverter compressor, A++ energy class, multi-airflow cooling "
        "for fridges). Mark all such inferences as ai_enriched_details."
    ),
    "kenstar": (
        "Kenstar is an India-based mid-range appliance brand widely sold in "
        "Mauritius. When specs are missing from the document, deduce typical "
        "mid-range specs and mark them as ai_enriched_details."
    ),
    "sunon": (
        "Sunon is a Chinese office-furniture manufacturer. Specs printed on "
        "Sunon proformas are usually accurate (dimensions, weight capacity, "
        "material). For missing fields, infer typical commercial-grade office "
        "chair / desk specs (ergonomic adjustments, mesh back, gas lift)."
    ),
    "tcl": (
        "TCL is a mass-market Chinese electronics brand sold across Mauritius "
        "supermarket chains. Assume mid-range smart-TV defaults (Google TV / "
        "Android TV platform, HDR10 support, 60Hz panel) when not documented."
    ),
    "hisense": (
        "Hisense is a mid-range Chinese electronics manufacturer. For TVs, "
        "default to VIDAA OS, HDR10, 4K UHD on 50\"+ models when not stated."
    ),
}


def _resolve_brand_context(brand_hint: str | None) -> str:
    """Return the prompt-ready brand-context block. The Mauritius default is
    always prepended so every extraction gets the local retail context."""
    block = _MAURITIUS_DEFAULT_CONTEXT
    if not brand_hint:
        return block
    key = brand_hint.strip().lower()
    for known, ctx in BRAND_CONTEXT_LIBRARY.items():
        if known in key:
            return block + f"\nBRAND CONTEXT — {brand_hint}:\n{ctx}\n"
    return block


_MODE_INSTRUCTIONS = {
    "auto": "Auto-detect the category. Examine the document and apply the Clustering Algorithm below.",
    "single": "The reviewer has confirmed this is a SINGLE PRODUCT proforma. Output exactly one product object — never split.",
    "multiple": "The reviewer has confirmed this proforma contains MULTIPLE DISTINCT PRODUCTS. Treat each model line as its own product. Use the variants rule only for true colour/size variations of the same base model.",
}


def _upload_files_to_gemini(file_paths):
    if file_paths is None:
        file_paths = []
    elif isinstance(file_paths, str):
        file_paths = [file_paths]
    uploaded = []
    for fp in file_paths:
        if not fp:
            continue
        uf = _get_client().files.upload(file=fp)
        uf = _wait_for_file_upload(uf)
        uploaded.append(uf)
    return uploaded


def _build_url_context(url_data):
    """Build the prompt-ready blocks injected into proforma_extraction:
        - web_context           — text + HTML + (Phase 2.4) structured data
        - image_candidates_str  — bulleted list of candidate hero-shot URLs
    Phase 2.4: structured data (JSON-LD Product + OpenGraph + Twitter card)
    is rendered as a clearly-marked AUTHORITATIVE block so the AI prefers
    it over re-reading the raw HTML.
    """
    web_context = ""
    image_candidates_str = ""
    if not url_data:
        return web_context, image_candidates_str

    structured = url_data.get('structured_data') or {}
    structured_block = ""
    if structured.get('jsonld_products') or structured.get('og') or structured.get('twitter'):
        try:
            structured_block = (
                "\nSTRUCTURED DATA (AUTHORITATIVE — extracted from the page's "
                "own metadata, prefer over rendered HTML):\n"
                + json.dumps(structured, ensure_ascii=False, indent=2)[:6000]
                + "\n"
            )
        except Exception:
            structured_block = ""

    if url_data.get('text') or structured_block:
        web_context = (
            f"{structured_block}"
            f"WEBSITE TEXT CONTENT: {url_data.get('text', '')}\n\n"
            f"WEBSITE HTML (Partial): {url_data.get('html', '')}"
        )
        candidates = url_data.get('image_candidates', [])
        image_candidates_str = "IMAGE CANDIDATES (Ranked by crawler):\n" + "\n".join(
            [f"- {url}" for url in candidates]
        )
    return web_context, image_candidates_str


def generate_proforma_data(
    file_paths,
    url_data,
    extraction_mode: str = "auto",
    brand_hint: str | None = None,
    prior_data: list | None = None,
    feedback: str | None = None,
):
    """Unified proforma extraction.

    Returns a list of product dicts, each with `source_facts`,
    `ai_enriched_details`, and optional `variants`.

    Args:
        file_paths: One or many uploaded document paths (PDF/DOCX/img).
        url_data:   Output of scrape_url_data(...) — may be empty.
        extraction_mode: 'auto' | 'single' | 'multiple'. Guides the prompt.
        brand_hint: Optional brand name to inject brand-context into prompt.
        prior_data: Previous extraction (for the rework flow).
        feedback:   Reviewer feedback text (triggers the rework prompt).
    """
    uploaded_files = _upload_files_to_gemini(file_paths)
    web_context, image_candidates_str = _build_url_context(url_data or {})
    mode = (extraction_mode or "auto").lower()
    if mode not in _MODE_INSTRUCTIONS:
        mode = "auto"
    mode_instruction = _MODE_INSTRUCTIONS[mode]
    brand_context_block = _resolve_brand_context(brand_hint)

    is_rework = bool(feedback)
    prompt_id = "proforma_rework" if is_rework else "proforma_extraction"
    prompt_template = _require_prompt(prompt_id)

    fmt_kwargs = {
        "extraction_mode": mode,
        "mode_instruction": mode_instruction,
        "brand_context": brand_context_block,
        "image_candidates_str": image_candidates_str,
        "web_context": web_context,
    }
    if is_rework:
        fmt_kwargs["prior_data_json"] = json.dumps(prior_data or [], ensure_ascii=False)
        fmt_kwargs["feedback"] = feedback

    prompt = prompt_template.format(**fmt_kwargs)
    content_parts = [prompt] + uploaded_files

    response = _get_client().models.generate_content(
        model=_MODEL,
        contents=content_parts,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    result = safe_json_loads(response.text, fallback={})

    # Normalise: always return a list of product dicts.
    if isinstance(result, list):
        products = result
    elif isinstance(result, dict):
        products = result.get("products")
        if not isinstance(products, list):
            # Single product returned at top-level
            products = [result]
    else:
        products = []
    return products


def generate_bulk_pis_data(file_paths, url_data, product_filter="") -> list[dict[str, Any]]:
    """Generate bulk PIS data for multiple products from one or more documents.
    
    Args:
        file_paths: A single file path string, a list of file paths, or empty list/None.
        url_data: Scraped website data dict.
        product_filter: Optional newline-separated string of specific product names/models to extract.
    """
    # Normalize file_paths
    if file_paths is None:
        file_paths = []
    elif isinstance(file_paths, str):
        file_paths = [file_paths]
    
    # Upload all files to Gemini
    uploaded_files = []
    for fp in file_paths:
        if fp:
            uploaded_file = _get_client().files.upload(file=fp)
            uploaded_file = _wait_for_file_upload(uploaded_file)
            uploaded_files.append(uploaded_file)
    
    web_context = ""
    image_candidates_str = ""
    if url_data.get('text'):
        web_context = f"WEBSITE TEXT CONTENT: {url_data['text']}\n\nWEBSITE HTML (Partial): {url_data['html']}"
        candidates = url_data.get('image_candidates', [])
        image_candidates_str = "IMAGE CANDIDATES (Ranked by crawler):\n" + "\n".join([f"- {url}" for url in candidates])

    # Build product filter instruction
    product_filter_instruction = ""
    if product_filter:
        filter_lines = [line.strip() for line in product_filter.split('\n') if line.strip()]
        if filter_lines:
            product_list_str = "\n".join([f"- {p}" for p in filter_lines])
            product_filter_instruction = f"""
    **PRODUCT FILTER — CRITICAL**:
    Extract ONLY the following specific products. Ignore all other products in the document/URL:
{product_list_str}
    Match by product name, model number, or any close variation. If a listed product is not found, skip it.
    """

    # Load prompt from admin-editable prompt manager
    filter_instruction = "Identify ONLY the specific products listed in the PRODUCT FILTER below." if product_filter_instruction else "Identify EVERY unique product model listed."
    prompt_template = _require_prompt('bulk_pis_extraction')
    prompt = prompt_template.format(
        filter_instruction=filter_instruction,
        product_filter_instruction=product_filter_instruction,
        image_candidates_str=image_candidates_str,
        web_context=web_context
    )
    
    content_parts = [prompt] + uploaded_files
    
    response = _get_client().models.generate_content(
        model=_MODEL,
        contents=content_parts,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    # Normalize: AI sometimes wraps the products list under a key like
    # "products", or returns a single dict for a one-product document.
    # Callers iterate the return value, so always hand back a list of dicts.
    parsed = safe_json_loads(response.text, fallback=[])
    if isinstance(parsed, dict):
        inner = parsed.get("products")
        parsed = inner if isinstance(inner, list) else [parsed]
    if not isinstance(parsed, list):
        return []
    return [p for p in parsed if isinstance(p, dict)]


def generate_specsheet_optimization(product_data):
    """Generate spec sheet optimization suggestions."""
    # Load prompt from admin-editable prompt manager
    prompt_template = _require_prompt('spec_optimization')
    prompt = prompt_template.format(product_data_json=json.dumps(product_data))
    try:
        response = _get_client().models.generate_content(
            model=_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        return safe_json_loads(response.text, fallback={})
    except:
        return {}


def generate_ai_revision(section_name, original_content, director_comment):
    """
    Uses Gemini to rewrite content based on Director's feedback.
    Ensures correct data types:
    - sales_arguments -> List[str]
    - technical_specifications -> Dict[str, str]
    - header_info -> Dict with fixed keys
    - others -> str
    """

    # ---------- FORMAT ENFORCEMENT ----------
    if section_name == "sales_arguments":
        format_instr = (
            "Output MUST be a valid JSON array of strings.\n"
            "Each sales argument MUST be its own list item.\n"
            "Do NOT combine points into sentences.\n"
            "Do NOT return a single string."
        )
    elif isinstance(original_content, list):
        format_instr = "Output a valid JSON array of strings."
    elif isinstance(original_content, dict):
        format_instr = "Output a valid JSON object with key-value pairs."
    else:
        format_instr = "Return plain rewritten text only."

    # Load prompt from admin-editable prompt manager
    original_content_str = json.dumps(original_content, ensure_ascii=False) if isinstance(original_content, (dict, list)) else original_content
    prompt_template = _require_prompt('ai_revision')
    prompt = prompt_template.format(
        section_name=section_name,
        original_content=original_content_str,
        director_comment=director_comment,
        format_instr=format_instr
    )

    try:
        response = _get_client().models.generate_content(model=_MODEL, contents=prompt)
        result = _response_text(response).strip()

        # ---------- CLEAN MARKDOWN ----------
        if result.startswith("```"):
            result = (
                result.replace("```json", "")
                      .replace("```python", "")
                      .replace("```", "")
                      .strip()
            )

        # ---------- PARSING ----------
        try:
            parsed = json.loads(result)

            # ---------- HARD TYPE ENFORCEMENT ----------
            if section_name == "sales_arguments":
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
                return [str(parsed)]

            if isinstance(original_content, dict) and isinstance(parsed, dict):
                return parsed

            if isinstance(original_content, list) and isinstance(parsed, list):
                return parsed

            return parsed

        except Exception:
            # ---------- FAILSAFE FALLBACKS ----------
            if section_name == "sales_arguments":
                # Split common AI separators safely
                return [
                    x.strip()
                    for x in re.split(r'[;\n•\-]', result)
                    if x.strip()
                ]

            if isinstance(original_content, list):
                return [x.strip() for x in result.split("\n") if x.strip()]

            return result

    except Exception as e:
        print(f"AI Revision Error [{section_name}]: {e}")
        return original_content
