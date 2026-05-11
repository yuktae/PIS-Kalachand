"""
Variant-aware PDF image extraction pipeline (Slice → Assign → Extract → Save).

Replaces the older `bulk_image_routing.route_and_extract` for Bulk Import.
The old approach asked Gemini to route every photo on a multi-product page
to the right draft; bbox precision dropped sharply on busy proformas and we
relied on web-search + nano-banana fallbacks to paper over the misses.

This pipeline uses the `source_pages` triage gives us per row to deterministically
SLICE the proforma into per-product (or per-variant) mini-PDFs before doing
any extraction. The downstream extractor only sees pages relevant to ONE
product, so cross-row mixups disappear and we can drop the fallbacks entirely.

Public surface:
    • `slice_pdf_by_product(pdf_path, page_indexes, out_dir, label)`
    • `extract_images_from_slice(slice_path, target_label, upload_folder)`
    • `run_image_pipeline(drafts, file_paths, upload_folder, log_cb=None)`
    • `save_images_to_variant_gallery(product, bucket)`

`drafts` shape (extension of `_draft_to_routing_meta` from blueprints/marketing.py):
    {
        'id':           int,
        'name':         str,
        'brand':        str,
        'model_number': str,
        'kind':         'singleton' | 'variants',
        'source_pages': [int, ...],          # cluster-level pages
        'variants':     [{
            'label':        str,
            'model_number': str,
            'source_pages': [int, ...],      # per-variant pages
        }, ...],
    }

`run_image_pipeline` returns a dict shaped exactly like the legacy
`route_and_extract` output so the marketing.py persist code keeps working:
    {<draft_id>: {
        'image_path':    'uploads/...',
        'variant_paths': {SKU: [path, ...]},
        'candidates':    [{path, source, variant_sku, matched_label, confidence}],
        'status':        'done' | 'partial' | 'failed',
    }}
"""

from __future__ import annotations

import os
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

import io
import re

import fitz  # PyMuPDF
from PIL import Image
from werkzeug.utils import secure_filename

from .pdf_processing import extract_specific_image
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


# ── Slicer ──────────────────────────────────────────────────────────────────


def slice_pdf_by_product(pdf_path: str, page_indexes: list[int],
                          out_dir: str, label: str) -> str | None:
    """Write a mini-PDF containing only `page_indexes` from `pdf_path`.

    Returns the absolute path to the new PDF, or None when slicing fails or
    the input has no usable pages. The output filename is unique per call so
    parallel slicing doesn't collide.

    For non-PDF inputs (jpg/png), returns the input path unchanged — the
    image is already a "slice of one" and downstream extractors handle
    images natively.
    """
    if not pdf_path or not os.path.exists(pdf_path):
        return None

    ext = os.path.splitext(pdf_path)[1].lower()
    if ext != ".pdf":
        # Standalone image upload — no slicing needed.
        return pdf_path

    valid_pages: list[int] = []
    try:
        with fitz.open(pdf_path) as src:
            total = len(src)
            for p in page_indexes or []:
                try:
                    pi = int(p)
                except (TypeError, ValueError):
                    continue
                if 0 <= pi < total and pi not in valid_pages:
                    valid_pages.append(pi)
            if not valid_pages:
                return None

            os.makedirs(out_dir, exist_ok=True)
            safe_label = secure_filename(label) or "slice"
            stamp = int(time.time() * 1000)
            out_name = f"slice_{safe_label}_{stamp}.pdf"
            out_path = os.path.join(out_dir, out_name)

            dst = fitz.open()
            try:
                for pi in valid_pages:
                    dst.insert_pdf(src, from_page=pi, to_page=pi)
                dst.save(out_path)
            finally:
                dst.close()
        return out_path
    except Exception as e:
        print(f"  ⚠ slice_pdf_by_product failed: {e}")
        return None


# ── Per-slice extractor ─────────────────────────────────────────────────────


