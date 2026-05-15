"""
Phase 2.4 — Single-item proforma wizard (image-extraction pipeline).

Backs the 4-step `/import_proforma/single/*` flow:
    1. /scan         — upload doc, lightweight Gemini pre-scan for the
                       target model name + brand + model number.
    2. /find_url     — auto-discover supplier URL from the model name.
    3. /extract      — try doc-side image (skip AI verify); on failure
                       fall back to web screenshot crops (up to 3).
    4. /finalize     — full proforma extraction + Product creation with
                       the user-selected image.

This file owns:
  • The in-memory wizard session store (UUID → state, 30-min TTL).
    NOTE: process-local; multi-worker setups must use sticky session
    routing or move this to Redis. Marked TODO for a follow-up.
  • Structured NDJSON log helpers used by the streaming endpoints.
  • Quick-scan, supplier-URL, doc-image, and web-image candidate helpers.

Auto / Multiple modes are unaffected — they continue to hit the original
streaming endpoint in `marketing.py`.
"""

import os
import json
import time
import uuid
import threading
import logging

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

from concurrent.futures import ThreadPoolExecutor, as_completed

from .image_processing import (
    clean_search_query, resolve_brand_domain,
    _search_google_pages,
    simple_google_search, simple_bing_search, simple_ddg_search,
    scrape_images_from_url,
    download_web_image, is_bad_image_url,
    discover_urls,
    ai_select_best_image,
)
from .pdf_processing import (
    extract_specific_image, extract_product_from_image,
    extract_isolated_product_with_nano_banana,
)


# ── Gemini client ────────────────────────────────────────────────────────────
_MODEL = 'gemini-2.5-flash'

# Phase 3.0: thread-local — see ai_generation.py. Kept consistent across
# every module that holds a Gemini client; avoids surprises if a single-mode
# helper ever ends up called from a worker thread.
import threading as _threading
_thread_local = _threading.local()


def _get_client():
    c = getattr(_thread_local, 'client', None)
    if c is None:
        c = genai.Client(api_key=os.getenv('GOOGLE_API_KEY'))
        _thread_local.client = c
    return c


# ── Session store ───────────────────────────────────────────────────────────
# TODO(multi-worker): swap for Redis/DB-backed store when we run >1 worker
# without sticky sessions. Single-process Gunicorn is fine.
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


# ── Structured NDJSON logger ────────────────────────────────────────────────
# Each helper returns one NDJSON line ready to be `yield`-ed from a Flask
# stream_with_context generator. Frontend appends to a scrollable log panel.

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
    """Emit any structured payload as one NDJSON line (e.g. result blob)."""
    return json.dumps(fields) + "\n"


# ── Step 2: lightweight pre-scan for product name ───────────────────────────

_QUICK_SCAN_PROMPT = """You are scanning a single uploaded product document.
Extract ONLY these fields and return strict JSON.

{
  "product_name":  "marketing name (string)",
  "brand":         "manufacturer brand (string)",
  "model_number":  "SKU / model code (string)"
}

If a field is unknown leave it as an empty string. Reply with JSON only,
no prose.
"""


