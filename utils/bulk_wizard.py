"""
Bulk-import wizard — multi-product proforma flow (sibling to single_wizard).

Phase A delivers:
    • Session store keyed by triage_token (in-memory, 30 min TTL).
    • `triage_scan()` — one fast Gemini Flash call returning the document
      summary (density / has_images / origin / cluster_shape) plus a
      per-row item list with variant grouping. NOT the full extraction.

Subsequent phases will add:
    • cluster ops (merge / split / move),
    • full extraction with `batch_id`-tagged Product persistence,
    • lazy enrichment (image, content research, category),
    • commit / discard.

Pattern mirrors `utils/single_wizard.py` so reviewers can read both
together. Don't duplicate helpers across the two modules — anything used
by both should move to a shared `utils/_wizard_common.py` later.
"""

import os
import json
import time
import uuid
import threading

from google import genai
from google.genai import types

from .prompt_manager import get_prompt


# ── Gemini client ────────────────────────────────────────────────────────────
_MODEL = 'gemini-2.5-flash'
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv('GOOGLE_API_KEY'))
    return _client


# ── Session store (in-memory, 30 min TTL) ───────────────────────────────────
# TODO(multi-worker): same as single_wizard — needs Redis when we run >1 worker
# without sticky routing.
_SESSIONS: dict[str, dict] = {}
_LOCK = threading.Lock()
_TTL_SECONDS = 30 * 60


def _gc_locked() -> None:
    now = time.time()
    expired = [k for k, v in _SESSIONS.items() if v.get('_expires', 0) < now]
    for k in expired:
        _SESSIONS.pop(k, None)


def create_session(initial: dict | None = None) -> str:
    token = uuid.uuid4().hex
    payload = dict(initial or {})
    payload['_expires'] = time.time() + _TTL_SECONDS
    with _LOCK:
        _gc_locked()
        _SESSIONS[token] = payload
    return token


def get_session(token: str) -> dict | None:
    if not token:
        return None
    with _LOCK:
        sess = _SESSIONS.get(token)
        if not sess:
            return None
        sess['_expires'] = time.time() + _TTL_SECONDS
        return sess


def update_session(token: str, **fields) -> None:
    if not token:
        return
    with _LOCK:
        sess = _SESSIONS.get(token)
        if not sess:
            return
        sess.update(fields)
        sess['_expires'] = time.time() + _TTL_SECONDS


def drop_session(token: str) -> None:
    if not token:
        return
    with _LOCK:
        _SESSIONS.pop(token, None)


# ── Structured NDJSON logger (shared shape with single_wizard) ──────────────

_SEP = "─" * 56


def log_step(title: str) -> str:
    line = f"\n{_SEP}\n  {title.upper()}\n{_SEP}"
    print(line)
    return json.dumps({"log": {"type": "sep", "text": title}}) + "\n"


def log_info(msg: str) -> str:
    print(f"  · {msg}")
    return json.dumps({"log": {"type": "info", "text": msg}}) + "\n"


def log_ok(msg: str) -> str:
    print(f"  ✓ {msg}")
    return json.dumps({"log": {"type": "ok", "text": msg}}) + "\n"


def log_warn(msg: str) -> str:
    print(f"  ⚠ {msg}")
    return json.dumps({"log": {"type": "warn", "text": msg}}) + "\n"


def log_err(msg: str) -> str:
    print(f"  ✗ {msg}")
    return json.dumps({"log": {"type": "err", "text": msg}}) + "\n"


def log_progress(pct: int, msg: str | None = None) -> str:
    payload: dict = {"progress": int(pct)}
    if msg:
        payload["message"] = msg
    return json.dumps(payload) + "\n"


def log_payload(**fields) -> str:
    return json.dumps(fields) + "\n"


# ── Triage scan ─────────────────────────────────────────────────────────────

_DEFAULT_TRIAGE = {
    "summary": {
        "item_count":    0,
        "density":       "minimal",
        "has_images":    "none",
        "origin":        "unknown",
        "cluster_shape": "single",
        "notes":         "",
    },
    "items": [],
}


def _normalize_origin_hint(origin_hint: str | None) -> str:
    """User checkbox: True → 'kalachand_internal', False/None → 'external_supplier'.
    Keep 'unknown' available for callers that don't want to pre-bias the model.
    """
    h = (origin_hint or "").strip().lower()
    if h in ('kalachand', 'kalachand_internal', 'internal', 'true', '1', 'yes'):
        return 'kalachand_internal'
    if h in ('external', 'external_supplier', 'supplier', 'false', '0', 'no'):
        return 'external_supplier'
    return 'unknown'


