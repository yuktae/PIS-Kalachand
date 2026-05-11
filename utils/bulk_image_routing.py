"""
Per-card image regeneration helpers for the Bulk Import workspace.

`route_and_extract` was removed in Phase 4; batch extraction now lives in
`utils/image_pipeline.unified_extract`. This module retains only the
per-card menu actions called from `blueprints/marketing.py`:

    • `regenerate_image_via_web(draft, upload_folder, max_results, mode)`
    • `regenerate_image_via_ai(draft, file_paths, upload_folder, variant_sku)`
    • `render_proforma_page(file_path, page_index, upload_folder)`
"""

from __future__ import annotations

import os
import time
from typing import Callable

import fitz  # PyMuPDF
from werkzeug.utils import secure_filename

from .pdf_processing import extract_isolated_product_with_nano_banana


# ── Page render DPI (shared with image_pipeline) ────────────────────────────
_PAGE_RENDER_DPI_MATRIX = fitz.Matrix(2, 2)


# ── Logger plumbing ─────────────────────────────────────────────────────────


_LEVEL_GLYPHS = {"info": "·", "ok": "✓", "warn": "⚠", "err": "✗"}


def _emit(log_cb: Callable[[str, str], None] | None, level: str, msg: str) -> None:
    if log_cb:
        try:
            log_cb(level, msg)
        except Exception:
            pass
    print(f"  {_LEVEL_GLYPHS.get(level, '·')} {msg}")


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
        doc = fitz.open(file_path)  # type: ignore[attr-defined]
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
