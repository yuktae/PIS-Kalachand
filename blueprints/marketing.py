"""
Marketing blueprint — dashboards, PIS creation, and marketing review routes.
"""
import os
import json
import copy
import time
import threading

from flask import (
    Blueprint, session, redirect, url_for, render_template,
    request, flash, Response, stream_with_context, current_app
)
from werkzeug.utils import secure_filename

from model import db, Product, ProductHistory, ProductVersion, User
from helpers import (
    get_current_username, save_version_snapshot,
    _diff_and_log_changes, normalize_pis_data,
)
from utils.history import log_event
from utils.web_scraping import scrape_url_data, scrape_url_data_deep
from utils.ai_generation import generate_pis_data, generate_bulk_pis_data
from utils.pdf_processing import extract_specific_image, clear_pdf_cache
from utils.image_processing import (
    find_and_validate_image, find_image_simple, download_web_image
)
from sqlalchemy.orm.attributes import flag_modified
from extensions import limiter

marketing_bp = Blueprint('marketing', __name__)


# ── DASHBOARDS ────────────────────────────────────────────────────────────────

@marketing_bp.route('/dashboard/marketing')
def dashboard_marketing():
    if session.get('role') != 'marketing':
        return redirect(url_for('auth.login'))

    approved_stages = ['ready_for_web', 'specsheet_draft', 'pending_director_spec', 'web_changes_requested', 'finalized']
    marketing_stages = ['marketing_draft', 'marketing_in_progress', 'marketing_changes_requested', 'pending_director_pis'] + approved_stages

    active_pipeline = Product.query.filter(
        Product.workflow_stage.in_(marketing_stages),
        Product.deleted_at.is_(None)
    ).order_by(Product.created_at.desc()).all()

    metrics = {
        'total_active': len(active_pipeline),
        'drafts': sum(1 for p in active_pipeline if p.workflow_stage == 'marketing_draft'),
        'changes': sum(1 for p in active_pipeline if p.workflow_stage == 'marketing_changes_requested'),
        'need_review': sum(1 for p in active_pipeline if p.workflow_stage == 'pending_director_pis'),
        'in_process': sum(1 for p in active_pipeline if p.workflow_stage == 'marketing_in_progress'),
        'approved': sum(1 for p in active_pipeline if p.workflow_stage in approved_stages)
    }
    return render_template('dashboard_marketing.html', products=active_pipeline, metrics=metrics)


