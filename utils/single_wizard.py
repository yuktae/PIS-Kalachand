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

from google import genai
from google.genai import types

from .image_processing import (
    clean_search_query, resolve_brand_domain,
    _search_google_pages,
    simple_google_search, simple_bing_search, simple_ddg_search,
    search_google_api, search_duckduckgo, scrape_images_from_url,
    download_web_image, is_bad_image_url,
    find_multi_images_via_screenshot,
)
from .pdf_processing import (
    extract_specific_image, extract_product_from_image,
    extract_isolated_product_with_nano_banana,
)


# ── Gemini client ────────────────────────────────────────────────────────────
_MODEL = 'gemini-2.5-flash'
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv('GOOGLE_API_KEY'))
    return _client


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


# ── Step 4 helper: web fallback returning up to 3 candidates ────────────────

def extract_image_candidates_from_web(model_name: str,
                                       supplier_url: str | None,
                                       upload_folder: str,
                                       brand: str | None = None,
                                       max_results: int = 3,
                                       log_cb=None,
                                       cancel_event=None) -> list[dict]:
    """Phase 2.4 — fast image discovery for the wizard.

    Order (most → least effective; cheapest first):
      1. Scrape the supplier URL HTML — most accurate when the URL is
         actually the product page.
      2. Google Custom Search Image API (brand-locked → unrestricted).
      3. DuckDuckGo Image search.
      4. Playwright SERP screenshot+crop — last resort, slow and brittle.

    `cancel_event`: optional `threading.Event` checked between every engine
    call. When set, the function returns whatever it has so far instead of
    starting another engine — used by the wizard endpoint to abort the
    pipeline when the user has already committed a candidate. Whichever
    HTTP/Playwright call is currently mid-flight will still finish; this
    only prevents NEW engine calls from starting.

    Each candidate URL is downloaded with `download_web_image` (which
    validates content-type, dimensions, and file size). The first
    `max_results` successful downloads are returned. If everything above
    fails we fall through to the screenshot pipeline.
    """
    def _emit(msg: str) -> None:
        if log_cb:
            try: log_cb(msg)
            except Exception: pass

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    if not model_name or _cancelled():
        return []

    clean = clean_search_query(model_name)
    photo_q = f"{clean} official photo"
    brand_domain = resolve_brand_domain(brand)
    candidate_urls: list[str] = []

    # 1. Direct scrape of supplier URL — most relevant if URL is the actual
    #    product page. Pulls images from product galleries / OG tags.
    if supplier_url and not _cancelled():
        try:
            scraped = scrape_images_from_url(supplier_url) or []
            _emit(f"Scraped {len(scraped)} images from supplier URL")
            candidate_urls.extend(scraped)
        except Exception as e:
            _emit(f"Supplier scrape failed: {e}")

    # 2. Google Image Search API — brand-locked first.
    if brand_domain and len(candidate_urls) < max_results * 3 and not _cancelled():
        urls = search_google_api(photo_q, domain=brand_domain) or []
        _emit(f"Google Image API (brand-locked): {len(urls)} results")
        candidate_urls.extend(urls)

    # 3. Google Image Search API — unrestricted.
    if len(candidate_urls) < max_results * 3 and not _cancelled():
        urls = search_google_api(photo_q) or []
        _emit(f"Google Image API: {len(urls)} results")
        candidate_urls.extend(urls)

    # 4. DuckDuckGo Image Search.
    if len(candidate_urls) < max_results * 3 and not _cancelled():
        urls = search_duckduckgo(photo_q, max_results=10) or []
        _emit(f"DuckDuckGo Images: {len(urls)} results")
        candidate_urls.extend(urls)

    if _cancelled():
        _emit("Web pipeline cancelled by client — bailing before download")
        return []

    # Dedupe & filter known-bad domains.
    seen: set[str] = set()
    queue: list[str] = []
    for u in candidate_urls:
        if not u or u in seen:
            continue
        if is_bad_image_url(u):
            continue
        seen.add(u)
        queue.append(u)

    if not queue:
        _emit("No candidate URLs from any engine — falling back to screenshot pipeline")
        return find_multi_images_via_screenshot(
            target_label=model_name, supplier_url=supplier_url,
            upload_folder=upload_folder, brand=brand,
            max_results=max_results, skip_verify=True, log_cb=log_cb,
            cancel_event=cancel_event,
        )

    # Try downloads until we have `max_results` successes.
    _emit(f"Trying {min(len(queue), max_results * 4)} candidate(s) for download...")
    results: list[dict] = []
    for url in queue[:max_results * 4]:
        if _cancelled():
            _emit("Download loop cancelled by client — bailing")
            break
        if len(results) >= max_results:
            break
        rel_path = download_web_image(url, model_name, upload_folder)
        if rel_path:
            _emit(f"Downloaded → {rel_path}")
            results.append({"path": rel_path, "page_url": url})

    if results:
        return results

    if _cancelled():
        return []

    # Last resort — screenshot+bbox. This is what the wizard used to do as
    # the only path; we keep it as a fallback so anti-hotlink-protected
    # supplier sites still produce something.
    _emit("Direct download failed for all candidates — falling back to screenshot pipeline")
    return find_multi_images_via_screenshot(
        target_label=model_name, supplier_url=supplier_url,
        upload_folder=upload_folder, brand=brand,
        max_results=max_results, skip_verify=True, log_cb=log_cb,
        cancel_event=cancel_event,
    )
