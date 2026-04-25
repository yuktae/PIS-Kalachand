"""
PDF processing utilities for PIS System
Handles PDF image extraction using high-quality page screenshots
and AI-powered product detection.
"""

import os
import io
import json
import time
import fitz  # PyMuPDF
from PIL import Image, ImageFilter, ImageStat, ImageDraw, ImageChops
from werkzeug.utils import secure_filename
from google import genai
from google.genai import types
from .prompt_manager import get_prompt

_MODEL = 'gemini-2.0-flash'
_client = None

def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv('GOOGLE_API_KEY'))
    return _client


# ===== EMBEDDED IMAGE CACHE =====
# Avoids re-scanning the same PDF for every product in bulk uploads
_embedded_cache = {}  # {pdf_path: [candidate_images]}
_used_images = set()  # Track used image indices to prevent reuse in bulk

def clear_pdf_cache():
    """Call after a bulk upload finishes to free memory."""
    global _embedded_cache, _used_images
    _embedded_cache.clear()
    _used_images.clear()
    print("🧹 PDF image cache cleared")


def extract_specific_image(pdf_path, target_model, upload_folder):
    """
    Extracts a product image from a PDF using a multi-pass approach:
    
    Pass 1: AI-powered screenshot scanning — renders full pages so AI can see
            both images AND adjacent text labels (model numbers) for accurate matching
    Pass 2: Extract embedded images with page-text context as fallback
    
    Returns the path to the saved image, or None if not found.
    """
    if not pdf_path or not os.path.exists(pdf_path):
        return None
    
    print(f"🔍 PDF Image Extraction starting for: '{target_model}'")
    
    # ============ PASS 1: AI Screenshot Scan (MOST ACCURATE) ============
    # Screenshot scan is preferred because the AI sees full page context:
    # the product image AND the model number text next to it
    result = _extract_via_screenshot(pdf_path, target_model, upload_folder)
    if result:
        print(f"✅ Pass 1 SUCCESS: Found product via screenshot scan for '{target_model}'")
        return result
    
    print(f"--- Pass 1 (screenshot) found nothing, trying embedded images ---")
    
    # ============ PASS 2: Extract Embedded Images (FALLBACK) ============
    result = _extract_embedded_images(pdf_path, target_model, upload_folder)
    if result:
        print(f"✅ Pass 2 SUCCESS: Found embedded image for '{target_model}'")
        return result
    
    print(f"🚫 No product image found in PDF for '{target_model}'")
    return None


def _extract_embedded_images(pdf_path, target_model, upload_folder):
    """
    Pass 2 (fallback): Extract actual embedded images from the PDF.
    Uses cache to avoid re-scanning the same PDF in bulk uploads.
    Now includes page text context for better AI matching.
    """
    global _embedded_cache, _used_images
    
    try:
        # Check cache first — avoid re-scanning for every product
        if pdf_path in _embedded_cache:
            candidate_images = _embedded_cache[pdf_path]
            print(f"  📦 Using cached {len(candidate_images)} embedded images (skipping PDF scan)")
        else:
            # First time — scan the PDF and cache results
            doc = fitz.open(pdf_path)
            candidate_images = []
            
            for page_num in range(min(30, len(doc))):
                page = doc[page_num]
                image_list = page.get_images(full=True)
                
                # Get all text from the page for context
                page_text = page.get_text("text")
                
                for img_index, img_info in enumerate(image_list):
                    xref = img_info[0]
                    try:
                        base_image = doc.extract_image(xref)
                        if not base_image:
                            continue
                        
                        image_bytes = base_image["image"]
                        img_ext = base_image.get("ext", "png")
                        
                        pil_img = Image.open(io.BytesIO(image_bytes))
                        w, h = pil_img.size
                        
                        if w < 100 or h < 100:
                            continue
                        
                        if _is_mostly_solid(pil_img):
                            continue
                        
                        print(f"  📎 Embedded image found: {w}x{h} ({len(image_bytes)} bytes) on page {page_num+1}")
                        candidate_images.append({
                            'bytes': image_bytes,
                            'width': w,
                            'height': h,
                            'page': page_num,
                            'page_text': page_text,  # Include page text for context
                            'ext': img_ext,
                            'img_index': img_index  # Track position on page
                        })
                        
                        if len(candidate_images) >= 50:
                            break
                            
                    except Exception as e:
                        print(f"  ⚠ Could not extract image xref {xref}: {e}")
                        continue
                
                if len(candidate_images) >= 50:
                    break
            
            doc.close()
            
            # Cache for subsequent products
            _embedded_cache[pdf_path] = candidate_images
            print(f"  📦 Cached {len(candidate_images)} embedded images for reuse")
        
        if not candidate_images:
            return None
        
        # Filter out already-used images
        available = [(i, c) for i, c in enumerate(candidate_images) if i not in _used_images]
        
        if not available:
            print("  ⚠ All embedded images already assigned to other products")
            return None
        
        # If only one available candidate, use it
        if len(available) == 1 and available[0][1]['width'] >= 200:
            idx, candidate = available[0]
            _used_images.add(idx)
            return _save_candidate_image(candidate, target_model, upload_folder)
        
        # Use AI to pick the image matching this specific product (with text context)
        result_idx = _ai_pick_best_from_candidates(
            [c for _, c in available], target_model, upload_folder,
            available_indices=[i for i, _ in available]
        )
        return result_idx
        
    except Exception as e:
        print(f"  ⚠ Embedded extraction error: {e}")
        return None