@marketing_bp.route('/dashboard/history')
@marketing_bp.route('/dashboard/marketing/history')
def history_marketing():
    if not session.get('role'):
        return redirect(url_for('auth.login'))

    all_products = Product.query.filter(Product.deleted_at.is_(None)).order_by(Product.created_at.desc()).all()
    product_ids = [p.id for p in all_products]

    all_history = ProductHistory.query.filter(
        ProductHistory.product_id.in_(product_ids)
    ).order_by(ProductHistory.timestamp.desc()).all() if product_ids else []

    history_by_product = {}
    for event in all_history:
        history_by_product.setdefault(event.product_id, []).append(event)

    ICON_MAP = {
        'Created':  'M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z',
        'Submitted':'M12 19l9 2-9-18-9 18 9-2zm0 0v-8',
        'Approved': 'M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z',
        'Changes':  'M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z',
        'Updated':  'M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15',
        'Generated':'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z',
        'Restored': 'M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15',
        'Image':    'M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z',
    }

    def get_icon(title):
        for key, icon in ICON_MAP.items():
            if key.lower() in title.lower():
                return icon
        return 'M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z'

    STAGE_FILTER_MAP = {
        'marketing_draft': 'DRAFT PIS',
        'pending_director_pis': 'NEED REVIEW',
        'marketing_changes_requested': 'CHANGE REQUESTED',
        'ready_for_web': 'PIS APPROVED',
        'specsheet_draft': 'IN PROCESS',
        'pending_director_spec': 'IN PROCESS',
        'web_changes_requested': 'IN PROCESS',
        'finalized': 'PIS APPROVED',
        'marketing_in_progress': 'IN PROCESS',
    }

    products_with_history = []
    for p in all_products:
        history_events = history_by_product.get(p.id, [])
        timeline = [{
            'date': e.timestamp.strftime('%Y-%m-%d'),
            'time': e.timestamp.strftime('%H:%M'),
            'title': e.action_title,
            'description': e.description or '',
            'actor': e.actor,
            'status': e.action_type or 'neutral',
            'icon': get_icon(e.action_title)
        } for e in history_events]

        if not timeline:
            timeline.append({
                'date': p.created_at.strftime('%Y-%m-%d'),
                'time': p.created_at.strftime('%H:%M'),
                'title': 'PIS Draft Created', 'description': 'Product data imported.',
                'actor': 'System', 'status': 'neutral', 'icon': ICON_MAP['Created']
            })

        stage = p.workflow_stage or ''
        pis_approved_stages = ['ready_for_web', 'specsheet_draft', 'pending_director_spec', 'web_changes_requested', 'finalized']
        current_pis_status = 'Draft'
        if 'pending_director_pis' in stage:   current_pis_status = 'Pending Review'
        elif 'marketing_changes_requested' in stage: current_pis_status = 'Changes Requested'
        elif any(s in stage for s in pis_approved_stages): current_pis_status = 'Approved'

        latest_actor = timeline[0]['actor'] if timeline else 'System'
        latest_event = timeline[0]['title'] if timeline else 'Created'
        latest_date  = timeline[0]['date']  if timeline else p.created_at.strftime('%Y-%m-%d')
        latest_time  = timeline[0]['time']  if timeline else p.created_at.strftime('%H:%M')
        filter_status = STAGE_FILTER_MAP.get(stage, 'DRAFT PIS')

        products_with_history.append({
            'product': p, 'pis_status': current_pis_status, 'filter_status': filter_status,
            'latest_actor': latest_actor, 'latest_event': latest_event,
            'latest_date': latest_date, 'latest_time': latest_time,
            'timeline': timeline, 'changelog': []
        })

    products_json = json.dumps([{
        'id': item['product'].id,
        'model_name': item['product'].model_name,
        'brand': item['product'].pis_data.get('header_info', {}).get('brand', 'Unknown') if item['product'].pis_data else 'Unknown',
        'image_path': url_for('static', filename=item['product'].image_path) if item['product'].image_path and not item['product'].image_path.startswith('http') else item['product'].image_path,
        'pis_status': item['pis_status'],
        'filter_status': item['filter_status'],
        'latest_actor': item['latest_actor'],
        'latest_event': item['latest_event'],
        'latest_date': item['latest_date'],
        'latest_time': item['latest_time'],
        'created_date': item['product'].created_at.strftime('%Y-%m-%d'),
        'timeline': item['timeline'],
        'changelog': item['changelog']
    } for item in products_with_history])

    return render_template('history_marketing.html', products_json=products_json)


@marketing_bp.route('/dashboard/marketing/archive')
def marketing_archive():
    if session.get('role') != 'marketing':
        return redirect(url_for('auth.login'))
    approved_stages = ['finalized', 'ready_for_web', 'specsheet_draft', 'pending_director_spec', 'web_changes_requested']
    archived_products = Product.query.filter(
        Product.workflow_stage.in_(approved_stages),
        Product.deleted_at.is_(None)
    ).order_by(Product.created_at.desc()).all()
    return render_template('archive_marketing.html', products=archived_products)


# ── PRODUCT CREATION ─────────────────────────────────────────────────────────

