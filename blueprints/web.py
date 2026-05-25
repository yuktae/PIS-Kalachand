"""
Web blueprint — web team dashboard, SpecSheet creation and downloads.
"""
import os
import csv
import io
import re
import json
import copy
import time
from typing import Iterator

from flask import (
    Blueprint, session, redirect, url_for, render_template,
    request, Response, stream_with_context, current_app, jsonify
)
from werkzeug.utils import secure_filename
from sqlalchemy.orm.attributes import flag_modified

from model import db, Product, ProductVersion
from helpers import (
    get_current_username, save_version_snapshot,
    _diff_and_log_changes,
    get_forbidden_words_for_category,
    get_product_category, set_product_category,
    get_product_category_label, CATEGORY_UNCATEGORISED,
)


def _format_hits_summary(hits):
    """Compact human-readable summary of a scrub-hits dict for the history
    log. Example: `{"experience": 3, "discover": 1}` → "experience×3, discover×1"."""
    if not hits:
        return ''
    pairs = sorted(hits.items(), key=lambda kv: kv[1], reverse=True)
    return ', '.join(f'{w}×{c}' for w, c in pairs[:8])
from utils.decorators import require_role
from utils.workflow import Stage
from utils.history import log_event
from utils.ai_generation import generate_comprehensive_spec_data, regenerate_seo_only
from extensions import limiter

web_bp = Blueprint('web', __name__)


# ── DASHBOARDS ────────────────────────────────────────────────────────────────

@web_bp.route('/dashboard/web')
@require_role('web')
def dashboard_web():
    # Order by `last_edited_at` — bumped on every UPDATE so autosaves,
    # SpecSheet edits, director approvals, and stage transitions all
    # surface the task to the top. Fallback to created_at for any row
    # where the column is NULL.
    tasks = (
        Product.query
        .filter(Product.workflow_stage.in_([
            Stage.READY_FOR_WEB, Stage.WEB_CHANGES_REQUESTED,
            Stage.SPECSHEET_DRAFT, Stage.PENDING_DIRECTOR_SPEC, Stage.FINALIZED
        ]))
        .filter(Product.deleted_at.is_(None))
        .order_by(
            db.func.coalesce(Product.last_edited_at, Product.created_at).desc()
        )
        .all()
    )

    products_json = [{
        "id": p.id,
        "model_name": p.model_name or "",
        "brand": p.pis_data.get("header_info", {}).get("brand", "Unknown") if p.pis_data else "Unknown",
        "image": _image_url(p),
        "date": p.created_at.strftime("%d %b"),
        "stage": p.workflow_stage,
        "category": get_product_category_label(p),
        "action_url": url_for("web.create_specsheet", product_id=p.id)
    } for p in tasks]

    metrics = {
        "total_tasks": len(tasks),
        "new_specsheets":     sum(1 for p in tasks if p.workflow_stage == Stage.READY_FOR_WEB),
        "changes_requested":  sum(1 for p in tasks if p.workflow_stage == Stage.WEB_CHANGES_REQUESTED),
        "need_review":        sum(1 for p in tasks if p.workflow_stage == Stage.PENDING_DIRECTOR_SPEC),
        "approved":           sum(1 for p in tasks if p.workflow_stage == Stage.FINALIZED),
        "in_process":         sum(1 for p in tasks if p.workflow_stage == Stage.SPECSHEET_DRAFT),
    }

    # Filter dropdown options — only categories actually present in the
    # current task list. "Uncategorised" pinned last (alphabetical otherwise).
    available_categories = sorted({p["category"] for p in products_json} - {CATEGORY_UNCATEGORISED})
    if any(p["category"] == CATEGORY_UNCATEGORISED for p in products_json):
        available_categories.append(CATEGORY_UNCATEGORISED)

    return render_template(
        "dashboard_web.html",
        tasks=tasks, products_json=products_json, metrics=metrics,
        available_categories=available_categories,
        uncategorised_label=CATEGORY_UNCATEGORISED,
    )


