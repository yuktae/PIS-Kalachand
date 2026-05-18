"""
Image processing utilities for PIS System
Handles image search, validation, and downloading
"""

import os
import re
import io
import json
import time
import random
import requests
import shutil
import warnings
import logging
from urllib.parse import urlparse, urljoin, quote_plus

logger = logging.getLogger(__name__)

# Phase 3.0: gate the per-URL scrape/download/search trace prints behind
# an env var. Default = quiet (just the structured `[STAGE]` markers from
# bulk_wizard / single_wizard show up). Set IMAGE_DEBUG=1 to bring them
# back for diagnosing why a specific product returned 0 candidates.
_IMAGE_DEBUG = os.getenv("IMAGE_DEBUG", "").lower() in ("1", "true", "yes")


def _dbg(msg: str) -> None:
    """Print only when IMAGE_DEBUG=1 is set."""
    if _IMAGE_DEBUG:
        print(msg)


# Phase 2.3: silence the noisy `duckduckgo_search` deprecation warning.
# The `ddgs` package is what we actually want to use, but it transitively
# imports `duckduckgo_search`, which prints a RuntimeWarning on every call.
# It floods the logs without telling us anything actionable.
warnings.filterwarnings(
    "ignore",
    message=r".*duckduckgo_search.*has been renamed to.*",
    category=RuntimeWarning,
)
from werkzeug.utils import secure_filename
from google import genai
from google.genai import types
from concurrent.futures import ThreadPoolExecutor, as_completed

_MODEL = 'gemini-2.5-flash'

# Phase 3.0: thread-local Gemini clients — see comment in ai_generation.py.
# Sharing one genai.Client across the bulk-extract worker pool causes
# "Cannot send a request, as the client has been closed" once any thread
# finishes a Files-API request.
import threading as _threading
_thread_local = _threading.local()


def _get_client():
    c = getattr(_thread_local, 'client', None)
    if c is None:
        c = genai.Client(api_key=os.getenv('GOOGLE_API_KEY'))
        _thread_local.client = c
    return c
from PIL import Image
from .prompt_manager import get_prompt

# DuckDuckGo image search (free, no API key needed).
# Phase 2.3: prefer the maintained `ddgs` package; fall back to the old
# `duckduckgo_search` import for environments that haven't been re-pip'd yet.
DDGS = None
HAS_DDGS = False
try:
    from ddgs import DDGS  # type: ignore
    HAS_DDGS = True
except ImportError:
    try:
        from duckduckgo_search import DDGS  # type: ignore
        HAS_DDGS = True
    except ImportError:
        print("⚠ Neither `ddgs` nor `duckduckgo_search` installed — DDG fallback disabled")


# Phase 2.3: brand → official domain map. When the AI recognizes one of
# these brands we lock the search to that domain via `site:` to bias toward
# canonical hero shots. Keep keys lowercase, no www.
BRAND_OFFICIAL_DOMAINS = {
    "belair":  "belair.mu",
    "kenstar": "kenstar.in",
    "sunon":   "sunon.com",
    "samsung": "samsung.com",
    "lg":      "lg.com",
    "sony":    "sony.com",
    "philips": "philips.com",
    "bosch":   "bosch-home.com",
    "miele":   "miele.com",
    "panasonic": "panasonic.com",
    "haier":   "haier.com",
    "tcl":     "tcl.com",
    "hisense": "hisense.com",
    "dyson":   "dyson.com",
    "xiaomi":  "mi.com",
}


def resolve_brand_domain(brand: str | None) -> str | None:
    """Return the official domain for a recognized brand or None."""
    if not brand:
        return None
    key = (brand or "").strip().lower()
    for known, domain in BRAND_OFFICIAL_DOMAINS.items():
        if known in key:
            return domain
    return None


# ── Rate-limit prevention helpers (Phase 2.3) ───────────────────────────────

def _human_jitter(min_s: float = 2.0, max_s: float = 5.0) -> None:
    """Sleep a random amount between requests so bulk runs don't hit the
    same engine on a perfectly regular cadence (which is the easiest pattern
    for the engine to flag and 403)."""
    delay = random.uniform(min_s, max_s)
    time.sleep(delay)


def _is_rate_limited(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in (
        "ratelimit", "rate limit", "rate-limit", "too many",
        "api_key_service_blocked", "403", "429", "quota",
    ))


def _retry_with_backoff(fn, *args, max_attempts: int = 3, base_delay: float = 1.5, **kwargs):
    """Run `fn(*args, **kwargs)`, retrying on rate-limit-shaped errors with
    progressively longer delays. Returns whatever fn returns; on final
    failure returns whatever fn returned last (e.g. an empty list)."""
    last_result = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = fn(*args, **kwargs)
            return result
        except Exception as e:
            last_result = e
            if not _is_rate_limited(e) or attempt == max_attempts:
                raise
            wait = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.75)
            print(f"  ⏳ Rate-limited ({e.__class__.__name__}); backing off {wait:.1f}s "
                  f"(attempt {attempt}/{max_attempts - 1})")
            time.sleep(wait)
    return last_result


def extract_domain(url):
    """Extracts the base domain (e.g., mi.com) from a full URL."""
    try:
        parsed = urlparse(url)
        return parsed.netloc.replace("www.", "")
    except:
        return None


def _google_search_api_key() -> str | None:
    """Return the API key to use for Google Custom Search.

    Phase 2.3: prefer a dedicated `GOOGLE_SEARCH_API_KEY` if set, falling back
    to the shared `GOOGLE_API_KEY`. This lets orgs that bind their Gemini key
    to a service account use a separate, plain key for Custom Search.
    """
    return os.getenv("GOOGLE_SEARCH_API_KEY") or os.getenv("GOOGLE_API_KEY")


# Phase 2.3: auto-disable Google search API for the rest of the process
# once we see an org-policy / project-not-enabled / billing-blocked 403.
# Every retry costs ~3 seconds; pretending the API doesn't exist saves a lot
# of time when the org policy is hard-no. Set explicitly via env var to
# skip even the first attempt, e.g. when you already know the org blocks it.
_GOOGLE_API_DISABLED = os.getenv("DISABLE_GOOGLE_SEARCH_API", "").lower() in ("1", "true", "yes")


def _is_org_blocked_error(body: dict) -> bool:
    """Return True if a Google JSON error body indicates this whole process
    should give up on the API (org policy, API not enabled, key blocked)."""
    try:
        err = (body or {}).get("error", {}) or {}
        msg = (err.get("message") or "").lower()
        if any(s in msg for s in (
            "does not have the access",
            "api_key_service_blocked",
            "are blocked",
            "permission_denied",
            "service has not been used",
        )):
            return True
        for d in err.get("details", []) or []:
            if (d.get("reason") or "").upper() in ("API_KEY_SERVICE_BLOCKED", "SERVICE_DISABLED"):
                return True
    except Exception:
        logger.debug("Failed to parse Google Search error payload", exc_info=True)
    return False


def _disable_google_api(reason: str) -> None:
    global _GOOGLE_API_DISABLED
    if not _GOOGLE_API_DISABLED:
        _GOOGLE_API_DISABLED = True
        print(f"⛔ Google Custom Search API disabled for this process: {reason}")


def search_google_api(query: str, domain: str | None = None) -> list[str]:
    if _GOOGLE_API_DISABLED:
        return []
    api_key = _google_search_api_key()
    cx = os.getenv("GOOGLE_SEARCH_CX")

    if not api_key or not cx:
        return []

    params = {
        "q": query,
        "cx": cx,
        "key": api_key,
        "searchType": "image",
        "num": 10,
        "imgSize": "large",
        "safe": "active",
    }

    if domain:
        params["siteSearch"] = domain
        params["siteSearchFilter"] = "i"

    def _call():
        _dbg(f"--- Calling Google Image API with query: '{query}' ---")
        if domain:
            _dbg(f"--- Domain filter: {domain} ---")

        import time as _time
        _started = _time.monotonic()
        resp = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params=params,
            timeout=10,
        )
        try:
            from .api_metering import log_search_call
            log_search_call(
                provider="google_cse",
                query_count=1,
                latency_ms=int((_time.monotonic() - _started) * 1000),
                error=None if resp.status_code == 200 else f"HTTP {resp.status_code}",
            )
        except Exception:
            logger.debug("Failed to log Google Search API call", exc_info=True)
        _dbg(f"--- Google status code: {resp.status_code} ---")
        data = resp.json()

        # Phase 2.3: surface 403/429 as exceptions so the backoff layer
        # can retry instead of silently returning [].
        # If the body shape is "org/project policy block", short-circuit the
        # rest of this process — there's no point in retrying a hard-no.
        if resp.status_code in (403, 429):
            if _is_org_blocked_error(data):
                _disable_google_api(f"{resp.status_code} {data.get('error', {}).get('message','')[:80]}")
                return []
            err_text = json.dumps(data)[:200]
            raise RuntimeError(f"Google {resp.status_code}: {err_text}")
        if "items" not in data:
            _dbg("Google returned NO image results")
            if resp.status_code != 200:
                _dbg(f"--- Google Response Error: {json.dumps(data)[:200]} ---")
            return []

        urls = [item["link"] for item in data.get("items", [])]
        _dbg(f"--- Google returned {len(urls)} image results ---")
        return urls

    try:
        return _retry_with_backoff(_call)
    except Exception as e:
        print(f"--- Google API Error after retries: {e} ---")
        return []


def _ddg_images(ddgs, query: str, max_results: int):
    """Call DDGS.images across both the legacy and new (ddgs) API surfaces.
    Both packages expose a callable `images(query, ...)` but kwargs differ
    slightly: the new lib uses `safesearch` strings the same way and accepts
    `region` so the call below is compatible with either."""
    return ddgs.images(query, region="wt-wt", safesearch="moderate",
                       max_results=max_results)


# Phase 2.3: DDG kill switch. Once DuckDuckGo starts rate-limiting us, every
# subsequent call also fails for the next ~10 minutes. There is zero point
# in retrying with backoff — we just disable DDG for the rest of the
# process and let the SERP scraper carry the load.
_DDG_DISABLED = False


def _disable_ddg(reason: str) -> None:
    global _DDG_DISABLED
    if not _DDG_DISABLED:
        _DDG_DISABLED = True
        print(f"⛔ DuckDuckGo disabled for this process: {reason[:100]}")


def _is_ddg_ratelimit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return ("ratelimit" in msg) or ("403" in msg and "ddg" in msg.lower()) \
        or ("ratelimitexception" in exc.__class__.__name__.lower())