@marketing_bp.route('/create', methods=['GET', 'POST'])
@limiter.limit("10 per minute", methods=['POST'])
def create_pis():
    if request.method == 'GET':
        return render_template('create.html')

    model_name = request.form.get('model_name')
    supplier_url = request.form.get('supplier_url')
    ai_files = request.files.getlist('ai_document')
    contains_images = request.form.get('contains_images') == 'on'

    ai_filepaths = []
    for ai_file in ai_files:
        if ai_file and ai_file.filename:
            filename = secure_filename(ai_file.filename)
            filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
            ai_file.save(filepath)
            ai_filepaths.append(filepath)

    upload_folder = current_app.config['UPLOAD_FOLDER']
    _app = current_app._get_current_object()

    def generate_updates():
        yield json.dumps({"progress": 10, "message": "Initializing Analysis..."}) + "\n"
        site_data = {"text": "", "html": ""}
        if supplier_url:
            yield json.dumps({"progress": 20, "message": "Reading Website Text..."}) + "\n"
            site_data = scrape_url_data(supplier_url)

        yield json.dumps({"progress": 40, "message": "Generating PIS Content..."}) + "\n"
        try:
            ai_data = generate_pis_data(ai_filepaths, model_name, site_data)
            extracted_image_path = None

            if contains_images and ai_filepaths:
                yield json.dumps({"progress": 55, "message": "Scanning PDF for product image..."}) + "\n"
                yield " \n"
                extracted_image_path = extract_specific_image(ai_filepaths[0], model_name, upload_folder)
                yield " \n"
                if not extracted_image_path:
                    yield json.dumps({"progress": 65, "message": "PDF scan found nothing, trying web..."}) + "\n"
                    ai_found_url = ai_data.get('found_image_url')
                    if ai_found_url and ai_found_url.startswith('http'):
                        extracted_image_path = download_web_image(ai_found_url, model_name, upload_folder)
                if not extracted_image_path:
                    rich_query = _build_rich_query(ai_data, model_name)
                    yield " \n"
                    public_url = find_and_validate_image(rich_query, supplier_url)
                    if public_url:
                        extracted_image_path = download_web_image(public_url, model_name, upload_folder)
            else:
                ai_found_url = ai_data.get('found_image_url')
                if ai_found_url and ai_found_url.startswith('http'):
                    yield json.dumps({"progress": 55, "message": "AI found a product image — downloading..."}) + "\n"
                    extracted_image_path = download_web_image(ai_found_url, model_name, upload_folder)
                if not extracted_image_path:
                    yield json.dumps({"progress": 60, "message": "Searching Google Images..."}) + "\n"
                    rich_query = _build_rich_query(ai_data, model_name)
                    yield " \n"
                    public_url = find_and_validate_image(rich_query, supplier_url)
                    if public_url:
                        yield json.dumps({"progress": 70, "message": "Downloading Image..."}) + "\n"
                        extracted_image_path = download_web_image(public_url, model_name, upload_folder)

            yield " \n"
            if not extracted_image_path:
                yield json.dumps({"progress": 80, "message": "Trying DuckDuckGo fallback search..."}) + "\n"
                yield " \n"
                header = ai_data.get('header_info', {})
                simple_query = f"{header.get('brand', '')} {header.get('product_name', '')}".strip() or model_name
                simple_url = find_image_simple(simple_query, supplier_url)
                if simple_url:
                    yield json.dumps({"progress": 85, "message": "Found image via DuckDuckGo!"}) + "\n"
                    extracted_image_path = download_web_image(simple_url, model_name, upload_folder)

            status_msg = "Visual Acquired." if extracted_image_path else "No visual found."
            yield json.dumps({"progress": 90, "message": status_msg}) + "\n"

            with _app.app_context():
                new_product = Product(
                    model_name=model_name,
                    pis_data=ai_data,
                    image_path=extracted_image_path,
                    seo_keywords=ai_data.get('seo_data', {}).get('generated_keywords', ''),
                    workflow_stage='marketing_draft'
                )
                db.session.add(new_product)
                db.session.commit()
                log_event(new_product.id, get_current_username(), 'New Product Added',
                          'A new product information sheet was created from a single import.', 'neutral')
                save_version_snapshot(new_product, label='Initial version', is_major=True)
                yield json.dumps({"progress": 100, "message": "Done!", "redirect": url_for('marketing.review_pis_marketing', product_id=new_product.id)}) + "\n"

        except Exception as e:
            yield json.dumps({"error": str(e)}) + "\n"

    return Response(stream_with_context(generate_updates()), mimetype='application/x-ndjson')


