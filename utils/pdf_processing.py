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
# PyMuPDF's bundled type stubs don't expose `fitz.open` (it's set up via
# C-extension binding at import time), so Pyrefly flags every call site.
# Pulling it through an explicit alias once silences the warning everywhere.
fitz_open = fitz.open  # type: ignore[attr-defined]
from PIL import Image, ImageFilter, ImageStat, ImageDraw, ImageChops
from werkzeug.utils import secure_filename
from google import genai
from google.genai import types
from .prompt_manager import get_prompt

_MODEL = 'gemini-2.5-flash'
_NANO_BANANA_MODEL = 'gemini-2.5-flash-image'   # Image-out model (a.k.a. nano-banana)

# Phase 3.0: thread-local — see ai_generation.py. Branch A's
# extract_and_allocate_embedded runs concurrently across cluster workers.
import threading as _threading
_thread_local = _threading.local()


def _get_client():
    c = getattr(_thread_local, 'client', None)
    if c is None:
        c = genai.Client(api_key=os.getenv('GOOGLE_API_KEY'))
        _thread_local.client = c
    return c


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


def extract_specific_image(pdf_path, target_model, upload_folder, skip_verify: bool = False,
                           all_matches: bool = False, prefer_embedded: bool = False):
    """
    Extracts a product image from a PDF using a multi-pass approach:

    Pass 1 (default): AI-powered screenshot scanning — renders full pages so
            AI can see both images AND adjacent text labels (model numbers).
    Pass 2 (default): Extract embedded images with page-text context.

    Args:
        skip_verify: When True, the secondary `ai_verify_crop_matches` gate is
            skipped (used by the single-item wizard, where the user picks the
            final image manually so a second AI check is redundant).
        all_matches: When True, return list[str] of every match found on the
            page (multi-view rows: open + closed wardrobe, etc.). When False
            (default), return Optional[str] for backward compatibility.
        prefer_embedded: When True, run embedded-image pass FIRST. PDFs that
            embed product photos as discrete JPEG/PNG streams give us
            pixel-perfect originals. The single-item wizard sets this True;
            Auto/Multiple modes leave it False to preserve existing behavior.

    Returns:
        - When all_matches=False: relative path str, or None if not found.
        - When all_matches=True:  list[str] of relative paths (possibly empty).
    """
    empty_return = [] if all_matches else None
    if not pdf_path or not os.path.exists(pdf_path):
        return empty_return

    print(f"🔍 PDF Image Extraction starting for: '{target_model}' (all_matches={all_matches}, prefer_embedded={prefer_embedded})")

    pass_order = ('embedded', 'screenshot') if prefer_embedded else ('screenshot', 'embedded')

    collected: list[str] = []
    for name in pass_order:
        # Dispatch by name so Pyrefly sees each call against a single
        # concrete function (the two extractors have different signatures
        # — only the screenshot pass takes `skip_verify`).
        if name == 'screenshot':
            result = _extract_via_screenshot(
                pdf_path, target_model, upload_folder,
                skip_verify=skip_verify, all_matches=all_matches,
            )
        else:
            result = _extract_embedded_images(
                pdf_path, target_model, upload_folder, all_matches=all_matches,
            )

        if result:
            if all_matches:
                # result is a list — extend and decide whether to keep going.
                collected.extend(result)
                print(f"✅ Pass '{name}' SUCCESS: collected {len(result)} match(es) for '{target_model}'")
                # First pass returning anything is enough — second pass would
                # likely duplicate the same product photo from another angle.
                break
            print(f"✅ Pass '{name}' SUCCESS: Found product image for '{target_model}'")
            return result
        print(f"--- Pass '{name}' found nothing, trying next pass ---")

    if all_matches:
        if not collected:
            print(f"🚫 No product image found in PDF for '{target_model}'")
        return collected

    print(f"🚫 No product image found in PDF for '{target_model}'")
    return None


