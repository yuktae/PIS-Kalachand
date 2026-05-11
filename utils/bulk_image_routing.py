"""
Bulk image routing & extraction (Phase D Image).

Single batch-level pass that routes proforma images to the correct draft
PIS — including per-variant images for variant clusters. The routing
prompt sees:

    1. The list of drafts (id, name, brand, SKU, kind, variants[])
    2. One PDF page rendered as a high-res image at a time

…and returns matches `{draft_id, variant_sku, page, box_2d, confidence}`.
We then crop each match in parallel, snap to table-cell borders, clean
edges, and save under `static/uploads/`. Variant clusters get one image
per SKU stored at `pis_data.variants[i].image_path`; singletons get one
image at `pis_data._image_path`.

Reuses every existing helper in `pdf_processing` / `image_processing`:
    • `_snap_to_cell_border`, `_clean_product_image`, `_is_mostly_solid`
    • `_pad_to_aspect_4_3`, `extract_isolated_product_with_nano_banana`
    • `extract_image_candidates_from_web` (for the no-image fallback)

Public surface:
    • `route_and_extract(drafts, file_paths, upload_folder, log_cb=None)`
    • `regenerate_image_via_web(draft, upload_folder)`
    • `regenerate_image_via_ai(draft, upload_folder)`
    • `crop_from_proforma(draft, page_index, crop_rel, upload_folder)`
"""

from __future__ import annotations

import io
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

import fitz  # PyMuPDF
from PIL import Image
from werkzeug.utils import secure_filename
from google import genai
from google.genai import types

from .pdf_processing import (
    _clean_product_image,
    _is_mostly_solid,
    _pad_to_aspect_4_3,
    _snap_to_cell_border,
    extract_isolated_product_with_nano_banana,
)
from .prompt_manager import get_prompt


# ── Gemini client ───────────────────────────────────────────────────────────
_MODEL = "gemini-2.5-flash"
_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
    return _client


# ── Tunables ────────────────────────────────────────────────────────────────
# Cost knobs — tuned for a typical 5-PIS / 1-page proforma. The page-text
# pre-filter already drops irrelevant pages, so most bulk imports hit the
# vision model exactly once. The caps here are belt-and-braces against
# pathological inputs (50-page catalogs).
_MAX_PAGES_PER_BATCH = 12
_PAGE_RENDER_DPI_MATRIX = fitz.Matrix(2, 2)   # 2× — proven enough for bbox routing
_ROUTE_CONCURRENCY = 4
_CROP_CONCURRENCY = 4

# Quality gate constants — applied AFTER cell-snap and cleanup, BEFORE saving.
# Anything that fails one of these is treated as if the doc-side routing did
# not produce a usable crop, so the draft falls through to web/AI fallbacks
# instead of writing garbage to disk and confusing the user.
_MIN_CROP_PX           = 200    # absolute pixel floor (was 80; bumped because
                                # 80px crops post-cell-snap are basically
                                # always row-edge artefacts, never products)
_MIN_CROP_PAGE_FRAC    = 0.015  # crop must occupy >= 1.5% of the page area —
                                # filters out tiny corner-of-cell garbage
_MAX_ASPECT_RATIO      = 4.0    # reject extreme-aspect bboxes (long thin
                                # rules / vertical strips). Either dimension
                                # may be the longer one.
_MIN_CONFIDENCE_RANK   = {"high": 3, "medium": 2, "low": 1, "": 0}
_MIN_CONFIDENCE        = "low"  # accept the model's own "low" — quality is
                                # still gated by the geometry checks above

_MAX_OUTPUT_EDGE = 1600  # cap saved file size (longest edge), no upscale


# ── Logger plumbing ─────────────────────────────────────────────────────────


_LEVEL_GLYPHS = {"info": "·", "ok": "✓", "warn": "⚠", "err": "✗"}


def _emit(log_cb: Callable[[str, str], None] | None, level: str, msg: str) -> None:
    if log_cb:
        try:
            log_cb(level, msg)
        except Exception:
            pass
    print(f"  {_LEVEL_GLYPHS.get(level, '·')} {msg}")


# ── Routing prompt (text-only fallback when DB has no override) ─────────────
# Kept in code so the system works on a fresh DB; an admin override at
# `bulk_image_routing` in the Prompt table wins when present.
_DEFAULT_ROUTING_PROMPT = """You are routing product images on a supplier proforma to the correct Product Information Sheet (PIS) drafts.

DRAFTS WAITING FOR IMAGES (the "shopping list"):
{drafts_block}

The attached image is page {page_num} of the proforma. For EACH draft above, identify the image(s) on this page that depict THAT specific product. The strongest signal is the printed text label (model number, SKU, product name) directly adjacent to the photo — match by label, not by which photo looks "best".

CRITICAL RULES:
1. For VARIANT clusters (kind=variants), each variant SKU should map to ITS OWN image when distinct photos are present. Use the SKU printed next to each photo to match.
2. For SINGLETON clusters (kind=singleton), return at most ONE primary match per draft. If multiple views (open/closed, front/side) exist for the same singleton, return ONLY the clearest single view — do not duplicate.
3. If a draft has NO matching image on this page, OMIT it from the matches array. Do not invent matches. Do not return a fallback bbox.
4. Bounding boxes must be TIGHT around just the product photo — no surrounding text, no table rules, no model labels. Format: [ymin, xmin, ymax, xmax] on a 0–1000 scale.
5. `confidence` reports how certain you are about the match: "high" when the text label clearly identifies the product, "medium" when the label is partial or implied, "low" when you matched on visual similarity alone.

OUTPUT (strict JSON only — no prose, no markdown):
{{
  "matches": [
    {{
      "draft_id": 123,
      "variant_sku": "EXACT SKU printed near the photo, or empty string for singletons",
      "box_2d": [ymin, xmin, ymax, xmax],
      "matched_label": "the printed text near the image that proved the match",
      "confidence": "high" | "medium" | "low"
    }}
  ]
}}

If no images on this page belong to any of the drafts, return: {{"matches": []}}"""


