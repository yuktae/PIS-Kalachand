"""
Comparison Table Generator — selector page, AI alignment endpoint, and exports.

Open to every authenticated role (marketing / web / admin / director). The
flow is intentionally simple and read-only:
  1. GET  /compare                — selector page, lists active products
  2. POST /compare/generate       — returns the aligned table JSON for preview
  3. POST /compare/export/xlsx    — streams an .xlsx of the previewed table
  4. POST /compare/export/csv     — streams a .csv of the previewed table

The export endpoints accept the same `table` JSON the preview renders so the
user gets exactly what they see (after any inline label edits on the page).
"""
import io
from datetime import datetime

from flask import (
    Blueprint, session, redirect, url_for, render_template,
    request, jsonify, send_file
)

from model import db, Product
from helpers import get_product_category, get_product_category_label, CATEGORY_UNCATEGORISED
from utils.storage import get_image_url
from utils.compare import align_specs, build_xlsx, build_csv, build_pdf
from extensions import limiter


compare_bp = Blueprint('compare', __name__)

_MAX_PRODUCTS = 10
_MIN_PRODUCTS = 2


def _resolve_image_url(path: str) -> str:
    """Return a browser-loadable URL for a product image.

    `get_image_url` handles Azure SAS signing but leaves local paths
    untouched — e.g. 'uploads/foo.jpg'. Those need the Flask static prefix
    or the browser will resolve them against the current page path (e.g.
    /compare/uploads/foo.jpg → 404). Anything already absolute (http/https)
    passes through unchanged."""
    if not path:
        return ''
    resolved = get_image_url(path)
    if not resolved:
        return ''
    if resolved.startswith('http://') or resolved.startswith('https://'):
        return resolved
    # Local static path — prepend the Flask static URL prefix.
    return url_for('static', filename=resolved.lstrip('/'))


def _require_login():
    if not session.get('role'):
        return redirect(url_for('auth.login'))
    return None


# ── SELECTOR PAGE ────────────────────────────────────────────────────────────

@compare_bp.route('/compare')
def compare_select():
    guard = _require_login()
    if guard:
        return guard

    products = (
        Product.query
        .filter(Product.deleted_at.is_(None))
        .order_by(
            db.func.coalesce(Product.last_edited_at, Product.created_at).desc()
        )
        .all()
    )

    # Lightweight payload for the picker — only what the card needs to render.
    cards = []
    for p in products:
        pis = p.pis_data or {}
        hi = pis.get('header_info') or {}
        cards.append({
            'id': p.id,
            'name': p.model_name or f'Product #{p.id}',
            'brand': hi.get('brand') or '',
            'model_number': hi.get('model_number') or '',
            'category': get_product_category_label(p),
            'image_url': _resolve_image_url(p.image_path),
            'spec_count': len((pis.get('technical_specifications') or {})),
        })

    available_categories = sorted({c['category'] for c in cards} - {CATEGORY_UNCATEGORISED})
    if any(c['category'] == CATEGORY_UNCATEGORISED for c in cards):
        available_categories.append(CATEGORY_UNCATEGORISED)

    return render_template(
        'compare_select.html',
        products=cards,
        available_categories=available_categories,
        uncategorised_label=CATEGORY_UNCATEGORISED,
        min_products=_MIN_PRODUCTS,
        max_products=_MAX_PRODUCTS,
    )


# ── GENERATE ─────────────────────────────────────────────────────────────────