def extract_images_from_slice(slice_path: str, target_label: str,
                               upload_folder: str,
                               variant_sku: str = "",
                               brand: str = "") -> list[str]:
    """Extract product image(s) from a slice for ONE specific variant.

    Strategy:
      1. **SKU-anchored row crop** (preferred when `variant_sku` is set and
         the PDF has selectable text). PyMuPDF's `search_for` locates the
         y-band of the SKU's printed label, we crop a tight row around it,
         and Gemini bbox-routes the crop to find the product photo. This
         works even when many variants share one PDF page — each row is
         routed independently with only that variant in the prompt.
      2. **Embedded-image fallback** (used when the slice is a scanned
         PDF, an image upload, or the SKU isn't found). Returns a single
         best embedded image picked by AI from the slice — NOT every
         photo on the page (which is what `all_matches=True` would do
         and what produced cross-variant duplicates in the previous
         iteration).

    Returns a list of relative `uploads/...` paths (possibly empty).
    """
    if not slice_path or not os.path.exists(slice_path):
        return []

    ext = os.path.splitext(slice_path)[1].lower()
    sku = (variant_sku or "").strip()

    # ── Path A: SKU-anchored row crop (PDF only, needs selectable text) ──
    if ext == ".pdf" and sku:
        saved = _extract_via_sku_row_routing(
            slice_path, target_label, sku, brand, upload_folder,
        )
        if saved:
            return saved
        # Fall through to embedded if no SKU rows were found in the slice
        # text (scanned PDF or unusual layout).

    # ── Path B: embedded-image fallback (single best, not all-matches) ──
    # Using `all_matches=False` so the AI picks ONE best image rather than
    # returning every embedded photo on the slice's pages — that's what
    # produced the cross-variant duplicate images before.
    try:
        rel = extract_specific_image(
            slice_path, target_label, upload_folder,
            skip_verify=True, all_matches=False, prefer_embedded=True,
        )
        if not rel:
            return []
        return [_trim_near_white_edges(rel, upload_folder)]
    except Exception as e:
        print(f"  ⚠ extract_images_from_slice('{target_label}') failed: {e}")
        return []


def _extract_via_sku_row_routing(slice_path: str, target_label: str,
                                  variant_sku: str, brand: str,
                                  upload_folder: str) -> list[str]:
    """Find rows of the slice whose printed text contains `variant_sku`,
    crop each row's y-band, and run Gemini bbox routing scoped to ONE
    variant. Reuses the row-detection + per-row routing helpers already
    proven on the SUNON proforma in `bulk_image_routing`.

    Returns the list of saved relative paths. Empty list signals the
    caller to try the embedded fallback.
    """
    # Late import — pulls Gemini client + Playwright transitively.
    from .bulk_image_routing import (
        _PAGE_RENDER_DPI_MATRIX,
        _crop_match_to_file,
        _expand_row_band,
        _find_rows_via_pdf_text,
        _route_within_row,
    )

    # Build a single-draft "shopping list" so the row finder + per-row
    # router only consider this one variant. Cluster kind doesn't matter
    # for the row search — it indexes by SKU.
    fake_draft = {
        "id":           0,
        "name":         target_label,
        "brand":        brand or "",
        "model_number": variant_sku,
        "kind":         "singleton",
        "variants":     [],
    }

    saved: list[str] = []
    try:
        with fitz.open(slice_path) as doc:
            for pno in range(len(doc)):
                page = doc[pno]
                pix = page.get_pixmap(matrix=_PAGE_RENDER_DPI_MATRIX)
                pil_page = Image.open(
                    io.BytesIO(pix.tobytes("png"))
                ).convert("RGB")

                raw_rows = _find_rows_via_pdf_text(slice_path, pno, [fake_draft])
                if not raw_rows:
                    continue
                rows = _expand_row_band(
                    raw_rows, page_height_px=float(pil_page.size[1]),
                )
                if not rows:
                    continue

                for row in rows:
                    top = max(0, int(row["band_top"]))
                    bot = min(pil_page.size[1], int(row["band_bottom"]))
                    if bot - top < 60:
                        continue
                    row_pil = pil_page.crop((0, top, pil_page.size[0], bot))
                    matches = _route_within_row(row_pil, row, pno + 1)
                    for m in matches:
                        rel, _reason = _crop_match_to_file(
                            row_pil, m, target_label, upload_folder,
                        )
                        if rel:
                            saved.append(_trim_near_white_edges(rel,
                                                                 upload_folder))
    except Exception as e:
        print(f"  ⚠ SKU-row routing failed for '{target_label}' / "
              f"'{variant_sku}': {e}")
        return []

    return saved


