"""
Bulk image extraction pipeline for the Proforma Import workspace.

Public surface:
    • `unified_extract(drafts, file_paths, upload_folder, log_cb=None)`
    • `save_images_to_variant_gallery(product, bucket)`

`unified_extract` renders every page once, calls Gemini to detect ALL product-
photo regions on the page in one shot, then fuzzy-matches each region's printed
SKU text to the draft list. Embedded raster streams are preferred over rendered
crops for quality. Web + AI fallbacks live in the marketing.py stream layer.

`drafts` shape (from `_draft_to_routing_meta` in blueprints/marketing.py):
    {
        'id':           int,
        'name':         str,
        'brand':        str,
        'model_number': str,
        'kind':         'singleton' | 'variants',
        'source_pages': [int, ...],
        'variants':     [{'label': str, 'model_number': str, ...}, ...],
    }

Return value — per-draft bucket dict keyed by draft id:
    {
        <id>:          {'image_path', 'variant_paths', 'candidates', 'status'},
        '_unassigned': {'candidates': [...]},   # orphan regions
    }
"""

from __future__ import annotations

import io
import os
import re
import time
from difflib import SequenceMatcher
from typing import Any, Callable

import fitz  # PyMuPDF
from PIL import Image

from .prompt_manager import get_prompt


# ── Logger plumbing (mirrors bulk_image_routing for log_cb compatibility) ───

_LEVEL_GLYPHS = {"info": "·", "ok": "✓", "warn": "⚠", "err": "✗"}


def _emit(log_cb: Callable[[str, str], None] | None, level: str, msg: str) -> None:
    if log_cb:
        try:
            log_cb(level, msg)
        except Exception:
            pass
    print(f"  {_LEVEL_GLYPHS.get(level, '·')} {msg}")


# ── Post-extraction edge cleanup ────────────────────────────────────────────