def render_proforma_preview(file_paths: list[str], target_model: str,
                            upload_folder: str) -> str | None:
    """Return a relative `uploads/...` path to a static-servable preview of
    the uploaded proforma — used by the wizard so the user can manually
    crop the source themselves while AI/web pipelines run in parallel.

    For images: returns the original upload path (already inside upload_folder).
    For PDFs: renders the page that mentions `target_model` (or page 1) at 2×
    and saves it as a PNG. Multi-file uploads only the FIRST file is
    previewed — single-product wizard rarely has more than one proforma.

    Returns None when the source isn't accessible or rendering fails.
    """
    if not file_paths:
        return None
    fp = file_paths[0]
    if not fp or not os.path.exists(fp):
        return None

    ext = os.path.splitext(fp)[1].lower()
    try:
        if ext in ('.jpg', '.jpeg', '.png', '.webp'):
            # Image is already saved in upload_folder under its secure_filename.
            return f"uploads/{os.path.basename(fp)}"

        if ext == '.pdf':
            import fitz  # PyMuPDF — already a hard dep
            doc = fitz.open(fp)
            target_page = 0
            model_parts = [p for p in (target_model or '').split() if len(p) >= 4]
            for page_num in range(min(15, len(doc))):
                page_text = doc[page_num].get_text("text").upper()
                if any(p.upper() in page_text for p in model_parts):
                    target_page = page_num
                    break
            pix = doc[target_page].get_pixmap(matrix=fitz.Matrix(2, 2))
            png_bytes = pix.tobytes("png")
            doc.close()

            stem = os.path.splitext(os.path.basename(fp))[0]
            from werkzeug.utils import secure_filename
            safe_stem = secure_filename(stem) or 'proforma'
            filename = f"proforma_preview_{safe_stem}_{int(time.time())}.png"
            save_path = os.path.join(upload_folder, filename)
            with open(save_path, 'wb') as f:
                f.write(png_bytes)
            return f"uploads/{filename}"

    except Exception as e:
        print(f"⚠ render_proforma_preview failed: {e}")
        return None

    return None


def quick_scan_for_name(file_paths: list[str]) -> dict:
    """Upload the first doc, ask Gemini for product_name / brand / model_number.
    Falls back to the filename stem if the API call fails."""
    fallback = {"product_name": "", "brand": "", "model_number": ""}
    if not file_paths:
        return fallback

    fp = file_paths[0]
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
            contents=[_QUICK_SCAN_PROMPT, uploaded],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        parsed = json.loads(response.text or "{}")
        return {
            "product_name":  (parsed.get("product_name")  or "").strip(),
            "brand":         (parsed.get("brand")         or "").strip(),
            "model_number":  (parsed.get("model_number")  or "").strip(),
        }
    except Exception as e:
        print(f"⚠ quick_scan_for_name failed: {e}")
        stem = os.path.splitext(os.path.basename(fp))[0]
        return {"product_name": stem, "brand": "", "model_number": ""}


# ── Image triage: does the proforma actually have product photos? ───────────

_TRIAGE_HAS_IMAGES_PROMPT = """You are scanning a supplier proforma / spec sheet.
Examine ALL pages. Decide whether it contains product photographs.

Reply with strict JSON:
{ "has_images": "all" | "partial" | "none" }

Definitions:
- "all"     — every line item / SKU has a product photo next to it.
- "partial" — some items have photos, some don't (mixed catalog).
- "none"    — no product photos at all (text-only invoice, line-item
              table, plain spec sheet without imagery).

Logos, header banners, decorative icons, and tiny thumbnails of UI
elements do NOT count as product photos. Reply with JSON only, no prose.
"""