@web_bp.route('/dashboard/web/archive')
@require_role('web')
def web_archive():
    from helpers import get_archive_rows
    approved_stages = [Stage.FINALIZED, Stage.READY_FOR_WEB, Stage.SPECSHEET_DRAFT, Stage.PENDING_DIRECTOR_SPEC, Stage.WEB_CHANGES_REQUESTED]
    archive_rows = get_archive_rows(approved_stages)
    return render_template('archive_web.html', archive_rows=archive_rows)


@web_bp.route('/dashboard/web/forbidden-words')
@require_role('web')
def web_forbidden_words():
    # The Magento category tree is fetched client-side via
    # /api/magento_categories so the page paints instantly and the user
    # sees a spinner in the categories area instead of a blank tab.
    return render_template('forbidden_words.html')


# ── SPECSHEET CREATION ────────────────────────────────────────────────────────

@web_bp.route('/create_specsheet/<int:product_id>', methods=['GET', 'POST'])
def create_specsheet(product_id):
    product = Product.query.get_or_404(product_id)

    if not product.spec_data:
        product.spec_data = {
            'header_info': product.pis_data.get('header_info', {}),
            'customer_friendly_description': product.pis_data.get('seo_data', {}).get('seo_long_description', ''),
            'key_features': product.pis_data.get('sales_arguments', []),
            'internal_web_keywords': product.pis_data.get('seo_data', {}).get('generated_keywords', ''),
            'seo': {
                'meta_title': product.pis_data.get('seo_data', {}).get('meta_title', ''),
                'meta_description': product.pis_data.get('seo_data', {}).get('meta_description', ''),
                'keywords': product.pis_data.get('seo_data', {}).get('generated_keywords', '')
            }
        }
        db.session.commit()

    if not isinstance(product.spec_data.get("key_features"), list):
        product.spec_data["key_features"] = []

    if request.method == 'POST':
        action = request.form.get('action')
        last_version = ProductVersion.query.filter_by(product_id=product.id).order_by(ProductVersion.version_num.desc()).first()
        old_spec = copy.deepcopy(last_version.spec_data) if last_version and last_version.spec_data else {}
        old_pis  = copy.deepcopy(last_version.pis_data)  if last_version and last_version.pis_data  else {}
        spec_data = product.spec_data or {}

        if 'header_info' not in spec_data: spec_data['header_info'] = {}
        if 'header_info' not in product.pis_data: product.pis_data['header_info'] = {}
        h_info = {
            'product_name':  request.form.get('product_name'),
            'model_number':  request.form.get('model_number'),
            'brand':         request.form.get('brand'),
            'price_estimate':request.form.get('price_estimate')
        }
        spec_data['header_info']         = h_info
        product.pis_data['header_info']  = h_info

        spec_data['customer_friendly_description'] = request.form.get('customer_friendly_description')
        features_raw = request.form.getlist('key_features')
        spec_data['key_features'] = [f.strip() for f in features_raw if f.strip()]

        if 'seo' not in spec_data: spec_data['seo'] = {}
        spec_data['seo']['meta_title']       = request.form.get('seo_meta_title')
        spec_data['seo']['meta_description'] = request.form.get('seo_meta_description')
        spec_data['seo']['keywords']         = request.form.get('seo_keywords')
        spec_data['internal_web_keywords']   = request.form.get('internal_web_keywords')

        cat1 = request.form.get('category_1', '')
        cat2 = request.form.get('category_2', '')
        cat3 = request.form.get('category_3', '')
        if cat1 == '__custom__': cat1 = request.form.get('category_1_custom', '').strip()
        if cat2 == '__custom__': cat2 = request.form.get('category_2_custom', '').strip()
        if cat3 == '__custom__': cat3 = request.form.get('category_3_custom', '').strip()
        if cat1:
            # Canonical write — the helper updates Product.category_1/2/3 and
            # mirrors to spec_data.categories so legacy readers keep working.
            product.spec_data = spec_data
            set_product_category(product, cat1, cat2, cat3)
            spec_data = product.spec_data  # re-read after mirror

        tech_specs_json = request.form.get('technical_specifications')
        if tech_specs_json:
            try:
                spec_data['technical_specifications'] = json.loads(tech_specs_json)
            except Exception:
                spec_data['technical_specifications'] = product.pis_data.get('technical_specifications', {})

        warranty_period   = request.form.get('warranty_period')
        warranty_coverage = request.form.get('warranty_coverage')
        if warranty_period is not None or warranty_coverage is not None:
            for d in (spec_data, product.pis_data):
                d.setdefault('warranty_service', {})
                d['warranty_service']['period']   = warranty_period
                d['warranty_service']['coverage'] = warranty_coverage

        product.spec_data = spec_data
        flag_modified(product, 'spec_data')
        flag_modified(product, 'pis_data')

        is_major = action == 'submit_director'
        if action == 'submit_director':
            product.workflow_stage = Stage.PENDING_DIRECTOR_SPEC
            save_version_snapshot(product, label='SpecSheet sent for review', is_major=True)
            log_event(product.id, get_current_username(), 'SpecSheet Sent for Review',
                      'The specsheet has been submitted to the Director for final review.', 'waiting')
        else:
            if product.workflow_stage == Stage.READY_FOR_WEB:
                product.workflow_stage = Stage.SPECSHEET_DRAFT
            save_version_snapshot(product, label='SpecSheet draft saved', is_major=False)
            log_event(product.id, get_current_username(), 'SpecSheet Draft Saved',
                      'Changes to the specsheet have been saved as a draft.', 'neutral')

        _diff_and_log_changes(product.id, old_spec, spec_data, prefix='spec_data')
        _diff_and_log_changes(product.id, old_pis, product.pis_data, prefix='pis_data')
        db.session.commit()
        return redirect(url_for('web.dashboard_web'))

    return render_template('edit_specsheet.html', product=product, spec_data=product.spec_data or {})


