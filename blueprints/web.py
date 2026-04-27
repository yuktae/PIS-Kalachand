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

from flask import (
    Blueprint, session, redirect, url_for, render_template,
    request, Response, stream_with_context, current_app
)
from werkzeug.utils import secure_filename
from sqlalchemy.orm.attributes import flag_modified

from model import db, Product, ProductVersion
from helpers import (
    get_current_username, save_version_snapshot,
    _diff_and_log_changes, load_forbidden_words,
)
from utils.history import log_event
from utils.ai_generation import generate_comprehensive_spec_data
from extensions import limiter

web_bp = Blueprint('web', __name__)


# ── DASHBOARDS ────────────────────────────────────────────────────────────────

@web_bp.route('/dashboard/web')
def dashboard_web():
    if session.get('role') != 'web':
        return redirect(url_for('auth.login'))

    tasks = (
        Product.query
        .filter(Product.workflow_stage.in_([
            'ready_for_web', 'web_changes_requested',
            'specsheet_draft', 'pending_director_spec', 'finalized'
        ]))
        .filter(Product.deleted_at.is_(None))
        .order_by(Product.created_at.desc())
        .all()
    )

    products_json = [{
        "id": p.id,
        "model_name": p.model_name or "",
        "brand": p.pis_data.get("header_info", {}).get("brand", "Unknown") if p.pis_data else "Unknown",
        "image": _image_url(p),
        "date": p.created_at.strftime("%d %b"),
        "stage": p.workflow_stage,
        "action_url": url_for("web.create_specsheet", product_id=p.id)
    } for p in tasks]

    metrics = {
        "total_tasks": len(tasks),
        "new_specsheets":     sum(1 for p in tasks if p.workflow_stage == "ready_for_web"),
        "changes_requested":  sum(1 for p in tasks if p.workflow_stage == "web_changes_requested"),
        "need_review":        sum(1 for p in tasks if p.workflow_stage == "pending_director_spec"),
        "approved":           sum(1 for p in tasks if p.workflow_stage == "finalized"),
        "in_process":         sum(1 for p in tasks if p.workflow_stage == "specsheet_draft"),
    }
    return render_template("dashboard_web.html", tasks=tasks, products_json=products_json, metrics=metrics)


@web_bp.route('/dashboard/web/archive')
def web_archive():
    if session.get('role') != 'web':
        return redirect(url_for('auth.login'))
    finalized = Product.query.filter_by(workflow_stage='finalized').filter(Product.deleted_at.is_(None)).order_by(Product.created_at.desc()).all()
    return render_template('archive_web.html', products=finalized)


@web_bp.route('/dashboard/web/forbidden-words')
def web_forbidden_words():
    if session.get('role') != 'web':
        return redirect(url_for('auth.login'))
    try:
        from utils.magento_api import get_category_tree
        category_tree = get_category_tree()
    except Exception:
        from utils.category_classifier import load_categories
        raw_categories = load_categories()
        category_tree = {}
        for cat in raw_categories:
            a, b, c = cat['cat_A'], cat['cat_B'], cat['cat_C']
            category_tree.setdefault(a, {}).setdefault(b, [])
            if c not in category_tree[a][b]:
                category_tree[a][b].append(c)
    return render_template('forbidden_words.html', category_tree=category_tree)


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
            spec_data.setdefault('categories', {})
            spec_data['categories']['category_1'] = cat1
            spec_data['categories']['category_2'] = cat2
            spec_data['categories']['category_3'] = cat3

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
            product.workflow_stage = 'pending_director_spec'
            save_version_snapshot(product, label='SpecSheet sent for review', is_major=True)
            log_event(product.id, get_current_username(), 'SpecSheet Sent for Review',
                      'The specsheet has been submitted to the Director for final review.', 'waiting')
        else:
            if product.workflow_stage == 'ready_for_web':
                product.workflow_stage = 'specsheet_draft'
            save_version_snapshot(product, label='SpecSheet draft saved', is_major=False)
            log_event(product.id, get_current_username(), 'SpecSheet Draft Saved',
                      'Changes to the specsheet have been saved as a draft.', 'neutral')

        _diff_and_log_changes(product.id, old_spec, spec_data, prefix='spec_data')
        _diff_and_log_changes(product.id, old_pis, product.pis_data, prefix='pis_data')
        db.session.commit()
        return redirect(url_for('web.dashboard_web'))

    return render_template('edit_specsheet.html', product=product, spec_data=product.spec_data or {})