@marketing_bp.route('/create_bulk', methods=['GET', 'POST'])
@limiter.limit("5 per minute", methods=['POST'])
def create_bulk():
    if request.method == 'GET':
        return render_template('create_bulk.html')

    supplier_url = request.form.get('supplier_url')
    ai_files = request.files.getlist('ai_document')
    contains_images = request.form.get('contains_images') == 'on'
    product_filter = request.form.get('product_filter', '').strip()

    ai_filepaths = []
    for ai_file in ai_files:
        if ai_file and ai_file.filename:
            filename = secure_filename(ai_file.filename)
            filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
            ai_file.save(filepath)
            ai_filepaths.append(filepath)

    if not ai_filepaths and not supplier_url:
        return "Please provide at least a document or a supplier URL.", 400

    ai_filepath = ai_filepaths[0] if ai_filepaths else None
    upload_folder = current_app.config['UPLOAD_FOLDER']
    _app = current_app._get_current_object()

    def generate_bulk_updates():
        yield json.dumps({"progress": 10, "message": "Analyzing Invoice..."}) + "\n"
        site_data = {"text": "", "html": ""}
        if supplier_url:
            if not ai_filepaths:
                site_data = scrape_url_data_deep(supplier_url)
            else:
                site_data = scrape_url_data(supplier_url)

        try:
            products_list = generate_bulk_pis_data(ai_filepaths, site_data, product_filter=product_filter)
            total_items = len(products_list)
            product_names = []
            for idx, p_data in enumerate(products_list):
                header = p_data.get('header_info', {})
                p_name = header.get('product_name')
                m_num = header.get('model_number')
                d_name = p_name if p_name else (m_num if m_num else f"Item_{idx+1}")
                product_names.append(d_name)

            yield json.dumps({
                "progress": 20,
                "message": f"Found {total_items} items.",
                "products": [{"name": name, "status": "pending"} for name in product_names]
            }) + "\n"

            with _app.app_context():
                for idx, p_data in enumerate(products_list):
                    header = p_data.get('header_info', {})
                    brand = header.get('brand', '')
                    model_id = header.get('model_number', '')
                    prod_name = header.get('product_name', '')
                    display_name = prod_name if prod_name else (model_id if model_id else f"Item_{idx+1}")
                    current_progress = 20 + int(((idx + 1) / total_items) * 75)

                    yield json.dumps({
                        "progress": current_progress,
                        "message": f"Processing: {display_name}",
                        "item_update": {"name": display_name, "status": "searching"}
                    }) + "\n"

                    try:
                        search_query = _build_bulk_search_query(brand, prod_name, model_id, display_name)
                        extracted_image_path = None

                        if contains_images:
                            if ai_filepath:
                                yield " \n"
                                extracted_image_path = extract_specific_image(ai_filepath, model_id, upload_folder)
                                yield " \n"
                            if not extracted_image_path:
                                ai_found_url = p_data.get('found_image_url')
                                if ai_found_url and str(ai_found_url).startswith('http'):
                                    yield " \n"
                                    extracted_image_path = download_web_image(ai_found_url, display_name, upload_folder)
                            if not extracted_image_path:
                                yield " \n"
                                image_url = find_and_validate_image(search_query, supplier_url)
                                if image_url:
                                    extracted_image_path = download_web_image(image_url, display_name, upload_folder)
                        else:
                            ai_found_url = p_data.get('found_image_url')
                            if ai_found_url and str(ai_found_url).startswith('http'):
                                yield " \n"
                                extracted_image_path = download_web_image(ai_found_url, display_name, upload_folder)
                            if not extracted_image_path:
                                yield " \n"
                                image_url = find_and_validate_image(search_query, supplier_url)
                                if image_url:
                                    extracted_image_path = download_web_image(image_url, display_name, upload_folder)

                        if not extracted_image_path:
                            yield " \n"
                            simple_url = find_image_simple(search_query, supplier_url)
                            if simple_url:
                                extracted_image_path = download_web_image(simple_url, display_name, upload_folder)

                        new_product = Product(
                            model_name=display_name, pis_data=p_data,
                            image_path=extracted_image_path,
                            seo_keywords=p_data.get('seo_data', {}).get('generated_keywords', ''),
                            workflow_stage='marketing_draft'
                        )
                        db.session.add(new_product)
                        db.session.commit()
                        log_event(new_product.id, get_current_username(), 'New Product Added',
                                  'This product was imported as part of a bulk extraction.', 'neutral')
                        save_version_snapshot(new_product, label='Initial version', is_major=True)
                        yield json.dumps({"item_update": {"name": display_name, "status": "completed"}}) + "\n"

                    except Exception as product_err:
                        print(f"⚠️ Bulk import error for '{display_name}': {product_err}")
                        try:
                            fallback = Product(
                                model_name=display_name, pis_data=p_data, image_path=None,
                                seo_keywords=p_data.get('seo_data', {}).get('generated_keywords', ''),
                                workflow_stage='marketing_draft'
                            )
                            db.session.add(fallback)
                            db.session.commit()
                            log_event(fallback.id, get_current_username(), 'New Product Added',
                                      'Imported via bulk extraction (image could not be found).', 'neutral')
                            save_version_snapshot(fallback, label='Initial version', is_major=True)
                        except Exception:
                            db.session.rollback()
                        yield json.dumps({
                            "item_update": {"name": display_name, "status": "completed"},
                            "message": f"Saved {display_name} (image skipped)"
                        }) + "\n"

            yield json.dumps({"progress": 100, "message": "Bulk Import Complete!", "redirect": url_for('marketing.dashboard_marketing')}) + "\n"
            clear_pdf_cache()

        except Exception as e:
            yield json.dumps({"error": str(e)}) + "\n"

    return Response(stream_with_context(generate_bulk_updates()), mimetype='application/x-ndjson')