# ── DOWNLOADS ─────────────────────────────────────────────────────────────────

@web_bp.route('/preview_specsheet_html/<int:product_id>')
def preview_specsheet_html(product_id):
    """Inline HTML preview of the SpecSheet using the same `specsheet_pdf.html`
    template the PDF download uses. The Edit SpecSheet page embeds this in an
    iframe and cache-busts it after each Save Draft so reviewers see exactly
    what the printed PDF will look like — without spinning up Playwright."""
    from datetime import datetime
    product = Product.query.get_or_404(product_id)
    all_images_b64 = _load_images_b64(product)
    return render_template(
        'specsheet_pdf.html',
        data=product.pis_data,
        spec_data=product.spec_data or {},
        product=product,
        all_images_b64=all_images_b64,
        date_generated=datetime.now().strftime("%Y-%m-%d"),
    )


@web_bp.route('/download_specsheet/<int:product_id>')
def download_specsheet(product_id):
    import base64
    from playwright.sync_api import sync_playwright
    from datetime import datetime
    product = Product.query.get_or_404(product_id)
    all_images_b64 = _load_images_b64(product)
    date_generated = datetime.now().strftime("%Y-%m-%d")
    html = render_template('specsheet_pdf.html',
                           data=product.pis_data, spec_data=product.spec_data or {},
                           product=product, all_images_b64=all_images_b64,
                           date_generated=date_generated)

    # Chromium-native footer: rendered on EVERY physical PDF page after
    # content is laid out, so it can't be overlapped by overflowing
    # tech-spec rows the way the old absolute-positioned <footer> was.
    # Uses Chromium's special <span class="pageNumber"> / "totalPages"
    # placeholders so multi-page exports get correct page numbering.
    footer_template = f"""
    <div style="font-family: 'Inter', sans-serif; font-size: 8px;
                width: 100%; padding: 6px 14mm 0 14mm; box-sizing: border-box;
                background: #1e293b; color: #94a3b8;
                display: flex; justify-content: space-between; align-items: center;
                -webkit-print-color-adjust: exact; print-color-adjust: exact;">
        <div>
            <span style="color: #ffffff; font-weight: 700; letter-spacing: 1px;">J. KALACHAND</span>
            &nbsp;|&nbsp; SPECSHEET
        </div>
        <div>
            REF: SPEC-{product.id} &nbsp;|&nbsp; GENERATED ON {date_generated}
            &nbsp;|&nbsp; PAGE <span class="pageNumber"></span> / <span class="totalPages"></span>
        </div>
    </div>
    """
    # Empty header — Playwright requires `header_template` when
    # `display_header_footer` is on, but we don't want a top banner.
    header_template = "<div></div>"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage']
            )
            page = browser.new_page()
            page.set_content(html, wait_until='networkidle')
            page.wait_for_timeout(1500)
            pdf_bytes = page.pdf(
                format="A4", print_background=True,
                display_header_footer=True,
                header_template=header_template,
                footer_template=footer_template,
                # Margins match the PIS route. Bottom = 22mm reserves room
                # for the dark footer band without overlapping content.
                margin={"top": "18mm", "right": "14mm", "bottom": "22mm", "left": "14mm"},
            )
            browser.close()
        return Response(pdf_bytes, mimetype='application/pdf',
                        headers={"Content-Disposition": f"attachment;filename=SpecSheet_{secure_filename(product.model_name)}.pdf"})
    except Exception as e:
        return f"Error generating PDF: {e}"