def _routing_prompt_template() -> str:
    """Return the admin-overridable routing prompt; falls back to the
    embedded default so a fresh DB still works."""
    db_text = get_prompt("bulk_image_routing")
    return db_text or _DEFAULT_ROUTING_PROMPT


# ── Drafts → routing payload ────────────────────────────────────────────────


def _build_drafts_block(drafts: list[dict]) -> tuple[str, dict[str, dict]]:
    """Compose the human-readable drafts block AND a SKU→draft index lookup
    used to map matches back to their draft. The lookup is keyed by uppercased
    stripped SKU so casing/space differences in the proforma don't break it.

    Each entry in `drafts` is a flat metadata dict from the caller:
        {id, name, brand, model_number, kind, variants: [{label, model_number}, ...]}
    """
    lines: list[str] = []
    lookup: dict[str, dict] = {}
    for i, d in enumerate(drafts, 1):
        kind = (d.get("kind") or "singleton").lower()
        name = (d.get("name") or "").strip() or f"Draft #{d.get('id')}"
        brand = (d.get("brand") or "").strip() or "(no brand)"
        primary_sku = (d.get("model_number") or "").strip()
        lines.append(f"DRAFT {i} (id={d['id']}, kind={kind}):")
        lines.append(f"  name:  {name}")
        lines.append(f"  brand: {brand}")
        if kind == "variants":
            variants = d.get("variants") or []
            lines.append(f"  family SKU(s): {primary_sku or '(comma-sep below)'}")
            lines.append("  variants:")
            for v in variants:
                vlabel = (v.get("label") or "").strip() or "(unnamed)"
                vsku = (v.get("model_number") or "").strip()
                lines.append(f"    - {vlabel}  ·  SKU: {vsku or '(none)'}")
                if vsku:
                    lookup.setdefault(vsku.upper(), {"draft": d, "variant_sku": vsku})
        else:
            lines.append(f"  model_number: {primary_sku or '(none)'}")
            if primary_sku:
                lookup.setdefault(primary_sku.upper(), {"draft": d, "variant_sku": ""})
        # Always allow matching by primary SKU even on variant clusters — the
        # proforma sometimes prints only the family SKU on one row.
        if primary_sku and primary_sku.upper() not in lookup:
            lookup[primary_sku.upper()] = {"draft": d, "variant_sku": ""}
        lines.append("")
    return "\n".join(lines).rstrip(), lookup


# ── PDF rendering ───────────────────────────────────────────────────────────


# ── Row detection (Pass A of two-pass routing) ─────────────────────────────


def _sku_lookup_for_drafts(drafts: list[dict]) -> dict[str, tuple[dict, str]]:
    """Build SKU → (draft_dict, variant_sku) lookup. Variant SKUs first so
    they win when the same string appears in both a draft's primary SKU and
    a variant SKU."""
    lookup: dict[str, tuple[dict, str]] = {}
    # Primary SKUs first (lower priority) — variants will overwrite where
    # they appear (variant SKUs are the more specific match).
    for d in drafts:
        sku = (d.get("model_number") or "").strip().upper()
        if sku:
            lookup.setdefault(sku, (d, ""))
    for d in drafts:
        for v in d.get("variants") or []:
            vsku = (v.get("model_number") or "").strip().upper()
            if vsku:
                lookup[vsku] = (d, vsku)
    return lookup


def _find_rows_via_pdf_text(pdf_path: str, page_index: int,
                             drafts: list[dict]
                             ) -> list[dict]:
    """Use PyMuPDF's literal `search_for` to locate the y-band of every
    SKU printed on the page. Returns one row entry per draft+variant whose
    SKU is found:

        [{"draft": draft_dict, "variant_sku": "...",
          "y_top": float_px, "y_bottom": float_px,
          "matched_word": "the SKU that triggered this row"},
         ...]

    Returns [] for scanned PDFs (no extractable text) or when no SKU is
    found. Coordinates are in page-rendered pixels (multiplied by the
    matrix used to render the page), so they line up with the PIL image
    we hand to Pass B.

    `search_for` does literal substring search — much safer than the
    word-tokeniser approach, which would split `XDY60.120060-OAK-W` into
    `XDY60`, `120060`, `OAK`, `W` and then false-match `OAK` (3 letters)
    or `2` against unrelated text on the page.
    """
    sku_lookup = _sku_lookup_for_drafts(drafts)
    if not sku_lookup:
        return []

    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return []
    try:
        if page_index < 0 or page_index >= len(doc):
            return []
        page = doc[page_index]
        scale_y = _PAGE_RENDER_DPI_MATRIX.d   # 2.0
        rows: list[dict] = []
        seen: set[tuple[int, str, float]] = set()  # dedupe (draft.id, vsku, y)

        for sku, (d, vsku) in sku_lookup.items():
            # Skip too-short or combined-primary "SKU lists" — `model_number`
            # for variant clusters is `"X, Y"`-shaped and isn't a real
            # searchable string. The variant entries already cover the
            # individual SKUs.
            if len(sku) < 6 or ',' in sku:
                continue
            try:
                rects = page.search_for(sku) or []
            except Exception:
                rects = []
            for rect in rects:
                # Round y to the nearest 5 px before deduping so the same
                # row found by two slightly-different-cased SKU spellings
                # collapses to one entry.
                y_key = round(rect.y0 / 5) * 5
                key = (d["id"], vsku, y_key)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "draft":         d,
                    "variant_sku":   vsku,
                    "y_top":         rect.y0 * scale_y,
                    "y_bottom":      rect.y1 * scale_y,
                    "matched_word":  sku,
                })
        return rows
    finally:
        doc.close()