# ── REVIEW ROUTES ─────────────────────────────────────────────────────────────

@marketing_bp.route('/verify/<int:product_id>')
def old_verify_redirect(product_id):
    return redirect(url_for('marketing.review_pis_marketing', product_id=product_id))


@marketing_bp.route('/review/marketing/<int:product_id>', methods=['GET', 'POST'])
def review_pis_marketing(product_id):
    product = Product.query.get_or_404(product_id)

    if request.method == 'POST':
        last_version = ProductVersion.query.filter_by(product_id=product.id).order_by(ProductVersion.version_num.desc()).first()
        old_pis = copy.deepcopy(last_version.pis_data) if last_version and last_version.pis_data else {}

        updated_data = product.pis_data or {}
        if 'header_info' not in updated_data: updated_data['header_info'] = {}
        updated_data['header_info']['product_name']  = request.form.get('product_name')
        updated_data['header_info']['model_number']  = request.form.get('model_number')
        updated_data['header_info']['brand']         = request.form.get('brand')
        updated_data['header_info']['price_estimate']= request.form.get('price_estimate')
        updated_data['range_overview']               = request.form.get('range_overview')
        updated_data['sales_arguments']              = request.form.getlist('sales_arguments')

        spec_names  = request.form.getlist('spec_name')
        spec_values = request.form.getlist('spec_value')
        updated_data['technical_specifications'] = dict(zip(spec_names, spec_values))

        if 'warranty_service' not in updated_data: updated_data['warranty_service'] = {}
        updated_data['warranty_service']['period']   = request.form.get('warranty_period')
        updated_data['warranty_service']['coverage'] = request.form.get('warranty_coverage')

        product.pis_data = updated_data
        if product.revision_data:
            product.revision_data = None

        flag_modified(product, 'pis_data')
        flag_modified(product, 'revision_data')

        action = request.form.get('action')
        if action == 'submit_director':
            save_version_snapshot(product, label='Submitted for Director review', is_major=True)
            product.workflow_stage = 'pending_director_pis'
            log_event(product.id, get_current_username(), 'Sent for Director Review',
                      'The product sheet has been sent to the Director for approval.', 'waiting')
            flash('Sent to the Director for review ✓')
        else:
            save_version_snapshot(product, label='Draft saved', is_major=False)
            if product.workflow_stage in ('marketing_draft', 'marketing_changes_requested'):
                product.workflow_stage = 'marketing_in_progress'
            log_event(product.id, get_current_username(), 'Draft Updated',
                      'The marketing team updated and saved changes to the product sheet.', 'neutral')
            flash('Draft saved successfully ✓')

        _diff_and_log_changes(product.id, old_pis, updated_data, prefix='pis_data')
        db.session.commit()
        return redirect(url_for('marketing.dashboard_marketing'))

    return render_template('verify_marketing.html', product=product, data=normalize_pis_data(product.pis_data))