# ── Cluster-level multi-variant routing (handles shared-row proformas) ──────


def _finish_hint_for(label: str) -> str:
    """Parse the finish/colour keywords out of a variant label.

    Variant labels in this codebase are usually printed verbatim from the
    proforma — "2D WARDROBE-OAK/WARM WHITE", "FELIX WALNUT", "Black 256GB",
    "60L Stainless Steel", etc. The matching prompt benefits from a clean
    finish hint stripped of generic product-type words so Gemini focuses
    on the *differentiator*.
    """
    if not label:
        return ""
    text = label.upper()
    # Drop common product-type / size tokens that aren't finish words.
    noise = {
        "2D", "3D", "4D", "5D", "WARDROBE", "WADROBE", "TV", "FRIDGE",
        "REFRIGERATOR", "BLENDER", "CABINET", "OVEN", "FRYER", "WASHER",
        "DRYER", "AC", "HOOD", "MICROWAVE",
    }
    parts = []
    for chunk in re.split(r"[\s/\-_,]+", text):
        if not chunk or chunk in noise:
            continue
        # Drop pure-numeric tokens like "256GB", "60L" — those are size,
        # not finish, and the proforma only printed them as suffixes.
        if re.fullmatch(r"\d+([A-Z]{1,3})?", chunk):
            continue
        parts.append(chunk)
    return " ".join(parts) or text


def _match_photos_to_variants(photo_paths: list[str], variants: list[dict],
                                family_label: str, brand: str,
                                upload_folder: str) -> dict[int, int]:
    """Final-mile mapping pass — given N already-cropped photos and M
    variants, return `{photo_index: variant_index}` (both 1-based) using a
    Gemini call that compares VISUAL finish to the variant's label hint.

    Returns `{}` on failure so the caller falls back to deterministic
    left-to-right assignment.
    """
    if not photo_paths or not variants:
        return {}

    # Late import — Gemini client only needed inside this fn.
    import os as _os
    from google import genai
    from google.genai import types
    try:
        from utils.json_utils import safe_json_loads
    except Exception:
        import json as _json
        def safe_json_loads(s, fallback=None):  # type: ignore[no-redef]
            try:
                return _json.loads(s or "")
            except Exception:
                return fallback if fallback is not None else {}

    client = genai.Client(api_key=_os.getenv("GOOGLE_API_KEY"))

    variant_lines = []
    for i, v in enumerate(variants, 1):
        label = (v.get("label") or "").strip() or "(unnamed)"
        hint = _finish_hint_for(label)
        variant_lines.append(f"  {i}. {label}   [finish_hint: {hint or '(generic)'}]")
    variants_block = "\n".join(variant_lines)

    prompt_template = get_prompt("bulk_variant_photo_matching") or ""
    if not prompt_template:
        return {}
    prompt = prompt_template.format(
        family_label=family_label or "(unknown)",
        brand=brand or "(unknown)",
        variants_block=variants_block,
        photo_count=len(photo_paths),
    )

    contents: list = [prompt]
    for rel in photo_paths:
        abs_path = rel
        if not _os.path.isabs(abs_path):
            abs_path = _os.path.join(upload_folder,
                                      rel[len("uploads/"):]
                                      if rel.startswith("uploads/") else rel)
        if not _os.path.exists(abs_path):
            continue
        try:
            with open(abs_path, "rb") as f:
                contents.append(types.Part.from_bytes(
                    data=f.read(), mime_type="image/jpeg"))
        except Exception as e:
            print(f"  ⚠ photo-match read failed for {abs_path}: {e}")

    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        parsed = safe_json_loads(resp.text or "", fallback={})
    except Exception as e:
        print(f"  ⚠ photo-match call failed: {e}")
        return {}

    if not isinstance(parsed, dict):
        return {}
    assignments = parsed.get("assignments") or []
    out: dict[int, int] = {}        # photo_index → variant_index (1-based)
    used_photos: set[int] = set()
    for entry in assignments:
        if not isinstance(entry, dict):
            continue
        vi_raw = entry.get("variant_index")
        pi_raw = entry.get("photo_index")
        if vi_raw is None or pi_raw is None:
            continue
        try:
            vi = int(vi_raw)
            pi = int(pi_raw)
        except (TypeError, ValueError):
            continue
        if vi < 1 or vi > len(variants):
            continue
        if pi < 1 or pi > len(photo_paths):
            continue
        # First-write-wins on photo_index — if Gemini accidentally reused a
        # photo for two variants we keep the first assignment and let the
        # second variant fall through to deterministic backfill below.
        if pi in used_photos:
            continue
        out[pi] = vi
        used_photos.add(pi)
    return out