def triage_has_images(file_paths: list[str]) -> str:
    """Quick Gemini triage — does the uploaded proforma carry product
    photos? Returns one of 'all' | 'partial' | 'none'. On any failure
    returns 'partial' (conservative: keeps doc-side extraction enabled).

    Used by the wizard's image-extraction step to decide whether to run
    PDF bbox/embedded scraping at all. When the doc is text-only ('none')
    we skip straight to the web pipeline — saves 5-10s and prevents
    nano-banana from hallucinating a product from a blank page.
    """
    if not file_paths:
        return 'none'

    fp = file_paths[0]
    if not fp or not os.path.exists(fp):
        return 'none'

    try:
        uploaded = _get_client().files.upload(file=fp)
        for _ in range(30):
            if uploaded.state.name != "PROCESSING":
                break
            time.sleep(0.5)
            uploaded = _get_client().files.get(name=uploaded.name)

        response = _get_client().models.generate_content(
            model=_MODEL,
            contents=[_TRIAGE_HAS_IMAGES_PROMPT, uploaded],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        parsed = json.loads(response.text or "{}")
        val = str(parsed.get("has_images") or "").strip().lower()
        if val in ('all', 'partial', 'none'):
            return val
        return 'partial'
    except Exception as e:
        print(f"⚠ triage_has_images failed: {e}")
        return 'partial'


# ── User-supplied URL image extraction ──────────────────────────────────────

def extract_images_from_user_url(suggested_url: str,
                                  model_name: str,
                                  upload_folder: str,
                                  max_results: int = 3,
                                  log_cb=None) -> list[dict]:
    """Pull images from a URL the user pasted in (e.g. they found the
    exact product page themselves). Reuses the same scrape + download
    primitives as the auto-discovered supplier URL path so validation,
    dimension/size checks, and de-bad-domain filtering stay consistent.

    Returns the same shape as `extract_image_candidates_from_web`:
    `[{"path": "uploads/...", "page_url": <suggested_url>}, ...]`.
    """
    def _emit(msg: str) -> None:
        if log_cb:
            try: log_cb(msg)
            except Exception: logger.debug("emit/log callback failed", exc_info=True)

    if not suggested_url or not suggested_url.strip():
        return []
    suggested_url = suggested_url.strip()
    if not (suggested_url.startswith('http://') or suggested_url.startswith('https://')):
        _emit(f"Ignoring non-HTTP URL: {suggested_url}")
        return []

    try:
        scraped = scrape_images_from_url(suggested_url) or []
        _emit(f"Scraped {len(scraped)} image URL(s) from suggested page.")
    except Exception as e:
        _emit(f"Suggested-URL scrape failed: {e}")
        return []

    seen: set[str] = set()
    queue: list[str] = []
    for u in scraped:
        if not u or u in seen:
            continue
        if is_bad_image_url(u):
            continue
        seen.add(u)
        queue.append(u)

    if not queue:
        _emit("No usable image URLs on the suggested page.")
        return []

    results: list[dict] = []
    for url in queue[:max_results * 4]:
        if len(results) >= max_results:
            break
        rel_path = download_web_image(url, model_name or 'product', upload_folder)
        if rel_path:
            _emit(f"Downloaded → {rel_path}")
            results.append({"path": rel_path, "page_url": suggested_url})

    return results


# ── Step 3: supplier URL discovery ──────────────────────────────────────────

# Hosts we never want to land on as the "supplier URL" — search engines,
# generic marketplaces with stale results, social, etc.
_BAD_URL_HOSTS = (
    "google.", "bing.", "duckduckgo.", "startpage.", "yahoo.",
    "facebook.", "instagram.", "twitter.", "x.com",
    "youtube.", "wikipedia.", "pinterest.",
    "amazon.", "ebay.", "alibaba.", "aliexpress.",
)


def _is_useful_supplier_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    if not (u.startswith("http://") or u.startswith("https://")):
        return False
    return not any(bad in u for bad in _BAD_URL_HOSTS)


def _strip_query_punctuation(s: str) -> str:
    """Search engines treat `"` as exact-match phrase delimiters and `'`
    inconsistently. For a verbatim model-name search we want plain tokens
    so we drop both. Whitespace gets collapsed."""
    if not s:
        return ""
    cleaned = s.replace('"', ' ').replace("'", ' ').replace('“', ' ').replace('”', ' ')
    return " ".join(cleaned.split())


def discover_supplier_url(model_name: str, brand: str | None = None) -> dict:
    """Search the web for the supplier product page and return the best
    organic result + a small candidate list.

    Engines (cheapest → most expensive, tried in order until one returns
    anything for a given query):
      1. Google Custom Search API  (sub-second; no-op if not configured)
      2. Google HTTP scrape         (one GET, ~500 ms)
      3. Bing HTTP scrape           (one GET, ~500 ms)
      4. DuckDuckGo HTML lite       (one POST, ~500 ms)

    Query strategy — three passes, decreasing specificity, bailing at the
    first one that finds anything:
      Pass 1: `{brand} {model_name}` (only if the brand isn't already in the
              name).  SKU-style model names like "2D WARDROBE-FELIX WALNUT"
              return 0 on every engine when searched alone — there's no
              category context to ground on.  Adding the brand is what
              turns this into "find me the SUNON 2D wardrobe".
      Pass 2: `{model_name}` verbatim (the previous behaviour — fine for
              distinctive names like "Hisense 55A6K").
      Pass 3: `{model_name} site:{brand_official_domain}`, when we have a
              known brand domain.  Last resort for highly-internal SKUs
              that only appear on the manufacturer's catalog.

    Worst case ≈3 × 4 × 500 ms ≈ 6 s, but we early-exit on the first hit
    so the typical query stays under ~1 s.

    Returns: {"url": str|None, "candidates": [str]}.
    """
    if not model_name:
        return {"url": None, "candidates": []}

    base_query = _strip_query_punctuation(clean_search_query(model_name))
    if not base_query:
        return {"url": None, "candidates": []}

    brand_clean = (brand or "").strip()
    brand_domain = resolve_brand_domain(brand_clean)

    queries: list[str] = []
    if brand_clean and brand_clean.lower() not in base_query.lower():
        queries.append(f"{brand_clean} {base_query}")
    queries.append(base_query)
    if brand_domain:
        queries.append(f"{base_query} site:{brand_domain}")

    candidates: list[str] = []
    for q in queries:
        # Per-query cascade — first engine to return non-empty wins. The
        # `or` chain short-circuits on truthy lists, mirroring the previous
        # cascade but per query instead of once across all queries.
        results = (
            _search_google_pages(q)
            or simple_google_search(q, max_results=8)
            or simple_bing_search(q, max_results=8)
            or simple_ddg_search(q, max_results=8)
            or []
        )
        if results:
            candidates.extend(results)
            break  # this query worked — no need for broader variants

    # Dedupe + drop search-engine / social / marketplace pages.
    seen, cleaned = set(), []
    for u in candidates:
        if not _is_useful_supplier_url(u):
            continue
        if u in seen:
            continue
        seen.add(u)
        cleaned.append(u)

    # Prefer the brand's official domain when we recognize it — this is
    # independent of the query strategy above (a brand-locked query may
    # still surface third-party reseller pages first).
    chosen = None
    if brand_domain:
        for u in cleaned:
            if brand_domain in u.lower():
                chosen = u
                break
    if not chosen and cleaned:
        chosen = cleaned[0]

    return {"url": chosen, "candidates": cleaned[:5]}


# ── Step 4 helper: try doc image first (no AI verify) ───────────────────────

def extract_image_from_document(file_paths: list[str], target_model: str,
                                upload_folder: str) -> list[str]:
    """Try every uploaded file in turn — same product-detection pipeline
    regardless of upload format:

      • PDFs go through `extract_specific_image` with `prefer_embedded=True`:
        embedded JPEG/PNG streams come out at original quality (pixel-perfect),
        falling back to screenshot+bbox crop only when no embedded photo
        matches the model's page-text neighborhood.
      • Standalone images (.jpg/.png/.webp) go through
        `extract_product_from_image` so we ALSO bbox-crop to isolate the
        product. Both paths run with `all_matches=True` so multi-view rows
        (open + closed wardrobe, etc.) yield one candidate per view and the
        wizard shows them all for the user to pick from.

    Returns a list of relative `uploads/...` paths (possibly empty). Empty
    list → caller falls through to the web image pipeline.

    `skip_verify=True` per the wizard contract — the user picks the image
    manually so a second AI verification pass is redundant.
    """
    if not file_paths:
        return []

    # Run bbox/embedded extraction AND nano-banana isolation concurrently
    # for each uploaded file. Nano-banana saves us in cases where the bbox
    # crop is too tight (e.g. Gemini returned 99×67 for a low-res proforma)
    # by returning an AI-isolated render of the product. Both kinds of
    # candidate end up in the wizard grid; the user picks one.
    from concurrent.futures import ThreadPoolExecutor

    def _bbox_for(fp: str) -> list[str]:
        ext = os.path.splitext(fp)[1].lower()
        if ext == '.pdf':
            return extract_specific_image(
                fp, target_model, upload_folder,
                skip_verify=True, all_matches=True, prefer_embedded=True,
            ) or []
        if ext in ('.jpg', '.jpeg', '.png', '.webp'):
            return extract_product_from_image(
                fp, target_model, upload_folder,
                skip_verify=True, all_matches=True,
            ) or []
        return []

    def _nano_for(fp: str) -> list[str]:
        try:
            rel = extract_isolated_product_with_nano_banana(
                fp, target_model, upload_folder,
            )
            return [rel] if rel else []
        except Exception as e:
            print(f"  ⚠ Nano-banana wrapper failed: {e}")
            return []

    collected: list[str] = []
    for fp in file_paths:
        if not fp or not os.path.exists(fp):
            continue
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_bbox = pool.submit(_bbox_for, fp)
            f_nano = pool.submit(_nano_for, fp)
            collected.extend(f_bbox.result() or [])
            collected.extend(f_nano.result() or [])

    # Dedupe while preserving order.
    seen, deduped = set(), []
    for p in collected:
        if p and p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped


# ── Top-3 pages helper: search the web, scrape each result page ─────────────

def extract_images_from_top_pages(model_name: str,
                                    brand: str | None,
                                    upload_folder: str,
                                    max_pages: int = 3,
                                    max_per_page: int = 1,
                                    exclude_urls: list[str] | None = None,
                                    log_cb=None,
                                    cancel_event=None) -> list[dict]:
    """Web-search by KEYWORD → take top N organic page URLs → scrape each
    page's gallery / OG tags → download up to `max_per_page` images per page.

    This mirrors the user's manual workflow ("Extract from URL" but
    automated): we search Google web (NOT Google Image Search), pick the
    top product pages, and fetch images from each page directly. Images
    download more reliably this way because the supplier CDN sees a
    request that looks like a real browser visit, so hot-link protection
    on `cdn.supplier.com/image.jpg` rarely fires.

    `exclude_urls`: page URLs to skip (e.g. the supplier_url already
    scraped by the caller, to avoid duplicate work).

    Returns `[{"path": "uploads/...", "page_url": "https://..."}]`.
    """
    def _emit(msg: str) -> None:
        if log_cb:
            try: log_cb(msg)
            except Exception: logger.debug("emit/log callback failed", exc_info=True)

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    if not model_name or _cancelled():
        return []

    # Reuse the existing discovery cascade — same 3-pass query strategy
    # (brand+name, name verbatim, name+site:brand-domain) and engine
    # cascade (Google API → Google scrape → Bing → DDG).
    try:
        discovery = discover_supplier_url(model_name, brand) or {}
    except Exception as e:
        _emit(f"Top-pages discovery failed: {e}")
        return []

    candidates = discovery.get('candidates') or []
    excl = {u.strip().rstrip('/') for u in (exclude_urls or []) if u}
    pages: list[str] = []
    for u in candidates:
        if not u:
            continue
        key = u.strip().rstrip('/')
        if key in excl:
            continue
        pages.append(u)
        if len(pages) >= max_pages:
            break

    if not pages:
        _emit("Top-pages: no usable page URLs from web search")
        return []

    _emit(f"Top-pages: scraping {len(pages)} page(s)")
    results: list[dict] = []
    for page_url in pages:
        if _cancelled():
            break
        try:
            img_urls = scrape_images_from_url(page_url) or []
        except Exception as e:
            _emit(f"  · scrape failed for {page_url}: {e}")
            continue
        if not img_urls:
            continue

        # Filter known-bad domains + dedupe across pages.
        seen_in_page: set[str] = set()
        page_results = 0
        for img_url in img_urls:
            if _cancelled() or page_results >= max_per_page:
                break
            if not img_url or img_url in seen_in_page:
                continue
            if is_bad_image_url(img_url):
                continue
            seen_in_page.add(img_url)
            try:
                rel_path = download_web_image(img_url, model_name, upload_folder)
            except Exception as e:
                _emit(f"  · download {img_url} failed: {e}")
                continue
            if rel_path:
                results.append({"path": rel_path, "page_url": page_url})
                page_results += 1
        _emit(f"  · {page_url} → {page_results} image(s)")

    return results


# ── Step 4 helper: web fallback returning up to 3 candidates ────────────────

def extract_image_candidates_from_web(model_name: str,
                                       supplier_url: str | None,
                                       upload_folder: str,
                                       brand: str | None = None,
                                       max_results: int = 3,
                                       log_cb=None,
                                       cancel_event=None) -> list[dict]:
    """Phase 3.0 — slim web-image discovery: Gemini grounded search picks
    the top URLs, then we scrape each in parallel using the same
    page-scrape pipeline the manual "Extract from URL" flow uses.

    Flow:
      1. If `supplier_url` is given, scrape that page first (free, fastest).
      2. Ask Gemini (with Google Search grounding) for up to 2 authoritative
         product page URLs. Skips the cluster name being passed back as a
         supplier_url already covered.
      3. Scrape both URLs in parallel via `extract_images_from_user_url`,
         splitting the remaining quota evenly between them.

    Returns up to `max_results` `{"path": ..., "page_url": ...}` dicts.

    The legacy multi-engine SERP cascade and Playwright screenshot fallback
    are still importable from this module — they're now triggered only
    from Edit PIS via the user-clicked "search again" / "screenshot crop"
    actions, not from auto-extraction.

    `cancel_event`: optional `threading.Event` checked at each step so the
    wizard can abort once the user commits a candidate. In-flight HTTP
    requests still finish; this only prevents NEW work from starting.
    """
    def _emit(msg: str) -> None:
        if log_cb:
            try: log_cb(msg)
            except Exception: logger.debug("emit/log callback failed", exc_info=True)

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    if not model_name or _cancelled() or max_results <= 0:
        return []

    # ── Pool sizing for AI hero-pick ─────────────────────────────────────
    # Per-URL fetch cap (oversample so AI selection has real choice).
    # Total pool cap (bounds Vision-call cost + wall time).
    pool_per_url = max(max_results + 1, 3)
    pool_total = max(max_results * 2 + 2, 6)

    pool: list[dict] = []
    seen_paths: set[str] = set()

    def _absorb(items: list[dict]) -> bool:
        """Append items into the candidate pool, dedupe by path. Returns
        True once the pool is full enough to stop fetching more URLs."""
        for r in items or []:
            p = r.get('path') if isinstance(r, dict) else None
            if not p or p in seen_paths:
                continue
            seen_paths.add(p)
            pool.append(r)
            if len(pool) >= pool_total:
                return True
        return False

    # Tier 1 — caller-provided supplier URL (only used by the single-mode
    # flow when the user explicitly pastes a URL).
    if supplier_url and not _cancelled():
        try:
            sup = extract_images_from_user_url(
                supplier_url, model_name, upload_folder,
                max_results=pool_per_url,
            ) or []
            if sup:
                _emit(f"supplier URL → {len(sup)} image(s)")
                _absorb(sup)
        except Exception as e:
            _emit(f"supplier scrape failed: {type(e).__name__}")

    if _cancelled():
        return _ai_pick_and_trim(pool, model_name, max_results, _emit)

    # Tier 2 — URL discovery (Brave first, Gemini-grounded fallback) +
    # parallel page scrape. The orchestrator returns at most `max_results`
    # URLs and never raises.
    if len(pool) < pool_total:
        urls = discover_urls(
            model_name, brand=brand, max_results=2, log_cb=log_cb,
        )
        if supplier_url:
            sup_norm = supplier_url.strip().rstrip('/')
            urls = [u for u in urls if u.strip().rstrip('/') != sup_norm]

        if urls:
            def _fetch(u: str) -> list[dict]:
                if _cancelled():
                    return []
                try:
                    return extract_images_from_user_url(
                        u, model_name, upload_folder, max_results=pool_per_url,
                    ) or []
                except Exception as e:
                    _emit(f"page fetch failed ({type(e).__name__})")
                    return []

            with ThreadPoolExecutor(max_workers=len(urls)) as ex:
                futures = {ex.submit(_fetch, u): u for u in urls}
                for fut in as_completed(futures):
                    if _absorb(fut.result()):
                        break

    return _ai_pick_and_trim(pool, model_name, max_results, _emit)


def _ai_pick_and_trim(pool: list[dict], model_name: str,
                       max_results: int, emit) -> list[dict]:
    """Run Gemini Vision hero-selection over the candidate pool, move the
    AI's pick to position 0, then trim to `max_results`.

    Falls through quietly when:
      - The pool has fewer than 2 candidates (nothing to choose from)
      - Any image fails to load from disk (skip that one)
      - The Vision call returns nothing or errors (preserve original order)

    Caller (`_image_task` in bulk_wizard) treats `pool[0]` as the default
    hero shot, so reordering is the cheapest way to make the pick stick.
    """
    if not pool:
        return []
    if len(pool) < 2 or max_results <= 0:
        return pool[:max_results]

    upload_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    image_bytes_list: list[bytes] = []
    valid_indexes: list[int] = []
    for i, r in enumerate(pool):
        rel_path = (r.get('path') or '').lstrip('/')
        # `path` is stored relative to the static dir (e.g. uploads/foo.jpg).
        # The legacy single-mode wizard uses absolute paths for downloads,
        # so try the project-relative `static/<path>` first, then `<path>`
        # as a fallback for any caller that already passed absolute.
        candidates = [
            os.path.join(upload_root, 'static', rel_path),
            os.path.join(upload_root, rel_path),
            rel_path,
        ]
        b: bytes | None = None
        for cand in candidates:
            try:
                with open(cand, 'rb') as f:
                    b = f.read()
                break
            except OSError:
                continue
        if b:
            image_bytes_list.append(b)
            valid_indexes.append(i)

    if len(image_bytes_list) < 2:
        return pool[:max_results]

    try:
        ai_idx = ai_select_best_image(image_bytes_list, model_name)
    except Exception as e:
        try: emit(f"AI hero-pick failed ({type(e).__name__}) — keeping scrape order")
        except Exception: logger.debug("emit/log callback failed", exc_info=True)
        return pool[:max_results]

    if ai_idx is None or not (0 <= ai_idx < len(valid_indexes)):
        return pool[:max_results]

    pool_idx = valid_indexes[ai_idx]
    if pool_idx == 0:
        try: emit(f"AI hero-pick: kept #1 of {len(pool)}")
        except Exception: logger.debug("emit/log callback failed", exc_info=True)
        return pool[:max_results]

    # Move the AI's pick to the front so callers that take pool[0] as the
    # main image use the right one. Preserve the rest of the order so the
    # gallery still ranks runner-ups by Brave's original ranking.
    reordered = [pool[pool_idx]] + [r for j, r in enumerate(pool) if j != pool_idx]
    try: emit(f"AI hero-pick: promoted #{pool_idx + 1} of {len(pool)}")
    except Exception: logger.debug("emit/log callback failed", exc_info=True)
    return reordered[:max_results]