def _validate_triage(parsed: dict) -> dict:
    """Coerce arbitrary AI output into the expected shape so downstream code
    doesn't have to defend against missing keys / wrong types."""
    out = json.loads(json.dumps(_DEFAULT_TRIAGE))   # deep copy
    if not isinstance(parsed, dict):
        return out

    summary = parsed.get('summary') or {}
    if isinstance(summary, dict):
        for k, default in out['summary'].items():
            v = summary.get(k, default)
            if k == 'item_count':
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    v = 0
            elif not isinstance(v, str):
                v = str(v) if v is not None else default
            out['summary'][k] = v

    items_in = parsed.get('items')
    items_out: list[dict] = []
    if isinstance(items_in, list):
        for idx, raw in enumerate(items_in):
            if not isinstance(raw, dict):
                continue
            # Coerce source_pages to a sorted, deduped list of non-negative
            # ints. Defaults to [0] when the model omits the field — a
            # single-page proforma is the most common case and we never want
            # an empty list (the slicer treats "no pages" as "use the whole
            # document", which defeats the purpose of variant-aware slicing).
            raw_pages = raw.get('source_pages')
            pages_clean: list[int] = []
            if isinstance(raw_pages, list):
                seen_pgs: set[int] = set()
                for p in raw_pages:
                    try:
                        pi = int(p)
                    except (TypeError, ValueError):
                        continue
                    if pi >= 0 and pi not in seen_pgs:
                        seen_pgs.add(pi)
                        pages_clean.append(pi)
            elif isinstance(raw_pages, (int, float)):
                pi = int(raw_pages)
                if pi >= 0:
                    pages_clean = [pi]
            if not pages_clean:
                pages_clean = [0]
            pages_clean.sort()

            entry = {
                "row_index":     int(raw.get('row_index', idx)) if str(raw.get('row_index', idx)).lstrip('-').isdigit() else idx,
                "name":          str(raw.get('name') or '').strip(),
                "brand":         str(raw.get('brand') or '').strip(),
                "model_number":  str(raw.get('model_number') or '').strip(),
                "price":         str(raw.get('price') or '').strip(),
                "category_hint": str(raw.get('category_hint') or '').strip(),
                "has_image":     bool(raw.get('has_image', False)),
                "variant_group": (str(raw['variant_group']).strip()
                                  if raw.get('variant_group') else None),
                "source_pages":  pages_clean,
            }
            items_out.append(entry)

    out['items'] = items_out
    out['summary']['item_count'] = len(items_out) or out['summary']['item_count']
    return out


def _build_feedback_section(feedback: str | None) -> str:
    """Format reviewer feedback into a prompt block. Returns empty string
    when feedback is missing/blank so the prompt template stays clean.

    The feedback is treated as AUTHORITATIVE — the language explicitly tells
    the model to prefer the human's instruction over the default clustering
    heuristics so global directives ("split everything", "merge rows 3–5")
    take effect instead of getting silently overridden by the rules above.
    """
    fb = (feedback or '').strip()
    if not fb:
        return ''
    return (
        "\n═════════════════ REVIEWER FEEDBACK (AUTHORITATIVE) ═════════════════\n"
        "A human reviewed the previous triage and left the note below. THIS\n"
        "FEEDBACK OVERRIDES the default clustering and variant-detection\n"
        "rules stated elsewhere in this prompt. Apply it LITERALLY:\n"
        "  • \"split all into separate PIS\" / \"each row is its own product\"\n"
        "    → set variant_group = null on EVERY item, set cluster_shape\n"
        "    to \"distinct\".\n"
        "  • \"rows 3–5 are variants of the same wardrobe\" / \"merge X and Y\"\n"
        "    → group those rows under one variant_group label.\n"
        "  • \"rename row 4 to X\" / \"the brand is wrong, it's actually Y\"\n"
        "    → apply the rename verbatim.\n"
        "  • \"this is sparse, not detailed\" / \"these have no images\"\n"
        "    → update the summary classification accordingly.\n"
        "Only fields the feedback does NOT mention may keep their previous\n"
        "values. Do not second-guess the human.\n\n"
        f"FEEDBACK NOTE:\n{fb}\n"
        "════════════════════════════════════════════════════════════════════"
    )