def extract_cluster_images(slice_path: str, draft: dict, upload_folder: str,
                            log_cb=None) -> dict[str, list[str]]:
    """Variant-cluster extractor — two-phase Slice → Extract → Match flow.

    Why a separate path: many proformas (the SUNON Wardrobes one is the
    archetype) merge two variants into ONE row of the table — both SKUs are
    printed in the same cell, and the cell contains 2 photos (closed +
    open). Per-variant SKU search produces the same row band for both
    variants, so the previous per-variant routing returned the same first
    photo each time.

    Two-phase strategy:
      1. **Extract:** find the cluster's row band (union of every variant's
         SKU positions), crop the band, ask Gemini to bbox every product
         photo on it. Save each bbox as a candidate file. Quantise bbox
         keys (20 px) to dedupe two near-identical matches into one photo.
      2. **Match:** with the saved photos in hand, run a SECOND, smaller
         Gemini call that compares each photo's visible finish/colour to
         each variant's label hint and returns `{photo: variant}`. This
         step uses actual VISUAL features, not text proximity, so it
         disambiguates the OAK/WALNUT case Gemini's routing call gets
         wrong (since both SKUs print at the same y-band).

    If the visual-match call fails or assigns nothing, we backfill
    deterministically: leftmost photo → first variant, next → second, etc.

    Returns `{variant_sku: [path, ...]}` keyed by variant model_number.
    Empty `{}` signals the caller to fall back to per-variant SKU routing.
    """
    from .bulk_image_routing import (
        _PAGE_RENDER_DPI_MATRIX,
        _build_drafts_block,
        _crop_match_to_file,
        _expand_row_band,
        _find_rows_via_pdf_text,
        _route_one_page,
    )

    variants = [v for v in (draft.get("variants") or [])
                if isinstance(v, dict) and (v.get("model_number") or "").strip()]
    if not variants:
        return {}

    cluster_label = (draft.get("name") or "").strip() or "cluster"
    brand = (draft.get("brand") or "").strip()

    synthetic_draft = {
        "id":           draft.get("id") or 0,
        "name":         cluster_label,
        "brand":        brand,
        "model_number": "",
        "kind":         "variants",
        "variants":     [
            {"label":        (v.get("label") or "").strip(),
             "model_number": (v.get("model_number") or "").strip()}
            for v in variants
        ],
    }
    drafts_block, _sku_lookup = _build_drafts_block([synthetic_draft])

    out: dict[str, list[str]] = {
        (v.get("model_number") or "").strip(): [] for v in variants
    }

    # ── Phase 1: extract every photo from the cluster row band ─────────
    saved_photos: list[str] = []      # in left-to-right order
    try:
        with fitz.open(slice_path) as doc:
            for pno in range(len(doc)):
                page = doc[pno]
                pix = page.get_pixmap(matrix=_PAGE_RENDER_DPI_MATRIX)
                pil_page = Image.open(
                    io.BytesIO(pix.tobytes("png"))
                ).convert("RGB")
                page_w, page_h = pil_page.size

                raw_rows = _find_rows_via_pdf_text(slice_path, pno, [synthetic_draft])
                if not raw_rows:
                    continue
                expanded = _expand_row_band(raw_rows, page_height_px=float(page_h))
                if not expanded:
                    continue

                band_top = min(int(r["band_top"]) for r in expanded)
                band_bot = max(int(r["band_bottom"]) for r in expanded)
                band_top = max(0, band_top)
                band_bot = min(page_h, band_bot)
                if band_bot - band_top < 60:
                    continue

                band_pil = pil_page.crop((0, band_top, page_w, band_bot))

                buf = io.BytesIO()
                band_pil.save(buf, "PNG")
                matches = _route_one_page(pno + 1, buf.getvalue(), drafts_block)
                if not matches:
                    continue

                def _xmin(m: dict) -> int:
                    box = m.get("box_2d") or [0, 0, 0, 0]
                    return int(box[1]) if len(box) >= 2 else 0
                matches = sorted(matches, key=_xmin)

                # Quantise bbox keys to dedupe near-duplicate matches the
                # model occasionally returns when it's uncertain.
                used_keys: set[tuple[int, int, int, int]] = set()
                for m in matches:
                    box = m.get("box_2d") or [0, 0, 0, 0]
                    if len(box) != 4:
                        continue
                    key: tuple[int, int, int, int] = (
                        int(round(box[0] / 20) * 20),
                        int(round(box[1] / 20) * 20),
                        int(round(box[2] / 20) * 20),
                        int(round(box[3] / 20) * 20),
                    )
                    if key in used_keys:
                        continue
                    used_keys.add(key)
                    rel, _reason = _crop_match_to_file(
                        band_pil, m, cluster_label, upload_folder,
                    )
                    if rel:
                        saved_photos.append(_trim_near_white_edges(rel, upload_folder))
    except Exception as e:
        print(f"  ⚠ Cluster routing extract phase failed for "
              f"'{cluster_label}': {e}")
        return {}

    if not saved_photos:
        return {}

    # ── Phase 2: visual match — distribute photos to variants ──────────
    assignments = _match_photos_to_variants(
        saved_photos, variants, cluster_label, brand, upload_folder,
    )

    # Apply Gemini's visual matches first.
    placed_photos: set[int] = set()
    placed_variants: set[int] = set()
    for photo_i, variant_i in assignments.items():
        v = variants[variant_i - 1]
        sku = (v.get("model_number") or "").strip()
        if not sku:
            continue
        if photo_i - 1 >= len(saved_photos):
            continue
        out[sku].append(saved_photos[photo_i - 1])
        placed_photos.add(photo_i - 1)
        placed_variants.add(variant_i - 1)

    # Deterministic backfill for variants Gemini didn't assign — pair each
    # remaining variant with the next unused photo by left-to-right order.
    # When we run out of photos, share the closest already-used photo.
    unused_photo_idxs = [i for i in range(len(saved_photos))
                          if i not in placed_photos]
    for vi, v in enumerate(variants):
        if vi in placed_variants:
            continue
        sku = (v.get("model_number") or "").strip()
        if not sku:
            continue
        if unused_photo_idxs:
            out[sku].append(saved_photos[unused_photo_idxs.pop(0)])
        else:
            # Run out of distinct photos — fall back to the variant's
            # corresponding-index photo (or the first photo if out of
            # range), so the variant still ends up with SOMETHING.
            fallback_idx = vi if vi < len(saved_photos) else 0
            out[sku].append(saved_photos[fallback_idx])
        placed_variants.add(vi)

    # Any remaining unused photos become gallery extras attached to the
    # first variant — they'll show up in additional_images / the picker.
    for idx in unused_photo_idxs:
        first_sku = next(iter(out.keys()))
        out[first_sku].append(saved_photos[idx])

    if log_cb is not None:
        log_cb("info",
               f"Cluster '{cluster_label}': matched {len(assignments)} of "
               f"{len(saved_photos)} photo(s) by visual finish; backfilled "
               f"{len(variants) - len(placed_variants)} variant(s)")

    return out


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