def _trim_near_white_edges(rel_path: str, upload_folder: str,
                            white_threshold: int = 240,
                            min_dim: int = 80) -> str:
    """Trim rows/columns of pure or near-pure white from the edges of an
    already-saved JPEG. Operates on the file at `upload_folder/rel_path`
    in place; returns `rel_path` unchanged.

    The bbox-routing path already tries to crop tightly, but the embedded-
    image stream sometimes carries a 1-2 px white border that the difference
    threshold in `_clean_product_image` misses (faint near-white sits below
    the threshold but `pad = max(5, 3%)` pulls it back in). This helper
    runs at the very end and takes a more aggressive view: a row/column
    qualifies as "white margin" when ≥98 % of its pixels have all three
    channels ≥ `white_threshold`.

    Never trims below `min_dim` × `min_dim`. Failure is silent — the file
    is left untouched and the original path is returned.
    """
    if not rel_path:
        return rel_path
    abs_path = os.path.join(upload_folder, os.path.basename(rel_path))
    if not os.path.exists(abs_path):
        # Maybe the path is uploads/foo.jpg — strip the prefix.
        if rel_path.startswith("uploads/"):
            abs_path = os.path.join(upload_folder, rel_path[len("uploads/"):])
        if not os.path.exists(abs_path):
            return rel_path
    try:
        with Image.open(abs_path) as src:
            img = src.convert("RGB")
        w, h = img.size
        if w < min_dim * 2 or h < min_dim * 2:
            return rel_path

        px = img.load()

        def _row_is_white(y: int) -> bool:
            white = 0
            step = max(1, w // 80)
            samples = 0
            for x in range(0, w, step):
                samples += 1
                r, g, b = px[x, y]
                if r >= white_threshold and g >= white_threshold and b >= white_threshold:
                    white += 1
            return samples > 0 and (white / samples) >= 0.98

        def _col_is_white(x: int) -> bool:
            white = 0
            step = max(1, h // 80)
            samples = 0
            for y in range(0, h, step):
                samples += 1
                r, g, b = px[x, y]
                if r >= white_threshold and g >= white_threshold and b >= white_threshold:
                    white += 1
            return samples > 0 and (white / samples) >= 0.98

        new_top = 0
        while new_top < h - min_dim and _row_is_white(new_top):
            new_top += 1
        new_bottom = h
        while new_bottom > new_top + min_dim and _row_is_white(new_bottom - 1):
            new_bottom -= 1
        new_left = 0
        while new_left < w - min_dim and _col_is_white(new_left):
            new_left += 1
        new_right = w
        while new_right > new_left + min_dim and _col_is_white(new_right - 1):
            new_right -= 1

        if (new_left, new_top, new_right, new_bottom) == (0, 0, w, h):
            return rel_path  # nothing to trim

        trimmed = img.crop((new_left, new_top, new_right, new_bottom))
        trimmed.save(abs_path, "JPEG", quality=95)
        return rel_path
    except Exception as e:
        print(f"  ⚠ _trim_near_white_edges failed on {rel_path}: {e}")
        return rel_path


# ── Persistence (Save step) ─────────────────────────────────────────────────


def save_images_to_variant_gallery(product, bucket: dict) -> None:
    """Apply ONE draft's pipeline result back onto its Product row.

    Mutates `product.pis_data`, `product.image_path`, and
    `product.additional_images`. Caller commits.

    Idempotent — re-running adds new candidates to the picker without
    wiping existing user selections, and never overwrites the primary
    thumbnail when one is already pinned by the user.

    Hero image:
      • First image from the first variant that produced one (variant
        clusters), or the first candidate (singletons).
      • Mirrored into `Product.image_path` so the workspace card and the
        existing PIS PDF template both pick it up without further work.

    PIS gallery:
      • Every extracted image (across every variant) lands in
        `Product.additional_images`, deduped against any user-uploaded
        extras already there.

    Variant assignments:
      • `pis_data.variants[i].image_path`  — primary per variant
      • `pis_data.variants[i].image_paths` — full list per variant (the
        workspace's per-variant photo strip reads this)
    """
    from sqlalchemy.orm.attributes import flag_modified

    pis = dict(product.pis_data or {})

    # ── Hero / primary thumbnail ───────────────────────────────────────
    image_path = bucket.get("image_path")
    if image_path:
        # Don't clobber a user-pinned image. The workspace's pickImage()
        # writes `_image_path`; only overwrite when nothing is set yet OR
        # the existing path is clearly a leftover from a previous failed
        # run (sentinels we never actually wrote).
        if not pis.get("_image_path"):
            pis["_image_path"] = image_path
            product.image_path = image_path

    # ── Picker candidates (preserve user selections) ───────────────────
    new_candidates = bucket.get("candidates") or []
    if new_candidates:
        existing = pis.get("_bulk_image_candidates") or []
        seen_paths = {c.get("path") for c in existing if isinstance(c, dict)}
        for c in new_candidates:
            if c.get("path") and c["path"] not in seen_paths:
                existing.append(c)
                seen_paths.add(c["path"])
        pis["_bulk_image_candidates"] = existing

    # ── Variant assignments + PIS gallery ──────────────────────────────
    variant_paths = bucket.get("variant_paths") or {}
    if variant_paths:
        variants = pis.get("variants") or []
        all_extras: list[str] = []
        if isinstance(variants, list):
            for v in variants:
                if not isinstance(v, dict):
                    continue
                vsku = (v.get("model_number") or "").strip()
                if not vsku:
                    continue
                rels = variant_paths.get(vsku) or []
                if isinstance(rels, str):
                    rels = [rels]
                if not rels:
                    continue
                # Don't clobber a per-variant pin either.
                if not v.get("image_path"):
                    v["image_path"] = rels[0]
                # Append new paths into image_paths without duplicating.
                existing_vpaths = list(v.get("image_paths") or [])
                seen_vpaths = set(existing_vpaths)
                for r in rels:
                    if r not in seen_vpaths:
                        existing_vpaths.append(r)
                        seen_vpaths.add(r)
                v["image_paths"] = existing_vpaths
                all_extras.extend(rels)
            pis["variants"] = variants

        # Additional images = every extracted photo across every variant,
        # deduped, with the primary thumbnail excluded so the PIS gallery
        # doesn't double-print the hero.
        if all_extras:
            existing_extras = list(product.additional_images or [])
            seen_extras = {e for e in existing_extras if isinstance(e, str)}
            for rel in all_extras:
                if rel == product.image_path:
                    continue
                if rel in seen_extras:
                    continue
                seen_extras.add(rel)
                existing_extras.append(rel)
            product.additional_images = existing_extras
            flag_modified(product, "additional_images")
    else:
        # Singleton clusters: every doc-extracted candidate beyond the
        # hero becomes an additional image.
        doc_paths = [
            c.get("path") for c in new_candidates
            if isinstance(c, dict) and c.get("source") == "document"
            and c.get("path")
        ]
        if doc_paths:
            existing_extras = list(product.additional_images or [])
            seen_extras = {e for e in existing_extras if isinstance(e, str)}
            for rel in doc_paths:
                if rel == product.image_path:
                    continue
                if rel in seen_extras:
                    continue
                seen_extras.add(rel)
                existing_extras.append(rel)
            product.additional_images = existing_extras
            flag_modified(product, "additional_images")

    # ── Task + overall status ──────────────────────────────────────────
    tasks = dict(pis.get("_enrichment_tasks") or {})
    status = bucket.get("status") or "pending"
    tasks["image"] = "done" if status in ("done", "partial") else "failed"
    pis["_enrichment_tasks"] = tasks

    statuses = list(tasks.values())
    if statuses:
        if all(s == "done" for s in statuses):
            pis["_enrichment_status"] = "done"
        elif any(s == "done" for s in statuses):
            pis["_enrichment_status"] = "partial"
        else:
            pis["_enrichment_status"] = "failed"

    product.pis_data = pis
    flag_modified(product, "pis_data")


# ── Phase 1: Unified default extractor ─────────────────────────────────────
#
# Design: one Gemini call per page detects ALL product-photo regions and reads
# the SKU text printed near each one. Python does the fuzzy-matching post-hoc,
# which means the pipeline works identically for PDFs (rendered), scanned PDFs
# (rendered), and raw image uploads — all inputs look the same after step 1.
#
# Public entry point:  unified_extract(drafts, file_paths, upload_folder, log_cb)
# Returns the same shape as run_image_pipeline so save_images_to_variant_gallery
# works without changes.  An extra "_unassigned" key carries orphan candidates
# that couldn't be matched to any draft.

# Score thresholds for the assignment decision.
_UNAMBIGUOUS_SCORE = 0.60   # min score to consider a match at all
_UNAMBIGUOUS_GAP   = 0.20   # gap between 1st and 2nd must be this large for unambiguity
_TIE_SCORE         = 0.55   # second match must be ≥ this to count as a tie
_WEAK_SCORE        = 0.40   # ≥ this but below unambiguous → low-confidence assignment

_UNIFIED_RENDER_MATRIX = fitz.Matrix(2, 2)   # 2× DPI — same as bulk_image_routing


# ── SKU normaliser ──────────────────────────────────────────────────────────

def _normalise_sku(text: str) -> str:
    """Lowercase + strip everything that isn't a-z or 0-9.

    Handles the common proforma variations:
      "XDY60.120060-OAK-W"  →  "xdy60120060oakw"
      "XDY60/120060 OAK W"  →  "xdy60120060oakw"
    """
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def _score_sku_match(norm_a: str, norm_b: str) -> float:
    """Return a [0, 1] similarity score between two already-normalised SKU strings.

    Scoring tiers:
      1.0   exact match
      0.85  one string is a full substring of the other (scaled by length ratio)
      0.75× difflib ratio when ratio ≥ 0.70
      0.0   ratio < 0.70
    """
    if not norm_a or not norm_b or len(norm_a) < 3 or len(norm_b) < 3:
        return 0.0
    if norm_a == norm_b:
        return 1.0
    shorter, longer = (norm_a, norm_b) if len(norm_a) <= len(norm_b) else (norm_b, norm_a)
    if shorter in longer:
        return 0.85 * (len(shorter) / len(longer))
    ratio = SequenceMatcher(None, norm_a, norm_b).ratio()
    return ratio * 0.75 if ratio >= 0.70 else 0.0


# ── Match-target builder ────────────────────────────────────────────────────

def _build_match_targets(drafts: list[dict]) -> list[dict]:
    """Flatten every draft + variant into a flat list of searchable targets.

    Each entry: {draft_id, variant_sku, search_strings: [str, ...]}
    search_strings holds every printable identifier for that slot (SKU + label).
    """
    targets: list[dict] = []
    for d in drafts:
        did = d.get('id')
        if did is None:
            continue
        kind = (d.get('kind') or 'singleton').lower()
        if kind == 'variants':
            for v in (d.get('variants') or []):
                sku   = (v.get('model_number') or '').strip()
                label = (v.get('label') or '').strip()
                strs  = list({s for s in [sku, label] if s})
                if strs:
                    targets.append({'draft_id': did, 'variant_sku': sku,
                                    'search_strings': strs})
        else:
            sku  = (d.get('model_number') or '').strip()
            name = (d.get('name') or '').strip()
            strs = list({s for s in [sku, name] if s})
            if strs:
                targets.append({'draft_id': did, 'variant_sku': '',
                                'search_strings': strs})
    return targets


# ── Region → draft fuzzy matcher ───────────────────────────────────────────

def _fuzzy_match_region(sku_text: str,
                         targets: list[dict]) -> list[tuple[int, str, float]]:
    """Return [(draft_id, variant_sku, score), ...] sorted by score descending.

    Each target contributes its best-scoring search_string to its slot.
    Slots with score 0 are excluded.
    """
    norm_det = _normalise_sku(sku_text)
    if not norm_det or len(norm_det) < 3:
        return []

    scored: dict[tuple[int, str], float] = {}
    for t in targets:
        best = 0.0
        for ss in t['search_strings']:
            s = _score_sku_match(norm_det, _normalise_sku(ss))
            if s > best:
                best = s
        key = (t['draft_id'], t['variant_sku'])
        if best > scored.get(key, 0.0):
            scored[key] = best

    results = [(did, vsku, sc) for (did, vsku), sc in scored.items() if sc > 0]
    results.sort(key=lambda x: (-x[2], x[1]))
    return results


def _classify_assignment(
        matches: list[tuple[int, str, float]],
) -> tuple[str, list[tuple[int, str, float]]]:
    """Return (tier, assign_list).

    Tiers:
      'unambiguous' — one clear winner; assign_list has one entry
      'tie'         — two matches within gap threshold; assign_list has two entries
                      (both get the image as candidates, neither auto-promoted)
      'weak'        — single weak match below unambiguous threshold
      'orphan'      — nothing useful found
    """
    if not matches or matches[0][2] < _WEAK_SCORE:
        return 'orphan', []
    top   = matches[0][2]
    second = matches[1][2] if len(matches) >= 2 else 0.0
    if top >= _UNAMBIGUOUS_SCORE:
        if (top - second) >= _UNAMBIGUOUS_GAP or second < _TIE_SCORE:
            return 'unambiguous', [matches[0]]
        return 'tie', matches[:2]
    return 'weak', [matches[0]]


# ── Region detector (one Gemini call per page) ──────────────────────────────

def _detect_regions_on_page(pil_page: Image.Image, page_num: int,
                              log_cb: Callable[[str, str], None] | None = None,
                              ) -> list[dict]:
    """Send one rendered page to Gemini; return all detected product-photo regions.

    Each region: {box_2d: [ymin, xmin, ymax, xmax], sku_text: str, confidence: str}
    """
    from google import genai
    from google.genai import types
    from utils.json_utils import safe_json_loads

    prompt_text = get_prompt("unified_image_extraction") or ""
    if not prompt_text:
        _emit(log_cb, "warn",
              f"Page {page_num}: prompt 'unified_image_extraction' not found — skipping")
        return []

    buf = io.BytesIO()
    pil_page.save(buf, "PNG")

    try:
        client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        from .api_metering import gemini_call
        resp = gemini_call(
            prompt_id='unified_image_extraction',
            model="gemini-2.5-flash",
            client=client,
            contents=[prompt_text,
                      types.Part.from_bytes(data=buf.getvalue(),
                                             mime_type="image/png")],
            config=types.GenerateContentConfig(
                response_mime_type="application/json"),
        )
        parsed = safe_json_loads(resp.text or "", fallback={})
        if not isinstance(parsed, dict):
            return []
        regions = [r for r in (parsed.get("regions") or [])
                   if isinstance(r, dict)
                   and isinstance(r.get("box_2d"), list)
                   and len(r["box_2d"]) == 4]
        _emit(log_cb, "info",
              f"Page {page_num}: {len(regions)} region(s) detected")
        return regions
    except Exception as e:
        _emit(log_cb, "warn", f"Page {page_num}: detection failed ({e})")
        return []


# ── Pixel extractor (embedded → crop fallback) ──────────────────────────────

def _extract_region_pixels(
        source_path: str,
        page_index: int,
        pil_page: Image.Image,
        box_2d: list,
        fitz_doc: Any,
) -> tuple[bytes | None, str]:
    """Return (image_bytes, source) for one detected region.

    source is 'embedded' when we pulled original bytes from the PDF's
    embedded raster stream; 'crop' when we rendered and cropped.

    Quality gates:
      • Minimum rendered bbox: 80×80 px
      • Embedded images must be ≥ 100×100 px and ≥ 5 kB
      • Crops must not be mostly-solid (uniform colour)
    """
    from .pdf_processing import (_snap_to_cell_border, _clean_product_image,
                                  _is_mostly_solid)

    pil_w, pil_h = pil_page.size
    # box_2d = [ymin, xmin, ymax, xmax] on 0-1000
    px_x0 = int(box_2d[1] / 1000 * pil_w)
    px_y0 = int(box_2d[0] / 1000 * pil_h)
    px_x1 = int(box_2d[3] / 1000 * pil_w)
    px_y1 = int(box_2d[2] / 1000 * pil_h)

    px_x0 = max(0, min(px_x0, pil_w - 2))
    px_y0 = max(0, min(px_y0, pil_h - 2))
    px_x1 = max(px_x0 + 2, min(px_x1, pil_w))
    px_y1 = max(px_y0 + 2, min(px_y1, pil_h))

    if (px_x1 - px_x0) < 80 or (px_y1 - px_y0) < 80:
        return None, 'crop'

    # ── Embedded raster path (PDF only) ─────────────────────────────────
    if fitz_doc is not None and source_path.lower().endswith('.pdf'):
        try:
            page      = fitz_doc[page_index]
            page_rect = page.rect
            # Render scale: derived from actual rendered image width vs page pts.
            scale_x = pil_w / max(page_rect.width,  1)
            scale_y = pil_h / max(page_rect.height, 1)
            region_rect = fitz.Rect(
                px_x0 / scale_x, px_y0 / scale_y,
                px_x1 / scale_x, px_y1 / scale_y,
            )
            best_xref    = 0
            best_overlap = 0.30          # must cover ≥30 % of embedded image
            for img_info in page.get_image_info(xrefs=True):
                xref = img_info.get('xref', 0)
                if xref <= 0:
                    continue
                img_bbox = fitz.Rect(img_info.get('bbox', (0, 0, 0, 0)))
                if img_bbox.is_empty:
                    continue
                inter = img_bbox & region_rect
                if inter.is_empty:
                    continue
                area = img_bbox.width * img_bbox.height
                if area <= 0:
                    continue
                overlap = (inter.width * inter.height) / area
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_xref = xref
            if best_xref > 0:
                base   = fitz_doc.extract_image(best_xref)
                raw    = base.get('image')
                if raw and len(raw) > 5_000:
                    try:
                        with Image.open(io.BytesIO(raw)) as chk:
                            tw, th = chk.size
                        if tw >= 100 and th >= 100:
                            return raw, 'embedded'
                    except Exception:
                        pass
        except Exception as e:
            print(f"  ⚠ embedded extraction failed (page {page_index}): {e}")

    # ── Rendered crop fallback ───────────────────────────────────────────
    try:
        px_x0, px_y0, px_x1, px_y1 = _snap_to_cell_border(
            pil_page, (px_x0, px_y0, px_x1, px_y1))
        crop = pil_page.crop((px_x0, px_y0, px_x1, px_y1))
        if _is_mostly_solid(crop):
            return None, 'crop'
        crop = _clean_product_image(crop)
        buf  = io.BytesIO()
        crop.save(buf, 'JPEG', quality=92)
        return buf.getvalue(), 'crop'
    except Exception as e:
        print(f"  ⚠ crop extraction failed: {e}")
        return None, 'crop'


# ── Raw-bytes → disk saver ──────────────────────────────────────────────────

def _save_raw_image(img_bytes: bytes, upload_folder: str,
                    label: str = '') -> str | None:
    """Decode img_bytes (any PIL-supported format), resize to ≤1600 px on the
    long edge, save as JPEG, and return the relative 'uploads/...' path."""
    try:
        stamp      = int(time.time() * 1000)
        safe_label = re.sub(r'[^a-z0-9]', '', label.lower())[:20]
        fname      = f"unified_{stamp}_{safe_label or 'img'}.jpg"
        abs_path   = os.path.join(upload_folder, fname)
        with Image.open(io.BytesIO(img_bytes)) as img:
            rgb = img.convert('RGB')
            w, h = rgb.size
            if max(w, h) > 1600:
                scale = 1600 / max(w, h)
                rgb   = rgb.resize((int(w * scale), int(h * scale)),
                                    Image.LANCZOS)  # type: ignore[attr-defined]
            rgb.save(abs_path, 'JPEG', quality=92)
        return f"uploads/{fname}"
    except Exception as e:
        print(f"  ⚠ _save_raw_image failed: {e}")
        return None


# ── Per-region processor (used inside unified_extract loop) ─────────────────

def _process_unified_region(
        region:       dict,
        src_path:     str,
        page_index:   int,
        pil_page:     Image.Image,
        fitz_doc:     Any,
        targets:      list[dict],
        out:          dict,
        upload_folder: str,
) -> None:
    """Evaluate one detected region, extract pixels, match to drafts, and
    append the result to the appropriate output bucket(s)."""
    box_2d   = region.get('box_2d') or []
    sku_text = (region.get('sku_text') or '').strip()
    if len(box_2d) != 4:
        return

    matches       = _fuzzy_match_region(sku_text, targets)
    tier, assign  = _classify_assignment(matches)

    img_bytes, source_type = _extract_region_pixels(
        src_path, page_index, pil_page, box_2d, fitz_doc)
    if not img_bytes:
        return

    rel_path = _save_raw_image(img_bytes, upload_folder, sku_text[:30])
    if not rel_path:
        return
    if source_type != 'embedded':
        rel_path = _trim_near_white_edges(rel_path, upload_folder)

    # Confidence tag
    if tier == 'orphan':
        confidence = 'low'
    elif tier == 'tie':
        confidence = 'review'
    elif tier == 'weak':
        confidence = 'low'
    elif source_type == 'embedded':
        confidence = 'high'
    else:
        confidence = 'medium'

    if tier == 'orphan':
        unassigned = out.get('_unassigned')
        if isinstance(unassigned, dict):
            unassigned['candidates'].append({
                'path':          rel_path,
                'source':        'document',
                'variant_sku':   '',
                'matched_label': sku_text,
                'confidence':    'low',
                'box_2d':        box_2d,
                'page_index':    page_index,
            })
        return

    for draft_id, variant_sku, _score in assign:
        cand_conf = 'review' if tier == 'tie' else confidence
        bucket    = out.get(draft_id)
        if not isinstance(bucket, dict):
            continue
        existing_paths = {c.get('path') for c in bucket['candidates']}
        if rel_path not in existing_paths:
            bucket['candidates'].append({
                'path':          rel_path,
                'source':        'document',
                'variant_sku':   variant_sku,
                'matched_label': sku_text,
                'confidence':    cand_conf,
                'box_2d':        box_2d,
                'page_index':    page_index,
            })
        if variant_sku:
            vp = bucket['variant_paths']
            vp.setdefault(variant_sku, [])
            if rel_path not in vp[variant_sku]:
                vp[variant_sku].append(rel_path)


def _assign_primary_and_status(draft: dict, out: dict) -> None:
    """Pick the primary image and set status on one draft's bucket."""
    did    = draft.get('id')
    bucket = out.get(did)
    if not isinstance(bucket, dict):
        return
    kind     = (draft.get('kind') or 'singleton').lower()
    variants = draft.get('variants') or []

    if not bucket['image_path']:
        if kind == 'variants' and bucket['variant_paths']:
            for v in variants:
                vsku  = (v.get('model_number') or '').strip()
                paths = bucket['variant_paths'].get(vsku) or []
                if paths:
                    bucket['image_path'] = paths[0]
                    break
        if not bucket['image_path'] and bucket['candidates']:
            for c in bucket['candidates']:
                if c['confidence'] in ('high', 'medium'):
                    bucket['image_path'] = c['path']
                    break
            if not bucket['image_path']:
                bucket['image_path'] = bucket['candidates'][0]['path']

    if kind == 'variants' and variants:
        need = sum(1 for v in variants
                   if (v.get('model_number') or '').strip())
        have = sum(1 for v in variants
                   if (v.get('model_number') or '').strip()
                   and bucket['variant_paths'].get(
                       (v.get('model_number') or '').strip()))
        if have == 0 and not bucket['image_path']:
            bucket['status'] = 'failed'
        elif have < need:
            bucket['status'] = 'partial'
        else:
            bucket['status'] = 'done'
    else:
        bucket['status'] = 'done' if bucket['image_path'] else 'failed'


# ── Orchestrator ────────────────────────────────────────────────────────────

def unified_extract(
        drafts:        list[dict],
        file_paths:    list[str],
        upload_folder: str,
        log_cb:        Callable[[str, str], None] | None = None,
) -> dict:
    """Main bulk image extractor for the Proforma Import workspace.

    Renders every page once, fires ONE Gemini detection call per page to
    locate all product-photo regions and read their nearest printed SKU text,
    then fuzzy-matches each region to the workspace drafts. Embedded raster
    streams are used when available (pixel-perfect quality); rendered crops
    are the fallback. Web + AI fallbacks for empty drafts run in the
    marketing.py stream layer after this function returns.

    Returns a dict keyed by draft id (same shape save_images_to_variant_gallery
    expects) plus an extra '_unassigned' key for orphan regions that couldn't
    be confidently matched to any draft.
    """
    out: dict = {
        d["id"]: {
            "image_path":    None,
            "variant_paths": {},
            "candidates":    [],
            "status":        "pending",
        }
        for d in drafts if d.get("id") is not None
    }
    out["_unassigned"] = {"candidates": []}

    if not drafts or not file_paths:
        return out

    _emit(log_cb, "info",
          f"Unified extractor: {len(drafts)} draft(s), "
          f"{len(file_paths)} file(s)")

    targets = _build_match_targets(drafts)
    _emit(log_cb, "info", f"{len(targets)} SKU target(s) indexed")

    # Open every PDF once so the same fitz.Document is used for both rendering
    # and embedded-image extraction — avoids double-opening on multi-page files.
    open_fitz: dict[str, Any] = {}
    for src in file_paths:
        if src and os.path.exists(src) and src.lower().endswith('.pdf'):
            try:
                open_fitz[src] = fitz.open(src)  # type: ignore[attr-defined]
            except Exception as e:
                _emit(log_cb, "warn",
                      f"Cannot open {os.path.basename(src)}: {e}")
                open_fitz[src] = None

    try:
        for src in file_paths:
            if not src or not os.path.exists(src):
                continue
            ext = os.path.splitext(src)[1].lower()

            if ext == '.pdf':
                doc = open_fitz.get(src)
                if doc is None:
                    continue
                for pno in range(len(doc)):
                    try:
                        pix      = doc[pno].get_pixmap(matrix=_UNIFIED_RENDER_MATRIX)
                        pil_page = Image.open(
                            io.BytesIO(pix.tobytes('png'))).convert('RGB')
                    except Exception as e:
                        _emit(log_cb, "warn",
                              f"Render failed ({os.path.basename(src)} p{pno+1}): {e}")
                        continue
                    regions = _detect_regions_on_page(pil_page, pno + 1, log_cb)
                    for r in regions:
                        _process_unified_region(
                            r, src, pno, pil_page, doc,
                            targets, out, upload_folder)

            elif ext in ('.jpg', '.jpeg', '.png', '.webp',
                         '.bmp', '.tiff', '.tif'):
                try:
                    pil_page = Image.open(src).convert('RGB')
                except Exception as e:
                    _emit(log_cb, "warn",
                          f"Image load failed ({os.path.basename(src)}): {e}")
                    continue
                regions = _detect_regions_on_page(pil_page, 1, log_cb)
                for r in regions:
                    _process_unified_region(
                        r, src, 0, pil_page, None,
                        targets, out, upload_folder)

    finally:
        for doc in open_fitz.values():
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass

    for draft in drafts:
        _assign_primary_and_status(draft, out)

    drafts_ok  = sum(1 for k, b in out.items()
                     if isinstance(k, int) and b.get('image_path'))
    total_cand = sum(len(b['candidates']) for k, b in out.items()
                     if isinstance(k, int))
    orphans    = len(out.get('_unassigned', {}).get('candidates', []))
    _emit(log_cb,
          "ok" if drafts_ok else "warn",
          f"Unified extractor done: {drafts_ok}/{len(drafts)} draft(s) with image, "
          f"{total_cand} candidate(s), {orphans} orphan(s)")
    return out