def _extract_via_screenshot(pdf_path, target_model, upload_folder):
    """
    Pass 1 (primary): High-resolution page screenshots + AI bounding box detection.
    This is the most accurate method because the AI sees both the product image
    AND the model number text on the page, enabling correct matching.
    """
    try:
        doc = fitz.open(pdf_path)
        print(f"  📸 Screenshot scan: {min(15, len(doc))} pages at 3x resolution")
        
        # Scan up to 15 pages
        for page_num in range(min(15, len(doc))):
            if page_num > 0:
                time.sleep(0.5)  # Rate limiting
            
            try:
                page = doc[page_num]
                
                # Check if the target model is mentioned on this page
                page_text = page.get_text("text")
                # Extract just the model part for matching (e.g., "AR-6234" from full name)
                model_parts = target_model.split()
                model_on_page = False
                for part in model_parts:
                    if len(part) >= 4 and part.upper() in page_text.upper():
                        model_on_page = True
                        break
                
                if not model_on_page and page_num > 0:
                    # Skip pages that don't mention this model (save API calls)
                    continue
                
                # 3x resolution matrix for sharper rendering
                pix = page.get_pixmap(matrix=fitz.Matrix(3, 3))
                page_img_bytes = pix.tobytes("png")
                pil_image = Image.open(io.BytesIO(page_img_bytes))
                
                prompt = get_prompt('pdf_screenshot_scan').format(target_model=target_model)

                response = None
                for attempt in range(3):  # Up to 3 attempts
                    try:
                        response = _get_client().models.generate_content(
                            model=_MODEL,
                            contents=[prompt, types.Part.from_bytes(data=page_img_bytes, mime_type="image/png")],
                            config=types.GenerateContentConfig(response_mime_type="application/json")
                        )
                        break
                    except Exception as e:
                        print(f"    Attempt {attempt+1} failed: {e}")
                        if attempt < 2:
                            time.sleep(1)

                if not response:
                    continue

                result = json.loads(response.text)
                
                if result.get('found') and result.get('box_2d'):
                    confidence = result.get('confidence', 'medium')
                    matched = result.get('matched_label', '')
                    print(f"  🎯 Product found on page {page_num+1} (confidence: {confidence}, matched: '{matched}')")
                    
                    ymin, xmin, ymax, xmax = result['box_2d']
                    width, height = pil_image.size
                    
                    # Convert 0-1000 scale to pixel coordinates
                    left = (xmin / 1000) * width
                    top = (ymin / 1000) * height
                    right = (xmax / 1000) * width
                    bottom = (ymax / 1000) * height
                    
                    # Smart padding (5% of crop dimensions)
                    crop_w = right - left
                    crop_h = bottom - top
                    pad_x = crop_w * 0.05
                    pad_y = crop_h * 0.05
                    left = max(0, left - pad_x)
                    top = max(0, top - pad_y)
                    right = min(width, right + pad_x)
                    bottom = min(height, bottom + pad_y)
                    
                    # Validate crop dimensions
                    final_w = right - left
                    final_h = bottom - top
                    
                    if final_w < 80 or final_h < 80:
                        print(f"    ⚠ Crop too small ({final_w:.0f}x{final_h:.0f}), skipping")
                        continue
                    
                    # Don't accept a crop that's essentially the entire page
                    page_area = width * height
                    crop_area = final_w * final_h
                    if crop_area > page_area * 0.85:
                        print(f"    ⚠ Crop covers {crop_area/page_area*100:.0f}% of page — too large")
                        continue
                    
                    crop = pil_image.crop((left, top, right, bottom))
                    if crop.mode != 'RGB':
                        crop = crop.convert('RGB')
                    
                    # Validate the crop isn't mostly blank/white
                    if _is_mostly_solid(crop):
                        print(f"    ⚠ Crop is mostly solid/blank, skipping")
                        continue
                    
                    # Clean up border lines and text remnants
                    crop = _clean_product_image(crop)
                    
                    # Save high-quality crop
                    safe_name = secure_filename(target_model)
                    filename = f"visual_{safe_name}_{int(time.time())}.jpg"
                    save_path = os.path.join(upload_folder, filename)
                    crop.save(save_path, quality=95)
                    
                    doc.close()
                    return f"uploads/{filename}"

            except Exception as e:
                print(f"  ⚠ Error on page {page_num}: {e}")
                continue
        
        doc.close()
        return None
        
    except Exception as e:
        print(f"  ⚠ Screenshot scan error: {e}")
        return None