# ── Per-draft processor ─────────────────────────────────────────────────────


def _process_draft(draft: dict, file_paths: list[str], upload_folder: str,
                   slice_dir: str,
                   log_cb: Callable[[str, str], None] | None) -> dict:
    """Run the pipeline for ONE draft. Variant clusters get one slice + one
    extraction call PER variant SKU; singletons get one slice + one call.

    Returns the per-draft bucket in the same shape as the old `route_and_extract`
    result, minus the web/AI fallbacks (which Bulk Import no longer uses).
    """
    bucket: dict = {
        "image_path":    None,
        "variant_paths": {},
        "candidates":    [],
        "status":        "pending",
    }

    primary_pdf = next(
        (fp for fp in file_paths
         if fp and os.path.exists(fp)
         and os.path.splitext(fp)[1].lower() == ".pdf"),
        None,
    )
    # Fall back to the first usable file when no PDF is available.
    primary_src = primary_pdf or next(
        (fp for fp in file_paths if fp and os.path.exists(fp)), None,
    )
    if not primary_src:
        bucket["status"] = "failed"
        return bucket

    kind = (draft.get("kind") or "singleton").lower()
    name = (draft.get("name") or "").strip() or f"draft_{draft.get('id')}"
    brand = (draft.get("brand") or "").strip()
    variants = draft.get("variants") or []

    if kind == "variants" and variants:
        # Build ONE cluster slice covering every variant's pages — proformas
        # frequently merge two variants into one row of the table, so per-
        # variant slicing produces the same slice twice and per-variant
        # routing returns the same first photo each time.
        cluster_pages = sorted({
            p for v in variants
            for p in (v.get("source_pages") or draft.get("source_pages") or [])
            if isinstance(p, int) and p >= 0
        })
        if not cluster_pages:
            cluster_pages = list(draft.get("source_pages") or [])

        cluster_slice = slice_pdf_by_product(
            primary_src, cluster_pages, slice_dir, name,
        )
        cluster_assignments: dict[str, list[str]] = {}
        if cluster_slice and cluster_slice.lower().endswith(".pdf"):
            cluster_assignments = extract_cluster_images(
                cluster_slice, draft, upload_folder, log_cb=log_cb,
            ) or {}
            total = sum(len(v) for v in cluster_assignments.values())
            _emit(log_cb,
                  "ok" if total else "warn",
                  f"Draft #{draft.get('id')} cluster '{name}': "
                  f"{total} image(s) routed across "
                  f"{len(cluster_assignments)} variant(s)")

        # Per-variant fallback path — covers (a) any variant the cluster
        # router didn't fill and (b) image-only uploads where cluster
        # routing was skipped because the slice isn't a PDF.
        for v in variants:
            vsku = (v.get("model_number") or "").strip()
            vlabel = (v.get("label") or "").strip() or vsku or name
            cluster_paths = cluster_assignments.get(vsku) or []
            if cluster_paths:
                bucket["variant_paths"].setdefault(vsku, []).extend(cluster_paths)
                for p in cluster_paths:
                    bucket["candidates"].append({
                        "path":          p,
                        "source":        "document",
                        "variant_sku":   vsku,
                        "matched_label": vlabel,
                        "confidence":    "high",
                    })
                continue

            pages = list(v.get("source_pages") or
                          draft.get("source_pages") or [])
            slice_path = slice_pdf_by_product(
                primary_src, pages, slice_dir, vlabel,
            )
            if not slice_path:
                _emit(log_cb, "warn",
                      f"Draft #{draft.get('id')} variant '{vlabel}': "
                      f"no slice produced (pages={pages})")
                continue
            target_for_extract = vlabel if vlabel else name
            paths = extract_images_from_slice(
                slice_path, target_for_extract, upload_folder,
                variant_sku=vsku, brand=brand,
            )
            if not paths:
                _emit(log_cb, "warn",
                      f"Draft #{draft.get('id')} variant '{vlabel}': "
                      f"no images extracted from slice")
                continue
            if vsku:
                bucket["variant_paths"].setdefault(vsku, []).extend(paths)
            for p in paths:
                bucket["candidates"].append({
                    "path":          p,
                    "source":        "document",
                    "variant_sku":   vsku,
                    "matched_label": vlabel,
                    "confidence":    "high",
                })
            _emit(log_cb, "ok",
                  f"Draft #{draft.get('id')} variant '{vlabel}' "
                  f"(per-variant fallback): {len(paths)} image(s)")
    else:
        pages = list(draft.get("source_pages") or [])
        slice_path = slice_pdf_by_product(
            primary_src, pages, slice_dir, name,
        )
        if slice_path:
            singleton_sku = (draft.get("model_number") or "").strip()
            paths = extract_images_from_slice(
                slice_path, name, upload_folder,
                variant_sku=singleton_sku, brand=brand,
            )
            for p in paths:
                bucket["candidates"].append({
                    "path":          p,
                    "source":        "document",
                    "variant_sku":   "",
                    "matched_label": name,
                    "confidence":    "high",
                })
            _emit(log_cb, "ok" if paths else "warn",
                  f"Draft #{draft.get('id')} '{name}': "
                  f"{len(paths)} image(s) extracted from slice")
        else:
            _emit(log_cb, "warn",
                  f"Draft #{draft.get('id')} '{name}': "
                  f"no slice produced (pages={pages})")

    # Pick a primary thumbnail. Variant clusters: first photo of the first
    # variant that produced one. Singletons: first candidate.
    if kind == "variants" and bucket["variant_paths"]:
        for v in variants:
            vsku = (v.get("model_number") or "").strip()
            paths = bucket["variant_paths"].get(vsku) or []
            if paths:
                bucket["image_path"] = paths[0]
                break
    if not bucket["image_path"] and bucket["candidates"]:
        bucket["image_path"] = bucket["candidates"][0]["path"]

    # Status roll-up.
    if kind == "variants" and variants:
        need = sum(1 for v in variants if (v.get("model_number") or "").strip())
        have = len(bucket["variant_paths"])
        if have == 0 and not bucket["image_path"]:
            bucket["status"] = "failed"
        elif have < need:
            bucket["status"] = "partial"
        else:
            bucket["status"] = "done"
    else:
        bucket["status"] = "done" if bucket["image_path"] else "failed"

    return bucket


