"""
Image processing utilities for PIS System
Handles image search, validation, and downloading
"""

import os
import re
import io
import json
import time
import requests
import shutil
from urllib.parse import urlparse, urljoin
from werkzeug.utils import secure_filename
from google import genai
from google.genai import types
from concurrent.futures import ThreadPoolExecutor, as_completed

_MODEL = 'gemini-2.0-flash'
_client = None

def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv('GOOGLE_API_KEY'))
    return _client
from PIL import Image
from .prompt_manager import get_prompt

# DuckDuckGo image search (free, no API key needed)
try:
    from duckduckgo_search import DDGS
    HAS_DDGS = True
except ImportError:
    HAS_DDGS = False
    print("⚠ duckduckgo-search not installed — DDG fallback disabled")


def extract_domain(url):
    """Extracts the base domain (e.g., mi.com) from a full URL."""
    try:
        parsed = urlparse(url)
        return parsed.netloc.replace("www.", "")
    except:
        return None


def search_google_api(query: str, domain: str | None = None) -> list[str]:
    api_key = os.getenv("GOOGLE_API_KEY")
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

    try:
        print(f"--- Calling Google Image API with query: '{query}' ---")
        if domain:
            print(f"--- Domain filter: {domain} ---")

        resp = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params=params,
            timeout=10
        )
        print(f"--- Google status code: {resp.status_code} ---")
        data = resp.json()

        if "items" not in data:
            print("Google returned NO image results")
            if resp.status_code != 200:
                print(f"--- Google Response Error: {json.dumps(data)} ---")
            return []

        urls = [item["link"] for item in data.get("items", [])]
        print(f"--- Google returned {len(urls)} image results ---")
        return urls

    except Exception as e:
        print(f"--- Google API Error: {str(e)} ---")
        return []

def search_duckduckgo(query: str, max_results: int = 10) -> list[str]:
    """Search DuckDuckGo Images — FREE, no API key, no daily quota."""
    if not HAS_DDGS:
        return []
    try:
        print(f"--- DuckDuckGo Image Search: '{query}' ---")
        ddgs = DDGS()
        results = ddgs.images(
            query,
            region="wt-wt",
            safesearch="moderate",
            max_results=max_results,
        )
        urls = [r["image"] for r in results if r.get("image")]
        print(f"--- DuckDuckGo returned {len(urls)} images ---")
        return urls
    except Exception as e:
        print(f"--- DuckDuckGo Search Error: {e} ---")
        return []



def clean_search_query(query: str) -> str:
    """
    Removes internal SKUs, bracketed numbers, and ERP codes
    before sending query to Google.
    """
    query = re.sub(r"\([^)]*\)", "", query)
    query = re.sub(r"\b[A-Z0-9]{8,}\b", "", query)

    cleaned = " ".join(query.split())
    print(f"--- Cleaned Search Query: '{cleaned}' ---")
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

        response = _get_client().models.generate_content(
            model=_MODEL,
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
        response = _get_client().models.generate_content(
            model=_MODEL,
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
        
        print(f"--- Scraped {len(images)} images from {url} ---")
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
        pass
    return None


def find_best_images(model_name: str, supplier_url: str | None = None) -> list[str]:
    """
    Multi-strategy image search pipeline.
    Returns a prioritized list of candidate image URLs.
    """
    candidates = []

    clean_name = clean_search_query(model_name)

    # --- 1️ Supplier-domain search (HIGHEST PRIORITY) ---
    if supplier_url:
        domain = extract_domain(supplier_url)
        if domain:
            supplier_query = f"{clean_name}"
            print(f"--- Strategy 1: Supplier Domain Search ({domain}) ---")
            candidates.extend(search_google_api(supplier_query, domain=domain))

    # --- 2️ Direct web scraping of supplier page ---
    if supplier_url:
        print(f"--- Strategy 2: Direct Scrape of {supplier_url} ---")
        scraped = scrape_images_from_url(supplier_url)
        candidates.extend(scraped)

    # --- 3️ Open-web search with product name ---
    open_query = f"{clean_name} product"
    print(f"--- Strategy 3: Open Web Search: '{open_query}' ---")
    candidates.extend(search_google_api(open_query))

    # --- 4️ Exact model number search (if different from product name) ---
    # Sometimes the model number alone yields better results
    if clean_name.lower() != model_name.lower():
        exact_query = f"{model_name}"
        print(f"--- Strategy 4: Exact Model Search: '{exact_query}' ---")
        candidates.extend(search_google_api(exact_query))

    # --- 5️ DuckDuckGo search (FREE, no quota limit) ---
    if len(candidates) < 5:
        ddg_query = f"{clean_name} product official photo"
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
        print(f"--- Attempting Web Download for {model_name}: {image_url} ---")
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
                print(f"--- Web Download Success: {filename} ({len(image_data)} bytes) ---")
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

    # 1. DuckDuckGo (free, no quota)
    ddg_urls = search_duckduckgo(f"{clean_name} product", max_results=8)
    all_urls.extend(ddg_urls)

    # 2. Google (if DDG found nothing)
    if not all_urls:
        google_urls = search_google_api(f"{clean_name} product")
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