def extract_product_from_image(image_path, target_model, upload_folder,
                               skip_verify: bool = False,
                               all_matches: bool = False):
    """Phase 2.4 — wizard helper for standalone uploaded images.

    Mirrors the PDF screenshot pipeline: ask Gemini for tight bounding box(es)
    around the requested product, crop, clean borders, save. Handles three
    common upload shapes:
      • Multi-product layout (proforma scan with text + several products) —
        Gemini returns one bbox per matching photo (open + closed views,
        etc.), we crop each.
      • Single-product proforma (text-heavy with one product photo) — same
        as above, just with one candidate region.
      • Clean product photo (just the product, no surrounding text) —
        Gemini returns a near-full-image bbox at high confidence, we accept
        the whole image.

    Args:
        all_matches: When True, return list[str] of every match. When False
            (default), return Optional[str] for backward compatibility.

    Returns:
        - When all_matches=False: relative path str, or None when nothing was
          isolated (caller should fall through to the web pipeline).
        - When all_matches=True:  list[str] of relative paths (possibly empty).
    """
    empty_return = [] if all_matches else None
    if not image_path or not os.path.exists(image_path):
        return empty_return

    print(f"🔍 Image-source extraction starting for: '{target_model}' (all_matches={all_matches})")

    try:
        with open(image_path, 'rb') as f:
            raw_bytes = f.read()
        pil_image = Image.open(io.BytesIO(raw_bytes))
        if pil_image.mode != 'RGB':
            pil_image = pil_image.convert('RGB')

        # Re-encode as PNG for Gemini consistency.
        buf = io.BytesIO()
        pil_image.save(buf, 'PNG')
        png_bytes = buf.getvalue()

        tpl = get_prompt('pdf_screenshot_scan')
        if not tpl:
            print("    ⚠ Prompt 'pdf_screenshot_scan' missing from DB and defaults")
            return empty_return
        prompt = tpl.format(target_model=target_model)
        response = None
        for attempt in range(3):
            try:
                response = _get_client().models.generate_content(
                    model=_MODEL,
                    contents=[prompt, types.Part.from_bytes(data=png_bytes, mime_type="image/png")],
                    config=types.GenerateContentConfig(response_mime_type="application/json"),
                )
                break
            except Exception as e:
                print(f"    Attempt {attempt+1} failed: {e}")
                if attempt < 2:
                    time.sleep(1)
        if not response:
            return empty_return

        result = json.loads(response.text or "{}")
        if not result.get('found'):
            print(f"  🚫 No product image found inside uploaded image for '{target_model}'")
            return empty_return

        # New schema: list of products. Old schema: single box_2d. Support both.
        product_entries = _normalize_products_field(result)
        if not product_entries:
            print(f"  🚫 Response had found=true but no usable bbox(es)")
            return empty_return

        width, height = pil_image.size
        saved_paths: list[str] = []

        for idx, entry in enumerate(product_entries):
            box = entry.get('box_2d')
            if not box or len(box) != 4:
                continue
            confidence = (entry.get('confidence') or '').lower()
            ymin, xmin, ymax, xmax = box
            left   = max(0, (xmin / 1000) * width)
            top    = max(0, (ymin / 1000) * height)
            right  = min(width,  (xmax / 1000) * width)
            bottom = min(height, (ymax / 1000) * height)

            crop_w = right - left
            crop_h = bottom - top
            if crop_w < 80 or crop_h < 80:
                print(f"    ⚠ Match #{idx+1}: crop too small ({crop_w:.0f}x{crop_h:.0f})")
                continue

            page_area = width * height
            crop_area = crop_w * crop_h
            full_image = (crop_area / page_area) >= 0.95

            if full_image:
                if confidence not in ('high', 'medium'):
                    print(f"    ⚠ Match #{idx+1}: full-image bbox at low confidence — skipping")
                    continue
                crop = pil_image
                print(f"    ℹ Match #{idx+1}: full-image accepted (confidence={confidence})")
            else:
                pad_x = crop_w * 0.05
                pad_y = crop_h * 0.05
                left   = max(0, left - pad_x)
                top    = max(0, top - pad_y)
                right  = min(width,  right + pad_x)
                bottom = min(height, bottom + pad_y)
                # Snap to nearest table-cell border / strong straight line so
                # we don't keep table rules in the saved image.
                left, top, right, bottom = _snap_to_cell_border(
                    pil_image, (left, top, right, bottom)
                )
                crop = pil_image.crop((left, top, right, bottom))
                if crop.mode != 'RGB':
                    crop = crop.convert('RGB')

            if _is_mostly_solid(crop):
                print(f"    ⚠ Match #{idx+1}: crop is mostly solid/blank")
                continue

            crop = _clean_product_image(crop)

            if not skip_verify:
                try:
                    from .image_processing import ai_verify_crop_matches
                    verify_buf = io.BytesIO()
                    crop.save(verify_buf, "JPEG", quality=92)
                    if not ai_verify_crop_matches(verify_buf.getvalue(), target_model):
                        print(f"    ⚠ Match #{idx+1}: rejected by AI verification")
                        continue
                except Exception as e:
                    print(f"    ⚠ Verification crashed (allowing crop): {e}")

            safe_name = secure_filename(target_model)
            filename = f"visual_{safe_name}_{int(time.time())}_{idx}.jpg"
            save_path = os.path.join(upload_folder, filename)
            crop.save(save_path, quality=95)
            print(f"  ✅ Saved cropped product image → {filename}")
            saved_paths.append(f"uploads/{filename}")

            if not all_matches:
                # Legacy single-result behavior — return immediately.
                return f"uploads/{filename}"

        if all_matches:
            return saved_paths
        return None

    except Exception as e:
        print(f"  ⚠ Image-source extraction failed: {e}")
        return empty_return