# ── Orchestrator ────────────────────────────────────────────────────────────


def run_image_pipeline(
    drafts: list[dict],
    file_paths: list[str],
    upload_folder: str,
    log_cb: Callable[[str, str], None] | None = None,
) -> dict[int, dict]:
    """Slice → Assign → Extract → Save for every draft in the batch.

    Each draft is processed independently in a thread (slicing + extraction
    are I/O bound) so a 5-PIS proforma finishes in roughly the time of a
    single extraction call.

    No web search, no nano-banana — Bulk Import is now strictly doc-only.
    Drafts that can't produce a doc-side image come back with status='failed'
    and the workspace's per-card menu lets the user upload, manually crop,
    or run the existing single-product fallbacks on demand.
    """
    out: dict[int, dict] = {
        d["id"]: {
            "image_path":    None,
            "variant_paths": {},
            "candidates":    [],
            "status":        "pending",
        }
        for d in drafts if d.get("id") is not None
    }
    if not drafts or not file_paths:
        return out

    _emit(log_cb, "info",
          f"Slice-and-extract pipeline starting "
          f"({len(drafts)} draft(s), {len(file_paths)} file(s))")

    # All slices land in a single per-batch tempdir so they can be cleaned
    # up in one go after extraction. The slices themselves are throwaway
    # — only the cropped product images matter.
    with tempfile.TemporaryDirectory(prefix="pis_slices_") as slice_dir:
        with ThreadPoolExecutor(max_workers=4) as pool:
            future_to_id = {
                pool.submit(
                    _process_draft, d, file_paths, upload_folder,
                    slice_dir, log_cb,
                ): d["id"]
                for d in drafts if d.get("id") is not None
            }
            for fut in as_completed(future_to_id):
                did = future_to_id[fut]
                try:
                    out[did] = fut.result()
                except Exception as e:
                    _emit(log_cb, "err",
                          f"Draft #{did}: pipeline crashed ({e})")
                    out[did] = {
                        "image_path":    None,
                        "variant_paths": {},
                        "candidates":    [],
                        "status":        "failed",
                    }

    total_candidates = sum(len(b["candidates"]) for b in out.values())
    drafts_with_image = sum(1 for b in out.values() if b.get("image_path"))
    _emit(log_cb, "ok" if drafts_with_image else "warn",
          f"Pipeline finished: {drafts_with_image}/{len(drafts)} draft(s) "
          f"with image, {total_candidates} candidate(s) total")

    return out


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