@compare_bp.route('/compare/generate', methods=['POST'])
@limiter.limit('5 per minute')
def compare_generate():
    guard = _require_login()
    if guard:
        return guard

    payload = request.get_json(silent=True) or {}
    ids = payload.get('product_ids') or []
    if not isinstance(ids, list):
        return jsonify({'error': 'product_ids must be a list'}), 400
    # Coerce to ints, drop garbage
    try:
        ids = [int(x) for x in ids]
    except (TypeError, ValueError):
        return jsonify({'error': 'product_ids must contain integers'}), 400

    if len(ids) < _MIN_PRODUCTS:
        return jsonify({'error': f'Select at least {_MIN_PRODUCTS} products.'}), 400
    if len(ids) > _MAX_PRODUCTS:
        return jsonify({'error': f'Maximum {_MAX_PRODUCTS} products per comparison.'}), 400

    # Preserve user selection order
    seen = set()
    ordered_ids = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            ordered_ids.append(i)

    products = (
        Product.query
        .filter(Product.id.in_(ordered_ids), Product.deleted_at.is_(None))
        .all()
    )
    # Re-sort to match the user's click order
    by_id = {p.id: p for p in products}
    products = [by_id[i] for i in ordered_ids if i in by_id]
    if len(products) < _MIN_PRODUCTS:
        return jsonify({'error': 'Some selected products are unavailable.'}), 400

    # Compose a category context for the prompt — helps Gemini reason about
    # what specs to expect. Mixed categories still align, just with less
    # category-specific clustering.
    cats = [get_product_category(p).get('category_1') or '' for p in products]
    unique_cats = [c for c in dict.fromkeys(cats) if c]
    if not unique_cats:
        category_context = 'mixed / uncategorised'
    elif len(unique_cats) == 1:
        category_context = unique_cats[0]
    else:
        category_context = 'mixed: ' + ', '.join(unique_cats[:4])

    table = align_specs(products, category_context=category_context)
    table['mixed_categories'] = len(unique_cats) > 1
    table['category_context'] = category_context
    return jsonify(table)


# ── EXPORTS ──────────────────────────────────────────────────────────────────

def _validate_export_payload(payload):
    """Lightweight shape check — the user may have edited labels client-side
    so we trust whatever they send, but the skeleton must be sane."""
    if not isinstance(payload, dict):
        return None, ('Invalid payload.', 400)
    products = payload.get('products')
    rows = payload.get('rows')
    if not isinstance(products, list) or not isinstance(rows, list):
        return None, ('Payload missing products or rows.', 400)
    if not products or not rows:
        return None, ('Nothing to export.', 400)
    return payload, None


def _filename(ext: str) -> str:
    ts = datetime.now().strftime('%Y%m%d-%H%M')
    return f'comparison-{ts}.{ext}'


@compare_bp.route('/compare/export/xlsx', methods=['POST'])
@limiter.limit('10 per minute')
def compare_export_xlsx():
    guard = _require_login()
    if guard:
        return guard
    payload, err = _validate_export_payload(request.get_json(silent=True))
    if err:
        return jsonify({'error': err[0]}), err[1]
    try:
        data = build_xlsx(payload)
    except Exception as e:
        print(f'⚠️  xlsx build failed: {e}')
        return jsonify({'error': 'Could not generate the Excel file.'}), 500
    return send_file(
        io.BytesIO(data),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=_filename('xlsx'),
    )


@compare_bp.route('/compare/export/csv', methods=['POST'])
@limiter.limit('10 per minute')
def compare_export_csv():
    guard = _require_login()
    if guard:
        return guard
    payload, err = _validate_export_payload(request.get_json(silent=True))
    if err:
        return jsonify({'error': err[0]}), err[1]
    csv_text = build_csv(payload)
    return send_file(
        io.BytesIO(csv_text.encode('utf-8-sig')),  # BOM so Excel opens UTF-8 cleanly
        mimetype='text/csv',
        as_attachment=True,
        download_name=_filename('csv'),
    )


@compare_bp.route('/compare/export/pdf', methods=['POST'])
@limiter.limit('10 per minute')
def compare_export_pdf():
    guard = _require_login()
    if guard:
        return guard
    payload, err = _validate_export_payload(request.get_json(silent=True))
    if err:
        return jsonify({'error': err[0]}), err[1]
    try:
        data = build_pdf(payload)
    except Exception as e:
        print(f'⚠️  pdf build failed: {e}')
        return jsonify({'error': 'Could not generate the PDF.'}), 500
    return send_file(
        io.BytesIO(data),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=_filename('pdf'),
    )