def _extract_embedded_images(pdf_path, target_model, upload_folder, all_matches: bool = False):
    """
    Embedded-image pass: Extract actual embedded images from the PDF.
    Uses cache to avoid re-scanning the same PDF in bulk uploads.
    Includes page text context for better AI matching.

    `all_matches`: when True, return list[str] of every embedded image whose
                   page text contains the target model. When False, fall back
                   to the legacy single-best AI selection (used by Auto/Multiple).
    """
    global _embedded_cache, _used_images
    empty_return = [] if all_matches else None

    try:
        # Check cache first — avoid re-scanning for every product
        if pdf_path in _embedded_cache:
            candidate_images = _embedded_cache[pdf_path]
            print(f"  📦 Using cached {len(candidate_images)} embedded images (skipping PDF scan)")
        else:
            # First time — scan the PDF and cache results
            doc = fitz_open(pdf_path)
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
            return empty_return

        # Filter out already-used images (only relevant in non-all_matches /
        # bulk-import mode — wizard always sees a fresh cache).
        available = [(i, c) for i, c in enumerate(candidate_images) if i not in _used_images]

        if not available:
            print("  ⚠ All embedded images already assigned to other products")
            return empty_return

        # ── all_matches branch: text-anchored multi-return for the wizard ──
        if all_matches:
            saved_paths: list[str] = []
            model_parts = [p for p in target_model.split() if len(p) >= 4]
            for _, candidate in available:
                page_text = (candidate.get('page_text') or '').upper()
                # Anchor on any sufficiently-distinctive token from the model
                # name appearing on the same page as the embedded image.
                if not any(part.upper() in page_text for part in model_parts):
                    continue
                if candidate['width'] < 150 or candidate['height'] < 150:
                    continue
                saved = _save_candidate_image(candidate, target_model, upload_folder)
                if saved:
                    saved_paths.append(saved)
            return saved_paths

        # ── Legacy single-best branch (Auto/Multiple modes) ──
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
        return empty_return


def _extract_via_screenshot(pdf_path, target_model, upload_folder, skip_verify: bool = False,
                            all_matches: bool = False):
    """
    Screenshot pass: High-resolution page screenshots + AI bounding box detection.
    Renders each page as a 3× pixmap so the AI sees both the product image
    AND the surrounding text (model labels) for accurate matching.

    `skip_verify`: when True, the post-crop AI verification call is bypassed.
    `all_matches`: when True, return list[str] of every match across all pages
                   (including multiple bboxes returned per page). When False,
                   return the first hit as a single str (legacy behavior).
    """
    empty_return = [] if all_matches else None
    saved_paths: list[str] = []
    try:
        doc = fitz_open(pdf_path)
        print(f"  📸 Screenshot scan: {min(15, len(doc))} pages at 3x resolution")

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

                tpl = get_prompt('pdf_screenshot_scan')
                if not tpl:
                    print("    ⚠ Prompt 'pdf_screenshot_scan' missing from DB and defaults")
                    continue
                prompt = tpl.format(target_model=target_model)

                response = None
                for attempt in range(3):
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

                result = json.loads(response.text or "{}")
                if not result.get('found'):
                    continue

                product_entries = _normalize_products_field(result)
                if not product_entries:
                    continue

                width, height = pil_image.size

                for idx, entry in enumerate(product_entries):
                    box = entry.get('box_2d')
                    if not box or len(box) != 4:
                        continue
                    confidence = entry.get('confidence', 'medium')
                    matched = entry.get('matched_label', '')
                    print(f"  🎯 Match #{idx+1} on page {page_num+1} (confidence: {confidence}, matched: '{matched}')")

                    ymin, xmin, ymax, xmax = box
                    left = (xmin / 1000) * width
                    top = (ymin / 1000) * height
                    right = (xmax / 1000) * width
                    bottom = (ymax / 1000) * height

                    crop_w = right - left
                    crop_h = bottom - top
                    pad_x = crop_w * 0.05
                    pad_y = crop_h * 0.05
                    left = max(0, left - pad_x)
                    top = max(0, top - pad_y)
                    right = min(width, right + pad_x)
                    bottom = min(height, bottom + pad_y)

                    # Snap to nearest table-cell border so we don't keep
                    # table rules in the saved image. Deterministic.
                    left, top, right, bottom = _snap_to_cell_border(
                        pil_image, (left, top, right, bottom)
                    )

                    final_w = right - left
                    final_h = bottom - top

                    if final_w < 80 or final_h < 80:
                        print(f"    ⚠ Crop too small ({final_w:.0f}x{final_h:.0f}), skipping")
                        continue

                    page_area = width * height
                    crop_area = final_w * final_h
                    if crop_area > page_area * 0.85:
                        print(f"    ⚠ Crop covers {crop_area/page_area*100:.0f}% of page — too large")
                        continue

                    crop = pil_image.crop((left, top, right, bottom))
                    if crop.mode != 'RGB':
                        crop = crop.convert('RGB')

                    if _is_mostly_solid(crop):
                        print(f"    ⚠ Crop is mostly solid/blank, skipping")
                        continue

                    crop = _clean_product_image(crop)

                    if not skip_verify:
                        try:
                            from .image_processing import ai_verify_crop_matches
                            verify_buf = io.BytesIO()
                            crop.save(verify_buf, "JPEG", quality=92)
                            if not ai_verify_crop_matches(verify_buf.getvalue(), target_model):
                                print("    ⚠ Crop rejected by AI verification, trying next match")
                                continue
                        except Exception as e:
                            # Fail-open — never block on a verification error.
                            print(f"    ⚠ Verification step crashed (allowing crop): {e}")

                    safe_name = secure_filename(target_model)
                    filename = f"visual_{safe_name}_{int(time.time())}_{page_num}_{idx}.jpg"
                    save_path = os.path.join(upload_folder, filename)
                    crop.save(save_path, quality=95)
                    saved_paths.append(f"uploads/{filename}")

                    if not all_matches:
                        doc.close()
                        return f"uploads/{filename}"

            except Exception as e:
                print(f"  ⚠ Error on page {page_num}: {e}")
                continue

        doc.close()

        if all_matches:
            return saved_paths
        return None

    except Exception as e:
        print(f"  ⚠ Screenshot scan error: {e}")
        return empty_return


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
        
        tpl = get_prompt('pdf_embedded_image_selection')
        if not tpl:
            print("  ⚠ Prompt 'pdf_embedded_image_selection' missing from DB and defaults")
            return None
        prompt = tpl.format(
            target_model=target_model,
            context_str=context_str,
            candidate_count=len(candidates)
        )

        # Heterogeneous list of str labels + Part image data. The SDK's
        # `contents` accepts this (`list[Part | str | ...]`) but Pyrefly
        # narrows the literal type after the first append, so we widen
        # via `list[Any]` to match the signature.
        from typing import Any
        content: list[Any] = [prompt]
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
        result = json.loads(response.text or "{}")
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