# ── DOWNLOADS ─────────────────────────────────────────────────────────────────

@web_bp.route('/download_specsheet/<int:product_id>')
def download_specsheet(product_id):
    import base64
    from playwright.sync_api import sync_playwright
    from datetime import datetime
    product = Product.query.get_or_404(product_id)
    all_images_b64 = _load_images_b64(product)
    html = render_template('specsheet_pdf.html',
                           data=product.pis_data, spec_data=product.spec_data or {},
                           product=product, all_images_b64=all_images_b64,
                           date_generated=datetime.now().strftime("%Y-%m-%d"))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage']
            )
            page = browser.new_page()
            page.set_content(html, wait_until='networkidle')
            page.wait_for_timeout(1500)
            pdf_bytes = page.pdf(format="A4", print_background=True,
                                 margin={"top": "15mm", "right": "15mm", "bottom": "15mm", "left": "15mm"})
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

@web_bp.route('/api/generate_specsheet/<int:product_id>', methods=['POST'])
@limiter.limit("5 per minute")
def api_generate_specsheet(product_id):
    product = Product.query.get_or_404(product_id)
    is_rework = request.args.get('rework') == '1'
    _app = current_app._get_current_object()

    def generate():
        if is_rework:
            yield json.dumps({"progress": 10, "message": "Loading forbidden words..."}) + "\n"
            time.sleep(0.3)
            yield json.dumps({"progress": 25, "message": "Analyzing existing content..."}) + "\n"
        else:
            yield json.dumps({"progress": 20, "message": "Analyzing PIS Data..."}) + "\n"
        time.sleep(0.5)
        yield json.dumps({"progress": 50, "message": "Rewriting Customer Content..."}) + "\n"

        try:
            all_fw = load_forbidden_words()
            combined_forbidden = list(set(w for words in all_fw.values() for w in words))
            if is_rework and combined_forbidden:
                yield json.dumps({"progress": 55, "message": f"Enforcing {len(combined_forbidden)} forbidden words..."}) + "\n"

            spec_data = generate_comprehensive_spec_data(product.pis_data, forbidden_words=combined_forbidden)
            yield json.dumps({"progress": 80, "message": "Optimizing SEO Metadata..."}) + "\n"

            with _app.app_context():
                p = Product.query.get(product_id)
                p.spec_data = spec_data
                p.workflow_stage = 'specsheet_draft'
                flag_modified(p, 'spec_data')
                save_version_snapshot(p, label='SpecSheet regenerated' if is_rework else 'SpecSheet auto-generated', is_major=True)
                db.session.commit()
                if is_rework:
                    log_event(p.id, get_current_username(), 'SpecSheet Regenerated',
                              f'The system regenerated content ({len(combined_forbidden)} restricted terms applied).', 'neutral')
                else:
                    log_event(p.id, 'System', 'SpecSheet Auto-Generated',
                              'The system automatically created customer-facing product descriptions and SEO keywords.', 'neutral')

            yield json.dumps({"progress": 100, "message": "Generation Complete!", "redirect": url_for('web.create_specsheet', product_id=product.id)}) + "\n"
        except Exception as e:
            print(f"SpecSheet gen error: {e}")
            yield json.dumps({"error": "AI Generation Failed. Please try again."}) + "\n"

    return Response(stream_with_context(generate()), mimetype='application/x-ndjson')


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
