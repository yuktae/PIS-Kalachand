"""
Web scraping utilities for PIS System
Handles URL scraping and data extraction.

Tiered scraping pipeline:
  1. Firecrawl (premium, if FIRECRAWL_API_KEY is set)
  2. Jina AI Reader (free, handles JS, clean markdown)
  3. BeautifulSoup (basic fallback)
"""

import os
import re
import json
import time
from typing import Any
import requests  # type: ignore[import-untyped]
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup


# ===================== TIER 1: FIRECRAWL (PREMIUM) =====================

def scrape_with_firecrawl(url, timeout=30):
    """
    Scrape a URL using Firecrawl API (premium, best quality).
    Requires FIRECRAWL_API_KEY environment variable.
    Returns markdown content or None if unavailable/failed.
    """
    api_key = os.getenv('FIRECRAWL_API_KEY', '').strip()
    if not api_key:
        return None

    try:
        print(f"🔥 [Firecrawl] Scraping: {url}")
        response = requests.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            },
            json={
                "url": url,
                "formats": ["markdown"]
            },
            timeout=timeout
        )

        if response.status_code == 200:
            data = response.json()
            markdown = data.get("data", {}).get("markdown", "")
            if markdown and len(markdown) > 100:
                print(f"✅ [Firecrawl] Got {len(markdown)} chars of markdown")
                return markdown
            else:
                print(f"⚠️ [Firecrawl] Response too short ({len(markdown)} chars)")
                return None
        elif response.status_code == 402:
            print("⚠️ [Firecrawl] Credits exhausted (402)")
            return None
        elif response.status_code == 429:
            print("⚠️ [Firecrawl] Rate limited (429)")
            return None
        else:
            print(f"⚠️ [Firecrawl] HTTP {response.status_code}: {response.text[:200]}")
            return None

    except requests.exceptions.Timeout:
        print("⚠️ [Firecrawl] Request timed out")
        return None
    except Exception as e:
        print(f"⚠️ [Firecrawl] Error: {e}")
        return None


# ===================== TIER 2: JINA AI READER (FREE) =====================

def scrape_with_jina(url, timeout=30):
    """
    Scrape a URL using Jina AI Reader (FREE, handles JavaScript).
    No API key required for basic usage (20 RPM).
    Returns clean markdown content or None if failed.
    """
    try:
        jina_url = f"https://r.jina.ai/{url}"
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0"
        }

        # Add API key if available (500 RPM vs 20 RPM)
        jina_key = os.getenv('JINA_API_KEY', '').strip()
        if jina_key:
            headers["Authorization"] = f"Bearer {jina_key}"

        print(f"📖 [Jina Reader] Scraping: {url}")
        response = requests.get(jina_url, headers=headers, timeout=timeout)

        if response.status_code == 200:
            try:
                data = response.json()
                content = data.get("data", {}).get("content", "") or data.get("content", "")
            except (json.JSONDecodeError, ValueError):
                content = response.text

            if content and len(content) > 100:
                print(f"✅ [Jina Reader] Got {len(content)} chars of clean content")
                return content
            else:
                print(f"⚠️ [Jina Reader] Response too short ({len(content) if content else 0} chars)")
                return None
        else:
            print(f"⚠️ [Jina Reader] HTTP {response.status_code}")
            return None

    except requests.exceptions.Timeout:
        print("⚠️ [Jina Reader] Request timed out")
        return None
    except Exception as e:
        print(f"⚠️ [Jina Reader] Error: {e}")
        return None


# ===================== TIER 3: BEAUTIFULSOUP (FALLBACK) =====================