# ───────────────────────────────────────────────────────────────────────────
#  extract_and_allocate_embedded()
#
#  Purpose: replace the per-variant "extract & save all images, repeat per
#  target" loop in the bulk pipeline. We now do ONE embedded-image extraction
#  per PDF and then ask Gemini Vision to allocate each photo to a specific
#  variant in a single call.
#
#  Output shape:
#    {
#      'images':       [{'index': int, 'path': str, 'page': int, 'w': int, 'h': int}],
#      'allocations':  {variant_key: [path, path, ...]},
#      'unallocated':  [path, ...],
#    }
#  Empty `images` means the PDF had no usable embedded photos — caller falls
#  through to bbox/screenshot extraction.
# ───────────────────────────────────────────────────────────────────────────


def _save_embedded_once(candidate: dict, batch_tag: str, idx: int,
                         upload_folder: str) -> str | None:
    """Mirror of `_save_candidate_image` but using a generic filename so
    we don't write the same bytes once per variant target."""
    try:
        pil_img = Image.open(io.BytesIO(candidate['bytes']))
        if pil_img.mode != 'RGB':
            pil_img = pil_img.convert('RGB')
        pil_img = _clean_product_image(pil_img)
        ts = int(time.time())
        filename = f"bulkembed_{batch_tag}_{idx:02d}_{ts}.jpg"
        save_path = os.path.join(upload_folder, filename)
        pil_img.save(save_path, quality=95)
        return f"uploads/{filename}"
    except Exception as e:
        print(f"  ⚠ embedded save #{idx} failed: {e}")
        return None


def _render_page_screenshot_png(pdf_path: str, page_num: int,
                                 zoom: float = 2.0) -> bytes | None:
    """Render one PDF page as a PNG byte string. Used to give Gemini the
    layout context so it can match photos to row labels."""
    try:
        doc = fitz_open(pdf_path)
        try:
            page_num = max(0, min(page_num, len(doc) - 1))
            page = doc[page_num]
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)
            return pix.tobytes("png")
        finally:
            doc.close()
    except Exception as e:
        print(f"  ⚠ page screenshot for page {page_num} failed: {e}")
        return None