SPEC_KEY_ABBR = {
    'brand': 'BRD', 'processor': 'PRC', 'resolution': 'RES', 'ram': 'RAM',
    'memory': 'MEM', 'dimensions': 'DMS', 'weight': 'WGT', 'ports': 'POR',
    'wireless': 'WLA', 'wifi': 'WLA', 'color': 'CLR', 'colour': 'CLR',
    'graphics': 'GPC', 'camera': 'CAM', 'operating system': 'OGS', 'os': 'OGS',
    'storage': 'SRA', 'guarantee': 'GUA', 'warranty': 'GUA', 'origin': 'ORG',
    'display': 'DPL', 'battery': 'BAT', 'bluetooth': 'BLU', 'usb': 'UTH',
    'sim': 'SIM', 'screen size': 'SCR', 'material': 'MAT', 'capacity': 'CAP',
    'power': 'PWR', 'voltage': 'VLT', 'frequency': 'FRQ', 'connectivity': 'CON',
    'audio': 'AUD', 'microphone': 'MIC', 'sensor': 'SNR', 'gps': 'GPS',
    'nfc': 'NFC', 'water resistance': 'WTR', 'refresh rate': 'RFR',
}


def _abbreviate_spec_key(key):
    k_lower = key.strip().lower()
    if k_lower in SPEC_KEY_ABBR:
        return SPEC_KEY_ABBR[k_lower]
    for phrase, abbr in SPEC_KEY_ABBR.items():
        if phrase in k_lower or k_lower in phrase:
            return abbr
    return key.strip()[:3].upper()