def _expand_row_band(rows: list[dict], page_height_px: float) -> list[dict]:
    """Each detected row is just the SKU's text bbox — a thin strip. Expand
    each row vertically so it captures the full table row (the photo above/
    beside the SKU).

    Strategy: half the gap to the next row above and below, capped at 35%
    of the page height. Returns a NEW list with `band_top`/`band_bottom`
    fields added.
    """
    if not rows:
        return []
    sorted_rows = sorted(rows, key=lambda r: r["y_top"])
    out = []
    for i, r in enumerate(sorted_rows):
        prev_y = sorted_rows[i - 1]["y_bottom"] if i > 0 else 0
        next_y = (sorted_rows[i + 1]["y_top"]
                  if i + 1 < len(sorted_rows) else page_height_px)
        # Half the gap above/below → wider band when rows are far apart.
        # Min 80px so a one-row proforma still has room for the photo.
        margin_top = max(80, (r["y_top"] - prev_y) * 0.5)
        margin_bot = max(80, (next_y - r["y_bottom"]) * 0.5)
        # Cap at 35% of page height so a single row can't claim the whole page.
        margin_top = min(margin_top, page_height_px * 0.35)
        margin_bot = min(margin_bot, page_height_px * 0.35)
        out.append({
            **r,
            "band_top":    max(0.0, r["y_top"] - margin_top),
            "band_bottom": min(page_height_px, r["y_bottom"] + margin_bot),
        })
    return out


def _route_within_row(row_pil: Image.Image, row: dict,
                      page_num_for_prompt: int) -> list[dict]:
    """Pass B — send ONE row crop to Gemini with just the draft+variant
    that owns that row. The prompt is much narrower than the full-page
    routing, so accuracy is materially better.

    Returns matches in the SAME format as `_route_one_page`, but the
    bboxes are relative to the ROW crop. The caller must offset them
    back to page coordinates before cropping.
    """
    d = row["draft"]
    sku = row["variant_sku"] or (d.get("model_number") or "")
    name = (d.get("name") or "").strip() or f"Draft #{d['id']}"
    brand = (d.get("brand") or "").strip()

    # Re-use the standard routing prompt with a single-row drafts_block.
    # The prompt already handles the case of one draft / one variant.
    drafts_block = (
        f"DRAFT 1 (id={d['id']}, kind=singleton):\n"
        f"  name:  {name}\n"
        f"  brand: {brand or '(no brand)'}\n"
        f"  model_number: {sku or '(none)'}\n"
    )
    buf = io.BytesIO()
    row_pil.save(buf, "PNG")
    return _route_one_page(page_num_for_prompt, buf.getvalue(), drafts_block)


def _gather_relevant_pages(file_paths: list[str], drafts: list[dict]
                           ) -> list[tuple[str, int, bytes, Image.Image]]:
    """Render proforma pages as PNG bytes + PIL images, filtered by whether
    the page text contains any draft's SKU or distinctive name token.

    For non-PDF uploads (jpg/png), the entire image is returned as page 0.
    Returns: [(file_path, page_index_0based, png_bytes, pil_image), ...]
    Capped at `_MAX_PAGES_PER_BATCH` total across all files.
    """
    # Build a token list to scan page text for. SKUs first (more unique),
    # then any name token >=4 chars (catches catalogs that don't print SKUs).
    tokens: set[str] = set()
    for d in drafts:
        sku = (d.get("model_number") or "").strip().upper()
        if sku:
            tokens.add(sku)
        for v in d.get("variants") or []:
            vsku = (v.get("model_number") or "").strip().upper()
            if vsku:
                tokens.add(vsku)
        for word in (d.get("name") or "").upper().split():
            if len(word) >= 4 and not word.isdigit():
                tokens.add(word)
    out: list[tuple[str, int, bytes, Image.Image]] = []

    for fp in file_paths:
        if not fp or not os.path.exists(fp) or len(out) >= _MAX_PAGES_PER_BATCH:
            continue
        ext = os.path.splitext(fp)[1].lower()
        try:
            if ext in (".jpg", ".jpeg", ".png", ".webp"):
                with open(fp, "rb") as f:
                    raw = f.read()
                pil = Image.open(io.BytesIO(raw)).convert("RGB")
                buf = io.BytesIO()
                pil.save(buf, "PNG")
                out.append((fp, 0, buf.getvalue(), pil))
                continue

            if ext != ".pdf":
                continue

            doc = fitz.open(fp)
            try:
                # First pass: pages whose text mentions a SKU/distinctive name token.
                # Second pass (only if first found nothing): take page 0 — small
                # proformas without selectable text still need routing.
                relevant_pages: list[int] = []
                for pno in range(len(doc)):
                    if len(out) + len(relevant_pages) >= _MAX_PAGES_PER_BATCH:
                        break
                    text = (doc[pno].get_text("text") or "").upper()
                    if any(tok in text for tok in tokens):
                        relevant_pages.append(pno)
                if not relevant_pages and len(doc) > 0:
                    relevant_pages = [0]

                for pno in relevant_pages:
                    if len(out) >= _MAX_PAGES_PER_BATCH:
                        break
                    pix = doc[pno].get_pixmap(matrix=_PAGE_RENDER_DPI_MATRIX)
                    png_bytes = pix.tobytes("png")
                    pil = Image.open(io.BytesIO(png_bytes)).convert("RGB")
                    out.append((fp, pno, png_bytes, pil))
            finally:
                doc.close()
        except Exception as e:
            print(f"  ⚠ Could not render '{fp}': {e}")
            continue

    return out