def search_duckduckgo(query: str, max_results: int = 10) -> list[str]:
    """Search DuckDuckGo Images — FREE, no API key, no daily quota.
    Phase 2.3:
      - Skips immediately if DDG was rate-limited earlier in this process.
      - Does NOT retry on a DDG 403 Ratelimit (retrying is pointless and
        burns ~5s per call); flips the kill switch so subsequent calls noop.
    """
    if not HAS_DDGS or _DDG_DISABLED:
        return []

    import time as _time
    from .api_metering import log_search_call
    _started = _time.monotonic()

    def _call():
        _dbg(f"--- DuckDuckGo Image Search: '{query}' ---")
        ddgs = DDGS()
        results = _ddg_images(ddgs, query, max_results)
        urls = [r.get("image") or r.get("image_url") for r in (results or [])]
        urls = [u for u in urls if u]
        _dbg(f"--- DuckDuckGo returned {len(urls)} images ---")
        return urls

    try:
        out = _retry_with_backoff(_call, max_attempts=1)
        try:
            log_search_call(provider="duckduckgo", query_count=1,
                            latency_ms=int((_time.monotonic() - _started) * 1000))
        except Exception: logger.debug("suppressed in image_processing", exc_info=True)
        return out
    except Exception as e:
        try:
            log_search_call(provider="duckduckgo", query_count=1,
                            latency_ms=int((_time.monotonic() - _started) * 1000),
                            error=f"{type(e).__name__}")
        except Exception: logger.debug("suppressed in image_processing", exc_info=True)
        if _is_ddg_ratelimit(e):
            _disable_ddg(f"{type(e).__name__}: {e}")
        else:
            print(f"--- DuckDuckGo Search Error: {e} ---")
        return []



# ── Phase 3.1 — Brave Search API (primary URL discovery) ───────────────────
# Brave returns *real ranked search results* — no LLM hallucination, no
# stale URL citations, sub-second latency. It's the primary URL source for
# the auto-extraction flow; Gemini grounded search (defined further down)
# stays as the fallback when Brave returns empty or hits an error.

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


def _brave_api_key() -> str | None:
    return os.getenv("BRAVE_SEARCH_API_KEY")


# Hosts we never want as a discovered product page — same list as
# discover_supplier_url's `_BAD_URL_HOSTS`. Cross-checked here so search
# engines don't smuggle social/marketplace URLs into the auto-extraction
# pipeline.
_BAD_DISCOVERY_HOSTS = (
    "google.", "bing.", "duckduckgo.", "startpage.", "yahoo.",
    "facebook.", "instagram.", "twitter.", "x.com", "tiktok.",
    "youtube.", "wikipedia.", "pinterest.", "reddit.",
    "alibaba.", "aliexpress.",
)


def _is_bad_discovery_url(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in _BAD_DISCOVERY_HOSTS)


def discover_urls_via_brave(model_name: str,
                              brand: str | None = None,
                              max_results: int = 2,
                              log_cb=None) -> list[str]:
    """Query Brave Search for the top product page URLs. Returns up to
    `max_results` http(s) URLs from the organic results. Returns [] on
    any failure (no API key, HTTP error, no usable results) — caller
    decides whether to fall back to another engine.

    Brand-domain bias: when the brand is in our `BRAND_OFFICIAL_DOMAINS`
    map, we try a `site:<domain>` query first (cheap shortcut to the
    manufacturer page) and only fall back to a generic query if that
    returns nothing.
    """
    if not model_name:
        return []
    api_key = _brave_api_key()
    if not api_key:
        if log_cb:
            try: log_cb("Brave: no API key configured")
            except Exception: logger.debug("suppressed in image_processing", exc_info=True)
        return []

    clean = clean_search_query(model_name)
    brand_clean = (brand or "").strip()
    brand_domain = resolve_brand_domain(brand_clean)

    queries: list[str] = []
    # Manufacturer-locked first — typically gives the cleanest hero shot.
    # Phase 2 Fix #3: append "specifications" so we hit the spec page on the
    # brand site rather than the homepage / catalogue landing page.
    if brand_domain:
        queries.append(f"{clean} specifications site:{brand_domain}")
        queries.append(f"{clean} site:{brand_domain}")
    # Brand+model query targeted at spec pages — for large brands like Xiaomi
    # this avoids the generic catalogue page (monitors, earbuds, TVs all
    # mixed). Falls back to the legacy `... official` query if specs query
    # returns nothing usable.
    if brand_clean:
        queries.append(f"{brand_clean} {clean} specifications")
        queries.append(f"{brand_clean} {clean} official")
    else:
        queries.append(f"{clean} specifications")
        queries.append(f"{clean} official")

    headers = {
        "X-Subscription-Token": api_key,
        "Accept": "application/json",
    }
    seen: set[str] = set()
    out: list[str] = []

    import time as _time
    from .api_metering import log_search_call
    for q in queries:
        if len(out) >= max_results:
            break
        _started = _time.monotonic()
        try:
            r = requests.get(
                _BRAVE_ENDPOINT,
                headers=headers,
                params={"q": q, "count": 10, "safesearch": "moderate"},
                timeout=5,
            )
        except Exception as e:
            try:
                log_search_call(provider="brave_search", query_count=1,
                                latency_ms=int((_time.monotonic() - _started) * 1000),
                                error=f"{type(e).__name__}")
            except Exception: logger.debug("suppressed in image_processing", exc_info=True)
            if log_cb:
                try: log_cb(f"Brave HTTP error: {type(e).__name__}")
                except Exception: logger.debug("suppressed in image_processing", exc_info=True)
            continue
        try:
            log_search_call(provider="brave_search", query_count=1,
                            latency_ms=int((_time.monotonic() - _started) * 1000),
                            error=None if r.status_code == 200 else f"HTTP {r.status_code}")
        except Exception: logger.debug("suppressed in image_processing", exc_info=True)
        if r.status_code != 200:
            if log_cb:
                try: log_cb(f"Brave HTTP {r.status_code} for {q!r}")
                except Exception: logger.debug("suppressed in image_processing", exc_info=True)
            continue
        try:
            data = r.json()
        except Exception:
            continue

        for item in (data.get("web", {}) or {}).get("results", []) or []:
            url = (item.get("url") or "").strip()
            if not url or url in seen:
                continue
            if _is_bad_discovery_url(url):
                continue
            seen.add(url)
            out.append(url)
            if len(out) >= max_results:
                break

    if log_cb:
        try: log_cb(f"Brave → {len(out)} URL(s)")
        except Exception: logger.debug("suppressed in image_processing", exc_info=True)
    return out


# ── Phase 3.0 — Gemini grounded URL discovery (fallback) ───────────────────
# Used when Brave returns nothing — covers the rare cases where the product
# is too obscure for Brave's index to rank. Cited URLs may be stale (404)
# or wrapped in grounding-redirect proxies, both handled defensively below.