@web_bp.route('/download_specsheet_csv/<int:product_id>')
def download_specsheet_csv(product_id):
    product = Product.query.get_or_404(product_id)
    pis  = product.pis_data  or {}
    spec = product.spec_data or {}
    header  = spec.get('header_info') or pis.get('header_info') or {}
    seo     = spec.get('seo') or {}
    cats    = spec.get('categories') or {}
    warranty = pis.get('warranty_service') or spec.get('warranty_service') or {}
    tech_specs = spec.get('technical_specifications') or pis.get('technical_specifications') or {}

    sku          = header.get('model_number', product.model_name or '')
    product_name = header.get('product_name', product.model_name or '')
    cat1 = cats.get('category_1', '')
    cat2 = cats.get('category_2', '')
    cat3 = cats.get('category_3', '')
    category_parts = []
    if cat1:
        category_parts.append(f"Category/{cat1}")
        if cat2:
            category_parts.append(f"Category/{cat1}/{cat2}")
            if cat3:
                category_parts.append(f"Category/{cat1}/{cat2}/{cat3}")

    features = spec.get('key_features') or pis.get('sales_arguments') or []
    description = f'<ul>{"".join(f"<li>{f}</li>" for f in features if f)}</ul>' if features else ''

    raw_price = str(header.get('price_estimate', ''))
    price = re.sub(r'[^\d.]', '', raw_price) or '0'

    attr_parts = []
    brand = header.get('brand', '')
    if brand: attr_parts.append(f"BRD={brand}")
    for key, val in tech_specs.items():
        if not key or not val: continue
        abbr = _abbreviate_spec_key(key)
        if abbr == 'BRD' and brand: continue
        attr_parts.append(f"{abbr}={val}")
    if warranty.get('period') and not any(p.startswith('GUA=') for p in attr_parts):
        attr_parts.append(f"GUA={warranty['period']}")

    short_desc = spec.get('customer_friendly_description') or spec.get('refined_description') or pis.get('range_overview') or ''
    url_key = f"{product_name}~{sku}".replace(' ', '~')

    csv_columns = [
        'sku', 'attribute_set_code', 'product_type', 'categories', 'product_websites',
        'name', 'description', 'is_in_stock', 'weight', 'product_online',
        'tax_class_name', 'visibility', 'price', 'display_product_options_in',
        'additional_attributes', 'qty', 'url_key', 'short_description',
        'meta_title', 'meta_description'
    ]
    row = {
        'sku': sku, 'attribute_set_code': cat3 or cat2 or cat1 or 'Default',
        'product_type': 'simple', 'categories': ','.join(category_parts),
        'product_websites': 'base', 'name': product_name, 'description': description,
        'is_in_stock': '1', 'weight': '1', 'product_online': '1',
        'tax_class_name': 'Taxable Goods', 'visibility': 'Catalog, Search',
        'price': price, 'display_product_options_in': 'Block after Info Column',
        'additional_attributes': ','.join(attr_parts), 'qty': '0', 'url_key': url_key,
        'short_description': short_desc,
        'meta_title': seo.get('meta_title', ''), 'meta_description': seo.get('meta_description', ''),
    }

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=csv_columns, delimiter=';',
                            quoting=csv.QUOTE_MINIMAL, lineterminator='\r\n')
    writer.writeheader()
    writer.writerow(row)
    csv_content = output.getvalue()
    output.close()
    return Response(csv_content, mimetype='text/csv',
                    headers={"Content-Disposition": f"attachment; filename=SpecSheet_{secure_filename(product.model_name)}.csv"})


# ── AI REGENERATION ───────────────────────────────────────────────────────────

@web_bp.route('/api/product/<int:product_id>/regenerate_seo', methods=['POST'])
@limiter.limit("10 per minute")
@require_role('web', api=True)
def api_regenerate_seo(product_id):
    """Regenerate ONLY the SEO metadata (meta_title, meta_description, keywords)
    for an existing SpecSheet — leaves customer_friendly_description, key_features,
    and categories untouched. Used by the "Regenerate SEO" button in the editor.

    Returns the freshly generated SEO block as JSON so the frontend can drop it
    straight into the form fields without a full page reload. The DB is also
    updated so the live-preview iframe picks up the change.
    """
    product = Product.query.get_or_404(product_id)

    spec_data = product.spec_data or {}
    # Apply the same per-category forbidden-word guard the full SpecSheet
    # generator uses so the regenerated SEO block can't reintroduce blocked
    # terms (issue #5 from the redesign audit).
    cat_3 = get_product_category(product)['category_3'] or None
    fw_entries = get_forbidden_words_for_category(cat_3)

    new_seo = regenerate_seo_only(product.pis_data, spec_data, forbidden_words=fw_entries)

    spec_data['seo'] = new_seo
    product.spec_data = spec_data
    flag_modified(product, 'spec_data')
    try:
        db.session.commit()
        log_event(product.id, get_current_username(), 'SEO Regenerated',
                  'AI regenerated meta title, description and keywords.', 'neutral')
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'save failed: {e}'}), 500

    return jsonify({'status': 'success', 'seo': new_seo})