# ── Single page → matches ───────────────────────────────────────────────────


def _route_one_page(page_num_for_prompt: int, png_bytes: bytes,
                    drafts_block: str) -> list[dict]:
    """Send ONE rendered page to Gemini with the routing prompt. Returns the
    raw `matches` list (validated to dict-shape) — empty on any failure."""
    try:
        prompt = _routing_prompt_template().format(
            page_num=page_num_for_prompt,
            drafts_block=drafts_block,
        )
        resp = _get_client().models.generate_content(
            model=_MODEL,
            contents=[prompt, types.Part.from_bytes(data=png_bytes, mime_type="image/png")],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        from utils.json_utils import safe_json_loads
        parsed = safe_json_loads(resp.text or "", fallback={})
        if not isinstance(parsed, dict):
            return []
        matches = parsed.get("matches") or []
        return [m for m in matches if isinstance(m, dict) and m.get("box_2d")]
    except Exception as e:
        print(f"  ⚠ Routing call failed (page {page_num_for_prompt}): {e}")
        return []


# ── Match → cropped image file ──────────────────────────────────────────────


def _crop_match_to_file(pil_page: Image.Image, match: dict, target_label: str,
                        upload_folder: str
                        ) -> tuple[str | None, str | None]:
    """Crop one bbox match → snap to cell borders → clean → quality gate →
    4:3 pad → save.

    Returns `(relative_path, None)` on success, or `(None, reason_str)` on
    rejection. The reason string is structured so callers can decide whether
    to fall through to web/AI fallbacks (`reason='gate:*'`) or treat the
    rejection as a hard error (`reason='crash:*'`).

    Aspect 4:3 is enforced by padding (never cropping) so every saved image
    fits the PIS thumbnail frame without further work in the template.
    """
    try:
        box = match.get("box_2d") or []
        if len(box) != 4:
            return None, "gate:bad-bbox-shape"

        # AI-supplied confidence — reject too-low matches even when the
        # geometry checks pass. The model uses "low" when it matched on
        # visual similarity alone (no text label), which is exactly the
        # case where bboxes tend to be wrong.
        conf = (match.get("confidence") or "").strip().lower()
        if (_MIN_CONFIDENCE_RANK.get(conf, 0)
                < _MIN_CONFIDENCE_RANK.get(_MIN_CONFIDENCE, 0)):
            return None, f"gate:low-confidence({conf or 'unset'})"

        ymin, xmin, ymax, xmax = box
        w, h = pil_page.size
        left = max(0.0, (xmin / 1000) * w)
        top = max(0.0, (ymin / 1000) * h)
        right = min(float(w), (xmax / 1000) * w)
        bottom = min(float(h), (ymax / 1000) * h)
        bw = right - left
        bh = bottom - top
        if bw < _MIN_CROP_PX or bh < _MIN_CROP_PX:
            return None, f"gate:too-small({int(bw)}x{int(bh)})"

        # 5% pre-pad so cell-snap has wiggle room.
        pad_x = bw * 0.05
        pad_y = bh * 0.05
        left = max(0, left - pad_x)
        top = max(0, top - pad_y)
        right = min(w, right + pad_x)
        bottom = min(h, bottom + pad_y)
        left, top, right, bottom = _snap_to_cell_border(
            pil_page, (left, top, right, bottom)
        )
        crop = pil_page.crop((left, top, right, bottom))
        if crop.mode != "RGB":
            crop = crop.convert("RGB")

        # ── Quality gate (post-snap) ─────────────────────────────────
        cw, ch = crop.size
        if cw < _MIN_CROP_PX or ch < _MIN_CROP_PX:
            return None, f"gate:post-snap-too-small({cw}x{ch})"

        # Reject extreme aspect — long thin strips are almost always
        # table rules or column edges, not products.
        aspect = max(cw, ch) / max(1, min(cw, ch))
        if aspect > _MAX_ASPECT_RATIO:
            return None, f"gate:bad-aspect({aspect:.1f}:1)"

        # Reject crops that are tiny relative to the page — the AI sometimes
        # returns valid-looking bboxes around a single label or icon.
        page_area = w * h
        crop_area = cw * ch
        if page_area > 0 and (crop_area / page_area) < _MIN_CROP_PAGE_FRAC:
            return None, f"gate:tiny-vs-page({crop_area/page_area:.2%})"

        if _is_mostly_solid(crop):
            return None, "gate:mostly-solid"

        crop = _clean_product_image(crop)

        # Re-check size after cleanup (the auto-crop step inside
        # _clean_product_image trims to content bbox and can slim a crop
        # that was already on the edge).
        cw2, ch2 = crop.size
        if cw2 < _MIN_CROP_PX or ch2 < _MIN_CROP_PX:
            return None, f"gate:post-cleanup-too-small({cw2}x{ch2})"

        crop = _pad_to_aspect_4_3(crop)
        # Down-scale only — never upscale.
        if max(crop.size) > _MAX_OUTPUT_EDGE:
            crop.thumbnail((_MAX_OUTPUT_EDGE, _MAX_OUTPUT_EDGE), Image.LANCZOS)

        safe_name = secure_filename(target_label) or "product"
        filename = f"bulk_{safe_name}_{int(time.time() * 1000)}.jpg"
        save_path = os.path.join(upload_folder, filename)
        crop.save(save_path, quality=95)
        return f"uploads/{filename}", None
    except Exception as e:
        print(f"  ⚠ Crop failed for '{target_label}': {e}")
        return None, f"crash:{e}"


# ── High-level: route_and_extract for an entire batch ───────────────────────


def route_and_extract(
    drafts: list[dict],
    file_paths: list[str],
    upload_folder: str,
    log_cb: Callable[[str, str], None] | None = None,
) -> dict[int, dict]:
    """Run the batch image-extraction pipeline.

    `drafts` is a list of metadata dicts, one per workspace card:
        {id, name, brand, model_number, kind, variants: [...]}
    `file_paths` are absolute paths to the proforma file(s).

    Returns a dict keyed by draft id:
        {
            <id>: {
                "image_path":      "uploads/...primary thumbnail" | None,
                "variant_paths":   {SKU: "uploads/..."} for variant clusters,
                "candidates":      [{"path", "source", "variant_sku",
                                      "matched_label", "confidence"}, ...],
                "status":          "done" | "partial" | "failed",
            },
            ...
        }

    Drafts with NO doc-side match get a web-search fallback (one engine,
    one result — not the deep multi-engine search the single wizard runs).
    Drafts that strike out on both fall through to nano-banana generation.

    The pipeline is best-effort throughout — any single failing draft
    won't block the others.
    """
    # `variant_paths` schema:
    #   {SKU: [path, path, ...]} — list, NOT scalar. Many proformas show
    #   multiple views per variant (closed + open wardrobe, front + side,
    #   etc.) and we want to surface ALL of them so the user can pick the
    #   thumbnail and the rest become `additional_images` on the Product
    #   row, which the PIS PDF template renders as a photo gallery.
    out: dict[int, dict] = {
        d["id"]: {
            "image_path": None,
            "variant_paths": {},
            "candidates": [],
            "status": "pending",
        }
        for d in drafts if d.get("id") is not None
    }
    if not drafts:
        return out

    drafts_block, sku_lookup = _build_drafts_block(drafts)
    pages = _gather_relevant_pages(file_paths or [], drafts)
    if log_cb:
        log_cb("info", f"Rendered {len(pages)} relevant page(s) for routing")

    # ── Step 1: routing — two-pass row-aware path with full-page fallback ──
    #
    # For each PDF page, FIRST try the cheap text-based row detector. If it
    # finds row bands for one or more drafts, we crop each row out of the
    # page PIL and run the routing prompt PER ROW with only that row's
    # draft+variant in the prompt. This is materially more accurate than
    # asking the model to find every product on a busy page in one call —
    # especially for tabular layouts like the SUNON proforma (6 rows × 2
    # photo columns × 6 variants = 12 things to match in a single call,
    # bbox precision drops sharply).
    #
    # If row detection finds nothing (scanned PDF, image upload, or none
    # of the SKU tokens appear in the page text), we fall through to the
    # original full-page single-call routing.
    #
    # `page_matches` is the union of both paths — each entry is
    # `(pil_image, matches_list)` where bboxes are 0-1000 relative to that
    # specific PIL image (page or row crop). The downstream crop loop
    # already uses `pil_page.size` for scaling, so it works for both.
    page_matches: list[tuple[Image.Image, list[dict]]] = []
    # Two typed job buckets — split so the type system can keep up and so
    # the dispatch loop doesn't need a sentinel discriminator.
    row_jobs: list[tuple[Image.Image, dict, int]] = []          # (row_pil, row_meta, page_num)
    full_page_jobs: list[tuple[Image.Image, bytes, int]] = []   # (page_pil, png_bytes, page_num)

    for idx, (fp, pno, png, pil) in enumerate(pages):
        rows: list[dict] = []
        if fp.lower().endswith(".pdf"):
            try:
                raw_rows = _find_rows_via_pdf_text(fp, pno, drafts)
                rows = _expand_row_band(raw_rows, page_height_px=float(pil.size[1]))
            except Exception as e:
                print(f"  ⚠ Row detection failed on page {pno}: {e}")
                rows = []

        if rows:
            # Pass A succeeded — crop each row and queue a Pass B routing job.
            for row in rows:
                top = max(0, int(row["band_top"]))
                bot = min(pil.size[1], int(row["band_bottom"]))
                if bot - top < 60:
                    continue
                row_pil = pil.crop((0, top, pil.size[0], bot))
                row_jobs.append((row_pil, row, idx + 1))
        else:
            # Fall back to the original full-page single-call routing.
            full_page_jobs.append((pil, png, idx + 1))

    if log_cb:
        log_cb("info",
               f"Routing plan: {len(row_jobs)} row(s) + "
               f"{len(full_page_jobs)} full-page call(s)")

    if row_jobs or full_page_jobs:
        with ThreadPoolExecutor(max_workers=_ROUTE_CONCURRENCY) as pool:
            future_to_pil: dict = {}
            for row_pil, row_meta, page_num in row_jobs:
                future_to_pil[pool.submit(
                    _route_within_row, row_pil, row_meta, page_num,
                )] = row_pil
            for page_pil, png_bytes, page_num in full_page_jobs:
                future_to_pil[pool.submit(
                    _route_one_page, page_num, png_bytes, drafts_block,
                )] = page_pil
            for fut in as_completed(future_to_pil):
                pil = future_to_pil[fut]
                try:
                    page_matches.append((pil, fut.result() or []))
                except Exception as e:
                    print(f"  ⚠ Routing future raised: {e}")
                    page_matches.append((pil, []))

    total_matches = sum(len(m) for _, m in page_matches)
    if log_cb:
        log_cb("ok" if total_matches else "warn",
               f"Routing returned {total_matches} match(es) across "
               f"{len(page_matches)} call(s)")

    # ── Step 2: crop every match in parallel ─────────────────────────────
    # Group crop jobs (pil_page, match, target_label) so workers can run
    # PIL operations off the routing thread.
    crop_jobs: list[tuple[Image.Image, dict, str, dict]] = []
    for pil, matches in page_matches:
        for m in matches:
            sku_raw = (m.get("variant_sku") or "").strip().upper()
            looked = sku_lookup.get(sku_raw)
            if not looked:
                # Last-chance match by draft_id directly (the model is told to
                # output it, but variant_sku is the more reliable key).
                draft_id = m.get("draft_id")
                if draft_id is not None:
                    for d in drafts:
                        if d.get("id") == draft_id:
                            looked = {"draft": d, "variant_sku": ""}
                            break
            if not looked:
                continue
            d = looked["draft"]
            target_label = (d.get("name") or "").strip() or f"draft_{d['id']}"
            crop_jobs.append((pil, m, target_label, looked))

    # Track gate-rejection counts per draft so the orchestrator log explains
    # WHY a draft fell through to the web/AI fallback. (Without this the
    # logs just say "Web fallback for 3 draft(s) without doc match" with no
    # indication of whether routing got bboxes that were filtered out.)
    rejected_per_draft: dict[int, list[str]] = {}

    if crop_jobs:
        with ThreadPoolExecutor(max_workers=_CROP_CONCURRENCY) as pool:
            future_to_job = {
                pool.submit(_crop_match_to_file, pil, m, label, upload_folder): (m, looked)
                for pil, m, label, looked in crop_jobs
            }
            for fut in as_completed(future_to_job):
                m, looked = future_to_job[fut]
                d = looked["draft"]
                bucket = out.get(d["id"])
                try:
                    rel, reason = fut.result()
                except Exception as e:
                    print(f"  ⚠ Crop worker failed: {e}")
                    if bucket is not None:
                        rejected_per_draft.setdefault(d["id"], []).append(f"crash:{e}")
                    continue
                if rel is None:
                    # Gate rejection — track it so we can log a useful
                    # explanation when this draft falls through to web/AI.
                    if bucket is not None and reason:
                        rejected_per_draft.setdefault(d["id"], []).append(reason)
                    continue
                if bucket is None:
                    continue
                variant_sku = (m.get("variant_sku") or "").strip()
                bucket["candidates"].append({
                    "path": rel,
                    "source": "document",
                    "variant_sku": variant_sku,
                    "matched_label": (m.get("matched_label") or "").strip(),
                    "confidence": (m.get("confidence") or "").strip().lower(),
                })
                # Map to per-variant slot when SKU matches; otherwise primary.
                # Multiple photos per variant are kept (closed + open views,
                # etc.) — the first is the variant's primary thumbnail, the
                # rest land in additional_images for the PDF gallery.
                kind = (d.get("kind") or "singleton").lower()
                if kind == "variants" and variant_sku:
                    bucket["variant_paths"].setdefault(variant_sku, []).append(rel)
                if not bucket["image_path"]:
                    bucket["image_path"] = rel

    # Surface gate rejections in the log so the workspace user sees WHY
    # their card landed on a web/AI image instead of the doc bbox.
    if log_cb:
        for did, reasons in rejected_per_draft.items():
            bucket = out.get(did)
            if bucket and bucket.get("image_path"):
                # Doc-side still landed something (a different match for
                # this draft passed the gate) — no need to nag.
                continue
            log_cb("warn", f"Draft #{did}: {len(reasons)} doc bbox(es) "
                           f"rejected by quality gate "
                           f"({', '.join(reasons[:3])}"
                           f"{', …' if len(reasons) > 3 else ''}) — "
                           f"falling through to web/AI fallback")

    # ── Step 3: variant-aware promotion (first variant's first photo
    #            becomes the primary thumbnail) ───────────────────────────
    for d in drafts:
        bucket = out.get(d.get("id"))
        if not bucket:
            continue
        kind = (d.get("kind") or "singleton").lower()
        if kind == "variants" and bucket["variant_paths"]:
            variants = d.get("variants") or []
            if variants:
                first_sku = (variants[0].get("model_number") or "").strip()
                primary_paths = bucket["variant_paths"].get(first_sku) or []
                if primary_paths:
                    bucket["image_path"] = primary_paths[0]

    # ── Step 4: web fallback for drafts with NO doc match ────────────────
    fallback_targets = [d for d in drafts if not (out.get(d.get("id")) or {}).get("image_path")]
    if fallback_targets and log_cb:
        log_cb("info", f"Web fallback for {len(fallback_targets)} draft(s) without doc match")

    # Late import — single_wizard pulls Playwright transitively.
    if fallback_targets:
        from utils.single_wizard import extract_image_candidates_from_web
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {}
            for d in fallback_targets:
                target = (d.get("name") or "").strip()
                brand = (d.get("brand") or "").strip()
                if not target:
                    continue
                futures[pool.submit(
                    extract_image_candidates_from_web,
                    model_name=target, supplier_url=None,
                    upload_folder=upload_folder, brand=brand or None,
                    max_results=2, log_cb=None,
                )] = d
            for fut in as_completed(futures):
                d = futures[fut]
                try:
                    candidates = fut.result() or []
                except Exception as e:
                    print(f"  ⚠ Web fallback for draft {d.get('id')} failed: {e}")
                    candidates = []
                bucket = out.get(d["id"])
                if not bucket:
                    continue
                for c in candidates:
                    bucket["candidates"].append({
                        "path": c.get("path"),
                        "source": "web",
                        "page_url": c.get("page_url"),
                        "variant_sku": "",
                        "matched_label": "",
                        "confidence": "medium",
                    })
                if candidates and not bucket["image_path"]:
                    bucket["image_path"] = candidates[0].get("path")

    # ── Step 5: AI fallback (nano-banana) for the remaining empties ──────
    still_empty = [d for d in drafts if not (out.get(d.get("id")) or {}).get("image_path")]
    if still_empty and log_cb:
        log_cb("info", f"AI fallback (nano-banana) for {len(still_empty)} draft(s)")

    if still_empty and file_paths:
        # nano-banana is sequential per call (~5–8 s each); cap concurrency
        # at 2 to avoid burning the Gemini per-minute quota.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {}
            for d in still_empty:
                target = (d.get("name") or "").strip() or f"draft_{d['id']}"
                brand = (d.get("brand") or "").strip()
                primary_sku = (d.get("model_number") or "").strip()
                # Variant clusters: use first variant's SKU + label so the
                # prompt has enough context to disambiguate which row's
                # product to isolate (vs the whole catalog page).
                variant_label = ""
                use_sku = primary_sku
                if (d.get("kind") or "").lower() == "variants":
                    vs = d.get("variants") or []
                    if vs:
                        use_sku = (vs[0].get("model_number") or "").strip() or primary_sku
                        variant_label = (vs[0].get("label") or "").strip()
                # Use the first proforma file as the source — nano-banana
                # will lift the matching product photo from it.
                futures[pool.submit(
                    extract_isolated_product_with_nano_banana,
                    file_paths[0], target, upload_folder,
                    brand=brand or None,
                    sku=use_sku or None,
                    variant_label=variant_label or None,
                    color=variant_label or None,   # variant label ≈ color/finish
                )] = d
            for fut in as_completed(futures):
                d = futures[fut]
                try:
                    rel = fut.result()
                except Exception as e:
                    print(f"  ⚠ AI fallback for draft {d.get('id')} failed: {e}")
                    rel = None
                if not rel:
                    continue
                bucket = out.get(d["id"])
                if bucket is None:
                    continue
                bucket["candidates"].append({
                    "path": rel,
                    "source": "ai",
                    "variant_sku": "",
                    "matched_label": "",
                    "confidence": "medium",
                })
                bucket["image_path"] = rel

    # ── Step 6: final status per draft ───────────────────────────────────
    for d in drafts:
        bucket = out.get(d.get("id"))
        if not bucket:
            continue
        kind = (d.get("kind") or "singleton").lower()
        variants = d.get("variants") or []
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

    return out


# ── Per-draft regeneration helpers (used by per-card menu actions) ─────────


def regenerate_image_via_web(draft: dict, upload_folder: str,
                              max_results: int = 3,
                              mode: str = "general") -> list[dict]:
    """Run the same web-image pipeline the single wizard uses, scoped to
    one draft. Returns the candidate list (the caller decides how to
    surface them in the picker).

    `mode` selects the query strategy:
      • "general"  — broad search, prepends the brand to the model name so
                     SERPs land on the right product across reseller
                     sites. Best for products that have wide web presence.
      • "supplier" — locks the search to the brand's official domain via
                     `site:` (when known). Best for internal SKUs that
                     only appear on the manufacturer's catalog.

    For variant clusters: appends the first variant's label/color so the
    query targets a specific finish ("SUNON 4D Wardrobe oak warm white")
    rather than the generic family name.
    """
    from utils.single_wizard import extract_image_candidates_from_web
    from utils.image_processing import resolve_brand_domain

    target = (draft.get("name") or "").strip()
    brand = (draft.get("brand") or "").strip()
    if not target:
        return []

    # Variant clusters: enrich the target with the first variant's label
    # so we don't search for the generic family name (which returns 6
    # different photos none of which is the user's specific finish).
    variants = draft.get("variants") or []
    if (draft.get("kind") or "").lower() == "variants" and variants:
        v_label = (variants[0].get("label") or "").strip()
        if v_label and v_label.lower() not in target.lower():
            target = f"{target} {v_label}"

    # For supplier-only mode, fake a supplier_url under the brand's
    # official domain so the URL-scrape pass is bypassed and the search
    # engines are biased toward that domain. This is the cleanest way to
    # signal "supplier-only" without changing the core pipeline.
    supplier_hint = None
    if mode == "supplier":
        domain = resolve_brand_domain(brand)
        if domain:
            supplier_hint = f"https://{domain}/"

    try:
        return extract_image_candidates_from_web(
            model_name=target, supplier_url=supplier_hint,
            upload_folder=upload_folder, brand=brand or None,
            max_results=max_results, log_cb=None,
        ) or []
    except Exception as e:
        print(f"⚠ regenerate_image_via_web({target!r}, mode={mode}): {e}")
        return []


def regenerate_image_via_ai(draft: dict, file_paths: list[str],
                             upload_folder: str,
                             variant_sku: str | None = None) -> str | None:
    """Re-run nano-banana on the proforma source for one draft, returning
    the relative `uploads/...` path on success.

    Plumbs FULL product context into the prompt — brand, SKU, variant
    label, dimensions, color, description — so the model can disambiguate
    when the proforma contains multiple visually-similar products. Without
    these the model frequently picks the wrong row's product.

    `variant_sku` (optional) targets ONE specific variant within a variant
    cluster. When set, the prompt focuses on that variant's color/label
    (so e.g. "isolate the OAK/WARM WHITE 2D wardrobe", not "any 2D wardrobe").
    """
    target = (draft.get("name") or "").strip() or f"draft_{draft.get('id')}"
    if not file_paths:
        return None

    brand = (draft.get("brand") or "").strip()
    primary_sku = (draft.get("model_number") or "").strip()
    variant_label_str = ""
    color_str = ""

    # Pick the right SKU + variant context. For variant clusters, prefer
    # the per-variant SKU (most specific). For singletons, the primary
    # model_number IS the SKU.
    use_sku = primary_sku
    variants = draft.get("variants") or []
    if variant_sku:
        for v in variants:
            if (v.get("model_number") or "").strip() == variant_sku:
                use_sku = variant_sku
                variant_label_str = (v.get("label") or "").strip()
                # The variant label often encodes the color/finish
                # ("4D WARDROBE-OAK/WARM WHITE"), so reuse it as the
                # color hint when no separate color field exists.
                color_str = variant_label_str
                break
    elif (draft.get("kind") or "").lower() == "variants" and variants:
        # Variant cluster but no specific SKU — use the first variant
        # so the prompt at least picks ONE concrete product.
        use_sku = (variants[0].get("model_number") or "").strip() or primary_sku
        variant_label_str = (variants[0].get("label") or "").strip()
        color_str = variant_label_str

    try:
        return extract_isolated_product_with_nano_banana(
            file_paths[0], target, upload_folder,
            brand=brand or None,
            sku=use_sku or None,
            variant_label=variant_label_str or None,
            color=color_str or None,
        )
    except Exception as e:
        print(f"⚠ regenerate_image_via_ai({target!r}): {e}")
        return None


def render_proforma_page(file_path: str, page_index: int,
                          upload_folder: str) -> str | None:
    """Render ONE page of the proforma (or return the original image if the
    upload was already an image) as a static-servable PNG so the workspace
    can show it inside a manual-crop modal. Returns the relative
    `uploads/...` path or None.
    """
    if not file_path or not os.path.exists(file_path):
        return None
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            return f"uploads/{os.path.basename(file_path)}"
        if ext != ".pdf":
            return None
        doc = fitz.open(file_path)
        try:
            pno = max(0, min(page_index, len(doc) - 1))
            pix = doc[pno].get_pixmap(matrix=_PAGE_RENDER_DPI_MATRIX)
            png_bytes = pix.tobytes("png")
        finally:
            doc.close()
        stem = os.path.splitext(os.path.basename(file_path))[0]
        safe_stem = secure_filename(stem) or "proforma"
        filename = f"bulkpage_{safe_stem}_p{pno}_{int(time.time())}.png"
        save_path = os.path.join(upload_folder, filename)
        with open(save_path, "wb") as f:
            f.write(png_bytes)
        return f"uploads/{filename}"
    except Exception as e:
        print(f"⚠ render_proforma_page failed: {e}")
        return None