def discover_urls_via_gemini(model_name: str,
                              brand: str | None = None,
                              max_results: int = 2,
                              log_cb=None) -> list[str]:
    """Ask Gemini (with Google Search grounding) for the most authoritative
    product page URLs. Returns up to `max_results` http(s) URLs.

    On any failure (grounding tool error, malformed JSON, no results)
    returns []. Caller should treat that as "no web URLs available" and
    rely on PDF-extracted images / manual entry instead — never raise.

    `log_cb`: optional callable for one summary line per call. Internal
    debug detail goes to stdout only when IMAGE_DEBUG=1.
    """
    if not model_name:
        return []

    brand_hint = f" by {brand}" if brand and brand.lower() not in model_name.lower() else ""
    # Small oversample so that if the top suggestion 404s the downstream
    # parallel fetch can still satisfy `max_results`. Bigger oversamples
    # actually *hurt* quality because Gemini starts citing weaker results.
    ask_for = max_results + 1
    prompt = (
        f'Find the {ask_for} most authoritative product page URLs for: '
        f'"{model_name}"{brand_hint}.\n\n'
        f'Prefer the manufacturer\'s official site first, then major retailers. '
        f'Avoid comparison blogs, listicles, and social media. '
        f'Only suggest URLs that appear in the search results — '
        f'do NOT include URLs from memory.\n\n'
        f'Return ONLY a JSON array of URL strings. No prose, no markdown, '
        f'no code fences. Example: '
        f'["https://manufacturer.com/product/x", "https://retailer.com/p/x"]'
    )

    try:
        from .api_metering import gemini_call
        response = gemini_call(
            prompt_id='discover_urls_via_gemini',
            model=_MODEL,
            client=_get_client(),
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.0,
            ),
        )
    except Exception as e:
        if log_cb:
            try: log_cb(f"Gemini URL discovery failed: {type(e).__name__}")
            except Exception: logger.debug("suppressed in image_processing", exc_info=True)
        return []

    text = (getattr(response, 'text', None) or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\s*|\s*```$', '', text).strip()

    raw_urls: list[str] = []
    seen: set[str] = set()

    def _push(u: str) -> bool:
        u = u.strip().rstrip('.,;:)')
        if not (u.startswith('http://') or u.startswith('https://')):
            return False
        # Sanity guard against LLM repetition glitches: gemini-2.5-flash
        # occasionally produces URLs with hundreds of repeated digits
        # (e.g. `/p_1000…000…`). Cap on length AND reject any URL with a
        # run of >20 identical chars — both are well clear of any sane
        # real product URL.
        if len(u) > 400 or re.search(r'(.)\1{20,}', u):
            return False
        if u in seen:
            return False
        seen.add(u)
        raw_urls.append(u)
        return len(raw_urls) >= ask_for

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            for u in parsed:
                if isinstance(u, str) and _push(u):
                    break
    except Exception:
        for m in re.finditer(r'https?://[^\s\'")>\]]+', text):
            if _push(m.group(0)):
                break

    # Fallback: if the model returned no URLs in text (or all were
    # malformed), pull from grounding metadata directly.
    if not raw_urls:
        try:
            gm = response.candidates[0].grounding_metadata
            for chunk in (getattr(gm, 'grounding_chunks', None) or []):
                web = getattr(chunk, 'web', None)
                if web and getattr(web, 'uri', None):
                    if _push(web.uri):
                        break
        except Exception:
            logger.debug("Grounding-chunk URL extraction failed", exc_info=True)

    # Resolve Gemini's grounding-redirect proxy URLs to the real target.
    # We deliberately do NOT HEAD-check live-ness — empirically that costs
    # more than it saves: HEAD on the malformed/large URLs Gemini sometimes
    # cites can hang for minutes (server doesn't fast-fail on a 400-char
    # path), and the downstream parallel fetcher already returns [] cleanly
    # in ~1s on a 404. The small oversample above (ask_for = max_results+1)
    # gives us one spare URL in case the top suggestion is dead.
    resolved: list[str] = []
    if raw_urls:
        with ThreadPoolExecutor(max_workers=len(raw_urls)) as ex:
            for live in ex.map(_resolve_grounding_redirect, raw_urls):
                if live and live not in resolved:
                    resolved.append(live)
                    if len(resolved) >= max_results:
                        break

    if log_cb:
        try: log_cb(f"Gemini → {len(resolved)} URL(s)")
        except Exception: logger.debug("suppressed in image_processing", exc_info=True)
    return resolved[:max_results]


_GROUNDING_PROXY_HOST = "vertexaisearch.cloud.google.com"


def _resolve_grounding_redirect(url: str) -> str | None:
    """Follow Gemini's grounding-api-redirect proxy to its real target via
    one HEAD request. Pass-through for non-proxy URLs. Returns None only
    if a proxy URL fails to resolve."""
    if not url:
        return None
    if _GROUNDING_PROXY_HOST not in url:
        return url
    try:
        r = requests.head(url, allow_redirects=False, timeout=5)
        loc = r.headers.get('location')
        if loc and (loc.startswith('http://') or loc.startswith('https://')):
            return loc
    except Exception:
        logger.debug("Redirect resolution failed", exc_info=True)
    return None


def discover_urls(model_name: str,
                  brand: str | None = None,
                  max_results: int = 2,
                  log_cb=None) -> list[str]:
    """Discover the top product page URLs for `model_name`.

    Strategy: Brave Search API first (real ranked results, ~300ms, no
    hallucination). If Brave returns empty (no key, HTTP error, no
    matching results), fall back to Gemini grounded search (`google_search`
    tool, ~3-5s, may cite stale URLs but covers the long tail).

    Returns up to `max_results` URLs, never raises.
    """
    urls = discover_urls_via_brave(model_name, brand=brand,
                                   max_results=max_results, log_cb=log_cb)
    if urls:
        return urls
    if log_cb:
        try: log_cb("Brave returned 0 — trying Gemini fallback")
        except Exception: logger.debug("suppressed in image_processing", exc_info=True)
    return discover_urls_via_gemini(model_name, brand=brand,
                                    max_results=max_results, log_cb=log_cb)


# ── Phase 3.3 — Web context for PIS content extraction ─────────────────────
# Pulls clean page text from the same Brave-discovered URLs the image
# pipeline uses, then feeds it to `generate_pis_data` as `web_context`.
# Without this, content extraction sees only the proforma — which often
# yields zero technical specs for sparse spec sheets. With it, Gemini
# cross-references the proforma against the manufacturer page and pulls
# the full spec table from the live site.

# Tags inside the HTML we strip before extracting text — these contain
# nav, scripts, ads, etc., which would just consume tokens without adding
# any product information.
_HTML_NOISE_TAGS = (
    'script', 'style', 'noscript',
    'nav', 'header', 'footer', 'aside',
    'iframe', 'svg', 'form', 'button',
)

# Soft cap on the combined page text before we send it to Gemini. ~8KB is
# roughly 2k tokens, generous enough to cover a full spec table + product
# description from one page, cheap enough to stay well under input limits.
_WEB_CONTEXT_BYTE_CAP = 8000


def _fetch_clean_page_text(url: str, timeout: float = 8.0) -> str:
    """HTTP GET `url`, strip nav/script/footer/etc., return cleaned text.
    Returns '' on any failure — never raises. Cap individual page text
    to half the global cap so two pages can't blow the budget combined."""
    if not url:
        return ""
    try:
        from bs4 import BeautifulSoup
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                 'AppleWebKit/537.36'}
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.content, 'html.parser')
        for tag in soup(_HTML_NOISE_TAGS):
            tag.decompose()
        # `.get_text(' ', strip=True)` collapses each text node and joins
        # them with single spaces — keeps spec table cells separated
        # without preserving every line break.
        text = soup.get_text(' ', strip=True)
        # Collapse runs of whitespace.
        text = re.sub(r'\s+', ' ', text)
        # Trim to half the global cap so a single bloated page can't
        # drown out the second page's contribution.
        return text[: _WEB_CONTEXT_BYTE_CAP // 2]
    except Exception:
        return ""


_RELEVANCE_TOKEN_MIN_LEN = 3


def _relevance_tokens(text: str) -> list[str]:
    """Tokenise an identifier for relevance checking — drops stop-noise like
    bare "the", keeps model numbers and product nouns intact."""
    if not text:
        return []
    raw = re.findall(r"[A-Za-z0-9]+", text)
    return [t.lower() for t in raw if len(t) >= _RELEVANCE_TOKEN_MIN_LEN]


def _page_is_relevant(text: str, model_name: str,
                       brand: str | None = None) -> bool:
    """Phase 2 Fix #3 — relevance check. A fetched page is considered
    relevant if its text contains either the model number/SKU OR at least
    two distinct tokens drawn from BOTH the model name AND the brand.

    Including the brand fixes products like "TV A Pro 32 GL" where the
    model name on its own only contributes one ≥3-char token ("pro") —
    not enough to satisfy the ≥2 rule — even though the legit supplier
    page clearly mentions both the brand and the model. Combining the
    two token sets keeps catalogue-only pages out (one brand mention
    isn't enough on its own) while letting real product pages through.
    """
    if not text:
        return False
    haystack = text.lower()
    model_tokens = _relevance_tokens(model_name)
    brand_tokens = _relevance_tokens(brand or "")
    all_tokens = set(model_tokens) | set(brand_tokens)
    if not all_tokens:
        # No tokens to check against — fall back to accepting whatever we
        # got so we don't accidentally starve a niche product of context.
        return True
    # SKU-shape token (mixed letters+digits, length ≥ 4) is the strongest
    # signal — one match is enough to call the page relevant. Only the
    # model name supplies SKUs; brands are word-shaped.
    for tok in model_tokens:
        if len(tok) >= 4 and any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
            if tok in haystack:
                return True
    # Otherwise require ≥2 distinct tokens (from model + brand) to appear,
    # so a page that only mentions the brand isn't accepted as relevant.
    hits = sum(1 for tok in all_tokens if tok in haystack)
    return hits >= 2


def gather_web_context_for_content(model_name: str,
                                     brand: str | None = None,
                                     max_urls: int = 2,
                                     log_cb=None) -> str:
    """Discover the top product-page URLs via the same Brave→Gemini
    pipeline used for image discovery, fetch each page's text in parallel,
    and return ONE combined cleaned string suitable for handing to
    `generate_pis_data` as `web_context`.

    Returns '' on any failure (no URLs discovered, all fetches failed).
    Caller treats '' as "no web context available" and proceeds with
    proforma-only extraction.
    """
    if not model_name:
        return ""

    urls = discover_urls(model_name, brand=brand,
                         max_results=max_urls, log_cb=log_cb)
    if not urls:
        return ""

    with ThreadPoolExecutor(max_workers=len(urls)) as ex:
        texts = list(ex.map(_fetch_clean_page_text, urls))

    # Phase 2 Fix #3: drop pages whose text doesn't reference the model.
    # Passing irrelevant catalogue text to Gemini is worse than passing
    # nothing — it produces specs that look sourced but aren't.
    parts: list[str] = []
    dropped = 0
    for url, text in zip(urls, texts):
        if not text:
            continue
        if not _page_is_relevant(text, model_name, brand):
            dropped += 1
            if log_cb:
                try: log_cb(f"web ctx: dropped irrelevant page {url}")
                except Exception: logger.debug("suppressed in image_processing", exc_info=True)
            continue
        parts.append(f"--- SOURCE: {url} ---\n{text}")

    combined = "\n\n".join(parts)
    if len(combined) > _WEB_CONTEXT_BYTE_CAP:
        combined = combined[:_WEB_CONTEXT_BYTE_CAP] + "\n[…truncated]"

    if log_cb:
        try: log_cb(f"web context: {len(combined)} chars from "
                    f"{len(parts)}/{len(urls)} page(s) "
                    f"(dropped {dropped} as irrelevant)")
        except Exception: logger.debug("suppressed in image_processing", exc_info=True)
    return combined


def clean_search_query(query: str) -> str:
    """
    Removes internal SKUs, bracketed numbers, and ERP codes before sending
    the query to Google / DDG / Bing.

    Two non-obvious rules baked in:

    1. An "ERP code" must contain BOTH a letter and a digit. The previous
       rule was just `\\b[A-Z0-9]{8,}\\b`, which accidentally matched plain
       all-caps words like "WARDROBE", "MICROWAVE", "CONNECTOR". Stripping
       them turned model names like "2D WARDROBE-FELIX WALNUT" into
       "2D -FELIX WALNUT" — and search engines then read the leading "-"
       on -FELIX as an exclusion operator and returned zero results.

    2. After any token removal, drop dangling "-" prefixes from the next
       word. Otherwise residue from rule 1 (or from a real SKU strip on a
       hyphenated original) silently turns a positive search term into a
       NOT-clause.
    """
    query = re.sub(r"\([^)]*\)", "", query)

    def _strip_if_sku(match: re.Match) -> str:
        tok = match.group(0)
        if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
            return ""    # real SKU: drop it
        return tok       # plain word like "WARDROBE": keep it

    query = re.sub(r"\b[A-Z0-9]{8,}\b", _strip_if_sku, query)

    # Defuse stray exclusion operators left behind by the strip above.
    # `(\s|^)-(?=\S)` matches a "-" that begins a token (start of string or
    # after whitespace) and is glued to the next word — exactly the shape
    # search engines treat as NOT.
    query = re.sub(r"(\s|^)-(?=\S)", r"\1", query)

    cleaned = " ".join(query.split())
    _dbg(f"--- Cleaned Search Query: '{cleaned}' ---")
    return cleaned


# ==================== BAD DOMAIN FILTER ====================
# These domains serve placeholder/generic images, not real product photos
BAD_IMAGE_DOMAINS = {
    'placeholder.com', 'via.placeholder.com', 'placehold.it',
    'dummyimage.com', 'picsum.photos', 'lorempixel.com',
    'fakeimg.pl', 'placekitten.com',
}

def is_bad_image_url(url: str) -> bool:
    """Filter out known bad image sources."""
    try:
        domain = urlparse(url).netloc.replace("www.", "").lower()
        return domain in BAD_IMAGE_DOMAINS
    except:
        return False


def ai_validate_image(image_bytes: bytes, product_name: str) -> bool:
    """
    Lightweight AI check:
    - Is the main product visible?
    - Is the image appropriate and relevant?
    """

    prompt = get_prompt('image_validation').format(product_name=product_name)

    try:
        if len(image_bytes) > 20 * 1024 * 1024:
            print("❌ Image too large for validation")
            return False

        from .api_metering import gemini_call
        response = gemini_call(
            prompt_id='image_validation',
            model=_MODEL,
            client=_get_client(),
            contents=[prompt, types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")],
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )

        result = json.loads(response.text)
        return bool(result.get("approve", False))

    except Exception as e:
        print(f"AI image validation failed for '{product_name}':", e)
        return False


def ai_select_best_image(image_list: list[bytes], product_name: str) -> int | None:
    """
    Evaluates a list of images simultaneously and selects the best 'Hero Shot'.
    Returns the index (0-based) of the best image, or None if none are suitable.
    """
    if not image_list:
        return None

    prompt = get_prompt('best_image_selection').format(
        product_name=product_name,
        image_count=len(image_list)
    )

    content = [prompt]
    for i, img_bytes in enumerate(image_list):
        content.append(f"IMAGE {i+1}:")
        content.append(types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))

    try:
        from .api_metering import gemini_call
        response = gemini_call(
            prompt_id='best_image_selection',
            model=_MODEL,
            client=_get_client(),
            contents=content,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        result = json.loads(response.text)
        best = result.get("best_index")
        
        if best == "none" or best is None:
            return None
        
        return int(best) - 1  # Convert 1-based to 0-based
    except Exception as e:
        print(f"Batch AI Image Selection failed: {e}")
        return None


# ── Phase 3.2 — User-triggered AI image actions (Edit PIS) ─────────────────
# Two distinct flows backed by two different Google models:
#   • enhance_image_with_gemini → gemini-2.5-flash-image (image-in + image-out).
#     Faithful retouch — keeps the product pixel-identical, only fixes
#     background, lighting, stray artifacts. User picks a starting image.
#   • generate_image_with_imagen → imagen-4.0-generate-001 (text-only).
#     Synthetic generation from the product description. No source image.
# Both honour an optional user_note that's appended to the system prompt
# so the user can steer ("remove the white line on the left", "modern
# living-room background", etc.).

_IMAGEN_MODEL = 'imagen-4.0-generate-001'
_RETOUCH_MODEL = 'gemini-2.5-flash-image'

_RETOUCH_SYSTEM_PROMPT = (
    "You are a professional product-photo retoucher.\n\n"
    "Clean up the attached product photo for an e-commerce catalog.\n\n"
    "ABSOLUTELY STRICT RULES — NO EXCEPTIONS:\n"
    "1. Do NOT alter the product itself in any way. Do NOT change colors, "
    "   materials, textures, proportions, or shape.\n"
    "2. Do NOT add or remove product features, buttons, knobs, ports, or "
    "   labels.\n"
    "3. Do NOT change the product's pose, framing, or perspective.\n"
    "4. Only fix surrounding issues: distracting backgrounds, stray lines, "
    "   shadows that obscure the product, watermarks, color casts, "
    "   uneven lighting on the backdrop.\n"
    "5. Output the product on a clean, uniform background — pure white "
    "   (#FFFFFF) by default unless the user direction below says otherwise.\n"
    "6. 4:3 LANDSCAPE aspect ratio. Product centered with modest margin.\n"
    "7. No text, watermarks, borders, or decorative elements added.\n\n"
    "USER DIRECTION (optional — overrides the default cleanup behaviour "
    "where it conflicts):\n"
    "{user_note}"
)


def enhance_image_with_gemini(image_path: str,
                               product_name: str,
                               upload_folder: str,
                               user_note: str | None = None) -> str | None:
    """Send an existing product photo to Gemini's image-out model with the
    'retouch, don't recreate' system prompt + an optional user note.

    `image_path` should be either an absolute path to an image file on disk
    OR a relative path (`uploads/foo.jpg`). The function tries a few common
    roots before giving up.

    Returns the saved relative path (e.g. `uploads/enhanced_…jpg`) on
    success, or None on any failure (file missing, model error, output
    was solid/blank). Never raises.
    """
    from PIL import Image as _PIL_Image
    if not image_path:
        return None

    # Locate the source file. Candidates: absolute, project-static, project-root.
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rel = image_path.lstrip('/')
    src_candidates = [
        image_path,
        os.path.join(project_root, 'static', rel),
        os.path.join(project_root, rel),
        os.path.join(upload_folder, os.path.basename(rel)),
    ]
    src_bytes: bytes | None = None
    for cand in src_candidates:
        try:
            with open(cand, 'rb') as f:
                src_bytes = f.read()
            break
        except OSError:
            continue
    if not src_bytes:
        print(f"  ⚠ Retouch: source image not found ({image_path!r})")
        return None

    note = (user_note or "").strip() or "(none — apply default cleanup)"
    prompt = _RETOUCH_SYSTEM_PROMPT.format(user_note=note)

    # Heterogeneous content list (str + Part) — widened so Pyrefly accepts
    # it against the SDK's `Content | str | Part | list[...]` union.
    from typing import Any
    contents: list[Any] = [prompt,
                           types.Part.from_bytes(data=src_bytes, mime_type="image/png")]

    try:
        response = _get_client().models.generate_content(
            model=_RETOUCH_MODEL,
            contents=contents,
        )
    except Exception as e:
        print(f"  ⚠ Retouch model call failed: {type(e).__name__}: {e}")
        return None

    # Walk response parts looking for inline image bytes — same shape as
    # the nano-banana isolation pipeline.
    candidates = getattr(response, 'candidates', None) or []
    for cand in candidates:
        content = getattr(cand, 'content', None)
        parts = getattr(content, 'parts', None) or []
        for part in parts:
            inline = getattr(part, 'inline_data', None)
            if not (inline and getattr(inline, 'data', None)):
                continue
            try:
                out_img = _PIL_Image.open(io.BytesIO(inline.data))
                if out_img.mode != 'RGB':
                    out_img = out_img.convert('RGB')
            except Exception as e:
                print(f"  ⚠ Retouch: PIL failed on returned bytes: {e}")
                continue

            safe_name = secure_filename(product_name) or 'product'
            filename = f"enhanced_{safe_name}_{int(time.time())}.jpg"
            save_path = os.path.join(upload_folder, filename)
            try:
                out_img.save(save_path, 'JPEG', quality=92)
            except Exception as e:
                print(f"  ⚠ Retouch: save failed: {e}")
                return None
            try:
                print(f"  ✨ Retouch saved → {filename} ({out_img.size[0]}x{out_img.size[1]})")
            except UnicodeEncodeError:
                print(f"  Retouch saved -> {filename} ({out_img.size[0]}x{out_img.size[1]})")
            return f"uploads/{filename}"

    print("  Retouch: model returned no inline image data")
    return None


def generate_image_with_imagen(product_name: str,
                                upload_folder: str,
                                brand: str | None = None,
                                description: str | None = None,
                                user_note: str | None = None) -> str | None:
    """Generate a synthetic product photo from a text description via
    Google Imagen 4 (`imagen-4.0-generate-001`). No source image needed —
    the model invents one from the spec block.

    Returns the saved relative path (e.g. `uploads/generated_…jpg`) on
    success, or None on any failure. Never raises.

    The output is always tagged synthetic at the call site
    (`source='ai_synthetic'`) so the gallery can warn reviewers it's not
    a real photo.
    """
    from PIL import Image as _PIL_Image
    if not product_name:
        return None

    spec_lines = [f"Product: {product_name}"]
    if brand:
        spec_lines.append(f"Brand: {brand}")
    if description:
        # Keep the description short — Imagen prompts work best under ~500 chars.
        spec_lines.append(f"Description: {description.strip()[:400]}")

    spec = "\n".join(spec_lines)
    note = (user_note or "").strip()

    # IMPORTANT: don't embed hex color codes or numeric-looking specs in the
    # prompt — Imagen has been observed to render them literally as text
    # watermarks (e.g. rendering "#FFFFFF" in the corner). Use plain English.
    prompt = (
        "Professional e-commerce product photograph.\n\n"
        f"{spec}\n\n"
        "Studio lighting, pure white seamless background, 4:3 landscape, "
        "product centered, modest margin on all sides. Sharp focus, "
        "true-to-life colors, no watermark, no decorative props.\n\n"
        "ABSOLUTELY NO TEXT, NUMBERS, OR LABELS ANYWHERE IN THE IMAGE — STRICT:\n"
        "- Do NOT render the product name, brand name, model number, "
        "  description, year, size, resolution, audio brand, or any words "
        "  or digits anywhere in the image — not on the product, not "
        "  beside it, not in the corner, not as a watermark.\n"
        "- Do NOT paint captions, taglines, badges, stickers, version "
        "  numbers, hex codes, or UI overlays onto the product.\n"
        "- If the product has a screen (TV, phone, tablet, monitor, "
        "  appliance display), show ONLY a clean abstract visual — a soft "
        "  gradient or neutral solid color. ABSOLUTELY NO app tiles, NO "
        "  icons, NO menus, NO 'Smart TV' or 'Google TV' or 'Dolby' "
        "  labels, NO on-screen text, NO row of app shortcuts.\n"
        "- The only acceptable graphic mark is the small physical brand "
        "  logo where it naturally appears on the product hardware "
        "  (e.g. a stamped bezel logo). Render it small, neutral, and "
        "  do not invent a logo that isn't in the actual brand's "
        "  identity.\n"
        "- No background text, no floor text, no shelf-talker labels, "
        "  no hex color codes visible anywhere."
    )
    if note:
        prompt += f"\n\nADDITIONAL DIRECTION FROM USER:\n{note}"

    try:
        response = _get_client().models.generate_images(
            model=_IMAGEN_MODEL,
            prompt=prompt,
            config={'number_of_images': 1, 'aspect_ratio': '4:3'},
        )
    except Exception as e:
        print(f"  ⚠ Imagen call failed: {type(e).__name__}: {e}")
        return None

    images = getattr(response, 'generated_images', None) or []
    if not images:
        print("  ⚠ Imagen returned no images")
        return None

    img_data = getattr(images[0].image, 'image_bytes', None)
    if not img_data:
        return None

    try:
        out_img = _PIL_Image.open(io.BytesIO(img_data))
        if out_img.mode != 'RGB':
            out_img = out_img.convert('RGB')
    except Exception as e:
        print(f"  ⚠ Imagen: PIL failed on returned bytes: {e}")
        return None

    safe_name = secure_filename(product_name) or 'product'
    filename = f"generated_{safe_name}_{int(time.time())}.jpg"
    save_path = os.path.join(upload_folder, filename)
    try:
        out_img.save(save_path, 'JPEG', quality=92)
    except Exception as e:
        print(f"  ⚠ Imagen: save failed: {e}")
        return None
    try:
        print(f"  🎨 Imagen saved → {filename} ({out_img.size[0]}x{out_img.size[1]})")
    except UnicodeEncodeError:
        print(f"  Imagen saved -> {filename} ({out_img.size[0]}x{out_img.size[1]})")
    return f"uploads/{filename}"


def download_image_bytes(image_url: str) -> bytes | None:
    """Downloads image bytes with quality validation."""
    try:
        # Skip known bad domains
        if is_bad_image_url(image_url):
            print(f"⚠ Skipping bad domain: {image_url}")
            return None

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8'
        }
        resp = requests.get(image_url, headers=headers, timeout=10, stream=True)
        if resp.status_code == 200:
            content_type = resp.headers.get('Content-Type', '')
            content_length = int(resp.headers.get('Content-Length', 0) or 0)
            
            if 'image' not in content_type:
                print(f"⚠ Skipping non-image content type: {content_type}")
                return None
            
            # Reject tiny images (likely icons/spacers) by Content-Length header
            if content_length > 0 and content_length < 5_000:  # < 5KB
                print(f"⚠ Skipping tiny image ({content_length} bytes): {image_url}")
                return None
            
            image_data = resp.content
            
            # Reject by actual size if Content-Length wasn't available
            if len(image_data) < 5_000:
                print(f"⚠ Skipping tiny image ({len(image_data)} bytes): {image_url}")
                return None
            
            # Verify image dimensions using PIL
            try:
                img = Image.open(io.BytesIO(image_data))
                w, h = img.size
                if w < 100 or h < 100:
                    print(f"⚠ Skipping small dimensions ({w}x{h}): {image_url}")
                    return None
                print(f"✓ Downloaded {len(image_data)} bytes ({w}x{h}, {content_type})")
            except Exception:
                # If PIL can't open it, still allow — might be a valid format PIL doesn't support
                print(f"--- Downloaded {len(image_data)} bytes (Type: {content_type}) ---")
            
            return image_data
        else:
            print(f"--- Download failed with status {resp.status_code} ---")
    except Exception as e:
        print("Image byte download failed:", e)
    return None


def scrape_images_from_url(url: str) -> list[str]:
    """
    Advanced web scraper for product images.
    Handles modern e-commerce image patterns.
    """
    try:
        from bs4 import BeautifulSoup
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return []
            
        soup = BeautifulSoup(resp.content, 'html.parser')
        images = []
        seen_urls = set()
        
        def add_image(src):
            """Normalize and add image URL if valid."""
            if not src:
                return
            # Resolve relative URLs
            if src.startswith('//'):
                src = 'https:' + src
            elif src.startswith('/'):
                src = urljoin(url, src)
            elif not src.startswith('http'):
                src = urljoin(url, src)
            
            # Filter out garbage
            lower_src = src.lower()
            if any(x in lower_src for x in ['logo', 'icon', 'banner', 'pixel', 'sprite', 'spacer', 'blank', '1x1', 'tracking', 'analytics']):
                return
            if src in seen_urls:
                return
            
            seen_urls.add(src)
            images.append(src)

        # --- PRIORITY 1: Product-context images ---
        # Look inside common product containers first
        product_selectors = [
            '[class*="product-image"]', '[class*="product_image"]', '[class*="productImage"]',
            '[class*="gallery"]', '[class*="main-image"]', '[class*="hero-image"]',
            '[id*="product"]', '[id*="gallery"]',
            '[class*="swiper"]', '[class*="slider"]', '[class*="carousel"]',
            '.product-media', '.product-photo', '.product-img',
        ]
        
        for selector in product_selectors:
            container = soup.select(selector)
            for el in container:
                # <img> tags
                for img in el.find_all('img'):
                    # Prefer high-res attributes
                    src = (img.get('data-zoom-image') or img.get('data-full-image') or 
                           img.get('data-large') or img.get('data-src') or 
                           img.get('data-lazy-src') or img.get('src'))
                    add_image(src)
                    
                    # Parse srcset for highest resolution
                    srcset = img.get('srcset')
                    if srcset:
                        best_src = _parse_srcset_best(srcset)
                        if best_src:
                            add_image(best_src)
                
                # <picture> / <source> tags
                for source in el.find_all('source'):
                    srcset = source.get('srcset')
                    if srcset:
                        best_src = _parse_srcset_best(srcset)
                        if best_src:
                            add_image(best_src)
        
        # --- PRIORITY 2: All page images (fallback) ---
        if len(images) < 5:
            for img in soup.find_all('img'):
                src = (img.get('data-zoom-image') or img.get('data-full-image') or
                       img.get('data-large') or img.get('data-src') or 
                       img.get('data-lazy-src') or img.get('src'))
                add_image(src)
                if len(images) >= 15:
                    break
        
        # --- PRIORITY 3: Open Graph / meta images ---
        og_image = soup.find('meta', property='og:image')
        if og_image and og_image.get('content'):
            add_image(og_image['content'])
        
        twitter_image = soup.find('meta', attrs={'name': 'twitter:image'})
        if twitter_image and twitter_image.get('content'):
            add_image(twitter_image['content'])
        
        _dbg(f"--- Scraped {len(images)} images from {url} ---")
        return images[:15]  # Cap at 15
        
    except Exception as e:
        print(f"Scrape image error: {e}")
        return []


def _parse_srcset_best(srcset: str) -> str | None:
    """Parse srcset attribute and return the highest resolution URL."""
    try:
        entries = []
        for part in srcset.split(','):
            part = part.strip()
            if not part:
                continue
            pieces = part.split()
            if len(pieces) >= 2:
                url_part = pieces[0]
                descriptor = pieces[1]
                # Parse width descriptor (e.g., "800w") or pixel density (e.g., "2x")
                if descriptor.endswith('w'):
                    try:
                        width = int(descriptor[:-1])
                        entries.append((url_part, width))
                    except ValueError:
                        pass
                elif descriptor.endswith('x'):
                    try:
                        density = float(descriptor[:-1])
                        entries.append((url_part, int(density * 1000)))
                    except ValueError:
                        pass
            elif len(pieces) == 1:
                entries.append((pieces[0], 0))
        
        if entries:
            entries.sort(key=lambda x: x[1], reverse=True)
            return entries[0][0]
    except Exception:
        logger.debug("Entry-ranking pass failed", exc_info=True)
    return None


def find_best_images(model_name: str, supplier_url: str | None = None) -> list[str]:
    """
    Multi-strategy image search pipeline.
    Returns a prioritized list of candidate image URLs.
    """
    candidates = []

    clean_name = clean_search_query(model_name)

    # Phase 2.2: append "official photo" to bias search engines toward
    # e-commerce hero shots over miscellaneous gallery / review images.
    photo_suffix = "official photo"

    # --- 1️ Supplier-domain search (HIGHEST PRIORITY) ---
    if supplier_url:
        domain = extract_domain(supplier_url)
        if domain:
            supplier_query = f"{clean_name} {photo_suffix}"
            print(f"--- Strategy 1: Supplier Domain Search ({domain}) ---")
            candidates.extend(search_google_api(supplier_query, domain=domain))

    # --- 2️ Direct web scraping of supplier page ---
    if supplier_url:
        print(f"--- Strategy 2: Direct Scrape of {supplier_url} ---")
        scraped = scrape_images_from_url(supplier_url)
        candidates.extend(scraped)

    # --- 3️ Brand-domain-locked open-web search (Phase 2.3) ---
    # If the cleaned name contains a recognized brand, bias toward that
    # official site first via a `site:` filter — this is the cheapest way
    # to short-circuit to an authoritative hero shot.
    brand_domain_for_lock = None
    for brand_key in BRAND_OFFICIAL_DOMAINS:
        if brand_key in clean_name.lower():
            brand_domain_for_lock = BRAND_OFFICIAL_DOMAINS[brand_key]
            break
    if brand_domain_for_lock:
        locked_query = f"{clean_name} {photo_suffix}"
        print(f"--- Strategy 3a: Brand-locked Search ({brand_domain_for_lock}) ---")
        candidates.extend(search_google_api(locked_query, domain=brand_domain_for_lock))

    # --- 3️ Open-web search with product name ---
    open_query = f"{clean_name} product {photo_suffix}"
    print(f"--- Strategy 3: Open Web Search: '{open_query}' ---")
    candidates.extend(search_google_api(open_query))

    # --- 4️ Exact model number search (if different from product name) ---
    # Sometimes the model number alone yields better results
    if clean_name.lower() != model_name.lower():
        exact_query = f"{model_name} {photo_suffix}"
        print(f"--- Strategy 4: Exact Model Search: '{exact_query}' ---")
        candidates.extend(search_google_api(exact_query))

    # --- 5️ DuckDuckGo search (FREE, no quota limit) ---
    if len(candidates) < 5:
        ddg_query = f"{clean_name} product {photo_suffix}"
        print(f"--- Strategy 5: DuckDuckGo Search: '{ddg_query}' ---")
        candidates.extend(search_duckduckgo(ddg_query, max_results=10))

    # Remove duplicates while preserving priority order
    seen = set()
    result = []
    for url in candidates:
        if url not in seen and not is_bad_image_url(url):
            result.append(url)
            seen.add(url)
    
    print(f"--- Total unique candidates: {len(result)} ---")
    return result



def find_and_validate_image(model_name: str, supplier_url: str | None = None) -> str | None:
    """
    Finds and validates the best product image using a batch selection strategy.
    Optimized with parallel downloading, size filtering, and domain short-circuiting.
    """
    image_candidates = find_best_images(model_name, supplier_url)
    
    if not image_candidates:
        print("🚫 No image candidates found")
        return None

    # Evaluate up to 10 candidates (increased from 5)
    max_batch = 10
    candidates_to_eval = image_candidates[:max_batch]
    
    print(f"🔄 Evaluating {len(candidates_to_eval)} candidate images in parallel")
    
    downloaded_data = [None] * len(candidates_to_eval)
    
    # Use parallel downloading to prevent timeouts
    with ThreadPoolExecutor(max_workers=min(max_batch, 8)) as executor:
        future_to_url = {executor.submit(download_image_bytes, url): i for i, url in enumerate(candidates_to_eval)}
        for future in as_completed(future_to_url, timeout=30):
            idx = future_to_url[future]
            try:
                downloaded_data[idx] = future.result()
            except Exception as e:
                print(f"Parallel download error for candidate {idx+1}: {e}")

    # Zip URLs with their successfully downloaded bytes
    valid_pairs = [(candidates_to_eval[i], downloaded_data[i]) for i in range(len(candidates_to_eval)) if downloaded_data[i]]
    
    if not valid_pairs:
        print("🚫 No images could be downloaded for evaluation")
        return None
    
    downloaded_urls = [p[0] for p in valid_pairs]
    downloaded_bytes = [p[1] for p in valid_pairs]
        
    print(f"🧠 Sending {len(downloaded_bytes)} valid candidates to AI for ranking...")
    best_index = ai_select_best_image(downloaded_bytes, model_name)
    
    if best_index is not None and 0 <= best_index < len(downloaded_urls):
        selected_url = downloaded_urls[best_index]
        print(f"✔ AI selected best image (Candidate {best_index + 1}): {selected_url}")
        return selected_url

    # FALLBACK: AI rejected all candidates, use the first downloaded image anyway
    if downloaded_urls:
        print(f"⚠ AI rejected all candidates — using first downloadable image as fallback")
        return downloaded_urls[0]

    print("🚫 No acceptable image found")
    return None




def download_web_image(image_url, model_name, upload_folder):
    """
    Downloads an image from a URL provided by the AI/Scraper.
    Validates the downloaded content is a real image before saving.
    """
    try:
        if not image_url or not image_url.startswith('http'):
            return None 
            
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        _dbg(f"--- Attempting Web Download for {model_name}: {image_url} ---")
        response = requests.get(image_url, headers=headers, timeout=15)
        
        if response.status_code == 200:
            image_data = response.content
            
            # Validate: reject tiny files (< 1KB — likely error pages or stubs)
            if len(image_data) < 1000:
                print(f"⚠ Download too small ({len(image_data)} bytes), skipping: {image_url}")
                return None
            
            # Validate: check Content-Type header
            content_type = response.headers.get('Content-Type', '')
            if content_type and 'image' not in content_type and 'octet-stream' not in content_type:
                print(f"⚠ Non-image content type ({content_type}), skipping: {image_url}")
                return None
            
            # Validate: verify it's a real image using PIL
            try:
                img = Image.open(io.BytesIO(image_data))
                w, h = img.size  # Get size BEFORE verify (verify invalidates the object)
                if w < 50 or h < 50:
                    print(f"⚠ Image too small ({w}x{h}), skipping: {image_url}")
                    return None
                try:
                    img.verify()  # Verify image integrity (can fail on valid progressive JPEGs)
                except Exception as verify_err:
                    print(f"⚠ Image verify warning (continuing anyway): {verify_err}")
            except Exception as img_err:
                print(f"⚠ Invalid image data from {image_url}: {img_err}")
                return None
            
            safe_name = secure_filename(model_name)
            # Add random timestamp to avoid caching/overwriting issues
            filename = f"web_{safe_name}_{int(time.time())}.jpg"
            save_path = os.path.join(upload_folder, filename)
            
            # Re-open and save as proper JPEG (handles format conversion)
            try:
                img = Image.open(io.BytesIO(image_data))
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img.save(save_path, 'JPEG', quality=92)
            except Exception:
                # Fallback: save raw bytes if PIL re-encode fails
                with open(save_path, 'wb') as out_file:
                    out_file.write(image_data)
            
            # Final check: verify saved file has content
            if os.path.exists(save_path) and os.path.getsize(save_path) > 500:
                _dbg(f"--- Web Download Success: {filename} ({len(image_data)} bytes) ---")
                return f"uploads/{filename}"
            else:
                # Clean up corrupt file
                if os.path.exists(save_path):
                    os.remove(save_path)
                print(f"⚠ Saved file is empty/corrupt, removed: {filename}")
                return None
        else:
            print(f"--- Download failed with status {response.status_code} for {image_url} ---")
    except Exception as e:
        print(f"Failed to download web image {image_url}: {e}")
        return None
    return None


def find_image_simple(model_name: str, supplier_url: str | None = None) -> str | None:
    """
    Simplified image search — NO AI validation.
    Uses DuckDuckGo (free, unlimited) + Google as fallback.
    Returns the first downloadable image URL.
    """
    clean_name = clean_search_query(model_name)
    all_urls = []

    # 1. DuckDuckGo (free, no quota) — Phase 2.2: append "official photo"
    #    so DDG/Google preferentially return e-commerce hero shots over
    #    miscellaneous gallery images.
    ddg_urls = search_duckduckgo(f"{clean_name} product official photo", max_results=8)
    all_urls.extend(ddg_urls)

    # 2. Google (if DDG found nothing)
    if not all_urls:
        google_urls = search_google_api(f"{clean_name} product official photo")
        all_urls.extend(google_urls)

    # 3. Try downloading each until one succeeds
    for url in all_urls:
        if is_bad_image_url(url):
            continue
        img_bytes = download_image_bytes(url)
        if img_bytes:
            print(f"✔ Simple search found image: {url}")
            return url

    print(f"🚫 Simple image search also failed for '{model_name}'")
    return None


# ══════════════════════════════════════════════════════════════════════════
# Phase 2.2 — AI verification pass + Playwright/rembg screenshot fallback
# ══════════════════════════════════════════════════════════════════════════

def ai_verify_crop_matches(image_bytes: bytes, target_label: str) -> bool:
    """Strict yes/no check: does this image actually show `target_label`?

    Used immediately after a crop step (PDF screenshot crop, webpage crop) to
    catch mis-aligned bounding boxes before saving.
    Fail-open: if Gemini errors out, we assume the crop is OK rather than
    block the whole pipeline on a transient API failure.
    """
    if not image_bytes or not target_label:
        return False
    try:
        prompt = get_prompt('image_match_verification').format(target_label=target_label)
        from .api_metering import gemini_call
        response = gemini_call(
            prompt_id='image_match_verification',
            model=_MODEL,
            client=_get_client(),
            contents=[prompt, types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        result = json.loads(response.text or "{}")
        match = bool(result.get("match", False))
        if not match:
            print(f"  🚫 AI verification rejected crop for '{target_label}': {result.get('reason')}")
        else:
            print(f"  ✅ AI verification accepted crop for '{target_label}'")
        return match
    except Exception as e:
        print(f"  ⚠ AI verification call failed (allowing crop): {e}")
        return True


def _maybe_remove_background(input_path: str) -> str:
    """Run rembg on a saved image if the package is installed; otherwise
    return the original path unchanged. Output is a clean white-background
    JPEG saved next to the input."""
    try:
        from rembg import remove  # type: ignore
    except ImportError:
        print("  ℹ rembg not installed — skipping background removal")
        return input_path

    try:
        with open(input_path, "rb") as f:
            input_bytes = f.read()
        output_bytes = remove(input_bytes)
        cut = Image.open(io.BytesIO(output_bytes)).convert("RGBA")
        # Composite onto white so the saved file is a clean catalog-ready JPEG
        white = Image.new("RGBA", cut.size, (255, 255, 255, 255))
        white.alpha_composite(cut)
        final = white.convert("RGB")
        out_path = os.path.splitext(input_path)[0] + "_clean.jpg"
        final.save(out_path, "JPEG", quality=92)
        print(f"  🧼 rembg cleaned background → {os.path.basename(out_path)}")
        return out_path
    except Exception as e:
        print(f"  ⚠ rembg background removal failed (keeping original): {e}")
        return input_path


def _search_google_pages(query: str) -> list[str]:
    """Plain Google web-search (NOT image search) — returns host page URLs we
    can open with Playwright. Used by the screenshot fallback pipeline.
    Phase 2.3: respects the auto-disable kill switch."""
    if _GOOGLE_API_DISABLED:
        return []
    api_key = _google_search_api_key()
    cx = os.getenv("GOOGLE_SEARCH_CX")
    if not api_key or not cx:
        return []
    try:
        resp = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"q": query, "cx": cx, "key": api_key, "num": 8, "safe": "active"},
            timeout=10,
        )
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            urls = [it.get("link") for it in items if it.get("link")]
            return [u for u in urls if u]
        # 403/429 → check if it's an org-block and kill switch
        try:
            body = resp.json()
        except Exception:
            body = {}
        if resp.status_code in (403, 429) and _is_org_blocked_error(body):
            _disable_google_api(f"{resp.status_code} {body.get('error', {}).get('message','')[:80]}")
        return []
    except Exception as e:
        print(f"--- Google web-search error: {e} ---")
        return []


def _capture_full_page_screenshot(url: str) -> bytes | None:
    """Open `url` with headless Chromium and return a full-page PNG screenshot,
    or None if the page can't be rendered.

    Phase 2.3 hardening: e-commerce product pages routinely lazy-load their
    hero image after `domcontentloaded` (sometimes only when the hero is in
    the viewport). The previous version captured too early and got blank /
    placeholder screenshots, which then made the AI bbox call point at white
    space. Now we:

      1. wait for the DOM
      2. scroll to the bottom (forces lazy-load observers to fire)
      3. wait for network to settle
      4. scroll back to top so the hero is the most prominent element
      5. wait for fonts/images to render
      6. screenshot
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"  ⚠ Playwright import failed: {e}")
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 1600},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                ),
            )
            _apply_stealth(ctx)
            page = ctx.new_page()
            page.goto(url, timeout=30_000, wait_until="domcontentloaded")

            # 1) Trigger lazy-load by scrolling to bottom and back to top.
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                page.wait_for_timeout(1200)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2);")
                page.wait_for_timeout(800)
                page.evaluate("window.scrollTo(0, 0);")
                page.wait_for_timeout(800)
            except Exception:
                logger.debug("Screenshot scroll/settle step failed", exc_info=True)

            # 2) Wait for network to settle — most e-commerce pages stop
            # fetching images after this. networkidle can hang on pages with
            # background polling, so we cap it.
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                logger.debug("networkidle wait timed out", exc_info=True)

            # 3) Final breathing room for CSS/font reflows.
            page.wait_for_timeout(800)

            png = page.screenshot(full_page=True, type="png")
            browser.close()
            return png
    except Exception as e:
        print(f"  ⚠ Playwright screenshot failed for {url}: {e}")
        return None


# ── Phase 2.3: Playwright SERP scraper with multi-engine rotation ──────────

def _apply_stealth(ctx) -> None:
    """Best-effort stealth patch on a Playwright BrowserContext. Silently
    skipped if `playwright-stealth` is missing."""
    try:
        from playwright_stealth import Stealth  # type: ignore
        Stealth().apply_stealth_sync(ctx)
    except Exception:
        # Stealth is optional. Without it the SERP scraper still works for
        # most queries; it just gets blocked more often.
        pass


# Engine descriptors: (name, search_url_template, link_selectors)
# Templates use {q} for the query; selectors are tried in order until one
# yields ≥1 result. Selectors prefer real result links over ads / images.
# Phase 2.3 v2: selectors updated for current SERP layouts. Bing and DDG
# changed their result HTML in the past year; the old class-name selectors
# matched zero links. Layouts these target (Nov 2025):
#   - Bing: organic results live under .b_algo cite parents; .b_attribution
#     is dead, primary anchor is direct .b_algo > h2 > a.
#   - DDG HTML lite endpoint: .web-result h2 a (current), .result__a (legacy).
#   - Startpage: clean, hasn't changed much.
#   - Google: avoided as primary because the layout requires JS to render.
_SERP_ENGINES = [
    (
        "startpage",
        "https://www.startpage.com/sp/search?query={q}",
        [
            "a.w-gl__result-title",
            "a.result-title",
            "a[data-testid='result-title-a']",
            "section.w-gl__result a[href^='http']",
        ],
    ),
    (
        "bing",
        "https://www.bing.com/search?q={q}&count=15&form=QBLH",
        [
            "li.b_algo h2 a[href^='http']",
            "ol#b_results li.b_algo h2 a",
            "li.b_algo a[href^='http']:not(.b_attribution a)",
            "h2 > a[href^='http']",
        ],
    ),
    (
        "duckduckgo_html",
        "https://html.duckduckgo.com/html/?q={q}",
        [
            "div.web-result h2 a",
            "h2.result__title a.result__a",
            "a.result__a",
            "a[rel='noopener'][href^='http']",
        ],
    ),
    (
        "google",
        "https://www.google.com/search?q={q}&num=15",
        [
            "div.yuRUbf > a[href^='http']",
            "div#search a[href^='http']:not([href*='google.com'])",
            "div#search a[href^='/url?']",
            "a[jsname][href^='http']",
        ],
    ),
]


def _normalize_serp_link(href: str, base: str) -> str | None:
    """Resolve relative + Google /url? wrappers, drop tracking links."""
    if not href:
        return None
    if href.startswith("/url?") or href.startswith("/?url="):
        # Google sometimes wraps results in /url?q=…
        from urllib.parse import parse_qs
        try:
            qs = parse_qs(urlparse(href).query)
            for key in ("q", "url"):
                if key in qs and qs[key]:
                    href = qs[key][0]
                    break
        except Exception:
            logger.debug("DuckDuckGo redirect parse failed", exc_info=True)
    if href.startswith("/"):
        href = urljoin(base, href)
    if not href.startswith("http"):
        return None
    # Skip search-engine-internal pages
    bad_hosts = {"google.com", "bing.com", "duckduckgo.com", "startpage.com",
                 "www.google.com", "www.bing.com", "www.startpage.com"}
    try:
        host = urlparse(href).netloc.lower()
    except Exception:
        host = ""
    if host in bad_hosts:
        return None
    return href


# ── Phase 2.4 — simple HTTP-only SERP scrapers ─────────────────────────────
#
# Playwright SERP scraping is reliable when an engine actually serves
# headless browsers, but Google in particular increasingly returns anti-bot
# HTML that our selectors don't match. The functions below skip Playwright
# entirely: one HTTP GET + BeautifulSoup parse, ~500ms per engine. We use
# them for the URL-discovery step where we just need the first organic
# results for a verbatim query.

_SIMPLE_SERP_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _normalize_external_link(href: str) -> str | None:
    """Drop search-engine-internal links + Google /url? wrappers."""
    if not href:
        return None
    if href.startswith("/url?") or href.startswith("/?"):
        from urllib.parse import parse_qs
        try:
            qs = parse_qs(urlparse(href).query)
            for k in ("q", "url", "uddg"):
                if k in qs and qs[k]:
                    href = qs[k][0]
                    break
        except Exception:
            return None
    if not href.startswith("http"):
        return None
    try:
        host = urlparse(href).netloc.lower()
    except Exception:
        return None
    bad = ("google.", "bing.", "duckduckgo.", "youtube.com")
    if any(b in host for b in bad):
        return None
    return href


def simple_google_search(query: str, max_results: int = 10) -> list[str]:
    """One-shot HTTP scrape of Google's SERP. Brittle (Google often serves
    anti-bot HTML) — best used as one of several fallbacks. Returns the
    first few organic result URLs."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    try:
        url = f"https://www.google.com/search?q={quote_plus(query)}&num={max_results}&hl=en"
        resp = requests.get(url, headers=_SIMPLE_SERP_HEADERS, timeout=10)
        if resp.status_code != 200:
            print(f"  ⚠ Google HTTP {resp.status_code}")
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[str] = []
        seen: set[str] = set()
        # Modern Google: each organic hit's title <h3> sits inside the
        # parent <a>. Walk h3s back up to the anchor.
        for h3 in soup.find_all("h3"):
            a = h3.find_parent("a")
            if not a:
                continue
            normalized = _normalize_external_link(a.get("href") or "")
            if normalized and normalized not in seen:
                seen.add(normalized)
                results.append(normalized)
            if len(results) >= max_results:
                break
        # Belt-and-braces: fall back to result-block selectors if the
        # h3-walk approach finds nothing (some Google variants wrap
        # differently).
        if not results:
            for sel in ("div.tF2Cxc a", "div.yuRUbf a", "div.g a"):
                for a in soup.select(sel):
                    normalized = _normalize_external_link(a.get("href") or "")
                    if normalized and normalized not in seen:
                        seen.add(normalized)
                        results.append(normalized)
                    if len(results) >= max_results:
                        break
                if results:
                    break
        print(f"  ✓ Google HTTP scrape: {len(results)} link(s) for {query!r}")
        return results
    except Exception as e:
        print(f"  ⚠ Google HTTP scrape failed: {e}")
        return []


def simple_bing_search(query: str, max_results: int = 10) -> list[str]:
    """One-shot HTTP scrape of Bing's SERP. Bing is far more bot-friendly
    than Google — this almost always returns results."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    try:
        url = f"https://www.bing.com/search?q={quote_plus(query)}&count={max_results}"
        resp = requests.get(url, headers=_SIMPLE_SERP_HEADERS, timeout=10)
        if resp.status_code != 200:
            print(f"  ⚠ Bing HTTP {resp.status_code}")
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[str] = []
        seen: set[str] = set()
        for li in soup.select("li.b_algo"):
            a = li.select_one("h2 a, .b_title a, a")
            if not a:
                continue
            normalized = _normalize_external_link(a.get("href") or "")
            if normalized and normalized not in seen:
                seen.add(normalized)
                results.append(normalized)
            if len(results) >= max_results:
                break
        print(f"  ✓ Bing HTTP scrape: {len(results)} link(s) for {query!r}")
        return results
    except Exception as e:
        print(f"  ⚠ Bing HTTP scrape failed: {e}")
        return []


def simple_ddg_search(query: str, max_results: int = 10) -> list[str]:
    """One-shot HTTP scrape of DuckDuckGo's HTML-lite endpoint. Designed
    for non-JS clients so it's the most consistently scrape-friendly."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    try:
        url = "https://html.duckduckgo.com/html/"
        resp = requests.post(
            url,
            data={"q": query, "kl": "wt-wt"},
            headers=_SIMPLE_SERP_HEADERS,
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"  ⚠ DDG HTTP {resp.status_code}")
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[str] = []
        seen: set[str] = set()
        for a in soup.select("a.result__a"):
            normalized = _normalize_external_link(a.get("href") or "")
            if normalized and normalized not in seen:
                seen.add(normalized)
                results.append(normalized)
            if len(results) >= max_results:
                break
        print(f"  ✓ DDG HTTP scrape: {len(results)} link(s) for {query!r}")
        return results
    except Exception as e:
        print(f"  ⚠ DDG HTTP scrape failed: {e}")
        return []


def playwright_serp_search(query: str, max_results: int = 5,
                           engines: list[str] | None = None) -> list[str]:
    """Phase 2.3: human-mimic search-result scraper.

    Opens a real Chromium tab on each search engine in turn (with stealth
    patches if available), reads the first page of results, and returns
    deduplicated host-page URLs. Falls through engines on 0 results / errors
    so a single block doesn't kill the pipeline.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"  ⚠ Playwright import failed: {e}")
        return []

    # Phase 2.4: honor the ORDER of `engines` when provided. Previously we
    # iterated `_SERP_ENGINES` in its declared order and only used `engines`
    # as a filter, which meant callers couldn't say "try Google first".
    engine_lookup = {n: (n, t, s) for n, t, s in _SERP_ENGINES}
    if engines:
        ordered_engines = [engine_lookup[e.lower()]
                           for e in engines
                           if e.lower() in engine_lookup]
    else:
        ordered_engines = list(_SERP_ENGINES)

    collected: list[str] = []
    seen: set[str] = set()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = browser.new_context(
                viewport={"width": 1366, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                ),
                locale="en-US",
            )
            _apply_stealth(ctx)
            page = ctx.new_page()

            for engine, url_tpl, selectors in ordered_engines:
                if len(collected) >= max_results:
                    break

                serp_url = url_tpl.format(q=quote_plus(query))
                print(f"  🔎 SERP scrape ({engine}): {query!r}")
                try:
                    page.goto(serp_url, timeout=15_000, wait_until="domcontentloaded")
                    page.wait_for_timeout(900)
                except Exception as e:
                    print(f"  ⚠ SERP {engine} navigation failed: {e}")
                    continue

                links: list[str] = []
                for sel in selectors:
                    try:
                        elements = page.query_selector_all(sel)
                    except Exception:
                        continue
                    for el in elements:
                        try:
                            href = el.get_attribute("href")
                        except Exception:
                            href = None
                        normalized = _normalize_serp_link(href or "", serp_url)
                        if normalized and normalized not in seen:
                            seen.add(normalized)
                            links.append(normalized)
                        if len(links) >= max_results:
                            break
                    if links:
                        break

                print(f"     → {len(links)} link(s) from {engine}")
                collected.extend(links)
                _human_jitter(2.0, 5.0)

            browser.close()
    except Exception as e:
        print(f"  ⚠ Playwright SERP run crashed: {e}")

    return collected[:max_results]


def _build_screenshot_candidate_pages(target_label: str, supplier_url: str | None,
                                      brand: str | None) -> tuple[list[str], str]:
    """Build the deduplicated candidate-page list used by the screenshot
    pipeline. Returns (ordered_pages, query) where `query` is the cleaned
    search string for downstream prompts."""
    clean_name = clean_search_query(target_label)
    query = f"{clean_name} official photo"
    brand_domain = resolve_brand_domain(brand)
    locked_query = f"{query} site:{brand_domain}" if brand_domain else query

    candidate_pages = []
    if supplier_url:
        candidate_pages.append(supplier_url)

    candidate_pages.extend(playwright_serp_search(locked_query, max_results=5))
    if brand_domain and len(candidate_pages) <= (1 if supplier_url else 0):
        candidate_pages.extend(playwright_serp_search(query, max_results=5))

    if len(candidate_pages) <= (1 if supplier_url else 0):
        candidate_pages.extend(_search_google_pages(locked_query))
        if brand_domain and len(candidate_pages) <= (1 if supplier_url else 0):
            candidate_pages.extend(_search_google_pages(query))

    if not candidate_pages and HAS_DDGS and not _DDG_DISABLED:
        import time as _time2
        _started = _time2.monotonic()
        try:
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=6):
                    href = r.get("href") or r.get("url")
                    if href:
                        candidate_pages.append(href)
            try:
                from .api_metering import log_search_call
                log_search_call(provider="duckduckgo", query_count=1,
                                latency_ms=int((_time2.monotonic() - _started) * 1000))
            except Exception: logger.debug("suppressed in image_processing", exc_info=True)
        except Exception as e:
            try:
                from .api_metering import log_search_call
                log_search_call(provider="duckduckgo", query_count=1,
                                latency_ms=int((_time2.monotonic() - _started) * 1000),
                                error=f"{type(e).__name__}")
            except Exception: logger.debug("suppressed in image_processing", exc_info=True)
            if _is_ddg_ratelimit(e):
                _disable_ddg(f"{type(e).__name__}: {e}")
            else:
                print(f"  ⚠ DDG text search failed: {e}")

    seen, ordered_pages = set(), []
    for u in candidate_pages:
        if not u or u in seen:
            continue
        if _is_screenshot_blocked_url(u):
            # Saves ~30s of Playwright launch+timeout per skipped social URL.
            continue
        ordered_pages.append(u)
        seen.add(u)
    return ordered_pages, query


# Hosts the Playwright screenshot fallback should never even try to open.
# Social/video sites need login walls, lazy-load product photos behind JS, or
# simply don't have a "primary product hero shot" — every visit costs ~30s of
# Playwright launch + nav timeout. Hard-block them up front.
#
# Match is by EXACT hostname (with subdomain support) — NOT substring. The
# previous substring approach silently dropped legit product pages whose
# domains happen to contain a fragment of one of these (e.g. `linux.com`,
# `box.com`, `unix.com` all contain `x.com` as a substring).
_SCREENSHOT_BLOCKED_HOSTS = frozenset({
    "instagram.com", "facebook.com", "fb.com", "fb.watch", "m.facebook.com",
    "tiktok.com",    "youtube.com",  "youtu.be", "m.youtube.com",
    "twitter.com",   "x.com",        "threads.net",
    "pinterest.com", "pinterest.fr", "pinterest.co.uk",
    "reddit.com",    "old.reddit.com",
    "linkedin.com",  "snapchat.com", "vimeo.com",
})


def _is_screenshot_blocked_url(url: str) -> bool:
    """True if the URL's hostname is (or is a subdomain of) a blocked
    social/video site. Subdomain match means `www.instagram.com` or
    `business.facebook.com` are blocked, but `linux.com` or `box.com`
    (which merely contain `x.com` as a substring) are NOT."""
    if not url:
        return True
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or '').lower().strip()
    except Exception:
        return False
    if not host:
        return False
    if host in _SCREENSHOT_BLOCKED_HOSTS:
        return True
    return any(host.endswith('.' + blocked) for blocked in _SCREENSHOT_BLOCKED_HOSTS)


def _crop_product_from_page(page_url: str, target_label: str, upload_folder: str,
                            skip_verify: bool = False) -> str | None:
    """Open `page_url`, screenshot it, ask Gemini for a bounding box, crop
    and (unless skip_verify) verify the crop. Returns the relative
    `uploads/...` path of the saved (rembg-cleaned) image, or None."""
    if _is_screenshot_blocked_url(page_url):
        print(f"  ⏭ Screenshot fallback: skipping social/video URL → {page_url}")
        return None
    print(f"  🌐 Screenshot fallback: opening {page_url}")
    png_bytes = _capture_full_page_screenshot(page_url)
    if not png_bytes:
        return None

    try:
        screenshot = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    except Exception as e:
        print(f"  ⚠ Could not decode screenshot: {e}")
        return None

    box_prompt = get_prompt('webpage_product_crop')
    try:
        prompt = box_prompt.format(target_label=target_label)
        from .api_metering import gemini_call
        response = gemini_call(
            prompt_id='webpage_product_crop',
            model=_MODEL,
            client=_get_client(),
            contents=[prompt, types.Part.from_bytes(data=png_bytes, mime_type="image/png")],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        box_result = json.loads(response.text or "{}")
    except Exception as e:
        print(f"  ⚠ Gemini bounding-box call failed: {e}")
        return None

    if not box_result.get("found") or not box_result.get("box_2d"):
        return None

    try:
        ymin, xmin, ymax, xmax = box_result["box_2d"]
        w, h = screenshot.size
        left   = max(0, int(xmin / 1000 * w))
        top    = max(0, int(ymin / 1000 * h))
        right  = min(w, int(xmax / 1000 * w))
        bottom = min(h, int(ymax / 1000 * h))
        if right - left < 80 or bottom - top < 80:
            return None
        crop = screenshot.crop((left, top, right, bottom))
        buf = io.BytesIO()
        crop.save(buf, "JPEG", quality=92)
        crop_bytes = buf.getvalue()
    except Exception as e:
        print(f"  ⚠ Crop step failed: {e}")
        return None

    if not skip_verify and not ai_verify_crop_matches(crop_bytes, target_label):
        return None

    safe_name = secure_filename(target_label)
    filename = f"web_{safe_name}_{int(time.time())}_{random.randint(100,999)}.jpg"
    save_path = os.path.join(upload_folder, filename)
    try:
        with open(save_path, "wb") as f:
            f.write(crop_bytes)
    except Exception as e:
        print(f"  ⚠ Could not save screenshot crop: {e}")
        return None

    cleaned_path = _maybe_remove_background(save_path)
    rel = f"uploads/{os.path.basename(cleaned_path)}"
    print(f"  ✅ Screenshot crop succeeded → {rel}")
    return rel


def find_image_via_screenshot(target_label: str, supplier_url: str | None = None,
                              upload_folder: str | None = None,
                              brand: str | None = None) -> str | None:
    """Phase 2.2 last-resort fallback for "no-image" proformas, hardened in
    Phase 2.3 with brand-domain locking, multi-engine SERP scraping, and
    rate-limit-aware retry/backoff.

    Returns the static-relative path (e.g. "uploads/web_X_TS_clean.jpg") or
    None.
    """
    if not target_label or not upload_folder:
        return None

    ordered_pages, _ = _build_screenshot_candidate_pages(target_label, supplier_url, brand)
    if not ordered_pages:
        print(f"  🚫 Screenshot fallback: no candidate pages for '{target_label}'")
        return None

    for page_url in ordered_pages[:4]:
        rel = _crop_product_from_page(page_url, target_label, upload_folder, skip_verify=False)
        if rel:
            return rel

    print(f"  🚫 Screenshot fallback exhausted for '{target_label}'")
    return None


def find_multi_images_via_screenshot(target_label: str,
                                     supplier_url: str | None = None,
                                     upload_folder: str | None = None,
                                     brand: str | None = None,
                                     max_results: int = 3,
                                     skip_verify: bool = True,
                                     log_cb=None,
                                     cancel_event=None) -> list[dict]:
    """Phase 2.4 wizard variant. Same pipeline as `find_image_via_screenshot`,
    but keeps cropping until we collect up to `max_results` candidates.

    Each candidate is a dict: {"path": "uploads/web_…jpg", "page_url": str}.
    `log_cb` is an optional `callable(msg)` for emitting structured progress
    lines back to the wizard logger.

    `cancel_event`: optional `threading.Event`. Checked between Playwright
    page captures so the wizard can stop the pipeline the moment the user
    commits a candidate (no point screenshotting a 30 MB MoMA PDF after
    they've already saved). Whichever screenshot is currently mid-flight
    will still finish — this only prevents *new* page captures from
    starting.
    """
    if not target_label or not upload_folder:
        return []

    def _emit(msg: str) -> None:
        if log_cb:
            try: log_cb(msg)
            except Exception: logger.debug("suppressed in image_processing", exc_info=True)

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    if _cancelled():
        return []

    ordered_pages, _q = _build_screenshot_candidate_pages(target_label, supplier_url, brand)
    if not ordered_pages:
        _emit(f"No candidate pages found for '{target_label}'")
        return []

    _emit(f"Trying {min(len(ordered_pages), max_results + 3)} candidate page(s)...")

    results: list[dict] = []
    seen_pages: set[str] = set()
    # Cap browser launches — try a few extra in case some pages don't crop.
    for page_url in ordered_pages[:max_results + 3]:
        if _cancelled():
            _emit("Screenshot pipeline cancelled by client — bailing")
            break
        if page_url in seen_pages:
            continue
        seen_pages.add(page_url)
        if len(results) >= max_results:
            break
        _emit(f"Capturing {page_url}")
        rel = _crop_product_from_page(page_url, target_label, upload_folder, skip_verify=skip_verify)
        if rel:
            _emit(f"Crop OK → {rel}")
            results.append({"path": rel, "page_url": page_url})
        else:
            _emit(f"No crop from {page_url}")

    return results