@web_bp.route('/api/generate_specsheet/<int:product_id>', methods=['POST'])
@limiter.limit("5 per minute")
def api_generate_specsheet(product_id):
    product = Product.query.get_or_404(product_id)
    is_rework = request.args.get('rework') == '1'
    _app = current_app._get_current_object()  # type: ignore[attr-defined]

    def generate() -> Iterator[str]:
        if is_rework:
            yield json.dumps({"progress": 10, "message": "Loading forbidden words..."}) + "\n"
            time.sleep(0.3)
            yield json.dumps({"progress": 25, "message": "Analyzing existing content..."}) + "\n"
        else:
            yield json.dumps({"progress": 20, "message": "Analyzing PIS Data..."}) + "\n"
        time.sleep(0.5)
        yield json.dumps({"progress": 50, "message": "Rewriting Customer Content..."}) + "\n"

        try:
            # Resolve the product's canonical category — used both to filter
            # forbidden-word rules and to pass directly to the spec generator
            # so it doesn't re-run the AI classifier.
            canonical_cat = get_product_category(product)
            cat_3 = canonical_cat['category_3'] or None
            forbidden_entries = get_forbidden_words_for_category(cat_3)

            if is_rework and forbidden_entries:
                yield json.dumps({"progress": 55, "message": f"Enforcing {len(forbidden_entries)} forbidden words..."}) + "\n"

            spec_data = generate_comprehensive_spec_data(
                product.pis_data,
                forbidden_words=forbidden_entries,
                categories=canonical_cat if canonical_cat['category_1'] else None,
            )
            # Pop the scrub-hits sentinel BEFORE we persist — it's a transient
            # log signal, not part of the SpecSheet payload.
            scrub_hits = spec_data.pop('_forbidden_hits', {}) if isinstance(spec_data, dict) else {}
            yield json.dumps({"progress": 80, "message": "Optimizing SEO Metadata..."}) + "\n"

            with _app.app_context():
                p = Product.query.get(product_id)
                if p is None:
                    yield json.dumps({"error": "Product not found"}) + "\n"
                    return
                p.spec_data = spec_data
                p.workflow_stage = Stage.SPECSHEET_DRAFT
                flag_modified(p, 'spec_data')
                # If the spec generator fell back to AI classification
                # (product had no canonical category yet), promote that
                # result into the canonical column. Re-syncs the mirror too.
                if not p.category_1:
                    gen_cats = (spec_data.get('categories') or {}) if isinstance(spec_data, dict) else {}
                    if gen_cats.get('category_1'):
                        set_product_category(p,
                            gen_cats.get('category_1', ''),
                            gen_cats.get('category_2', ''),
                            gen_cats.get('category_3', ''))
                save_version_snapshot(p, label='SpecSheet regenerated' if is_rework else 'SpecSheet auto-generated', is_major=True)
                db.session.commit()
                hits_summary = _format_hits_summary(scrub_hits)
                if is_rework:
                    msg = f'The system regenerated content ({len(forbidden_entries)} restricted terms applied).'
                    if hits_summary:
                        msg += f' Caught: {hits_summary}.'
                    log_event(p.id, get_current_username(), 'SpecSheet Regenerated', msg, 'neutral')
                else:
                    msg = 'The system automatically created customer-facing product descriptions and SEO keywords.'
                    if hits_summary:
                        msg += f' Forbidden terms caught: {hits_summary}.'
                    log_event(p.id, 'System', 'SpecSheet Auto-Generated', msg, 'neutral')

            yield json.dumps({"progress": 100, "message": "Generation Complete!", "redirect": url_for('web.create_specsheet', product_id=product.id)}) + "\n"
        except Exception as e:
            print(f"SpecSheet gen error: {e}")
            yield json.dumps({"error": "AI Generation Failed. Please try again."}) + "\n"

    return Response(stream_with_context(generate()), mimetype='application/x-ndjson')  # type: ignore[arg-type]


# ── PRIVATE HELPERS ───────────────────────────────────────────────────────────

def _image_url(product):
    if not product.image_path:
        return ""
    if product.image_path.startswith('http'):
        return product.image_path
    return url_for("static", filename=product.image_path)


def _load_images_b64(product):
    import base64
    result = []
    paths = []
    if product.image_path and not product.image_path.startswith('http'):
        paths.append(product.image_path)
    if product.additional_images:
        paths.extend(p for p in product.additional_images if not p.startswith('http'))
    for path in paths:
        try:
            abs_path = os.path.join(current_app.root_path, 'static', path.replace('/', os.sep))
            if os.path.exists(abs_path):
                with open(abs_path, "rb") as f:
                    ext = os.path.splitext(abs_path)[1].lower().replace('.', '')
                    if ext == 'jpg': ext = 'jpeg'
                    result.append(f"data:image/{ext};base64,{base64.b64encode(f.read()).decode('utf-8')}")
        except Exception as e:
            print(f"Image b64 error for {path}: {e}")
    return result