def _ai_pick_best_from_candidates(candidates, target_model, upload_folder, available_indices=None):
    """Use AI to select the image that matches a specific product from embedded PDF candidates.
    Now includes page text context for much better matching accuracy."""
    global _used_images
    
    try:
        # Build page text context summary
        page_texts = set()
        for c in candidates:
            pt = c.get('page_text', '')
            if pt:
                # Include a snippet of the page text for context
                page_texts.add(f"Page {c['page']+1}: {pt[:300]}")
        
        context_str = "\n".join(page_texts) if page_texts else "No text context available."
        
        prompt = get_prompt('pdf_embedded_image_selection').format(
            target_model=target_model,
            context_str=context_str,
            candidate_count=len(candidates)
        )
        
        content = [prompt]
        for i, candidate in enumerate(candidates):
            # Determine mime type
            ext = candidate.get('ext', 'png')
            mime_map = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg'}
            mime = mime_map.get(ext, 'image/png')
            
            content.append(f"IMAGE {i+1} ({candidate['width']}x{candidate['height']}, page {candidate['page']+1}):")
            content.append(types.Part.from_bytes(data=candidate['bytes'], mime_type=mime))

        response = _get_client().models.generate_content(
            model=_MODEL,
            contents=content,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        result = json.loads(response.text)
        best = result.get("best_index")
        
        if best == "none" or best is None:
            print("  🚫 AI found no suitable product image among embedded candidates")
            return None
        
        idx = int(best) - 1  # 1-based to 0-based
        if 0 <= idx < len(candidates):
            # Mark as used so other products don't reuse it
            if available_indices and idx < len(available_indices):
                _used_images.add(available_indices[idx])
            return _save_candidate_image(candidates[idx], target_model, upload_folder)
        
    except Exception as e:
        print(f"  ⚠ AI selection from embedded images failed: {e}")
    
    return None


def _save_candidate_image(candidate, target_model, upload_folder):
    """Save an image candidate to disk."""
    try:
        pil_img = Image.open(io.BytesIO(candidate['bytes']))
        if pil_img.mode != 'RGB':
            pil_img = pil_img.convert('RGB')
        
        # Clean up border lines and text remnants
        pil_img = _clean_product_image(pil_img)
        
        safe_name = secure_filename(target_model)
        filename = f"visual_{safe_name}_{int(time.time())}.jpg"
        save_path = os.path.join(upload_folder, filename)
        pil_img.save(save_path, quality=95)
        
        print(f"  💾 Saved: {filename} ({candidate['width']}x{candidate['height']})")
        return f"uploads/{filename}"
    except Exception as e:
        print(f"  ⚠ Failed to save candidate image: {e}")
        return None


def _clean_product_image(pil_img):
    """
    Remove border lines, table rules, and text remnants from extracted PDF images.
    Uses pure PIL (zero API cost, no numpy). Only touches edges — never modifies the product.
    """
    try:
        if pil_img.mode != 'RGB':
            pil_img = pil_img.convert('RGB')
        
        w, h = pil_img.size
        
        if h < 60 or w < 60:
            return pil_img  # Too small to safely clean
        
        # Work on a copy
        img = pil_img.copy()
        draw = ImageDraw.Draw(img)
        dark_threshold = 100
        line_ratio = 0.40
        
        # --- Step 1: Remove dark horizontal lines near top/bottom edges ---
        edge_zone = max(8, int(h * 0.15))
        
        for y in list(range(edge_zone)) + list(range(h - edge_zone, h)):
            dark_count = 0
            for x in range(0, w, max(1, w // 50)):  # Sample every ~50th pixel for speed
                r, g, b = img.getpixel((x, y))
                if (r + g + b) / 3 < dark_threshold:
                    dark_count += 1
            sample_count = max(1, w // max(1, w // 50))
            if dark_count / sample_count > line_ratio:
                draw.line([(0, y), (w - 1, y)], fill=(255, 255, 255), width=1)
        
        # --- Step 2: Remove dark vertical lines near left/right edges ---
        edge_zone_x = max(8, int(w * 0.15))
        
        for x in list(range(edge_zone_x)) + list(range(w - edge_zone_x, w)):
            dark_count = 0
            for y in range(0, h, max(1, h // 50)):
                r, g, b = img.getpixel((x, y))
                if (r + g + b) / 3 < dark_threshold:
                    dark_count += 1
            sample_count = max(1, h // max(1, h // 50))
            if dark_count / sample_count > line_ratio:
                draw.line([(x, 0), (x, h - 1)], fill=(255, 255, 255), width=1)
        
        # --- Step 3: Clean narrow edge strips (text remnants) ---
        thin_zone = max(4, int(h * 0.05))
        thin_zone_x = max(4, int(w * 0.05))
        
        # Top/bottom thin strips
        for y in list(range(thin_zone)) + list(range(h - thin_zone, h)):
            bright_count = 0
            total_samples = 0
            for x in range(0, w, max(1, w // 40)):
                r, g, b = img.getpixel((x, y))
                total_samples += 1
                if (r + g + b) / 3 > 200:
                    bright_count += 1
            if total_samples > 0 and bright_count / total_samples > 0.5:
                # Mostly white row — replace dark pixels with white
                for x in range(w):
                    r, g, b = img.getpixel((x, y))
                    if (r + g + b) / 3 < 180:
                        img.putpixel((x, y), (255, 255, 255))
        
        # Left/right thin strips
        for x in list(range(thin_zone_x)) + list(range(w - thin_zone_x, w)):
            bright_count = 0
            total_samples = 0
            for y in range(0, h, max(1, h // 40)):
                r, g, b = img.getpixel((x, y))
                total_samples += 1
                if (r + g + b) / 3 > 200:
                    bright_count += 1
            if total_samples > 0 and bright_count / total_samples > 0.5:
                for y in range(h):
                    r, g, b = img.getpixel((x, y))
                    if (r + g + b) / 3 < 180:
                        img.putpixel((x, y), (255, 255, 255))
        
        # --- Step 4: Auto-crop to content bounding box ---
        # Create a white background reference, diff to find content
        bg = Image.new('RGB', img.size, (255, 255, 255))
        diff = ImageChops.difference(img, bg)
        gray_diff = diff.convert('L')
        # Threshold: anything > 15 brightness difference is content
        bbox = gray_diff.point(lambda p: 255 if p > 15 else 0).getbbox()
        
        if bbox:
            # Add small padding
            pad = max(5, int(min(h, w) * 0.03))
            crop_box = (
                max(0, bbox[0] - pad),
                max(0, bbox[1] - pad),
                min(w, bbox[2] + pad),
                min(h, bbox[3] + pad)
            )
            crop_area = (crop_box[2] - crop_box[0]) * (crop_box[3] - crop_box[1])
            total_area = w * h
            
            if crop_area / total_area < 0.95:
                img = img.crop(crop_box)
                cw = crop_box[2] - crop_box[0]
                ch = crop_box[3] - crop_box[1]
                print(f"    ✂️ Cleaned: removed borders, cropped to {cw}x{ch}")
            else:
                print(f"    ✂️ Cleaned: removed border lines")
        else:
            print(f"    ✂️ Cleaned: removed border lines")
        
        return img
        
    except Exception as e:
        print(f"    ⚠ Image cleanup failed (keeping original): {e}")
        return pil_img


def _is_mostly_solid(pil_image, threshold=15):
    """
    Check if an image is mostly a single solid color 
    (blank backgrounds, separators, etc.)
    """
    try:
        # Resize to small size for fast analysis
        small = pil_image.resize((50, 50))
        if small.mode != 'RGB':
            small = small.convert('RGB')
        
        stat = ImageStat.Stat(small)
        # Standard deviation across all channels — low = mostly solid
        avg_stddev = sum(stat.stddev) / len(stat.stddev)
        
        return avg_stddev < threshold
    except:
        return False