def _ai_allocate_photos_to_variants(
    page_pngs: list[bytes],
    image_bytes_list: list[bytes],
    variants_meta: list[dict],
) -> dict[str, list[int]]:
    """Single Gemini call. Sends each page screenshot + every cropped photo
    (in order, so the model can refer to them by index) + the variant list.
    Asks for `{variant_key: [photo_index, ...]}` in JSON.

    Returns an empty dict on any error so the caller can fall back to a
    deterministic "all images go to the cluster default" assignment.
    """
    if not image_bytes_list or not variants_meta:
        return {}

    # Build the prompt: list page screenshots, then photos (with indices),
    # then the variants.
    n_pages = len(page_pngs)
    n_photos = len(image_bytes_list)
    var_lines = []
    for v in variants_meta:
        key = (v.get('key') or '').strip()
        label = (v.get('label') or key or '').strip()
        if not key:
            continue
        var_lines.append(f"  - key={key!r}  label={label!r}")
    if not var_lines:
        return {}

    prompt = (
        "You are looking at a product proforma.\n\n"
        f"The first {n_pages} image(s) above are screenshots of the proforma page(s). "
        f"The remaining {n_photos} image(s) are individual product photos already "
        "cropped from those pages.\n\n"
        f"Photo indices: 0..{n_photos - 1} (in the order they're attached, after the page screenshots).\n\n"
        "The proforma describes these variants of the same product. The variants "
        "differ only by COLOUR / FINISH / MATERIAL:\n"
        + "\n".join(var_lines) + "\n\n"
        "TASK: For each variant key, return the photo indices that belong to it. "
        "A photo belongs to a variant when its visible colour/finish matches the "
        "variant's label. A variant may have multiple photos (e.g. closed view + "
        "open view).\n\n"
        "Rules:\n"
        "  • Use the page screenshot(s) to match photos to row labels printed on the proforma.\n"
        "  • A photo can be assigned to AT MOST ONE variant.\n"
        "  • If a photo doesn't clearly match any variant, leave it out of every list.\n"
        "  • Keys MUST be copied verbatim from the variant list above.\n\n"
        "Return strict JSON ONLY — no prose, no markdown:\n"
        "{\n"
        '  "allocations": {\n'
        '    "<variant_key>": [<photo_index>, <photo_index>, ...]\n'
        "  }\n"
        "}"
    )

    parts: list = []
    for png in page_pngs:
        parts.append(types.Part.from_bytes(data=png, mime_type="image/png"))
    for img_bytes in image_bytes_list:
        parts.append(types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))
    parts.append(prompt)

    try:
        response = _get_client().models.generate_content(
            model=_MODEL,
            contents=parts,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        parsed = json.loads(response.text or "{}")
    except Exception as e:
        print(f"  ⚠ AI allocation call failed: {e}")
        return {}

    allocations_raw = parsed.get('allocations') or {}
    if not isinstance(allocations_raw, dict):
        return {}

    # Validate + coerce: only keep keys we asked about, only int indices in range.
    valid_keys = {v['key'] for v in variants_meta if v.get('key')}
    used: set[int] = set()
    out: dict[str, list[int]] = {}
    for k, v in allocations_raw.items():
        if k not in valid_keys or not isinstance(v, list):
            continue
        idxs: list[int] = []
        for raw_idx in v:
            try:
                idx = int(raw_idx)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < n_photos and idx not in used:
                idxs.append(idx)
                used.add(idx)
        if idxs:
            out[k] = idxs
    return out


def extract_and_allocate_embedded(
    pdf_path: str,
    variants_meta: list[dict],
    upload_folder: str,
    page_filter: list[int] | None = None,
    cluster_label: str = '',
) -> dict:
    """Extract every embedded image from `pdf_path` ONCE and ask Gemini to
    allocate each photo to a specific variant in a single AI call.

    `variants_meta`: list[{'key': str, 'label': str}]. Order matters — the
        first entry is treated as the cluster's primary (used when only one
        target exists, i.e. singleton cluster). `key` must be unique per
        cluster and is what gets attached to each candidate as `variant_sku`.

    `page_filter`: optional list of zero-based page indexes (from triage's
        per-cluster source_pages). When provided, only embedded images on
        those pages are considered.

    `cluster_label`: short identifier used in filenames + logs.

    Returns: {
        'images':       [{'index': int, 'path': str, 'page': int, 'w': int, 'h': int}],
        'allocations':  {variant_key: [path, path, ...]},
        'unallocated':  [path, ...],
    }
    """
    empty = {'images': [], 'allocations': {}, 'unallocated': []}
    if not pdf_path or not os.path.exists(pdf_path):
        return empty
    if os.path.splitext(pdf_path)[1].lower() != '.pdf':
        return empty
    if not variants_meta:
        return empty

    # ── 1. Pull embedded image bytes (reuse the existing cache) ────────────
    global _embedded_cache
    if pdf_path in _embedded_cache:
        candidates = _embedded_cache[pdf_path]
        print(f"  📦 Using cached {len(candidates)} embedded images")
    else:
        candidates = []
        try:
            doc = fitz_open(pdf_path)
            try:
                for page_num in range(min(30, len(doc))):
                    page = doc[page_num]
                    page_text = page.get_text("text")
                    for img_index, img_info in enumerate(page.get_images(full=True)):
                        xref = img_info[0]
                        try:
                            base = doc.extract_image(xref)
                            if not base:
                                continue
                            image_bytes = base["image"]
                            pil_img = Image.open(io.BytesIO(image_bytes))
                            w, h = pil_img.size
                            if w < 100 or h < 100:
                                continue
                            if _is_mostly_solid(pil_img):
                                continue
                            candidates.append({
                                'bytes':     image_bytes,
                                'width':     w,
                                'height':    h,
                                'page':      page_num,
                                'page_text': page_text,
                                'ext':       base.get('ext', 'png'),
                            })
                            if len(candidates) >= 50:
                                break
                        except Exception as e:
                            print(f"  ⚠ embedded read xref={xref}: {e}")
                            continue
                    if len(candidates) >= 50:
                        break
            finally:
                doc.close()
        except Exception as e:
            print(f"  ⚠ embedded scan failed: {e}")
            return empty
        _embedded_cache[pdf_path] = candidates
        print(f"  📦 Scanned PDF: {len(candidates)} embedded photos found")

    if not candidates:
        return empty

    # ── 2. Restrict by page_filter ─────────────────────────────────────────
    if page_filter:
        page_set = set(page_filter)
        filtered = [c for c in candidates if c['page'] in page_set]
        if filtered:
            candidates = filtered

    if not candidates:
        return empty

    # ── 3. Save each image ONCE under a generic name ───────────────────────
    safe_tag = (secure_filename(cluster_label) or 'cluster')[:24]
    images_out: list[dict] = []
    for idx, candidate in enumerate(candidates):
        rel = _save_embedded_once(candidate, safe_tag, idx, upload_folder)
        if not rel:
            continue
        images_out.append({
            'index':  idx,
            'path':   rel,
            'page':   candidate['page'],
            'w':      candidate['width'],
            'h':      candidate['height'],
        })
    if not images_out:
        return empty

    # ── 4. Allocation ──────────────────────────────────────────────────────
    # Singleton cluster (one variant key) → all images go to that key.
    keys = [v.get('key') for v in variants_meta if v.get('key')]
    if len(keys) <= 1:
        only_key = keys[0] if keys else variants_meta[0].get('key', '__primary__')
        return {
            'images':       images_out,
            'allocations':  {only_key: [im['path'] for im in images_out]},
            'unallocated':  [],
        }

    # Variant cluster → AI allocation.
    pages_used = sorted({im['page'] for im in images_out})
    page_pngs = [p for p in (_render_page_screenshot_png(pdf_path, p) for p in pages_used) if p]
    image_bytes_list = [candidates[im['index']]['bytes'] for im in images_out]

    print(f"  🧠 AI allocation: mapping {len(images_out)} photo(s) → "
          f"{len(keys)} variant(s)")
    ai_map = _ai_allocate_photos_to_variants(
        page_pngs, image_bytes_list, variants_meta,
    )

    # Build path-allocation dict + collect unallocated.
    by_local_idx: dict[int, str] = {i: im['path'] for i, im in enumerate(images_out)}
    used_local: set[int] = set()
    allocations: dict[str, list[str]] = {}
    for k, idx_list in ai_map.items():
        paths: list[str] = []
        for li in idx_list:
            p = by_local_idx.get(li)
            if p and li not in used_local:
                paths.append(p); used_local.add(li)
        if paths:
            allocations[k] = paths
    unallocated = [by_local_idx[i] for i in range(len(images_out)) if i not in used_local]

    return {
        'images':       images_out,
        'allocations':  allocations,
        'unallocated':  unallocated,
    }


def _normalize_products_field(result: dict) -> list[dict]:
    """Bridge old/new schemas of `pdf_screenshot_scan`.

    Old schema returned a single bbox: `{found:true, box_2d:[...], confidence, matched_label}`.
    New schema returns a list:        `{found:true, products:[{box_2d, confidence, matched_label}, ...]}`.
    Either is accepted so prompts that haven't been re-saved by an admin still work.
    """
    if not result:
        return []
    products = result.get('products')
    if isinstance(products, list) and products:
        return [p for p in products if isinstance(p, dict) and p.get('box_2d')]
    # Legacy single-bbox path.
    if result.get('box_2d'):
        return [{
            'box_2d':         result['box_2d'],
            'confidence':     result.get('confidence', 'medium'),
            'matched_label':  result.get('matched_label', ''),
        }]
    return []


def _snap_to_cell_border(pil_image: Image.Image, bbox: tuple) -> tuple:
    """Snap each edge of `bbox` to the nearest strong horizontal/vertical line
    in a small strip around it. Used to align crops to table-cell borders so
    we don't keep table rules in the saved photo.

    bbox is (left, top, right, bottom) in pixel coords. Returns the same shape.
    Pure PIL — no OpenCV dependency. Cheap (samples at most a few hundred px).
    """
    try:
        if pil_image.mode != 'L':
            gray = pil_image.convert('L')
        else:
            gray = pil_image
        w, h = gray.size
        left, top, right, bottom = (int(round(v)) for v in bbox)
        crop_w = max(1, right - left)
        crop_h = max(1, bottom - top)

        # Search window: ~6% of crop dimension, capped, never larger than the
        # available margin to the page edge.
        win_x = max(4, min(40, int(crop_w * 0.06)))
        win_y = max(4, min(40, int(crop_h * 0.06)))

        dark_threshold = 110     # pixel value < threshold → "dark" (a line)
        coverage_min = 0.55       # fraction of the row/col that must be dark

        def _row_darkness(y: int, x_start: int, x_end: int) -> float:
            if y < 0 or y >= h or x_end <= x_start:
                return 0.0
            dark = 0
            samples = 0
            step = max(1, (x_end - x_start) // 80)
            for x in range(x_start, x_end, step):
                samples += 1
                if gray.getpixel((x, y)) < dark_threshold:
                    dark += 1
            return dark / samples if samples else 0.0

        def _col_darkness(x: int, y_start: int, y_end: int) -> float:
            if x < 0 or x >= w or y_end <= y_start:
                return 0.0
            dark = 0
            samples = 0
            step = max(1, (y_end - y_start) // 80)
            for y in range(y_start, y_end, step):
                samples += 1
                if gray.getpixel((x, y)) < dark_threshold:
                    dark += 1
            return dark / samples if samples else 0.0

        # Top edge — search [top - win, top + win] for the strongest dark row,
        # then place the crop just BELOW it (not on it).
        new_top = top
        best_score = 0.0
        for y in range(max(0, top - win_y), min(h, top + win_y + 1)):
            score = _row_darkness(y, left, right)
            if score > best_score and score >= coverage_min:
                best_score = score
                new_top = y + 1

        # Bottom edge — strongest dark row, place crop just ABOVE it.
        new_bottom = bottom
        best_score = 0.0
        for y in range(max(0, bottom - win_y), min(h, bottom + win_y + 1)):
            score = _row_darkness(y, left, right)
            if score > best_score and score >= coverage_min:
                best_score = score
                new_bottom = y

        # Left edge — strongest dark column, place crop just RIGHT of it.
        new_left = left
        best_score = 0.0
        for x in range(max(0, left - win_x), min(w, left + win_x + 1)):
            score = _col_darkness(x, new_top, new_bottom)
            if score > best_score and score >= coverage_min:
                best_score = score
                new_left = x + 1

        # Right edge — strongest dark column, place crop just LEFT of it.
        new_right = right
        best_score = 0.0
        for x in range(max(0, right - win_x), min(w, right + win_x + 1)):
            score = _col_darkness(x, new_top, new_bottom)
            if score > best_score and score >= coverage_min:
                best_score = score
                new_right = x

        # Sanity: never invert or shrink to zero.
        if new_right - new_left < 60 or new_bottom - new_top < 60:
            return bbox
        return (new_left, new_top, new_right, new_bottom)
    except Exception as e:
        print(f"    ⚠ _snap_to_cell_border failed (using original bbox): {e}")
        return bbox


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


# ───────────────────────────────────────────────────────────────────────────
# Nano-banana extractor (Gemini 2.5 Flash Image)
# ───────────────────────────────────────────────────────────────────────────

def _pad_to_aspect_4_3(img: Image.Image, bg: tuple = (255, 255, 255)) -> Image.Image:
    """Pad `img` with white (or `bg`) so its aspect becomes exactly 4:3
    (W:H). Never crops — only adds canvas. Used to guarantee nano-banana
    output matches the PIS review thumbnail frame regardless of what
    aspect the model actually returned.
    """
    target = 4 / 3
    w, h = img.size
    if w <= 0 or h <= 0:
        return img
    cur = w / h
    if abs(cur - target) < 0.01:
        return img  # already close enough — skip a no-op canvas paste
    if cur > target:
        # Source is wider than 4:3 — pad top/bottom.
        new_h = round(w / target)
        canvas = Image.new('RGB', (w, new_h), bg)
        canvas.paste(img, (0, (new_h - h) // 2))
        return canvas
    # Source is taller than 4:3 — pad left/right.
    new_w = round(h * target)
    canvas = Image.new('RGB', (new_w, h), bg)
    canvas.paste(img, ((new_w - w) // 2, 0))
    return canvas


_NANO_BANANA_PROMPT = (
    "You are a product catalog editor processing a supplier proforma.\n\n"
    "The attached image contains a product row for the target product "
    "described below.\n\n"
    "TARGET PRODUCT (THIS is what you must isolate, NOT any other product "
    "on the same page):\n"
    "{product_spec_block}\n\n"
    "TASK: Return the product photo from the proforma — only the product — "
    "with all surrounding elements (text, table borders, model numbers, "
    "prices, descriptions, logos, page background, OTHER PRODUCTS) removed "
    "and replaced with pure white.\n\n"
    "ABSOLUTELY STRICT RULES — NO EXCEPTIONS:\n"
    "1. Match the target by SKU / model number first. The proforma may "
    "   contain multiple products that look visually similar — use the "
    "   printed SKU and color/finish description above to pick the RIGHT "
    "   one. If you cannot find the target product, return nothing.\n"
    "2. Reproduce the product photo VERBATIM. Do NOT alter, smooth, "
    "   restyle, denoise, sharpen, recolor, or 'improve' the product in "
    "   ANY way. Treat it as a copy-paste operation, not a generation.\n"
    "3. Preserve the EXACT colors, materials, textures, proportions, "
    "   shadows, highlights, and lighting from the source. Pixel-for-pixel "
    "   if you can. Color/finish in the spec above must match what's in "
    "   the source — do NOT recolor a walnut wardrobe to oak.\n"
    "4. Do NOT add or invent ANY angles, surfaces, reflections, shadows, "
    "   or details that are not visible in the source photo.\n"
    "5. Do NOT change the product's pose, framing, perspective, or "
    "   orientation. Keep it exactly as it appears in the source.\n"
    "6. Do NOT add a backdrop, floor, gradient, prop, or decorative "
    "   element. Only pure white (#FFFFFF) behind the product.\n"
    "7. If multiple views of the SAME target product appear (open + closed, "
    "   front + side), pick the SINGLE clearest view and reproduce ONLY "
    "   that one — do not collage or merge them.\n\n"
    "OUTPUT FORMAT — MANDATORY:\n"
    "- Aspect ratio: 4:3 LANDSCAPE (width : height).\n"
    "- Product centered horizontally and vertically, with modest white "
    "  margin on all four sides.\n"
    "- Background: pure white (#FFFFFF), uniform, no gradient or shadow.\n"
    "- Output the image only — no text, no labels, no watermarks, no "
    "  borders.\n"
)


def _build_product_spec_block(target_model: str,
                              brand: str | None = None,
                              sku: str | None = None,
                              variant_label: str | None = None,
                              dimensions: str | None = None,
                              color: str | None = None,
                              description: str | None = None) -> str:
    """Compose the labelled spec block the nano-banana prompt embeds. All
    fields are optional; missing ones are simply omitted so the prompt
    stays readable even with sparse metadata. The block is rendered as
    a key:value list because the model attends to structure here much
    better than a prose blob."""
    # Normalise every optional input to a stripped non-None string up front
    # so the rest of the function (and Pyrefly) sees plain `str` values.
    name_line  = (target_model or "").strip()
    brand_s    = (brand or "").strip()
    sku_s      = (sku or "").strip()
    variant_s  = (variant_label or "").strip()
    color_s    = (color or "").strip()
    dims_s     = (dimensions or "").strip()
    desc_s     = (description or "").strip()

    lines: list[str] = []
    if name_line:
        lines.append(f"  Product name: {name_line}")
    if brand_s:
        lines.append(f"  Brand:        {brand_s}")
    if sku_s:
        lines.append(f"  SKU:          {sku_s}")
    if variant_s and variant_s.lower() != name_line.lower():
        lines.append(f"  Variant:      {variant_s}")
    if color_s:
        lines.append(f"  Color/finish: {color_s}")
    if dims_s:
        lines.append(f"  Dimensions:   {dims_s}")
    if desc_s:
        # Keep description short — the model is generative and a long blob
        # invites it to "improve" the product to better match the prose.
        if len(desc_s) > 200:
            desc_s = desc_s[:200].rsplit(" ", 1)[0] + "…"
        lines.append(f"  Description:  {desc_s}")
    if not lines:
        lines.append("  (no spec available)")
    return "\n".join(lines)


def extract_isolated_product_with_nano_banana(
    source_path: str, target_model: str, upload_folder: str,
    brand: str | None = None,
    sku: str | None = None,
    variant_label: str | None = None,
    dimensions: str | None = None,
    color: str | None = None,
    description: str | None = None,
) -> str | None:
    """Send the proforma image (or rendered first PDF page) to Gemini's image-out
    model and save the returned isolated-product image. Returns the relative
    `uploads/...` path, or None on failure.

    The optional `brand`/`sku`/`variant_label`/`dimensions`/`color`/`description`
    args are folded into the prompt as a labelled spec block. They MATTER:
    when the proforma contains multiple visually-similar products (a 6-row
    wardrobe table, an N-product catalog page), the model needs the SKU and
    color/finish to disambiguate. Without these the model often picks the
    wrong row's product.

    Used by the single-item wizard as a parallel candidate alongside the
    bbox/embedded crops — gives the user a hallucination-tolerant option for
    cases where bbox extraction returns a too-small or wrong-region crop.
    Used by the bulk per-card "Generate via AI" action with the full draft
    spec so the model picks the correct row.
    """
    if not source_path or not os.path.exists(source_path):
        return None

    try:
        ext = os.path.splitext(source_path)[1].lower()
        if ext == '.pdf':
            # Render first page that mentions the model (or page 1) at 2x.
            doc = fitz_open(source_path)
            target_page = 0
            # Prefer matching by SKU (highly specific) before falling back
            # to name tokens, so multi-product PDFs route to the right page.
            search_tokens = []
            if sku and sku.strip():
                search_tokens.append(sku.strip())
            search_tokens += [p for p in (target_model or "").split() if len(p) >= 4]
            for page_num in range(min(15, len(doc))):
                page_text = doc[page_num].get_text("text").upper()
                if any(t.upper() in page_text for t in search_tokens):
                    target_page = page_num
                    break
            pix = doc[target_page].get_pixmap(matrix=fitz.Matrix(2, 2))
            png_bytes = pix.tobytes("png")
            doc.close()
        else:
            with open(source_path, 'rb') as f:
                raw = f.read()
            img = Image.open(io.BytesIO(raw))
            if img.mode != 'RGB':
                img = img.convert('RGB')
            buf = io.BytesIO()
            img.save(buf, 'PNG')
            png_bytes = buf.getvalue()

        spec_block = _build_product_spec_block(
            target_model=target_model, brand=brand, sku=sku,
            variant_label=variant_label, dimensions=dimensions,
            color=color, description=description,
        )
        prompt = _NANO_BANANA_PROMPT.format(product_spec_block=spec_block)
        response = _get_client().models.generate_content(
            model=_NANO_BANANA_MODEL,
            contents=[prompt, types.Part.from_bytes(data=png_bytes, mime_type="image/png")],
        )

        # Walk the response parts looking for the inline image bytes.
        candidates = getattr(response, 'candidates', None) or []
        for cand in candidates:
            content = getattr(cand, 'content', None)
            parts = getattr(content, 'parts', None) or []
            for part in parts:
                inline = getattr(part, 'inline_data', None)
                if inline and getattr(inline, 'data', None):
                    out_img = Image.open(io.BytesIO(inline.data))
                    if out_img.mode != 'RGB':
                        out_img = out_img.convert('RGB')
                    if _is_mostly_solid(out_img):
                        print("    ⚠ Nano-banana returned a mostly-solid image, skipping")
                        return None
                    # Force 4:3 — the prompt asks for it, but the model is
                    # generative and may return any aspect. Pad (never crop)
                    # with white so the saved file always matches the PIS
                    # review thumbnail frame.
                    out_img = _pad_to_aspect_4_3(out_img)
                    safe_name = secure_filename(target_model)
                    filename = f"visual_{safe_name}_nb_{int(time.time())}.jpg"
                    save_path = os.path.join(upload_folder, filename)
                    out_img.save(save_path, quality=95)
                    print(f"  🍌 Nano-banana isolated product → {filename} ({out_img.size[0]}x{out_img.size[1]})")
                    return f"uploads/{filename}"
        print("  ⚠ Nano-banana returned no inline image data")
        return None

    except Exception as e:
        print(f"  ⚠ Nano-banana extraction failed: {e}")
        return None