def triage_scan(file_paths: list[str], origin_hint: str | None = None,
                feedback: str | None = None) -> dict:
    """One fast Gemini Flash call. Uploads the FIRST file, asks for the
    document summary + per-row preview list. Returns the validated JSON
    (always conformant to `_DEFAULT_TRIAGE` shape, never raises).

    `feedback`: optional free-text from the reviewer applied to a re-scan
    (Phase B "Re-run with feedback" button). Empty/None → first-run scan.

    NOTE: bulk imports often have a single proforma PDF spanning many pages
    plus optional supplementary spec sheets. For triage we only feed the
    first file — extraction (later phase) will see all of them.
    """
    if not file_paths:
        return _validate_triage({})

    fp = file_paths[0]
    if not fp or not os.path.exists(fp):
        return _validate_triage({})

    prompt_template = get_prompt('bulk_triage_scan') or ''
    prompt = prompt_template.format(
        origin_hint=_normalize_origin_hint(origin_hint),
        feedback_section=_build_feedback_section(feedback),
    )

    try:
        uploaded = _get_client().files.upload(file=fp)
        # Wait up to ~15s for processing.
        for _ in range(30):
            if uploaded.state.name != "PROCESSING":
                break
            time.sleep(0.5)
            uploaded = _get_client().files.get(name=uploaded.name)

        response = _get_client().models.generate_content(
            model=_MODEL,
            contents=[prompt, uploaded],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        parsed = json.loads(response.text or "{}")
        return _validate_triage(parsed)

    except Exception as e:
        print(f"⚠ bulk triage_scan failed: {e}")
        # Return a minimal shape so the UI can still render something useful.
        out = _validate_triage({})
        out['summary']['notes'] = f"Triage failed: {e}"
        return out


# ── Cluster grouping (derived view of items) ────────────────────────────────

def build_stub_pis_from_cluster(cluster: dict, items: list[dict],
                                batch_id: str, origin_hint: str,
                                cluster_index: int = 0,
                                source_filenames: list[str] | None = None,
                                triage_summary: dict | None = None) -> dict | None:
    """Phase C — produce a minimal pis_data dict for ONE cluster from the
    triage items it covers. Skipped items are excluded. Returns None when
    every item in the cluster is skipped.

    The output is intentionally STUB-ONLY (no specs, no sales arguments, no
    range_overview yet) — Phase D's lazy enrichment populates those fields
    on demand. This keeps "Generate PIS" instant and lets the user start
    reviewing immediately. The wizard's contract:
      • For SINGLETON clusters → one PIS shaped from the lone row.
      • For VARIANT clusters   → one PIS where header_info comes from the
        first non-skipped row, and variants[] lists every non-skipped row.

    `_bulk_*` keys are added so Phase D's workspace can re-load this draft
    by batch and remember which triage rows informed it.
    """
    active_indexes = [
        idx for idx in cluster.get('item_indexes', [])
        if 0 <= idx < len(items) and not items[idx].get('skip')
    ]
    if not active_indexes:
        return None

    primary = items[active_indexes[0]] or {}
    is_variant_cluster = (cluster.get('kind') == 'variants' and len(active_indexes) > 1)

    # For variant clusters the stub should already look like a family-level
    # PIS — the cluster label is the GENERAL name (e.g. "Sunon 4D Wardrobe")
    # and model_number aggregates every variant's SKU. The previous
    # implementation copied the FIRST item's specific name (e.g. "4D
    # WARDROBE-OAK/WARM WHITE") and only one SKU, which made the stub —
    # and the post-enrich state, since header_info wasn't being overwritten
    # — read like a single-product PIS.
    if is_variant_cluster:
        cluster_label = (cluster.get('label') or '').strip()
        # Concatenate every variant's SKU, deduping while preserving order.
        sku_seen, sku_list = set(), []
        for idx in active_indexes:
            sku = ((items[idx] or {}).get('model_number') or '').strip()
            if sku and sku not in sku_seen:
                sku_seen.add(sku)
                sku_list.append(sku)
        header = {
            'product_name':   cluster_label or (primary.get('name') or '').strip(),
            'brand':          (primary.get('brand') or '').strip(),
            'model_number':   ', '.join(sku_list),
            'price_estimate': (primary.get('price') or '').strip(),
        }
    else:
        header = {
            'product_name':  (primary.get('name') or '').strip(),
            'brand':         (primary.get('brand') or '').strip(),
            'model_number':  (primary.get('model_number') or '').strip(),
            'price_estimate': (primary.get('price') or '').strip(),
        }

    variants: list[dict] = []
    if is_variant_cluster:
        # Each row becomes a variant entry. The primary's variant label
        # falls out from `name` so the user can see it next to the rest.
        # `source_pages` is preserved per-variant so the variant-aware
        # image pipeline can slice the proforma into mini-PDFs (one per
        # variant) before doing extraction.
        for idx in active_indexes:
            it = items[idx] or {}
            variants.append({
                'label':         (it.get('name') or '').strip(),
                'model_number':  (it.get('model_number') or '').strip(),
                'price':         (it.get('price') or '').strip(),
                'source_pages':  list(it.get('source_pages') or [0]),
            })

    pis = {
        'header_info':            header,
        'range_overview':         '',
        'sales_arguments':        [],
        'technical_specifications': {},
        'warranty_service':       {'period': '', 'coverage': ''},
        'seo_data':               {'generated_keywords': '', 'meta_title': '',
                                   'meta_description': '', 'seo_long_description': ''},
        # Phase B carry-overs: variants stay attached to the cluster.
        'variants':               variants,

        # Phase C bookkeeping (so Phase D's workspace can find/reload).
        '_bulk_batch_id':         batch_id,
        '_bulk_cluster_index':    cluster_index,
        '_bulk_cluster_kind':     cluster.get('kind') or 'singleton',
        '_bulk_cluster_label':    cluster.get('label') or header['product_name'],
        '_bulk_row_indexes':      list(active_indexes),
        # Aggregated source_pages across every active row in the cluster —
        # used by the variant-aware image pipeline to produce a single mini-
        # PDF when the cluster spans multiple rows on different pages
        # (e.g. a wardrobe family with each finish printed on its own page).
        '_bulk_source_pages':     sorted({
            p for idx in active_indexes
            for p in (items[idx] or {}).get('source_pages') or [0]
            if isinstance(p, int) and p >= 0
        }) or [0],
        '_bulk_origin_hint':      origin_hint,
        # Phase D: filenames let the enricher rebuild absolute paths via
        # current_app.config['UPLOAD_FOLDER'] long after the wizard session
        # has expired. The files themselves stay on disk under uploads/.
        '_bulk_source_filenames': list(source_filenames or []),
        # Mirror onto the legacy `_source_files` key used by
        # verify_marketing.html's right-side Source tab (web-relative paths
        # under /static). This way bulk drafts get the same proforma viewer
        # the single-product wizard has had since Phase 2.5.
        '_source_files': [f"uploads/{fn}" for fn in (source_filenames or []) if fn],
        # Carry the triage summary so the enricher can route image/content
        # tasks correctly (sparse + has_images='none' → web search;
        # detailed + has_images='all' → doc-only crops; etc.).
        '_bulk_triage_density':    (triage_summary or {}).get('density', 'minimal'),
        '_bulk_triage_has_images': (triage_summary or {}).get('has_images', 'none'),
        '_enrichment_status':     'pending',
        '_enrichment_tasks':      {
            'image':    'pending',
            'content':  'pending',
            'category': 'pending',
        },
    }

    # Use the cluster label as the displayed model_name when the row
    # didn't print a clear name. Falls back to model_number, then "Item N+1".
    model_name = (header['product_name']
                  or pis['_bulk_cluster_label']
                  or header['model_number']
                  or f"Item {cluster_index + 1}")
    pis['_bulk_model_name'] = model_name
    return pis


def _extract_variant_pis(file_paths: list[str], primary_name: str,
                         brand: str, variants: list[dict]) -> dict:
    """Run the bulk_variant_pis_extraction prompt against the uploaded
    document(s) — produces ONE PIS dict covering every variant in the
    cluster. Returns the validated dict (always conformant to the
    pis_extraction shape) or {} on failure so the caller can fall back."""
    if not file_paths or not primary_name or not variants:
        return {}

    # Format the variants block as a compact bulleted list the prompt can
    # quote back at the model.
    lines = []
    for v in variants:
        if not isinstance(v, dict):
            continue
        label = (v.get('label') or '').strip() or '(unnamed)'
        sku   = (v.get('model_number') or '').strip()
        price = (v.get('price') or '').strip()
        parts = [f"  - {label}"]
        if sku:   parts.append(f"· {sku}")
        if price: parts.append(f"· {price}")
        lines.append(' '.join(parts))
    variants_block = "\n".join(lines) if lines else "  (no variants listed)"

    prompt_template = get_prompt('bulk_variant_pis_extraction') or ''
    if not prompt_template:
        return {}
    prompt = prompt_template.format(
        primary_name=primary_name,
        brand=brand or '(unknown)',
        variants_block=variants_block,
        web_context='',
    )

    try:
        client = _get_client()
        # Upload every doc so the model sees full context.
        uploaded = []
        for fp in file_paths:
            uf = client.files.upload(file=fp)
            for _ in range(30):
                if uf.state.name != "PROCESSING":
                    break
                time.sleep(0.5)
                uf = client.files.get(name=uf.name)
            uploaded.append(uf)

        contents = [prompt] + uploaded
        response = client.models.generate_content(
            model=_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        # Use the project's tolerant parser — Gemini occasionally returns
        # markdown-fenced JSON or a stray trailing comma. Plain json.loads
        # bails on those (we hit "Expecting ',' delimiter" on the 4D
        # wardrobe variant cluster); safe_json_loads cleans them up.
        from utils.json_utils import safe_json_loads
        parsed = safe_json_loads(response.text or "", fallback={})
        return parsed if isinstance(parsed, dict) else {}
    except Exception as e:
        print(f"⚠ _extract_variant_pis failed: {e}")
        return {}


def _resolve_source_paths(filenames: list[str], upload_folder: str) -> list[str]:
    """Turn `_bulk_source_filenames` back into absolute paths via the
    current upload folder. Filters out any that no longer exist on disk
    (the user may have manually cleaned the uploads dir)."""
    resolved: list[str] = []
    for fn in filenames or []:
        if not fn:
            continue
        p = os.path.join(upload_folder, fn)
        if os.path.exists(p):
            resolved.append(p)
    return resolved


def _derive_product_type_query(out: dict, brand: str) -> str:
    """Build a generic product-type search query for the case where the
    exact model-name search returns nothing — typical for SKU-style
    proforma rows like "00438 POB ARIETE CREAM/BLINT" that no SERP knows.

    Pulls product type from category_data (if the classifier has run),
    then from the AI-deduced range_overview, falling back to a brand-only
    query. Color/finish hints come from the first variant label when the
    cluster is variant-shaped.

    Example outputs:
      "Ariete electric kettle cream"   (brand + type + color)
      "Sunon wardrobe oak"             (brand + type + finish)
      "Ariete electric kettle"         (no color hint available)
      ""                               (no signal at all → caller skips)
    """
    parts: list[str] = []
    if brand:
        parts.append(brand)

    # Product type from the category classifier (most reliable signal).
    cat = out.get('category_data') or {}
    if isinstance(cat, dict):
        for key in ('product_type', 'subcategory', 'category'):
            v = (cat.get(key) or '').strip()
            if v:
                parts.append(v)
                break

    # Fallback: first noun-phrase of the range_overview narrative.
    if len(parts) < 2:
        narrative = (out.get('range_overview') or '').strip()
        if narrative:
            # Take the first 6 words, drop common filler.
            stop = {'the', 'a', 'an', 'this', 'these', 'with', 'for', 'of'}
            tokens = [w for w in narrative.split()[:6]
                      if w.lower() not in stop]
            if tokens:
                parts.append(' '.join(tokens[:3]))

    # Color/finish from the first variant label (helps narrow visual search).
    variants = out.get('variants') or []
    if isinstance(variants, list) and variants:
        v0 = variants[0] if isinstance(variants[0], dict) else {}
        label = (v0.get('label') or '').strip()
        if label and label.lower() not in ' '.join(parts).lower():
            parts.append(label)

    return ' '.join(parts).strip() if len(parts) >= 2 else ''


def enrich_product(pis_data: dict, upload_folder: str,
                   tasks: list[str] | None = None) -> dict:
    """Phase D — fill in the rich PIS fields for ONE bulk-import draft.

    Runs three enrichment tasks in order so the image search can lean on
    content + category data when it runs:
        content  → category  → image

    Each task updates its own slot in `_enrichment_tasks` ('done' |
    'failed' | 'skipped') so the workspace UI can render per-task status.

    `tasks` filters which jobs to run (defaults to all three). The caller
    persists the returned dict back to `Product.pis_data`. The function is
    idempotent — running it twice on the same draft re-runs the enrichers
    and overwrites their last result, but never touches user-edited fields
    in `header_info` (those are treated as authoritative).

    Returns the updated pis_data dict (NOT mutated in place — caller can
    diff cleanly).
    """
    import copy

    out = copy.deepcopy(pis_data or {})
    out.setdefault('_enrichment_tasks', {'image': 'pending',
                                          'content': 'pending',
                                          'category': 'pending'})
    wanted = set(tasks or ['image', 'content', 'category'])

    file_paths = _resolve_source_paths(out.get('_bulk_source_filenames') or [],
                                       upload_folder)
    header = out.get('header_info') or {}
    target_name = (header.get('product_name') or '').strip() \
                   or (out.get('_bulk_cluster_label') or '').strip()
    brand = (header.get('brand') or '').strip()
    has_images = (out.get('_bulk_triage_has_images') or 'none').lower()
    # Variants give us additional names to search for — each variant in a
    # cluster (open vs closed wardrobe view, walnut vs oak finish) likely
    # has its own photo on the proforma and should appear as a candidate.
    variant_names: list[str] = []
    for v in (out.get('variants') or []):
        if isinstance(v, dict):
            label = (v.get('label') or '').strip()
            if label and label.lower() != target_name.lower():
                variant_names.append(label)

    # ── Task: image — extracts for primary AND every variant ──────────
    # Runs LAST (after content + category) so the product-type fallback
    # query can use category_data when the SKU-style model name fails.
    #
    # Routing by triage signal:
    #   has_images in ('all', 'partial')  — doc-side bbox/embedded
    #     extraction is the primary source; web is a backup.
    #   has_images == 'none'              — text-only proforma. NO doc
    #     extraction, NO auto-AI. Two-tier web only:
    #       Tier 1: supplier URL discovery + scrape via the discovered
    #               URL (same multi-engine cascade single uses).
    #       Tier 2: product-type search ("Brand + category + color"),
    #               only fires when Tier 1 returns nothing.
    #     Capped at 2 candidates per variant.
    def _image_task() -> dict:
        # Late imports — image_processing has heavy deps (Playwright,
        # PIL, etc.) so we don't pay for them when only content is enriched.
        from utils.single_wizard import (
            extract_image_from_document, extract_image_candidates_from_web,
            discover_supplier_url,
        )
        result = {'image_path': None, 'image_candidates': []}
        seen_paths: set[str] = set()

        def _push(path: str, source: str, page_url: str | None,
                  variant_label: str | None) -> None:
            if not path or path in seen_paths:
                return
            seen_paths.add(path)
            entry = {'path': path, 'page_url': page_url, 'source': source}
            if variant_label:
                entry['variant'] = variant_label
            result['image_candidates'].append(entry)

        # All names we'll search for. Primary first so it's the default thumb.
        all_targets = [target_name] + variant_names if target_name else variant_names

        try:
            # For text-only proformas, discover a supplier URL ONCE
            # (per draft) and reuse it for every variant search. The
            # single wizard does this the same way.
            supplier_url: str | None = None
            if has_images == 'none' and target_name:
                try:
                    sup = discover_supplier_url(target_name, brand or None) or {}
                    supplier_url = sup.get('url')
                except Exception as e:
                    print(f"⚠ supplier URL discovery for '{target_name}' failed: {e}")

            # Tier-2 fallback query (product type) — derived once from the
            # already-populated content + category data in `out`. Empty
            # when neither signal exists; caller skips that tier.
            product_type_query = (
                _derive_product_type_query(out, brand) if has_images == 'none' else ''
            )

            for tgt in all_targets:
                if not tgt:
                    continue
                # Doc-side extraction is only meaningful when triage saw photos.
                if file_paths and has_images in ('all', 'partial'):
                    try:
                        doc_paths = extract_image_from_document(
                            file_paths, tgt, upload_folder
                        ) or []
                    except Exception as e:
                        print(f"⚠ doc extract for '{tgt}' failed: {e}")
                        doc_paths = []
                    for p in doc_paths:
                        _push(p, 'document', None,
                              tgt if tgt != target_name else None)

                # Web — capped lower per-variant since this is a bulk pipeline.
                # Cap = 2 for the primary, 1 per variant.
                cap = 2 if tgt == target_name else 1
                try:
                    web = extract_image_candidates_from_web(
                        model_name=tgt, supplier_url=supplier_url,
                        upload_folder=upload_folder, brand=brand or None,
                        max_results=cap, log_cb=None,
                    ) or []
                except Exception as e:
                    print(f"⚠ web extract for '{tgt}' failed: {e}")
                    web = []
                for r in web:
                    _push(r['path'], 'web', r.get('page_url'),
                          tgt if tgt != target_name else None)

                # Tier 2 — product-type fallback. Only fires when:
                #   • this is a text-only proforma (has_images='none'), AND
                #   • Tier 1 (above) returned nothing for THIS target, AND
                #   • we have a usable type query.
                if (has_images == 'none' and not web and product_type_query):
                    try:
                        web2 = extract_image_candidates_from_web(
                            model_name=product_type_query, supplier_url=None,
                            upload_folder=upload_folder, brand=brand or None,
                            max_results=cap, log_cb=None,
                        ) or []
                    except Exception as e:
                        print(f"⚠ product-type web extract for '{tgt}' failed: {e}")
                        web2 = []
                    for r in web2:
                        _push(r['path'], 'web', r.get('page_url'),
                              tgt if tgt != target_name else None)

            # Pick the first doc-side candidate as the default thumbnail.
            # Variants stay in the candidate list for the workspace picker.
            doc_first = next(
                (c for c in result['image_candidates'] if c['source'] == 'document'),
                None,
            )
            if doc_first:
                result['image_path'] = doc_first['path']
            elif result['image_candidates']:
                result['image_path'] = result['image_candidates'][0]['path']
        except Exception as e:
            result['_error'] = f"Image task failed: {e}"
        return result

    # ── Task 2: content (range_overview, sales_arguments, specs, SEO, warranty) ──
    # Routes by cluster kind:
    #   singleton → existing pis_extraction prompt (same as single-mode wizard)
    #   variants  → new bulk_variant_pis_extraction prompt that produces ONE
    #               PIS covering ALL variants (mentions every variant in
    #               description, lists common specs with per-variant notes,
    #               highlights the range in sales arguments).
    def _content_task() -> dict:
        result: dict = {}
        try:
            if not file_paths or not target_name:
                result['_error'] = "Content task skipped (no source files or name)."
                return result

            cluster_kind = (out.get('_bulk_cluster_kind') or 'singleton').lower()
            variants_full = out.get('variants') or []

            from utils.ai_generation import generate_pis_data

            ai: dict = {}
            if cluster_kind == 'variants' and len(variants_full) > 1:
                ai = _extract_variant_pis(
                    file_paths, target_name, brand, variants_full,
                ) or {}
                # Variant extraction can return {} if Gemini's JSON had a
                # syntax error or the prompt template misfired. Fall back to
                # the same single-product extractor singleton clusters use,
                # so the user at least gets the primary variant's PIS rather
                # than an empty card.
                if not ai.get('range_overview'):
                    print("  ↩ Variant extraction empty — falling back to singleton path with primary name.")
                    ai = generate_pis_data(file_paths, target_name, {"text": "", "html": ""}) or {}
            else:
                ai = generate_pis_data(file_paths, target_name, {"text": "", "html": ""}) or {}

            if not isinstance(ai, dict):
                result['_error'] = "AI returned non-dict content"
                return result
            if not ai:
                result['_error'] = "AI returned empty content"
                return result
            # Copy the rich enrichment fields.
            for key in ('range_overview', 'sales_arguments',
                        'technical_specifications', 'warranty_service',
                        'seo_data'):
                if key in ai:
                    result[key] = ai[key]
            # For VARIANT clusters specifically, also surface the AI's
            # header_info — the variant prompt produces the right family
            # name + comma-separated SKUs that no stub heuristic can match.
            # The merger above will only apply this when the user hasn't
            # manually edited header_info (see `_user_edited_header` flag
            # set by the save endpoint).
            if cluster_kind == 'variants' and len(variants_full) > 1:
                ai_header = ai.get('header_info') or {}
                if isinstance(ai_header, dict) and ai_header:
                    result['_ai_header_info'] = ai_header
            # `seo_keywords` (Product column) is computed from seo_data later.
            seo = ai.get('seo_data') or {}
            if seo.get('generated_keywords'):
                result['_seo_keywords'] = seo['generated_keywords']
        except Exception as e:
            result['_error'] = f"Content task failed: {e}"
        return result

    # Sequential pass — content → category → image. Image runs LAST so
    # the product-type fallback query has access to category_data. The
    # previous parallel (image + content) layout meant the image task
    # always ran with empty content, which made the product-type tier
    # impossible.
    if 'content' in wanted:
        r = _content_task() or {}
        if r.get('_error'):
            out['_enrichment_tasks']['content'] = 'failed'
            out.setdefault('_enrichment_errors', {})['content'] = r['_error']
        else:
            for key in ('range_overview', 'sales_arguments',
                        'technical_specifications', 'warranty_service',
                        'seo_data'):
                if key in r:
                    out[key] = r[key]
            if r.get('_seo_keywords'):
                out['_seo_keywords_pending'] = r['_seo_keywords']
            # Apply AI header_info for variant clusters — but only when
            # the user hasn't manually overridden the header (the save
            # endpoint sets `_user_edited_header` once anything in
            # header_info is touched). On first enrich the flag isn't
            # set, so the family name + concatenated SKUs that the
            # variant prompt produces win out over the stub.
            ai_hdr = r.get('_ai_header_info') or {}
            if ai_hdr and not out.get('_user_edited_header'):
                cur = out.get('header_info') or {}
                merged = dict(cur)
                for hk in ('product_name', 'model_number',
                           'brand', 'price_estimate'):
                    v = ai_hdr.get(hk)
                    if isinstance(v, str) and v.strip():
                        merged[hk] = v.strip()
                out['header_info'] = merged

            # Origin map for the verify-PIS badge UI. Bulk extraction
            # doesn't split source_facts / ai_enriched_details the way
            # the single-product proforma flow does, so we grep-verify
            # each filled-in field against the uploaded Proforma's raw
            # text. Strict-fact rule applies: only Proforma-confirmed
            # values become 'verified' (yellow ✔); everything else
            # lands in the AI bucket (red ✨).
            try:
                from helpers import (
                    extract_raw_text_from_files,
                    classify_flat_pis_origins,
                )
                raw_doc_text = extract_raw_text_from_files(file_paths) or ""
                field_origins, spec_origins = classify_flat_pis_origins(
                    out, raw_doc_text
                )
                out['_field_origins'] = field_origins
                out['_spec_origins'] = spec_origins
            except Exception as e:
                print(f"⚠ origin classification failed for bulk PIS: {e}")

            out['_enrichment_tasks']['content'] = 'done'

    # ── Category (depends on content being filled in) ──────────────────
    if 'category' in wanted:
        try:
            from utils.category_classifier import classify_product_category
            result = classify_product_category(out) or {}
            if result and not result.get('error'):
                out['category_data'] = result
                out['_enrichment_tasks']['category'] = 'done'
            else:
                out['_enrichment_tasks']['category'] = 'failed'
        except Exception as e:
            out['_enrichment_tasks']['category'] = 'failed'
            out.setdefault('_enrichment_errors', {})['category'] = f"Category task failed: {e}"

    # ── Image (runs LAST — needs content + category context) ───────────
    if 'image' in wanted:
        r = _image_task() or {}
        if r.get('_error'):
            out['_enrichment_tasks']['image'] = 'failed'
            out.setdefault('_enrichment_errors', {})['image'] = r['_error']
        else:
            if r.get('image_path'):
                out['_image_path'] = r['image_path']
            if r.get('image_candidates'):
                out['_bulk_image_candidates'] = r['image_candidates']
            out['_enrichment_tasks']['image'] = 'done'

    # Mark overall status. 'done' if every wanted task finished cleanly,
    # 'partial' when some failed, 'failed' when all wanted tasks failed.
    statuses = [out['_enrichment_tasks'].get(t, 'pending') for t in wanted]
    if all(s == 'done' for s in statuses):
        out['_enrichment_status'] = 'done'
    elif any(s == 'done' for s in statuses):
        out['_enrichment_status'] = 'partial'
    else:
        out['_enrichment_status'] = 'failed'

    return out


def derive_cluster_groups(items: list[dict]) -> list[dict]:
    """Bucket items by `variant_group`. Items with `variant_group=None` each
    get their own singleton group. Output is ordered: variant groups first
    (by first-appearance), then singletons (by row_index).

    Each group: {"id", "label", "kind": "variants"|"singleton", "item_indexes": [int]}.
    Indexes refer to the position in the input list (so the frontend can
    splice items by index when the user reshapes clusters).
    """
    groups: list[dict] = []
    seen: dict[str, int] = {}      # variant_group label → groups[] index
    for i, item in enumerate(items):
        vg = item.get('variant_group')
        if vg:
            if vg not in seen:
                seen[vg] = len(groups)
                groups.append({
                    'id':           f"g_{i}",
                    'label':        vg,
                    'kind':         'variants',
                    'item_indexes': [i],
                })
            else:
                groups[seen[vg]]['item_indexes'].append(i)
        else:
            groups.append({
                'id':           f"g_{i}",
                'label':        item.get('name') or f"Item {i+1}",
                'kind':         'singleton',
                'item_indexes': [i],
            })
    return groups