@marketing_bp.route('/retry_revision/<int:product_id>/<section>', methods=['POST'])
def retry_revision(product_id, section):
    from utils.ai_generation import generate_ai_revision
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified
    product = Product.query.get_or_404(product_id)
    if not product.revision_data or section not in product.revision_data:
        return {"error": "No revision data"}, 400
    revision = product.revision_data[section]
    new_ai_suggestion = generate_ai_revision(
        section_name=section,
        original_content=revision.get("original"),
        director_comment=revision.get("comment")
    )
    product.revision_data[section]["ai_suggestion"] = new_ai_suggestion
    _flag_modified(product, 'revision_data')
    db.session.commit()
    return {"ai_suggestion": new_ai_suggestion}


# ── PDF DOWNLOADS ─────────────────────────────────────────────────────────────

@marketing_bp.route('/download_pis_pdf/<int:product_id>')
def download_pis_pdf(product_id):
    import base64
    from flask import Response as FlaskResponse
    from playwright.sync_api import sync_playwright
    from datetime import datetime
    product = Product.query.get_or_404(product_id)
    all_images_b64 = _load_images_b64(product)
    html = render_template('pdf_print.html',
                           data=product.pis_data, product=product,
                           all_images_b64=all_images_b64,
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
        return FlaskResponse(pdf_bytes, mimetype='application/pdf',
                             headers={"Content-Disposition": f"attachment;filename=PIS_{secure_filename(product.model_name)}.pdf"})
    except Exception as e:
        return f"Error generating PDF: {str(e)}"


# ── PRIVATE HELPERS ───────────────────────────────────────────────────────────

def _load_images_b64(product):
    import base64
    result = []
    image_paths = []
    if product.image_path and not product.image_path.startswith('http'):
        image_paths.append(product.image_path)
    if product.additional_images:
        image_paths.extend([p for p in product.additional_images if not p.startswith('http')])
    for path in image_paths:
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


def _build_rich_query(ai_data, fallback):
    header = ai_data.get('header_info', {})
    brand  = header.get('brand', '')
    m_num  = header.get('model_number', '')
    p_name = header.get('product_name', '')
    q_parts = []
    if brand:  q_parts.append(brand)
    if p_name: q_parts.append(p_name)
    if m_num and (any(c.isalpha() for c in m_num) or '-' in m_num):
        if m_num not in (p_name or ''):
            q_parts.append(m_num)
    words = []
    seen = set()
    for w in ' '.join(q_parts).split():
        if w.lower() not in seen:
            words.append(w)
            seen.add(w.lower())
    return ' '.join(words) if words else fallback


def _build_bulk_search_query(brand, prod_name, model_id, fallback):
    query_parts = []
    if brand:     query_parts.append(brand)
    if prod_name: query_parts.append(prod_name)
    is_real_model = model_id and (any(c.isalpha() for c in model_id) or '-' in model_id)
    if is_real_model and model_id not in (prod_name or ''):
        query_parts.append(model_id)
    seen_words = set()
    unique_words = []
    for w in ' '.join(query_parts).split():
        if w.lower() not in seen_words:
            unique_words.append(w)
            seen_words.add(w.lower())
    return ' '.join(unique_words) if unique_words else fallback