def _extract_structured_data(soup) -> dict:
    """Phase 2.4: pull authoritative product metadata out of the page —
    Schema.org JSON-LD (Product/Offer), OpenGraph (`og:*`) and Twitter
    (`twitter:*`) meta tags. The proforma extraction prompt treats this
    block as truth and uses it verbatim instead of relying on Gemini to
    re-read the rendered HTML.

    Returns: {'jsonld_products': [...], 'og': {...}, 'twitter': {...}}
    """
    out = {'jsonld_products': [], 'og': {}, 'twitter': {}}

    # ── JSON-LD Schema.org Product blocks ──
    for script in soup.find_all('script', type='application/ld+json'):
        raw = script.string or script.get_text() or ''
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        # JSON-LD blocks can be a single object, a list, or a graph wrapper
        candidates = []
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            if isinstance(data.get('@graph'), list):
                candidates = data['@graph']
            else:
                candidates = [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            t = item.get('@type', '')
            if isinstance(t, list):
                t = next((x for x in t if 'Product' in str(x)), t[0] if t else '')
            if 'Product' not in str(t) and not item.get('sku') and not item.get('mpn'):
                continue

            brand = item.get('brand')
            if isinstance(brand, dict):
                brand = brand.get('name')
            offers = item.get('offers')
            price = price_currency = availability = None
            if isinstance(offers, dict):
                price = offers.get('price')
                price_currency = offers.get('priceCurrency')
                availability = offers.get('availability')
            elif isinstance(offers, list) and offers and isinstance(offers[0], dict):
                price = offers[0].get('price')
                price_currency = offers[0].get('priceCurrency')
                availability = offers[0].get('availability')

            image = item.get('image')
            if isinstance(image, list):
                image = image[0] if image else None
            elif isinstance(image, dict):
                image = image.get('url')

            out['jsonld_products'].append({
                'name': item.get('name'),
                'brand': brand,
                'sku': item.get('sku'),
                'mpn': item.get('mpn'),
                'gtin': item.get('gtin') or item.get('gtin13') or item.get('gtin12'),
                'description': (item.get('description') or '')[:1500],
                'price': price,
                'price_currency': price_currency,
                'availability': availability,
                'image': image,
            })

    # ── OpenGraph ──
    for meta in soup.find_all('meta', attrs={'property': re.compile(r'^og:')}):
        name = (meta.get('property') or '').replace('og:', '')
        content = (meta.get('content') or '').strip()
        if name and content and name not in out['og']:
            out['og'][name] = content[:500]

    # Some product pages use `og:price:amount` / `product:price:amount`
    for meta in soup.find_all('meta', attrs={'property': re.compile(r'^product:')}):
        name = (meta.get('property') or '').replace('product:', 'product_')
        content = (meta.get('content') or '').strip()
        if name and content:
            out['og'][name] = content[:500]

    # ── Twitter Card ──
    for meta in soup.find_all('meta', attrs={'name': re.compile(r'^twitter:')}):
        name = (meta.get('name') or '').replace('twitter:', '')
        content = (meta.get('content') or '').strip()
        if name and content:
            out['twitter'][name] = content[:500]

    if out['jsonld_products'] or out['og'] or out['twitter']:
        print(f"📦 [Structured] {len(out['jsonld_products'])} JSON-LD products, "
              f"{len(out['og'])} OG tags, {len(out['twitter'])} Twitter tags")
    return out


def scrape_with_beautifulsoup(url):
    """
    Basic scraping with requests + BeautifulSoup.
    Cannot handle JavaScript-rendered content.
    Returns dict with 'text', 'html', 'image_candidates', 'structured_data'.
    """
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(response.content, 'html.parser')

        structured = _extract_structured_data(soup)

        # 1. Extract Image Candidates
        image_candidates = []
        exclude_patterns = [
            'logo', 'icon', 'facebook', 'instagram', 'twitter', 'linkedin', 'youtube',
            'visa', 'mastercard', 'amex', 'paypal', 'cart', 'search', 'menu', 'arrow',
            'pixel', 'banner', 'ads', 'loading', 'placeholder', '.svg', '.gif'
        ]

        for img in soup.find_all('img'):
            src = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
            if not src:
                continue

            if src.startswith('//'):
                src = 'https:' + src
            elif src.startswith('/'):
                src = urljoin(url, src)
            elif not src.startswith('http'):
                continue

            src_lower = src.lower()
            if any(p in src_lower for p in exclude_patterns):
                continue

            alt = (img.get('alt') or '').lower()
            if any(p in alt for p in exclude_patterns):
                continue

            if src not in image_candidates:
                image_candidates.append(src)

        image_candidates = image_candidates[:20]

        # 2. Cleanup Soup for Text
        for script in soup(["script", "style", "nav", "footer", "header", "aside"]):
            script.extract()

        text_content = " ".join(soup.get_text(separator=' ').split())[:20000]
        html_content = str(soup.body)[:40000] if soup.body else ""

        print(f"📄 [BeautifulSoup] Got {len(text_content)} chars text, {len(image_candidates)} images")

        return {
            "text": text_content,
            "html": html_content,
            "image_candidates": image_candidates,
            "structured_data": structured,
        }
    except Exception as e:
        print(f"⚠️ [BeautifulSoup] Scrape Error: {e}")
        return {"text": "", "html": "", "image_candidates": [], "structured_data": {}}


# ===================== SUB-PAGE DISCOVERY =====================

def discover_product_links(url, page_content):
    """
    Discover product sub-page links from a listing page.
    Uses both HTML link extraction and pattern matching.
    Returns a list of absolute URLs (max 15).
    """
    found_links = set()
    base_domain = urlparse(url).netloc

    try:
        # Try to fetch raw HTML for link extraction (Jina/Firecrawl give markdown)
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(response.content, 'html.parser')

        # Product link patterns - common e-commerce URL patterns
        product_patterns = [
            r'/product[s]?/',
            r'/item[s]?/',
            r'/p/',
            r'/dp/',
            r'/catalogue/',
            r'/catalog/',
            r'\?.*sku=',
            r'\?.*product_id=',
            r'\?.*item=',
        ]

        # Look for links in product-like containers
        product_containers = soup.select(
            '[class*="product"], [class*="item"], [class*="card"], '
            '[class*="listing"], [class*="catalog"], [class*="grid-item"], '
            '[data-product], [data-item]'
        )

        all_links = []

        # Priority 1: Links inside product containers
        for container in product_containers:
            for a_tag in container.find_all('a', href=True):
                href = a_tag['href']
                if href.startswith('/'):
                    href = urljoin(url, href)
                elif not href.startswith('http'):
                    href = urljoin(url, href)

                parsed = urlparse(href)
                if parsed.netloc and parsed.netloc != base_domain:
                    continue  # Skip external links

                # Skip common non-product paths
                path_lower = parsed.path.lower()
                skip_patterns = ['/cart', '/login', '/register', '/account', '/contact',
                                 '/about', '/faq', '/help', '/privacy', '/terms', '/blog',
                                 '/category', '/search', '#', 'javascript:']
                if any(s in path_lower for s in skip_patterns):
                    continue

                all_links.append(href)

        # Priority 2: All page links matching product patterns
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            if href.startswith('/'):
                href = urljoin(url, href)
            elif not href.startswith('http'):
                href = urljoin(url, href)

            parsed = urlparse(href)
            if parsed.netloc and parsed.netloc != base_domain:
                continue

            href_lower = href.lower()
            for pattern in product_patterns:
                if re.search(pattern, href_lower):
                    all_links.append(href)
                    break

        # Deduplicate while keeping order
        seen = set()
        for link in all_links:
            # Normalize: remove trailing slashes, fragments
            clean = link.split('#')[0].rstrip('/')
            if clean not in seen and clean != url.rstrip('/'):
                seen.add(clean)
                found_links.add(clean)

    except Exception as e:
        print(f"⚠️ Link discovery error: {e}")

    result = list(found_links)[:15]
    print(f"🔗 Discovered {len(result)} product sub-pages from {url}")
    return result


def scrape_subpages(urls, max_pages=10):
    """
    Scrape multiple sub-pages and combine their content.
    Uses Jina Reader for each page (free, handles JS).
    Returns combined markdown content.
    """
    if not urls:
        return ""

    urls_to_scrape = urls[:max_pages]
    combined_content = []

    for i, page_url in enumerate(urls_to_scrape):
        print(f"  📄 Scraping sub-page {i+1}/{len(urls_to_scrape)}: {page_url}")

        # Try Jina first, then BeautifulSoup
        content = scrape_with_jina(page_url, timeout=20)
        if not content:
            bs_data = scrape_with_beautifulsoup(page_url)
            content = bs_data.get("text", "")

        if content and len(content) > 50:
            # Truncate individual pages to avoid token overflow
            truncated = content[:8000]
            combined_content.append(f"\n\n--- PRODUCT PAGE: {page_url} ---\n{truncated}")

        # Rate limiting for Jina free tier (20 RPM)
        time.sleep(1)

    result = "\n".join(combined_content)
    print(f"📚 Combined {len(combined_content)} sub-pages ({len(result)} chars total)")
    return result


# ===================== MAIN ORCHESTRATOR =====================

def scrape_url_data(url) -> dict[str, Any]:
    """
    Scrapes a URL and returns a dictionary with 'text', 'html',
    'image_candidates', and 'structured_data' (Phase 2.4: JSON-LD + OG).
    Uses a tiered pipeline: Firecrawl → Jina Reader → BeautifulSoup.
    """
    # Always get image candidates via BeautifulSoup (fast, reliable for images
    # and the only path that extracts JSON-LD / OpenGraph metadata).
    bs_data = scrape_with_beautifulsoup(url)
    image_candidates = bs_data.get("image_candidates", [])
    structured_data = bs_data.get("structured_data", {})

    # Try enhanced scraping for text content
    markdown_content = None

    # Tier 1: Firecrawl (if API key set)
    markdown_content = scrape_with_firecrawl(url)

    # Tier 2: Jina Reader (free)
    if not markdown_content:
        markdown_content = scrape_with_jina(url)

    # Use enhanced content if available, otherwise fall back to BS
    if markdown_content:
        text_content = markdown_content[:50000]
        html_content = bs_data.get("html", "")
    else:
        print("⚠️ All enhanced scrapers failed, using BeautifulSoup only")
        text_content = bs_data.get("text", "")
        html_content = bs_data.get("html", "")

    return {
        "text": text_content,
        "html": html_content,
        "image_candidates": image_candidates,
        "structured_data": structured_data,
    }


def scrape_url_data_deep(url) -> dict[str, Any]:
    """
    Enhanced deep scraping for bulk URL-only extraction.
    Scrapes the main page + discovers and scrapes product sub-pages.
    Returns enriched data dict with combined content from all pages.
    """
    print(f"\n{'='*60}")
    print(f"🌐 DEEP SCRAPING: {url}")
    print(f"{'='*60}")

    # Step 1: Scrape the main page with the tiered pipeline
    main_data = scrape_url_data(url)
    main_text = main_data.get("text", "")

    # Step 2: Discover product sub-page links
    sub_links = discover_product_links(url, main_text)

    # Step 3: Scrape sub-pages for additional product data
    subpage_content = ""
    if sub_links:
        print(f"\n📡 Scraping {min(len(sub_links), 10)} product sub-pages...")
        subpage_content = scrape_subpages(sub_links, max_pages=10)

    # Step 4: Combine everything
    combined_text = main_text
    if subpage_content:
        combined_text += "\n\n" + "="*40 + "\nADDITIONAL PRODUCT PAGES DATA:\n" + "="*40 + "\n"
        combined_text += subpage_content

    # Limit total content to prevent token overflow
    combined_text = combined_text[:80000]

    print(f"\n✅ Deep scrape complete: {len(combined_text)} chars total ({len(sub_links)} sub-pages found)")
    print(f"{'='*60}\n")

    return {
        "text": combined_text,
        "html": main_data.get("html", ""),
        "image_candidates": main_data.get("image_candidates", []),
        "structured_data": main_data.get("structured_data", {}),
        "sub_pages_scraped": len(sub_links),
    }
