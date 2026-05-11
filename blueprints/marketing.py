"""
Marketing blueprint — dashboards, PIS creation, and marketing review routes.
"""
import os
import json
import copy
import time
from typing import Any

from flask import (
    Blueprint, session, redirect, url_for, render_template,
    request, flash, Response, stream_with_context, current_app
)
from werkzeug.utils import secure_filename

from model import db, Product, ProductHistory, ProductVersion
from helpers import (
    get_current_username, save_version_snapshot,
    _diff_and_log_changes, normalize_pis_data,
    proforma_to_pis_data, extract_raw_text_from_files,
)
from utils.history import log_event
from utils.web_scraping import scrape_url_data, scrape_url_data_deep
from utils.ai_generation import generate_pis_data, generate_bulk_pis_data, generate_proforma_data
from utils.pdf_processing import extract_specific_image, clear_pdf_cache
from utils.image_processing import (
    find_and_validate_image, find_image_simple, download_web_image,
    find_image_via_screenshot,
)
from utils import single_wizard as sw
from utils import bulk_wizard as bw
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


@marketing_bp.route('/product/<int:product_id>/history')
def product_history_timeline(product_id):
    """Phase 6 — per-product audit timeline. Renders the new timeline_v2
    partial that consumes /api/product/<id>/timeline."""
    if not session.get('role'):
        return redirect(url_for('auth.login'))
    product = Product.query.get_or_404(product_id)
    return render_template('product_history.html', product=product)


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

    # Annotated so Pyrefly doesn't widen `item['product']` to the union of
    # every dict value (Product | str | list | …) and lose the Product type
    # for `.id`, `.model_name`, etc. downstream.
    products_with_history: list[dict[str, Any]] = []
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
    # Marketing + Admin can view the archive. Admin needs read access for
    # oversight; the archive itself is read-only so there's no risk.
    if session.get('role') not in ('marketing', 'admin'):
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

    # `request.form.get` returns str | None; normalize to non-None strings so
    # downstream helpers (find_image_simple, scrape_url_data, etc.) get the
    # types they declare. An empty string is the safe equivalent of "missing".
    model_name = request.form.get('model_name') or ''
    supplier_url = request.form.get('supplier_url') or ''
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
    _app = current_app._get_current_object()  # type: ignore[attr-defined]

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

    return Response(stream_with_context(generate_updates()), mimetype='application/x-ndjson')  # type: ignore[arg-type]


# ── UNIFIED PROFORMA IMPORT (Phase 2) ────────────────────────────────────────
#
# /import_proforma is the single intake replacing /create + /create_bulk. The
# legacy routes are intentionally kept below as a fallback while this rolls
# out — see Phase2_Proforma.md.
#
@marketing_bp.route('/import_proforma', methods=['GET', 'POST'])
@limiter.limit("10 per minute", methods=['POST'])
def import_proforma():
    if session.get('role') != 'marketing':
        return redirect(url_for('auth.login'))

    if request.method == 'GET':
        return render_template('import_proforma.html')

    mode = (request.form.get('mode') or 'auto').strip().lower()
    if mode not in ('auto', 'single', 'multiple'):
        mode = 'auto'

    supplier_url    = request.form.get('supplier_url', '').strip()
    ai_files        = request.files.getlist('ai_document')
    contains_images = request.form.get('contains_images') == 'on'
    # NOTE: product_filter is read from the form for backwards compatibility
    # but is not yet plumbed through generate_proforma_data. Reviewers can
    # narrow the result via the rework / feedback flow on the review modal.
    model_name      = (request.form.get('model_name') or '').strip()

    if mode == 'single' and not model_name:
        return "Single Item mode requires a target model name.", 400

    ai_filepaths = []
    for ai_file in ai_files:
        if ai_file and ai_file.filename:
            filename = secure_filename(ai_file.filename)
            filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
            ai_file.save(filepath)
            ai_filepaths.append(filepath)

    if not ai_filepaths and not supplier_url:
        return "Please provide at least a document or a supplier URL.", 400

    upload_folder = current_app.config['UPLOAD_FOLDER']
    _app = current_app._get_current_object()  # type: ignore[attr-defined]

    def stream():
        yield json.dumps({"progress": 5, "message": "Initializing import..."}) + "\n"

        # 1) Scrape the supplier URL once (depth depends on whether we have files).
        site_data = {"text": "", "html": ""}
        if supplier_url:
            yield json.dumps({"progress": 12, "message": "Reading supplier website..."}) + "\n"
            site_data = scrape_url_data_deep(supplier_url) if (mode != 'single' and not ai_filepaths) \
                        else scrape_url_data(supplier_url)

        # 2) Phase 2.2 — call the proforma extraction prompt regardless of
        #    mode. It splits output into source_facts (strict) and
        #    ai_enriched_details (deduced/composed: range_overview,
        #    sales_arguments, inferred_specs, seo_data, warranty inference).
        #    `mode` is passed through so the prompt's clustering instructions
        #    line up with what the user asked for in the UI.
        proforma_mode = 'single' if mode == 'single' else ('multiple' if mode == 'multiple' else 'auto')
        try:
            yield json.dumps({"progress": 30, "message": "Extracting source facts + AI enrichment..."}) + "\n"
            extracted_products = generate_proforma_data(
                file_paths=ai_filepaths,
                url_data=site_data,
                extraction_mode=proforma_mode,
                brand_hint=None,
            )
            if not extracted_products:
                yield json.dumps({"error": "AI returned no products from this source."}) + "\n"
                return
            # Phase 2.4: pull raw text out of every uploaded document so
            # proforma_to_pis_data can grep-verify each claimed source_facts
            # value. PDF/DOCX go through real parsers; images and unsupported
            # types return "" — under the strict-fact rule those fields then
            # render as AI-generated rather than facts.
            # Strict-fact rule: only the uploaded Proforma counts as the
            # verification source. A value that only appears on the scraped
            # supplier site is "from search", not a Proforma fact.
            raw_doc_text = extract_raw_text_from_files(ai_filepaths) or ""

            # Convert each proforma object into the legacy pis_data shape (with
            # `_field_origins` and `_spec_origins` so the verify UI can mark
            # AI-deduced fields with the ✨ pill).
            items_list = []
            for raw in extracted_products:
                pis = proforma_to_pis_data(
                    raw, raw_text=raw_doc_text, source_files=ai_filepaths
                ) or {}
                # Preserve the user-typed model_name in single mode so we don't
                # lose intent if the source_facts didn't echo it back.
                if mode == 'single' and model_name:
                    pis.setdefault('header_info', {})
                    if not pis['header_info'].get('product_name'):
                        pis['header_info']['product_name'] = model_name
                items_list.append(pis)
        except Exception as e:
            import traceback; traceback.print_exc()
            yield json.dumps({"error": f"AI extraction failed: {e}"}) + "\n"
            return

        # 3) Seed the UI with the detected item list.
        names_for_ui = []
        for idx, p in enumerate(items_list):
            header = (p or {}).get('header_info', {}) or {}
            disp = header.get('product_name') or header.get('model_number') \
                   or (model_name if mode == 'single' else f"Item_{idx+1}")
            names_for_ui.append(disp)
        total = len(items_list)
        yield json.dumps({
            "progress": 35,
            "message": f"Found {total} item{'s' if total != 1 else ''}.",
            "products": [{"name": n, "status": "pending"} for n in names_for_ui]
        }) + "\n"

        ai_filepath = ai_filepaths[0] if ai_filepaths else None
        single_redirect_id = None

        # 4) Unified per-item loop — same code path for single (n=1) and bulk.
        # Phase 2.3: insert randomized jitter between consecutive items in
        # bulk so the search engines don't see a perfectly regular cadence.
        from utils.image_processing import _human_jitter as _jitter
        with _app.app_context():
            for idx, p_data in enumerate(items_list):
                if idx > 0 and total > 1:
                    _jitter(2.0, 5.0)
                header = (p_data or {}).get('header_info', {}) or {}
                brand     = header.get('brand', '')
                model_id  = header.get('model_number', '')
                prod_name = header.get('product_name', '')
                display_name = (model_name if mode == 'single' and model_name
                                else (prod_name or model_id or f"Item_{idx+1}"))
                pct = 35 + int(((idx + 1) / total) * 60)
                yield json.dumps({
                    "progress": pct,
                    "message": f"Processing {idx+1}/{total}: {display_name}",
                    "item_update": {"name": display_name, "status": "searching"}
                }) + "\n"

                try:
                    extracted_image_path = None
                    if mode == 'single':
                        search_query = _build_rich_query(p_data, model_name)
                    else:
                        search_query = _build_bulk_search_query(brand, prod_name, model_id, display_name)

                    if contains_images and ai_filepath:
                        pdf_term = model_id or display_name
                        extracted_image_path = extract_specific_image(ai_filepath, pdf_term, upload_folder)

                    if not extracted_image_path:
                        ai_found = (p_data or {}).get('found_image_url')
                        if ai_found and str(ai_found).startswith('http'):
                            extracted_image_path = download_web_image(ai_found, display_name, upload_folder)

                    if not extracted_image_path:
                        public_url = find_and_validate_image(search_query, supplier_url)
                        if public_url:
                            extracted_image_path = download_web_image(public_url, display_name, upload_folder)

                    if not extracted_image_path:
                        simple_url = find_image_simple(search_query, supplier_url)
                        if simple_url:
                            extracted_image_path = download_web_image(simple_url, display_name, upload_folder)

                    # Phase 2.2: screenshot-based last-resort fallback for
                    # documents without product photos (and where direct
                    # image downloads are blocked by anti-hotlink protection).
                    # Phase 2.3: pass `brand` so the SERP scraper can lock
                    # onto the official brand domain when one is recognized.
                    if not extracted_image_path:
                        extracted_image_path = find_image_via_screenshot(
                            display_name, supplier_url, upload_folder,
                            brand=brand,
                        )

                    new_product = Product(
                        model_name=display_name, pis_data=p_data,
                        image_path=extracted_image_path,
                        seo_keywords=(p_data or {}).get('seo_data', {}).get('generated_keywords', ''),
                        workflow_stage='marketing_draft'
                    )
                    db.session.add(new_product)
                    db.session.commit()
                    log_event(new_product.id, get_current_username(), 'New Product Added',
                              f'Imported via Proforma ({mode} mode).', 'neutral')
                    save_version_snapshot(new_product, label='Initial version', is_major=True)

                    if mode == 'single':
                        single_redirect_id = new_product.id

                    yield json.dumps({"item_update": {"name": display_name, "status": "completed"}}) + "\n"

                except Exception as item_err:
                    print(f"⚠️ [Proforma] error for '{display_name}': {item_err}")
                    db.session.rollback()
                    yield json.dumps({"item_update": {"name": display_name, "status": "failed"}}) + "\n"

        # 5) Final redirect — single goes to the review page, bulk to dashboard.
        clear_pdf_cache()
        if mode == 'single' and single_redirect_id:
            redirect_url = url_for('marketing.review_pis_marketing', product_id=single_redirect_id)
        else:
            redirect_url = url_for('marketing.dashboard_marketing')
        yield json.dumps({"progress": 100, "message": "Import complete!", "redirect": redirect_url}) + "\n"

    # Flask's stubs declare stream_with_context as wanting Iterator[AnyStr]
    # specifically; our generators yield bare str chunks which IS valid at
    # runtime but trips the variance check. Silenced narrowly.
    return Response(stream_with_context(stream()), mimetype='application/x-ndjson')  # type: ignore[arg-type]


# ── PHASE 2.6 — BULK WIZARD ENDPOINTS ───────────────────────────────────────
#
# Phase A delivers the triage scan: the user uploads N proformas, we run one
# fast Gemini Flash classifier pass, and return a preview the user can
# inspect/rework before paying for the full extraction. State lives in a
# 30-min in-memory session (see utils/bulk_wizard.py). Subsequent phases will
# add cluster-edit ops, persisted-draft creation, and a workspace UI.
#

@marketing_bp.route('/import_proforma/auto_detect', methods=['POST'])
@limiter.limit("20 per minute")
def import_proforma_auto_detect():
    """Auto-Detect entry: one upload → classifier → routes to the right
    wizard with its session already primed.

    Saves uploaded files ONCE, runs `bulk_triage_scan` (the cheapest call
    we have that returns both `cluster_shape` and `item_count`), then:
      • cluster_shape == 'single' AND item_count <= 1 → single-mode response.
        Also runs `quick_scan_for_name` + renders a proforma preview so
        the single wizard can resume from "URL search" without re-doing
        the pre-scan.
      • Otherwise → multiple-mode response. Reuses the same triage payload
        the dedicated /bulk/triage endpoint produces, so the frontend can
        drop straight onto the cluster-preview workspace with no second
        upload + no second classifier call.

    Returns ONE JSON object (not NDJSON — the front-end shows a single
    "Analyzing…" spinner and dispatches on the response).
    """
    if session.get('role') != 'marketing':
        return {"error": "unauthorized"}, 401

    ai_files = request.files.getlist('ai_document')
    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)

    saved_paths: list[str] = []
    saved_names: list[str] = []
    for f in ai_files:
        if f and f.filename:
            filename = secure_filename(f.filename)
            fpath = os.path.join(upload_folder, filename)
            f.save(fpath)
            saved_paths.append(fpath)
            saved_names.append(filename)

    if not saved_paths:
        return {"error": "No files uploaded"}, 400

    # ── Step 1: triage classifier — gives us cluster_shape + item_count ──
    try:
        triage = bw.triage_scan(saved_paths)
    except Exception as e:
        return {"error": f"Auto-detect classifier failed: {e}"}, 500

    summary = (triage or {}).get('summary') or {}
    cluster_shape = (summary.get('cluster_shape') or 'single').lower()
    try:
        item_count = int(summary.get('item_count') or 0)
    except (TypeError, ValueError):
        item_count = 0

    # Routing rule — keep this dumb-simple: "single" only when the doc is
    # unambiguously a single product. Anything with multiple rows (variants,
    # distinct products, mixed) goes to the bulk workspace.
    is_single = (cluster_shape == 'single' and item_count <= 1)

    if not is_single:
        # ── Multiple → mirror /bulk/triage's contract ────────────────────
        groups = bw.derive_cluster_groups(triage.get('items') or [])
        token = bw.create_session({
            'file_paths':     saved_paths,
            'file_names':     saved_names,
            'origin_hint':    'unknown',  # auto-detect doesn't ask the user
            'triage':         triage,
            'cluster_groups': groups,
        })
        return {
            "mode":            "multiple",
            "reason":          summary.get('notes')
                                or f"Detected {item_count} item(s) — "
                                   f"cluster shape '{cluster_shape}'.",
            "saved_filenames": saved_names,
            "multiple": {
                "session_token":  token,
                "triage":         triage,
                "cluster_groups": groups,
                "file_names":     saved_names,
            },
        }

    # ── Single → also run quick_scan_for_name + proforma preview so the
    # single wizard's cascade can resume from /find_url without re-doing
    # the upload OR the pre-scan. ────────────────────────────────────────
    try:
        scan = sw.quick_scan_for_name(saved_paths)
    except Exception as e:
        scan = {"product_name": "", "brand": "", "model_number": "",
                "_error": f"pre-scan crashed: {e}"}

    try:
        preview_rel = sw.render_proforma_preview(
            saved_paths, (scan or {}).get('product_name') or '', upload_folder,
        )
    except Exception:
        preview_rel = None

    token = sw.create_session({
        'file_paths':       saved_paths,
        'file_names':       saved_names,
        'scan':             scan,
        'proforma_preview': preview_rel,
    })

    return {
        "mode":            "single",
        "reason":          (summary.get('notes')
                              or "Detected one product — routed to single-item flow."),
        "saved_filenames": saved_names,
        "single": {
            "session_token":    token,
            "scan":             scan,
            "proforma_preview": preview_rel,
            "file_names":       saved_names,
        },
    }


@marketing_bp.route('/import_proforma/bulk/triage', methods=['POST'])
@limiter.limit("20 per minute")
def import_proforma_bulk_triage():
    """Phase A — accept upload(s), persist them under upload_folder, run the
    bulk_triage_scan, and return a session token + preview JSON.

    Request form fields:
        ai_document      — one or more file uploads (multipart).
        kalachand_internal — "on" / "true" if user checked the
                             "this is an internal Kalachand proforma" box.
        triage_feedback  — optional free-text hint applied to the rework
                           prompt (Phase A: only used as instruction context;
                           the actual rework endpoint is Phase B).

    Response: NDJSON stream. Final payload includes `session_token`,
    `triage` (validated dict), `cluster_groups`, and `file_names`.
    """
    if session.get('role') != 'marketing':
        return Response('{"error":"unauthorized"}\n', status=401, mimetype='application/x-ndjson')

    ai_files = request.files.getlist('ai_document')
    upload_folder = current_app.config['UPLOAD_FOLDER']

    saved_paths: list[str] = []
    saved_names: list[str] = []
    for f in ai_files:
        if f and f.filename:
            filename = secure_filename(f.filename)
            fpath = os.path.join(upload_folder, filename)
            f.save(fpath)
            saved_paths.append(fpath)
            saved_names.append(filename)

    if not saved_paths:
        return Response('{"error":"No files uploaded"}\n', status=400, mimetype='application/x-ndjson')

    is_internal = (request.form.get('kalachand_internal') or '').strip().lower() in ('on', 'true', '1', 'yes')
    origin_hint = 'kalachand_internal' if is_internal else 'external_supplier'

    def stream():
        yield bw.log_step("Bulk Step 1 — Document upload")
        yield bw.log_progress(8, "Files received")
        for n in saved_names:
            yield bw.log_info(f"Saved {n}")

        yield bw.log_step("Bulk Step 2 — Triage scan")
        yield bw.log_progress(25, "Asking Gemini for the document layout...")
        # The literal fallback below mixes dict/list values, which Pyrefly
        # widens to `dict | list` and then complains when we call `.get` on
        # the result. Type the variable explicitly so both arms agree.
        triage: dict[str, Any]
        try:
            triage = bw.triage_scan(saved_paths, origin_hint=origin_hint)
        except Exception as e:
            yield bw.log_err(f"Triage crashed: {e}")
            triage = {"summary": {"item_count": 0, "density": "minimal",
                                  "has_images": "none", "origin": origin_hint,
                                  "cluster_shape": "single",
                                  "notes": f"Triage crashed: {e}"},
                      "items": []}

        s = triage.get('summary', {})
        yield bw.log_ok(f"Detected {s.get('item_count', 0)} item(s).")
        yield bw.log_ok(f"density        = '{s.get('density', '?')}'")
        yield bw.log_ok(f"has_images     = '{s.get('has_images', '?')}'")
        yield bw.log_ok(f"origin         = '{s.get('origin', '?')}'")
        yield bw.log_ok(f"cluster_shape  = '{s.get('cluster_shape', '?')}'")
        if s.get('notes'):
            yield bw.log_info(f"AI note: {s['notes']}")

        groups = bw.derive_cluster_groups(triage.get('items') or [])
        yield bw.log_ok(f"Grouped into {len(groups)} cluster(s).")

        token = bw.create_session({
            'file_paths':      saved_paths,
            'file_names':      saved_names,
            'origin_hint':     origin_hint,
            'triage':          triage,
            'cluster_groups':  groups,
        })

        yield bw.log_progress(45, "Triage complete — review the preview.")
        yield bw.log_payload(
            session_token=token,
            triage=triage,
            cluster_groups=groups,
            file_names=saved_names,
            origin_hint=origin_hint,
            done_step='triage',
        )

    # Flask's stubs declare stream_with_context as wanting Iterator[AnyStr]
    # specifically; our generators yield bare str chunks which IS valid at
    # runtime but trips the variance check. Silenced narrowly.
    return Response(stream_with_context(stream()), mimetype='application/x-ndjson')  # type: ignore[arg-type]


@marketing_bp.route('/import_proforma/bulk/workspace/<batch_id>', methods=['GET'])
def import_proforma_bulk_workspace(batch_id: str):
    """Phase D — workspace view for one bulk-import batch. Shows every
    draft Product whose `pis_data._bulk_batch_id` matches, with controls
    to enrich, edit, or commit each one."""
    if session.get('role') != 'marketing':
        return redirect(url_for('auth.login'))

    batch_id = (batch_id or '').strip()
    if not batch_id:
        return redirect(url_for('marketing.dashboard_marketing'))

    # JSONB containment lookup — finds drafts whose pis_data has this id.
    # Include in-progress / changes-requested stages too: once the user opens
    # a draft in Edit PIS and clicks Save, its stage flips to
    # 'marketing_in_progress', but it's still part of the batch and should
    # remain visible in the workspace.
    drafts = Product.query.filter(
        Product.deleted_at.is_(None),
        Product.workflow_stage.in_(
            ('marketing_draft', 'marketing_in_progress', 'marketing_changes_requested')
        ),
        Product.pis_data['_bulk_batch_id'].astext == batch_id,
    ).order_by(Product.id.asc()).all()

    if not drafts:
        flash('That bulk batch has no drafts (or it was already submitted).', 'info')
        return redirect(url_for('marketing.dashboard_marketing'))

    # Compose a lightweight summary for the top toolbar.
    counts = {
        'total':    len(drafts),
        'pending':  sum(1 for d in drafts if (d.pis_data or {}).get('_enrichment_status') == 'pending'),
        'done':     sum(1 for d in drafts if (d.pis_data or {}).get('_enrichment_status') == 'done'),
        'partial':  sum(1 for d in drafts if (d.pis_data or {}).get('_enrichment_status') == 'partial'),
        'failed':   sum(1 for d in drafts if (d.pis_data or {}).get('_enrichment_status') == 'failed'),
    }
    return render_template(
        'bulk_workspace.html',
        batch_id=batch_id,
        drafts=drafts,
        counts=counts,
    )


def _bulk_load_draft(batch_id: str, product_id: int):
    """Look up one draft Product belonging to `batch_id`. Returns the
    Product or None (caller decides what HTTP status to return)."""
    if not batch_id:
        return None
    return Product.query.filter(
        Product.id == product_id,
        Product.deleted_at.is_(None),
        Product.pis_data['_bulk_batch_id'].astext == batch_id,
    ).first()


@marketing_bp.route('/import_proforma/bulk/workspace/<batch_id>/<int:product_id>/enrich', methods=['POST'])
@limiter.limit("30 per minute")
def import_proforma_bulk_enrich_item(batch_id: str, product_id: int):
    """Phase D — run image + content + category enrichment on ONE draft.
    Returns the enriched fields so the workspace card can update without
    a page reload. The Product row is updated in-place (atomic)."""
    if session.get('role') != 'marketing':
        return {"error": "unauthorized"}, 401
    product = _bulk_load_draft(batch_id, product_id)
    if not product:
        return {"error": "draft not found in this batch"}, 404

    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)

    # Phase D step-by-step: focus on CONTENT first. Image extraction is
    # opted-in via ?include_images=1 (defaults off so the workspace can
    # validate variant content rendering before we add image complexity).
    include_images = (request.args.get('include_images') or '').strip().lower() in ('1', 'true', 'yes')
    tasks = ['content', 'category']
    if include_images:
        tasks.append('image')

    try:
        enriched = bw.enrich_product(product.pis_data or {}, upload_folder, tasks=tasks)
    except Exception as e:
        return {"error": f"enrichment failed: {e}"}, 500

    product.pis_data = enriched
    if enriched.get('_image_path'):
        product.image_path = enriched['_image_path']
    if enriched.get('_seo_keywords_pending'):
        # `seo_keywords` column is varchar(255). The variant-aware prompt
        # produces longer keyword strings (it covers every variant SKU and
        # label) so trim before save to avoid the StringDataRightTruncation
        # error that was breaking 4D wardrobe enrichment. The full keyword
        # list is still preserved inside pis_data.seo_data.generated_keywords.
        kw = (enriched['_seo_keywords_pending'] or '').strip()
        product.seo_keywords = kw[:255]
    flag_modified(product, 'pis_data')

    log_event(product.id, get_current_username(), 'AI Generated',
              'Bulk enrichment ran (image / content / category).', 'neutral')
    save_version_snapshot(product, label='Bulk enrichment', is_major=False)
    db.session.commit()

    return {
        "product_id":               product.id,
        "header_info":              enriched.get('header_info', {}),
        "range_overview":           enriched.get('range_overview', ''),
        "sales_arguments":          enriched.get('sales_arguments', []),
        "technical_specifications": enriched.get('technical_specifications', {}),
        "image_path":               enriched.get('_image_path') or product.image_path or '',
        "image_candidates":         enriched.get('_bulk_image_candidates', []),
        "enrichment_status":        enriched.get('_enrichment_status', 'pending'),
        "tasks":                    enriched.get('_enrichment_tasks', {}),
    }


@marketing_bp.route('/import_proforma/bulk/workspace/<batch_id>/<int:product_id>/save', methods=['POST'])
@limiter.limit("60 per minute")
def import_proforma_bulk_save_item(batch_id: str, product_id: int):
    """Phase D — persist a partial pis_data update from the workspace.
    Body is a flat JSON dict whose keys are dotted paths into pis_data
    (e.g. `header_info.product_name`, `range_overview`, `_image_path`).
    Each value is set into pis_data at that path; intermediate dicts are
    auto-created. Unknown / unsafe paths are silently dropped."""
    if session.get('role') != 'marketing':
        return {"error": "unauthorized"}, 401
    product = _bulk_load_draft(batch_id, product_id)
    if not product:
        return {"error": "draft not found in this batch"}, 404

    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict) or not body:
        return {"error": "JSON body required"}, 400

    pis = dict(product.pis_data or {})

    # Allowed top-level keys — keep editable surface tight so the workspace
    # can't accidentally clobber bookkeeping fields like _bulk_batch_id.
    ALLOWED_PREFIXES = (
        'header_info.product_name', 'header_info.brand',
        'header_info.model_number', 'header_info.price_estimate',
        'range_overview', 'sales_arguments',
        'technical_specifications', 'warranty_service',
        'seo_data',
        '_image_path',
    )

    def _set_path(target: dict, dotted: str, value):
        parts = dotted.split('.')
        cur = target
        for p in parts[:-1]:
            if not isinstance(cur.get(p), dict):
                cur[p] = {}
            cur = cur[p]
        cur[parts[-1]] = value

    edited_header = False
    for key, value in body.items():
        if not isinstance(key, str):
            continue
        if not any(key == p or key.startswith(p + '.') for p in ALLOWED_PREFIXES):
            continue
        # `model_name` is the Product column, mirror it when name changes.
        _set_path(pis, key, value)
        if key.startswith('header_info.'):
            edited_header = True
        if key == 'header_info.product_name' and isinstance(value, str) and value.strip():
            product.model_name = value.strip()[:100]
        if key == '_image_path' and isinstance(value, str):
            product.image_path = value or None
    # Latch the "user edited the header" flag so future enrichments don't
    # overwrite their changes with the AI's family-level header_info.
    if edited_header:
        pis['_user_edited_header'] = True

    product.pis_data = pis
    flag_modified(product, 'pis_data')
    db.session.commit()
    return {"ok": True}


@marketing_bp.route('/import_proforma/bulk/workspace/<batch_id>/commit', methods=['POST'])
@limiter.limit("10 per minute")
def import_proforma_bulk_commit(batch_id: str):
    """Phase D — submit every draft in this batch to the Director (sets
    workflow_stage to 'pending_director_pis'). Skips drafts whose stage
    has already moved on (e.g. user edited one through the legacy review
    page mid-workflow)."""
    if session.get('role') != 'marketing':
        return {"error": "unauthorized"}, 401

    drafts = Product.query.filter(
        Product.deleted_at.is_(None),
        Product.workflow_stage == 'marketing_draft',
        Product.pis_data['_bulk_batch_id'].astext == batch_id,
    ).all()
    if not drafts:
        return {"error": "no drafts in this batch"}, 404

    actor = get_current_username()
    moved = 0
    try:
        for d in drafts:
            d.workflow_stage = 'pending_director_pis'
            log_event(d.id, actor, 'Sent for Director Review',
                      f'Submitted via bulk workspace (batch {batch_id[:8]}).',
                      'waiting')
            save_version_snapshot(d, label='Submitted for Director review',
                                  is_major=True)
            moved += 1
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return {"error": f"commit failed: {e}"}, 500

    return {"submitted": moved, "redirect": url_for('marketing.dashboard_marketing')}


@marketing_bp.route('/import_proforma/bulk/workspace/<batch_id>/discard', methods=['POST'])
@limiter.limit("10 per minute")
def import_proforma_bulk_discard(batch_id: str):
    """Phase D — soft-delete every draft in this batch."""
    if session.get('role') != 'marketing':
        return {"error": "unauthorized"}, 401

    from datetime import datetime as _dt, timezone as _tz
    drafts = Product.query.filter(
        Product.deleted_at.is_(None),
        Product.pis_data['_bulk_batch_id'].astext == batch_id,
    ).all()
    if not drafts:
        return {"error": "no drafts in this batch"}, 404

    actor = get_current_username()
    now = _dt.now(_tz.utc).replace(tzinfo=None)
    try:
        for d in drafts:
            d.deleted_at = now
            log_event(d.id, actor, 'Discarded',
                      f'Bulk batch {batch_id[:8]} discarded by user.',
                      'neutral')
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return {"error": f"discard failed: {e}"}, 500

    return {"discarded": len(drafts), "redirect": url_for('marketing.dashboard_marketing')}


# ── Phase D · Image extraction (batch routing + per-draft regen) ────────────


def _bulk_drafts_for_batch(batch_id: str) -> list[Product]:
    """Every active draft in a batch (any pre-Director stage), ordered by id.
    Mirrors the workspace query so endpoints stay consistent."""
    return Product.query.filter(
        Product.deleted_at.is_(None),
        Product.workflow_stage.in_(
            ('marketing_draft', 'marketing_in_progress', 'marketing_changes_requested')
        ),
        Product.pis_data['_bulk_batch_id'].astext == batch_id,
    ).order_by(Product.id.asc()).all()


def _draft_to_routing_meta(d: Product) -> dict:
    """Compose the metadata block the routing/pipeline code needs for one
    draft. `source_pages` (cluster-level) and per-variant `source_pages`
    are populated from triage so the variant-aware image pipeline can slice
    the proforma deterministically before extraction.
    """
    pis = d.pis_data or {}
    header = pis.get('header_info') or {}
    kind = (pis.get('_bulk_cluster_kind') or 'singleton').lower()
    variants_full = pis.get('variants') or []
    variants_meta = []
    if isinstance(variants_full, list):
        for v in variants_full:
            if not isinstance(v, dict):
                continue
            variants_meta.append({
                'label':        (v.get('label') or '').strip(),
                'model_number': (v.get('model_number') or '').strip(),
                'source_pages': list(v.get('source_pages') or []),
            })
    return {
        'id':           d.id,
        'name':         (header.get('product_name') or '').strip()
                          or (pis.get('_bulk_cluster_label') or '').strip()
                          or f"Draft #{d.id}",
        'brand':        (header.get('brand') or '').strip(),
        'model_number': (header.get('model_number') or '').strip(),
        'kind':         kind,
        'source_pages': list(pis.get('_bulk_source_pages') or []),
        'variants':     variants_meta,
    }


def _resolve_proforma_paths(drafts: list[Product]) -> list[str]:
    """Resolve `_bulk_source_filenames` (relative basenames) back to absolute
    paths under UPLOAD_FOLDER. Walks every draft because a batch is allowed
    to span multiple proforma files (single triage call but multiple uploads)."""
    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)
    seen, abs_paths = set(), []
    for d in drafts:
        for fn in (d.pis_data or {}).get('_bulk_source_filenames') or []:
            if not fn or fn in seen:
                continue
            seen.add(fn)
            p = os.path.join(upload_folder, fn)
            if os.path.exists(p):
                abs_paths.append(p)
    return abs_paths


@marketing_bp.route('/import_proforma/bulk/workspace/<batch_id>/extract_images', methods=['POST'])
@limiter.limit("6 per minute")
def import_proforma_bulk_extract_images(batch_id: str):
    """Phase D Image — variant-aware Slice → Assign → Extract → Save pipeline.

    Uses the `source_pages` triage gives us per row to slice the proforma
    into per-product (and per-variant) mini-PDFs before extraction. Each
    slice only contains pages relevant to ONE product, so cross-row mixups
    disappear and we no longer need web/AI fallbacks at the batch level.

    Streams NDJSON progress so the workspace can light up cards as they
    finish. Idempotent: re-running appends new candidates to the picker
    without dropping existing user-selected images.
    """
    if session.get('role') != 'marketing':
        return {"error": "unauthorized"}, 401

    drafts = _bulk_drafts_for_batch(batch_id)
    if not drafts:
        return {"error": "no drafts in this batch"}, 404

    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)

    file_paths = _resolve_proforma_paths(drafts)
    drafts_meta = [_draft_to_routing_meta(d) for d in drafts]

    # Snapshot draft IDs so the worker thread can re-fetch fresh ORM objects
    # inside the request context without sharing detached instances.
    draft_ids = [d.id for d in drafts]

    from utils import image_pipeline as ip

    def stream():
        # Acknowledge so the frontend can flip cards into 'enriching' state.
        yield json.dumps({"step": "start", "drafts": len(draft_ids),
                          "files": len(file_paths)}) + "\n"

        # Capture log lines from the pipeline so we can interleave them into
        # the NDJSON stream after the call returns. Live streaming during
        # the call would require threading the generator into the worker
        # pool — overkill for the seconds of work the pipeline takes.
        log_buffer: list[dict] = []

        def _capture(level: str, msg: str) -> None:
            log_buffer.append({"log": {"type": level, "text": msg}})

        try:
            results = ip.unified_extract(
                drafts_meta, file_paths, upload_folder, log_cb=_capture,
            )
        except Exception as e:
            yield json.dumps({"step": "error", "error": f"pipeline failed: {e}"}) + "\n"
            return

        # Persist + emit per-draft updates.
        for entry in log_buffer:
            yield json.dumps(entry) + "\n"

        for did in draft_ids:
            d = Product.query.get(did)
            if not d:
                continue
            bucket = results.get(did) or {
                "image_path": None, "variant_paths": {},
                "candidates": [], "status": "failed",
            }
            try:
                ip.save_images_to_variant_gallery(d, bucket)
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                yield json.dumps({
                    "step": "draft", "draft_id": did,
                    "status": "failed", "error": f"persist failed: {e}",
                }) + "\n"
                continue

            yield json.dumps({
                "step":         "draft",
                "draft_id":     did,
                "status":       bucket.get("status") or "pending",
                "image_path":   bucket.get("image_path"),
                "candidates":   bucket.get("candidates") or [],
                "variant_paths": bucket.get("variant_paths") or {},
            }) + "\n"

        # ── Phase 2: web + AI fallback for drafts with no image ─────────────
        # Only runs under USE_UNIFIED_EXTRACT=1 (i.e. the new pipeline).
        # The old slice-based pipeline has its own routing and doesn't need this.
        empty_ids = [
            did for did in draft_ids
            if not results.get(did, {}).get("image_path")
        ]
        if empty_ids:
            yield json.dumps({"log": {"type": "info",
                "text": f"Fallback pass: {len(empty_ids)} draft(s) with no document image — trying web + AI"}}) + "\n"

            from utils.single_wizard import extract_image_candidates_from_web

            for did in empty_ids:
                d = Product.query.get(did)
                if not d:
                    continue
                meta        = _draft_to_routing_meta(d)
                model_name  = (meta.get("name") or meta.get("model_number") or "").strip()
                brand       = (meta.get("brand") or "").strip()
                pis_snap    = dict(d.pis_data or {})
                supplier_url = pis_snap.get("_supplier_url")

                fb_cands: list[dict] = []

                # Web fallback only. Auto nano-banana was removed — it now
                # runs ONLY when the user clicks "Generate via AI" on a card,
                # so we never burn a Gemini call on a draft the user might
                # discard. Cap=2 keeps latency reasonable across a batch.
                if model_name:
                    try:
                        web_hits = extract_image_candidates_from_web(
                            model_name, supplier_url, upload_folder,
                            brand=brand, max_results=2,
                        )
                        for wh in (web_hits or []):
                            path = wh.get("path")
                            if path:
                                fb_cands.append({
                                    "path":          path,
                                    "source":        "web",
                                    "page_url":      wh.get("page_url"),
                                    "variant_sku":   "",
                                    "matched_label": "",
                                    "confidence":    "medium",
                                })
                    except Exception as exc:
                        yield json.dumps({"log": {"type": "warn",
                            "text": f"Draft #{did} web fallback: {exc}"}}) + "\n"

                if not fb_cands:
                    continue

                fallback_bucket: dict = {
                    "image_path":    fb_cands[0]["path"],
                    "variant_paths": {},
                    "candidates":    fb_cands,
                    "status":        "done",
                }
                try:
                    ip.save_images_to_variant_gallery(d, fallback_bucket)
                    db.session.commit()
                except Exception as exc:
                    db.session.rollback()
                    yield json.dumps({"log": {"type": "warn",
                        "text": f"Draft #{did} fallback persist failed: {exc}"}}) + "\n"
                    continue

                pis_after = dict(d.pis_data or {})
                yield json.dumps({
                    "step":          "draft",
                    "draft_id":      did,
                    "status":        "done",
                    "image_path":    d.image_path or "",
                    "candidates":    pis_after.get("_bulk_image_candidates") or [],
                    "variant_paths": {},
                }) + "\n"

        yield json.dumps({"step": "done"}) + "\n"

    # Flask's stubs declare stream_with_context as wanting Iterator[AnyStr]
    # specifically; our generators yield bare str chunks which IS valid at
    # runtime but trips the variance check. Silenced narrowly.
    return Response(stream_with_context(stream()), mimetype='application/x-ndjson')  # type: ignore[arg-type]


@marketing_bp.route(
    '/import_proforma/bulk/workspace/<batch_id>/<int:product_id>/image/web',
    methods=['POST'])
@limiter.limit("20 per minute")
def import_proforma_bulk_image_web(batch_id: str, product_id: int):
    """Per-draft web image regeneration. Accepts an optional `mode` query
    arg:
      • `?mode=supplier` — locks search to the brand's official domain
                           (best for internal SKUs).
      • `?mode=general`  — broad search with brand-prepended query
                           (best for products with wide web presence).
    Defaults to `general`. Returns the new candidate list; the workspace
    appends them to the picker without changing the current selection."""
    if session.get('role') != 'marketing':
        return {"error": "unauthorized"}, 401
    product = _bulk_load_draft(batch_id, product_id)
    if not product:
        return {"error": "draft not found in this batch"}, 404

    mode = (request.args.get('mode') or 'general').strip().lower()
    if mode not in ('general', 'supplier'):
        mode = 'general'

    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)

    from utils import bulk_image_routing as bir
    meta = _draft_to_routing_meta(product)
    candidates = bir.regenerate_image_via_web(meta, upload_folder,
                                              max_results=3, mode=mode)
    if not candidates:
        return {"candidates": [],
                "error": f"no {mode}-mode web results"}, 200

    pis = dict(product.pis_data or {})
    existing = pis.get('_bulk_image_candidates') or []
    seen = {c.get('path') for c in existing if isinstance(c, dict)}
    for c in candidates:
        path = c.get('path')
        if not path or path in seen:
            continue
        existing.append({
            'path': path, 'source': 'web',
            'page_url': c.get('page_url'),
            'variant_sku': '', 'matched_label': '', 'confidence': 'medium',
        })
        seen.add(path)
    pis['_bulk_image_candidates'] = existing
    if not pis.get('_image_path') and candidates:
        first = candidates[0].get('path')
        if first:
            pis['_image_path'] = first
            product.image_path = first
    product.pis_data = pis
    flag_modified(product, 'pis_data')
    db.session.commit()

    return {
        "image_path": pis.get('_image_path') or '',
        "candidates": pis.get('_bulk_image_candidates') or [],
    }


@marketing_bp.route(
    '/import_proforma/bulk/workspace/<batch_id>/<int:product_id>/image/extract_from_url',
    methods=['POST'])
@limiter.limit("20 per minute")
def import_proforma_bulk_image_extract_from_url(batch_id: str, product_id: int):
    """Per-draft on-demand: user pastes a URL (e.g. they found the exact
    product page) and we scrape + download up to 3 images from it. Same
    primitives as the auto web pipeline, just rooted at a user URL
    instead of the discovered supplier URL.
    """
    if session.get('role') != 'marketing':
        return {"error": "unauthorized"}, 401
    product = _bulk_load_draft(batch_id, product_id)
    if not product:
        return {"error": "draft not found in this batch"}, 404

    payload = request.get_json(silent=True) or {}
    suggested_url = (payload.get('url') or '').strip()
    if not suggested_url:
        return {"error": "url required"}, 400

    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)

    from utils.single_wizard import extract_images_from_user_url
    meta = _draft_to_routing_meta(product)
    model_name = (meta.get('name') or meta.get('model_number') or 'product').strip()

    try:
        results = extract_images_from_user_url(
            suggested_url, model_name, upload_folder, max_results=3,
        ) or []
    except Exception as e:
        return {"error": f"Suggested-URL fetch failed: {e}"}, 502

    if not results:
        return {"candidates": [],
                "error": "no usable images on the suggested page"}, 200

    pis = dict(product.pis_data or {})
    existing = pis.get('_bulk_image_candidates') or []
    seen = {c.get('path') for c in existing if isinstance(c, dict)}
    appended: list[dict] = []
    for r in results:
        path = r.get('path')
        if not path or path in seen:
            continue
        entry = {
            'path':          path,
            'source':        'user_url',
            'page_url':      suggested_url,
            'variant_sku':   '',
            'matched_label': '',
            'confidence':    'medium',
        }
        existing.append(entry)
        appended.append(entry)
        seen.add(path)
    pis['_bulk_image_candidates'] = existing
    if not pis.get('_image_path') and appended:
        first = appended[0]['path']
        pis['_image_path'] = first
        product.image_path = first
    product.pis_data = pis
    flag_modified(product, 'pis_data')
    db.session.commit()

    return {
        "image_path": pis.get('_image_path') or '',
        "candidates": pis.get('_bulk_image_candidates') or [],
    }


@marketing_bp.route(
    '/import_proforma/bulk/workspace/<batch_id>/<int:product_id>/image/ai',
    methods=['POST'])
@limiter.limit("20 per minute")
def import_proforma_bulk_image_ai(batch_id: str, product_id: int):
    """Per-draft AI (nano-banana) regeneration — runs the strict prompt over
    the proforma source and saves a 4:3 isolated render. Useful when the
    bbox crop is too tight or the doc had no clear product photo."""
    if session.get('role') != 'marketing':
        return {"error": "unauthorized"}, 401
    product = _bulk_load_draft(batch_id, product_id)
    if not product:
        return {"error": "draft not found in this batch"}, 404

    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)

    file_paths = _resolve_proforma_paths([product])
    if not file_paths:
        return {"error": "no proforma source on file"}, 400

    from utils import bulk_image_routing as bir
    meta = _draft_to_routing_meta(product)
    rel = bir.regenerate_image_via_ai(meta, file_paths, upload_folder)
    if not rel:
        return {"error": "AI generation returned no image"}, 502

    pis = dict(product.pis_data or {})
    candidates = pis.get('_bulk_image_candidates') or []
    if not any(c.get('path') == rel for c in candidates if isinstance(c, dict)):
        candidates.append({
            'path': rel, 'source': 'ai', 'variant_sku': '',
            'matched_label': '', 'confidence': 'medium',
        })
    pis['_bulk_image_candidates'] = candidates
    pis['_image_path'] = rel
    product.image_path = rel
    product.pis_data = pis
    flag_modified(product, 'pis_data')
    db.session.commit()
    return {"image_path": rel, "candidates": candidates}


@marketing_bp.route(
    '/import_proforma/bulk/workspace/<batch_id>/<int:product_id>/image/upload',
    methods=['POST'])
@limiter.limit("30 per minute")
def import_proforma_bulk_image_upload(batch_id: str, product_id: int):
    """Per-draft manual upload — accepts an `image` form field, saves it
    under `static/uploads/`, registers it as the primary image, and adds
    it to the candidate list."""
    if session.get('role') != 'marketing':
        return {"error": "unauthorized"}, 401
    product = _bulk_load_draft(batch_id, product_id)
    if not product:
        return {"error": "draft not found in this batch"}, 404

    f = request.files.get('image')
    if not f or not f.filename:
        return {"error": "no file uploaded"}, 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.webp'):
        return {"error": "unsupported file type — use JPG/PNG/WebP"}, 400

    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)

    safe_stem = secure_filename(os.path.splitext(f.filename)[0]) or 'upload'
    filename = f"bulkup_{product.id}_{int(time.time())}_{safe_stem}{ext}"
    save_path = os.path.join(upload_folder, filename)
    f.save(save_path)
    rel = f"uploads/{filename}"

    pis = dict(product.pis_data or {})
    candidates = pis.get('_bulk_image_candidates') or []
    candidates.append({
        'path': rel, 'source': 'upload', 'variant_sku': '',
        'matched_label': '', 'confidence': 'high',
    })
    pis['_bulk_image_candidates'] = candidates
    pis['_image_path'] = rel
    product.image_path = rel
    product.pis_data = pis
    flag_modified(product, 'pis_data')
    db.session.commit()
    return {"image_path": rel, "candidates": candidates}


@marketing_bp.route(
    '/import_proforma/bulk/workspace/<batch_id>/<int:product_id>/image/reassign',
    methods=['POST'])
@limiter.limit("60 per minute")
def import_proforma_bulk_image_reassign(batch_id: str, product_id: int):
    """Reassign an existing candidate image to a specific variant SKU.

    Body: {"path": "uploads/...", "variant_sku": "MODEL-SKU"}

    Updates:
      • `_bulk_image_candidates[i].variant_sku` for the matching candidate
      • Removes `path` from every variant's image_paths to avoid duplicates
      • Assigns `path` as the first image for `variant_sku`

    Returns updated `candidates` + `variants` so the card can re-render.
    """
    if session.get('role') != 'marketing':
        return {"error": "unauthorized"}, 401
    product = _bulk_load_draft(batch_id, product_id)
    if not product:
        return {"error": "draft not found in this batch"}, 404

    payload     = request.get_json(silent=True) or {}
    path        = (payload.get("path") or "").strip()
    variant_sku = (payload.get("variant_sku") or "").strip()
    if not path:
        return {"error": "path is required"}, 400

    pis = dict(product.pis_data or {})

    # Update the candidate's variant_sku tag
    candidates = pis.get("_bulk_image_candidates") or []
    for c in candidates:
        if isinstance(c, dict) and c.get("path") == path:
            c["variant_sku"] = variant_sku
            break

    # Re-assign variant image_paths: strip path from all, then prepend to target
    variants = pis.get("variants") or []
    for v in variants:
        if not isinstance(v, dict):
            continue
        existing = list(v.get("image_paths") or [])
        if v.get("image_path") == path:
            v.pop("image_path", None)
        if path in existing:
            existing.remove(path)
        v["image_paths"] = existing
        # Re-derive image_path from first remaining path after removal
        if not v.get("image_path") and existing:
            v["image_path"] = existing[0]

    # Add path to the target variant
    if variant_sku:
        for v in variants:
            if not isinstance(v, dict):
                continue
            if (v.get("model_number") or "").strip() == variant_sku:
                paths = list(v.get("image_paths") or [])
                if path not in paths:
                    paths.insert(0, path)
                v["image_paths"] = paths
                v["image_path"]  = paths[0]
                break

    pis["_bulk_image_candidates"] = candidates
    pis["variants"]               = variants
    product.pis_data = pis
    flag_modified(product, "pis_data")
    db.session.commit()

    return {"candidates": candidates, "variants": variants}


@marketing_bp.route(
    '/import_proforma/bulk/workspace/<batch_id>/<int:product_id>/image/page_preview',
    methods=['GET'])
@limiter.limit("60 per minute")
def import_proforma_bulk_image_page_preview(batch_id: str, product_id: int):
    """Render ONE page of the proforma as a static-servable PNG so the
    workspace's manual-crop modal has something to display. Defaults to
    page 0; client passes `?page=N` for multi-page documents."""
    if session.get('role') != 'marketing':
        return {"error": "unauthorized"}, 401
    product = _bulk_load_draft(batch_id, product_id)
    if not product:
        return {"error": "draft not found in this batch"}, 404
    file_paths = _resolve_proforma_paths([product])
    if not file_paths:
        return {"error": "no proforma source on file"}, 400
    try:
        page_index = max(0, int(request.args.get('page', 0)))
    except (TypeError, ValueError):
        page_index = 0

    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)

    from utils import bulk_image_routing as bir
    rel = bir.render_proforma_page(file_paths[0], page_index, upload_folder)
    if not rel:
        return {"error": "page render failed"}, 502

    # Page count — needed by the workspace's crop modal so the prev/next
    # navigator can disable itself at the boundaries. Cheap: PyMuPDF only
    # opens the file metadata. For non-PDF uploads (jpg/png/webp), there
    # is exactly one page.
    page_count = 1
    src = file_paths[0]
    if os.path.splitext(src)[1].lower() == '.pdf':
        try:
            import fitz
            # PyMuPDF re-exports `open` from a C extension; the Python stub
            # doesn't expose it but it's a stable runtime API used across the
            # codebase (see utils/bulk_image_routing.py).
            with fitz.open(src) as _doc:  # type: ignore[attr-defined]
                page_count = len(_doc) or 1
        except Exception:
            page_count = 1

    return {"path": rel, "page": page_index, "page_count": page_count}


@marketing_bp.route(
    '/import_proforma/bulk/workspace/<batch_id>/<int:product_id>/image/crop',
    methods=['POST'])
@limiter.limit("30 per minute")
def import_proforma_bulk_image_crop(batch_id: str, product_id: int):
    """Per-draft manual crop — accepts a relative crop rect over a
    static-servable source image (rendered proforma page or any existing
    candidate path). Saves a new JPEG and sets it as the primary image.

    Same path-traversal defense as /import_proforma/single/crop:
    realpath(source) MUST live inside UPLOAD_FOLDER.
    """
    if session.get('role') != 'marketing':
        return {"error": "unauthorized"}, 401
    product = _bulk_load_draft(batch_id, product_id)
    if not product:
        return {"error": "draft not found in this batch"}, 404

    payload = request.get_json(silent=True) or {}
    source_path = (payload.get('source_path') or '').strip()
    crop = payload.get('crop') or {}
    try:
        x = float(crop.get('x', 0)); y = float(crop.get('y', 0))
        w = float(crop.get('w', 0)); h = float(crop.get('h', 0))
    except (TypeError, ValueError):
        return {"error": "crop must contain x, y, w, h as numbers in [0,1]"}, 400
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1):
        return {"error": "crop out of range — x,y in [0,1) and w,h in (0,1]"}, 400
    if x + w > 1.0001 or y + h > 1.0001:
        return {"error": "crop extends beyond image bounds"}, 400

    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)
    static_root = os.path.join(current_app.root_path, 'static')

    if not source_path:
        return {"error": "source_path required"}, 400
    if source_path.startswith('/static/'):
        source_path = source_path[len('/static/'):]
    elif source_path.startswith('static/'):
        source_path = source_path[len('static/'):]

    abs_source = os.path.realpath(os.path.join(static_root, source_path))
    abs_upload = os.path.realpath(upload_folder)
    try:
        common = os.path.commonpath([abs_source, abs_upload])
    except ValueError:
        common = ''
    if common != abs_upload:
        return {"error": "source_path outside UPLOAD_FOLDER"}, 400
    if not os.path.exists(abs_source):
        return {"error": "source image not found"}, 404

    # `preview` mode: save the crop to disk but DON'T commit it to the DB
    # yet. The frontend renders the saved file as a confirmation thumbnail
    # and the user clicks "Looks good — add to gallery" → /crop_commit, or
    # "Redo" → /crop_discard. Files use a `bulkpreview_` prefix so the
    # commit/discard endpoints can verify the path's provenance.
    is_preview = (request.args.get('preview') or '').lower() in ('1', 'true', 'yes')

    try:
        from PIL import Image as _PILImage
        img = _PILImage.open(abs_source)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        iw, ih = img.size
        left = max(0, min(int(round(x * iw)), iw - 1))
        top = max(0, min(int(round(y * ih)), ih - 1))
        right = max(left + 1, min(int(round((x + w) * iw)), iw))
        bottom = max(top + 1, min(int(round((y + h) * ih)), ih))
        cropped = img.crop((left, top, right, bottom))

        MAX_EDGE = 1600
        if max(cropped.size) > MAX_EDGE:
            cropped.thumbnail((MAX_EDGE, MAX_EDGE), _PILImage.LANCZOS)  # type: ignore[attr-defined]

        header = (product.pis_data or {}).get('header_info') or {}
        target = (header.get('product_name') or '').strip() or f"draft_{product.id}"
        safe_name = secure_filename(target) or 'product'
        prefix = 'bulkpreview' if is_preview else 'bulkcrop'
        filename = f"{prefix}_{safe_name}_{int(time.time() * 1000)}.jpg"
        out_path = os.path.join(upload_folder, filename)
        cropped.save(out_path, quality=95)
        rel = f"uploads/{filename}"

        if is_preview:
            # Don't touch the DB — the frontend will call /crop_commit when
            # the user confirms. Return both keys for backward-compat with
            # any caller that still expects `image_path`.
            return {"preview_path": rel, "image_path": rel}

        # Legacy direct-save path (kept for callers that don't use the
        # preview flow). Adds to candidates AND to additional_images so the
        # PIS PDF gallery picks the crop up automatically. Does NOT promote
        # it to primary — the user picks the main image from the picker.
        pis = dict(product.pis_data or {})
        candidates = pis.get('_bulk_image_candidates') or []
        candidates.append({
            'path': rel, 'source': 'crop', 'variant_sku': '',
            'matched_label': '', 'confidence': 'high',
        })
        pis['_bulk_image_candidates'] = candidates
        pis['_image_path'] = pis.get('_image_path') or rel
        product.pis_data = pis
        flag_modified(product, 'pis_data')

        existing_extras = list(product.additional_images or [])
        if rel not in existing_extras and rel != product.image_path:
            existing_extras.append(rel)
            product.additional_images = existing_extras
            flag_modified(product, 'additional_images')

        if not product.image_path:
            product.image_path = rel
        db.session.commit()
        return {"image_path": product.image_path or rel, "candidates": candidates}
    except Exception as e:
        return {"error": f"crop failed: {e}"}, 500


def _is_safe_preview_path(rel_path: str, upload_folder: str) -> tuple[bool, str]:
    """Verify a `uploads/bulkpreview_*.jpg` path is real, lives inside
    UPLOAD_FOLDER, and looks like a preview file produced by /image/crop?preview=1.
    Returns (ok, abs_path_or_error_msg).
    """
    if not rel_path or not isinstance(rel_path, str):
        return False, 'preview_path required'
    p = rel_path
    if p.startswith('/static/'):
        p = p[len('/static/'):]
    elif p.startswith('static/'):
        p = p[len('static/'):]
    if not p.startswith('uploads/'):
        return False, 'preview_path must be under uploads/'
    basename = os.path.basename(p)
    if not basename.startswith('bulkpreview_'):
        return False, 'preview_path must be a bulkpreview_* file'
    abs_path = os.path.realpath(os.path.join(upload_folder, basename))
    abs_upload = os.path.realpath(upload_folder)
    try:
        common = os.path.commonpath([abs_path, abs_upload])
    except ValueError:
        common = ''
    if common != abs_upload:
        return False, 'preview_path outside UPLOAD_FOLDER'
    if not os.path.exists(abs_path):
        return False, 'preview file not found'
    return True, abs_path


@marketing_bp.route(
    '/import_proforma/bulk/workspace/<batch_id>/<int:product_id>/image/crop_commit',
    methods=['POST'])
@limiter.limit("60 per minute")
def import_proforma_bulk_image_crop_commit(batch_id: str, product_id: int):
    """Commit a previously-previewed crop to the draft's gallery.

    Body: {preview_path: "uploads/bulkpreview_..."}.

    The preview file is renamed to a permanent `bulkcrop_*.jpg` name and
    appended to BOTH `_bulk_image_candidates` (for the workspace picker)
    AND `additional_images` (for the PIS PDF gallery). The crop is NEVER
    promoted to primary — that's the user's call from the picker.

    Idempotent in spirit: the rename can only happen once, but if the user
    somehow commits the same preview twice the second call returns the
    existing candidates list with no duplicate entry (path equality check).
    """
    if session.get('role') != 'marketing':
        return {"error": "unauthorized"}, 401
    product = _bulk_load_draft(batch_id, product_id)
    if not product:
        return {"error": "draft not found in this batch"}, 404

    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)

    payload = request.get_json(silent=True) or {}
    preview_path = (payload.get('preview_path') or '').strip()
    ok, info = _is_safe_preview_path(preview_path, upload_folder)
    if not ok:
        return {"error": info}, 400
    abs_preview = info  # absolute path

    # Rename preview → permanent. This is cheap (POSIX rename within the
    # same directory) and avoids an extra disk write. If the rename fails
    # we leave the preview file alone and surface the error.
    basename = os.path.basename(abs_preview)
    perm_basename = basename.replace('bulkpreview_', 'bulkcrop_', 1)
    abs_perm = os.path.join(upload_folder, perm_basename)
    try:
        os.replace(abs_preview, abs_perm)
    except Exception as e:
        return {"error": f"could not finalize crop: {e}"}, 500
    rel = f"uploads/{perm_basename}"

    pis = dict(product.pis_data or {})
    candidates = pis.get('_bulk_image_candidates') or []
    if not any(c.get('path') == rel for c in candidates if isinstance(c, dict)):
        candidates.append({
            'path': rel, 'source': 'crop', 'variant_sku': '',
            'matched_label': '', 'confidence': 'high',
        })
    pis['_bulk_image_candidates'] = candidates
    if not pis.get('_image_path'):
        pis['_image_path'] = rel
    product.pis_data = pis
    flag_modified(product, 'pis_data')

    existing_extras = list(product.additional_images or [])
    if rel not in existing_extras and rel != product.image_path:
        existing_extras.append(rel)
        product.additional_images = existing_extras
        flag_modified(product, 'additional_images')

    # Only seed image_path (workspace card thumbnail) if nothing was set
    # yet — never overwrite a user pick from the picker.
    if not product.image_path:
        product.image_path = rel

    db.session.commit()
    return {
        "image_path": product.image_path or rel,
        "added_path": rel,
        "candidates": candidates,
        "additional_images": list(product.additional_images or []),
    }


@marketing_bp.route(
    '/import_proforma/bulk/workspace/<batch_id>/<int:product_id>/image/crop_discard',
    methods=['POST'])
@limiter.limit("60 per minute")
def import_proforma_bulk_image_crop_discard(batch_id: str, product_id: int):
    """Delete an unwanted preview file. Body: {preview_path}."""
    if session.get('role') != 'marketing':
        return {"error": "unauthorized"}, 401
    product = _bulk_load_draft(batch_id, product_id)
    if not product:
        return {"error": "draft not found in this batch"}, 404

    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)

    payload = request.get_json(silent=True) or {}
    preview_path = (payload.get('preview_path') or '').strip()
    ok, info = _is_safe_preview_path(preview_path, upload_folder)
    if not ok:
        # Treat "not found" as a no-op so the frontend can fire and forget.
        if info == 'preview file not found':
            return {"discarded": False}, 200
        return {"error": info}, 400
    abs_preview = info
    try:
        os.remove(abs_preview)
    except OSError as e:
        return {"error": f"could not discard preview: {e}"}, 500
    return {"discarded": True}


@marketing_bp.route('/import_proforma/bulk/extract', methods=['POST'])
@limiter.limit("10 per minute")
def import_proforma_bulk_extract():
    """Phase C — take the user-edited cluster preview and persist one draft
    Product per cluster (skipping flagged rows). No AI extraction is run
    here — Phase D's workspace lazily enriches each draft as the user
    reviews them. This keeps "Generate N PIS" effectively instant.

    Body (JSON): {
        session_token:  str,
        cluster_groups: [...],   # client's edited cluster shape
        items:          [...],   # client's edited triage items (with skip flags + renames)
    }

    Each created Product has:
        • workflow_stage = 'marketing_draft'
        • pis_data._bulk_batch_id = <new uuid>
        • pis_data._bulk_cluster_index/_kind/_label/_row_indexes set
        • pis_data._enrichment_status = 'pending' so the workspace knows to
          fill in specs/description/image/category.

    Returns: { batch_id, product_ids, count, redirect }.
    """
    if session.get('role') != 'marketing':
        return {"error": "unauthorized"}, 401

    payload = request.get_json(silent=True) or {}
    token = (payload.get('session_token') or '').strip()
    edited_groups = payload.get('cluster_groups') or []
    edited_items = payload.get('items') or []

    sess = bw.get_session(token)
    if not sess:
        return {"error": "Bulk session expired"}, 400
    if not isinstance(edited_groups, list) or not edited_groups:
        return {"error": "cluster_groups required"}, 400
    if not isinstance(edited_items, list):
        return {"error": "items required"}, 400

    origin_hint = sess.get('origin_hint') or 'unknown'
    source_filenames = sess.get('file_names') or []
    triage_summary = (sess.get('triage') or {}).get('summary') or {}

    import uuid as _uuid
    batch_id = _uuid.uuid4().hex
    actor = get_current_username()
    created_ids: list[int] = []

    try:
        for cluster_idx, cluster in enumerate(edited_groups):
            pis = bw.build_stub_pis_from_cluster(
                cluster, edited_items, batch_id, origin_hint,
                cluster_index=cluster_idx,
                source_filenames=source_filenames,
                triage_summary=triage_summary,
            )
            if not pis:
                # All items in this cluster were skipped — drop it silently.
                continue

            model_name = pis.pop('_bulk_model_name', None) \
                          or (pis.get('header_info', {}).get('product_name') or 'Item')

            new_product = Product(
                model_name=model_name,
                pis_data=pis,
                image_path=None,
                seo_keywords='',
                workflow_stage='marketing_draft',
            )
            db.session.add(new_product)
            db.session.flush()    # need .id for the history log below
            log_event(new_product.id, actor, 'New Product Added',
                      f'Imported via bulk wizard (batch {batch_id[:8]}, '
                      f'cluster {cluster_idx + 1}/{len(edited_groups)}).',
                      'neutral')
            save_version_snapshot(new_product, label='Initial bulk draft', is_major=True)
            created_ids.append(new_product.id)

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return {"error": f"Failed to persist bulk drafts: {e}"}, 500

    bw.update_session(token, batch_id=batch_id, created_product_ids=created_ids)

    return {
        "batch_id":     batch_id,
        "product_ids":  created_ids,
        "count":        len(created_ids),
        # Phase D: send the user straight to the workspace where each
        # draft can be enriched and edited individually.
        "redirect":     url_for('marketing.import_proforma_bulk_workspace',
                                 batch_id=batch_id),
    }


@marketing_bp.route('/import_proforma/bulk/triage/rework', methods=['POST'])
@limiter.limit("20 per minute")
def import_proforma_bulk_triage_rework():
    """Phase B — re-run the triage scan against the session's existing
    file(s) with reviewer feedback prepended to the prompt. Used by the
    'Re-run with feedback' button in the bulk wizard.

    Body (JSON): {
        session_token: str,
        feedback:      str,   # free-text instructions for the AI
        origin_hint:   str?,  # optional override; otherwise reuse session's
    }
    """
    if session.get('role') != 'marketing':
        return Response('{"error":"unauthorized"}\n', status=401, mimetype='application/x-ndjson')

    payload = request.get_json(silent=True) or {}
    token = (payload.get('session_token') or '').strip()
    feedback = (payload.get('feedback') or '').strip()
    override_origin = (payload.get('origin_hint') or '').strip()

    sess = bw.get_session(token)
    if not sess:
        return Response('{"error":"Bulk session expired"}\n', status=400, mimetype='application/x-ndjson')
    # Narrowed alias for the inner closure — Pyrefly doesn't carry the
    # `if not sess` check across the generator boundary.
    session_data: dict[str, Any] = sess

    file_paths = session_data.get('file_paths') or []
    if not file_paths:
        return Response('{"error":"No files in session"}\n', status=400, mimetype='application/x-ndjson')

    origin_hint = override_origin or session_data.get('origin_hint') or 'unknown'

    def stream():
        yield bw.log_step("Bulk Triage — Rework with feedback")
        if feedback:
            yield bw.log_info(f"Feedback: {feedback[:120]}{'…' if len(feedback) > 120 else ''}")
        else:
            yield bw.log_warn("No feedback provided — re-running clean scan.")
        yield bw.log_progress(20, "Re-asking Gemini with feedback applied...")

        # Same dict[str, Any]-vs-literal-fallback narrowing trick as elsewhere
        # in this file — without the annotation the union widens to
        # `dict | list` and downstream .get() calls fail type-check.
        triage: dict[str, Any]
        try:
            triage = bw.triage_scan(file_paths, origin_hint=origin_hint, feedback=feedback)
        except Exception as e:
            yield bw.log_err(f"Rework crashed: {e}")
            triage = session_data.get('triage') or {"summary": {}, "items": []}

        s = triage.get('summary', {})
        yield bw.log_ok(f"Re-detected {s.get('item_count', 0)} item(s).")
        yield bw.log_ok(f"cluster_shape  = '{s.get('cluster_shape', '?')}'")
        if s.get('notes'):
            yield bw.log_info(f"AI note: {s['notes']}")

        groups = bw.derive_cluster_groups(triage.get('items') or [])
        yield bw.log_ok(f"Re-grouped into {len(groups)} cluster(s).")

        bw.update_session(token,
                          triage=triage,
                          cluster_groups=groups,
                          last_feedback=feedback,
                          origin_hint=origin_hint)

        yield bw.log_progress(100, "Rework complete.")
        yield bw.log_payload(
            session_token=token,
            triage=triage,
            cluster_groups=groups,
            origin_hint=origin_hint,
            done_step='triage_rework',
        )

    # Flask's stubs declare stream_with_context as wanting Iterator[AnyStr]
    # specifically; our generators yield bare str chunks which IS valid at
    # runtime but trips the variance check. Silenced narrowly.
    return Response(stream_with_context(stream()), mimetype='application/x-ndjson')  # type: ignore[arg-type]


# ── PHASE 2.4 — SINGLE-ITEM WIZARD ENDPOINTS ────────────────────────────────
#
# These power the 4-step interactive flow on /import_proforma when the user
# picks "Single Item" mode. They share an in-memory wizard session keyed by
# UUID (see utils/single_wizard.py). Auto / Multiple modes still use the
# legacy single-shot streaming endpoint above.
#

@marketing_bp.route('/import_proforma/single/scan', methods=['POST'])
@limiter.limit("20 per minute")
def import_proforma_single_scan():
    """Step 1+2 — accept the upload(s), save them, run the lightweight
    Gemini name-scan, and return a session token + auto-filled fields."""
    if session.get('role') != 'marketing':
        return Response('{"error":"unauthorized"}\n', status=401, mimetype='application/x-ndjson')

    ai_files = request.files.getlist('ai_document')
    upload_folder = current_app.config['UPLOAD_FOLDER']

    saved_paths: list[str] = []
    saved_names: list[str] = []
    for f in ai_files:
        if f and f.filename:
            filename = secure_filename(f.filename)
            fpath = os.path.join(upload_folder, filename)
            f.save(fpath)
            saved_paths.append(fpath)
            saved_names.append(filename)

    if not saved_paths:
        return Response('{"error":"No files uploaded"}\n', status=400, mimetype='application/x-ndjson')

    def stream():
        yield sw.log_step("Step 1 — Document upload")
        yield sw.log_progress(8, "Files received")
        for n in saved_names:
            yield sw.log_info(f"Saved {n}")

        yield sw.log_step("Step 2 — Pre-scan for product name")
        yield sw.log_progress(20, "Asking Gemini to read the doc header...")
        try:
            scan = sw.quick_scan_for_name(saved_paths)
        except Exception as e:
            yield sw.log_err(f"Pre-scan crashed: {e}")
            scan = {"product_name": "", "brand": "", "model_number": ""}

        yield sw.log_ok(f"product_name = '{scan['product_name'] or '(empty)'}'")
        yield sw.log_ok(f"brand        = '{scan['brand'] or '(empty)'}'")
        yield sw.log_ok(f"model_number = '{scan['model_number'] or '(empty)'}'")

        # Render a static-servable preview of the proforma so the wizard's
        # manual-crop tool can display it. Cheap (~200ms for a PDF page,
        # zero work for direct image uploads).
        try:
            preview_rel = sw.render_proforma_preview(
                saved_paths, scan.get('product_name') or '', upload_folder
            )
        except Exception as e:
            yield sw.log_warn(f"Proforma preview render failed: {e}")
            preview_rel = None
        if preview_rel:
            yield sw.log_info(f"Proforma preview ready → {preview_rel}")

        token = sw.create_session({
            'file_paths': saved_paths,
            'file_names': saved_names,
            'scan': scan,
            'proforma_preview': preview_rel,
        })
        yield sw.log_progress(35, "Pre-scan complete.")
        yield sw.log_payload(
            session_token=token,
            scan=scan,
            file_names=saved_names,
            proforma_preview=preview_rel,
            done_step=2,
        )

    # Flask's stubs declare stream_with_context as wanting Iterator[AnyStr]
    # specifically; our generators yield bare str chunks which IS valid at
    # runtime but trips the variance check. Silenced narrowly.
    return Response(stream_with_context(stream()), mimetype='application/x-ndjson')  # type: ignore[arg-type]


@marketing_bp.route('/import_proforma/single/find_url', methods=['POST'])
@limiter.limit("30 per minute")
def import_proforma_single_find_url():
    """Step 3 — given the (possibly user-edited) model name, search the web
    for the most likely supplier URL."""
    if session.get('role') != 'marketing':
        return Response('{"error":"unauthorized"}\n', status=401, mimetype='application/x-ndjson')

    payload = request.get_json(silent=True) or {}
    token = (payload.get('session_token') or '').strip()
    model_name = (payload.get('model_name') or '').strip()
    brand = (payload.get('brand') or '').strip()

    sess = sw.get_session(token)
    if not sess:
        return Response('{"error":"Wizard session expired"}\n', status=400, mimetype='application/x-ndjson')
    if not model_name:
        return Response('{"error":"model_name required"}\n', status=400, mimetype='application/x-ndjson')

    def stream():
        yield sw.log_step("Step 3 — Discover supplier URL")
        yield sw.log_progress(45, f"Searching for '{model_name}'...")
        # Type the dict so the literal fallback's None/list mix doesn't widen
        # `result.get('candidates', [])` to `None | list` and break iteration.
        result: dict[str, Any]
        try:
            result = sw.discover_supplier_url(model_name, brand=brand)
        except Exception as e:
            yield sw.log_err(f"URL discovery crashed: {e}")
            result = {"url": None, "candidates": []}

        if result.get("url"):
            yield sw.log_ok(f"Best match: {result['url']}")
            for c in result.get("candidates", []):
                if c != result["url"]:
                    yield sw.log_info(f"Also found: {c}")
        else:
            yield sw.log_warn("No supplier URL found — proceeding without one.")

        sw.update_session(token, model_name=model_name, brand=brand,
                          supplier_url=result.get("url"))
        yield sw.log_progress(55, "URL step complete.")
        yield sw.log_payload(
            supplier_url=result.get("url"),
            candidates=result.get("candidates", []),
            done_step=3,
        )

    # Flask's stubs declare stream_with_context as wanting Iterator[AnyStr]
    # specifically; our generators yield bare str chunks which IS valid at
    # runtime but trips the variance check. Silenced narrowly.
    return Response(stream_with_context(stream()), mimetype='application/x-ndjson')  # type: ignore[arg-type]


@marketing_bp.route('/import_proforma/single/extract_images', methods=['POST'])
@limiter.limit("15 per minute")
def import_proforma_single_extract_images():
    """Step 4 — try the doc first (no AI verify); on failure fall back to
    multi-result web screenshot crops. Returns up to 3 candidate paths."""
    if session.get('role') != 'marketing':
        return Response('{"error":"unauthorized"}\n', status=401, mimetype='application/x-ndjson')

    payload = request.get_json(silent=True) or {}
    token = (payload.get('session_token') or '').strip()
    # Allow the user to override the supplier URL on step 3 — the value
    # arrives here so we can persist it before kicking off the crawl.
    override_url = (payload.get('supplier_url') or '').strip()

    sess = sw.get_session(token)
    if not sess:
        return Response('{"error":"Wizard session expired"}\n', status=400, mimetype='application/x-ndjson')

    if override_url:
        sw.update_session(token, supplier_url=override_url)
        sess = sw.get_session(token) or sess

    file_paths = sess.get('file_paths') or []
    model_name = sess.get('model_name') or ''
    brand = sess.get('brand') or ''
    supplier_url = sess.get('supplier_url')
    upload_folder = current_app.config['UPLOAD_FOLDER']

    if not model_name:
        return Response('{"error":"model_name missing — re-run step 2"}\n',
                        status=400, mimetype='application/x-ndjson')

    def stream():
        yield sw.log_step("Step 4 — Image extraction")
        yield sw.log_progress(60, "Triaging document for product photos...")

        # ── Triage: does the proforma actually carry product photos? ──
        # Cached on the session so re-extract / re-run reuses the same
        # decision instead of paying the Gemini call twice.
        has_images = (sess.get('has_images') or '').strip().lower()
        if has_images not in ('all', 'partial', 'none'):
            has_images = sw.triage_has_images(file_paths)
            sw.update_session(token, has_images=has_images)
        yield sw.log_payload(has_images=has_images)
        if has_images == 'none':
            yield sw.log_info("Proforma is text-only — skipping document-side extraction.")
        else:
            yield sw.log_info(f"Proforma carries product photos ({has_images}) — running doc + web in parallel.")

        # Doc-side (bbox/embedded) runs ONLY when the proforma has images.
        # Nano-banana is no longer automatic — the user triggers it via
        # the "✨ Clean up" button per candidate (saves a Gemini call per
        # import and lets the user pick when to spend it).
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from utils.pdf_processing import (
            extract_specific_image, extract_product_from_image,
        )

        # Cancellation: shared event the web pipeline checks between every
        # SERP engine and Playwright capture. We set it on GeneratorExit
        # (raised when the client disconnects — e.g. user clicked Save and
        # the frontend abort()ed the fetch). Doc bbox and nano-banana run
        # as single AI calls, so they can't be interrupted mid-flight; we
        # let them finish (~5-10 s each) but skip yielding their results.
        cancel_event = threading.Event()

        web_log_lines: list[str] = []
        def _web_cb(msg: str) -> None:
            web_log_lines.append(msg)

        def _bbox_for(fp: str):
            ext = os.path.splitext(fp)[1].lower()
            if ext == '.pdf':
                return extract_specific_image(
                    fp, model_name, upload_folder,
                    skip_verify=True, all_matches=True, prefer_embedded=True,
                ) or []
            if ext in ('.jpg', '.jpeg', '.png', '.webp'):
                return extract_product_from_image(
                    fp, model_name, upload_folder,
                    skip_verify=True, all_matches=True,
                ) or []
            return []

        candidates: list[dict] = []
        seen_paths: set[str] = set()

        def _emit_candidate(path: str, source: str,
                             page_url: str | None = None) -> str | None:
            """Build the standard candidate dict, dedupe, append, and return
            the NDJSON line so the caller can `yield` it. Returns None when
            the candidate is empty or already seen."""
            if not path or path in seen_paths:
                return None
            seen_paths.add(path)
            cand = {"path": path, "page_url": page_url, "source": source}
            candidates.append(cand)
            return sw.log_payload(candidate=cand)

        # Manual pool management (vs `with`) so we can shut down with
        # cancel_futures=True the moment the client disconnects — `with`
        # always blocks waiting for every running task to drain, defeating
        # the cancellation.
        pool = ThreadPoolExecutor(max_workers=4)
        try:
            futures: dict = {}
            # Doc-side extraction skipped entirely for text-only proformas.
            if has_images != 'none':
                for fp in file_paths:
                    if not fp:
                        continue
                    futures[pool.submit(_bbox_for, fp)] = ('bbox', fp)
            # Web pipeline always runs (with or without doc photos). It
            # gets the cancel_event so it can early-exit between SERP
            # engines / Playwright captures.
            web_future = pool.submit(
                sw.extract_image_candidates_from_web,
                model_name, supplier_url, upload_folder, brand, 3, _web_cb,
                cancel_event,
            )
            futures[web_future] = ('web', None)

            for fut in as_completed(futures):
                if cancel_event.is_set():
                    break
                kind, fp = futures[fut]
                try:
                    result = fut.result()
                except Exception as e:
                    yield sw.log_err(f"{kind} pipeline crashed: {e}")
                    continue

                if kind == 'web':
                    for line in web_log_lines:
                        yield sw.log_info(line)
                    web_log_lines.clear()
                    web_results = result or []
                    for r in web_results:
                        line = _emit_candidate(
                            r.get("path"), "web", r.get("page_url"),
                        )
                        if line:
                            yield line
                    if web_results:
                        yield sw.log_ok(f"Web pipeline → {len(web_results)} candidate(s).")
                    else:
                        yield sw.log_warn("Web pipeline returned no candidates.")

                else:
                    paths = result if isinstance(result, list) else (
                        [result] if result else []
                    )
                    for p in paths:
                        line = _emit_candidate(p, "document")
                        if line:
                            yield line
                    if paths:
                        yield sw.log_ok(f"Doc bbox → {len(paths)} candidate(s).")
                    else:
                        yield sw.log_warn("Doc bbox produced nothing.")

            sw.update_session(token, image_candidates=candidates)
            yield sw.log_progress(88, f"{len(candidates)} candidate(s) ready for review.")
            # Final payload doubles as a recovery checkpoint for clients
            # that missed individual candidate events.
            yield sw.log_payload(
                candidates=candidates,
                done_step=4,
            )

        except GeneratorExit:
            # Client disconnected (typically: user clicked Save and the
            # frontend aborted the fetch). Stop everything we can — the
            # web pipeline checks `cancel_event` between engines, and
            # `cancel_futures=True` reaps any not-yet-started futures.
            cancel_event.set()
            print("  ✗ /single/extract_images: client disconnected — cancelling pipeline")
            raise
        finally:
            cancel_event.set()
            # `cancel_futures=True` drops queued tasks and stops the pool
            # from accepting new work. Already-running tasks (one in-flight
            # SERP scrape / Playwright capture) finish on their own — they
            # check `cancel_event` between sub-steps to bail early too.
            pool.shutdown(wait=False, cancel_futures=True)

    # Flask's stubs declare stream_with_context as wanting Iterator[AnyStr]
    # specifically; our generators yield bare str chunks which IS valid at
    # runtime but trips the variance check. Silenced narrowly.
    return Response(stream_with_context(stream()), mimetype='application/x-ndjson')  # type: ignore[arg-type]


@marketing_bp.route('/import_proforma/single/crop', methods=['POST'])
@limiter.limit("30 per minute")
def import_proforma_single_crop():
    """Phase 2.5 — manual crop endpoint for the wizard.

    Accepts a candidate path (already inside UPLOAD_FOLDER) plus a relative
    crop rectangle (`x`, `y`, `w`, `h` as floats in [0, 1]). Saves a new
    cropped JPEG and returns its relative `uploads/...` path. The frontend
    swaps the candidate's `path` to point to the new file.

    Path-traversal defense: realpath of `source_path` must live under
    UPLOAD_FOLDER. Any attempt to escape is rejected with 400.
    """
    if session.get('role') != 'marketing':
        return {"error": "unauthorized"}, 401

    payload = request.get_json(silent=True) or {}
    token = (payload.get('session_token') or '').strip()
    source_path = (payload.get('source_path') or '').strip()
    crop = payload.get('crop') or {}

    sess = sw.get_session(token)
    if not sess:
        return {"error": "Wizard session expired"}, 400

    try:
        x = float(crop.get('x', 0)); y = float(crop.get('y', 0))
        w = float(crop.get('w', 0)); h = float(crop.get('h', 0))
    except (TypeError, ValueError):
        return {"error": "crop must contain x, y, w, h as numbers in [0,1]"}, 400

    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1):
        return {"error": "crop out of range — x,y in [0,1) and w,h in (0,1]"}, 400
    if x + w > 1.0001 or y + h > 1.0001:
        return {"error": "crop extends beyond image bounds"}, 400

    upload_folder = current_app.config['UPLOAD_FOLDER']
    # UPLOAD_FOLDER is configured as 'static/uploads' (relative). Anchor it
    # under the app root so realpath doesn't depend on cwd.
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)
    static_root = os.path.join(current_app.root_path, 'static')

    # `source_path` arrives as "uploads/visual_..." relative to /static.
    if not source_path:
        return {"error": "source_path required"}, 400
    # Strip a leading "/static/" if the frontend included it.
    if source_path.startswith('/static/'):
        source_path = source_path[len('/static/'):]
    elif source_path.startswith('static/'):
        source_path = source_path[len('static/'):]

    abs_source = os.path.realpath(os.path.join(static_root, source_path))
    abs_upload = os.path.realpath(upload_folder)

    # Containment check via commonpath — works on both Windows (\) and POSIX (/).
    try:
        common = os.path.commonpath([abs_source, abs_upload])
    except ValueError:
        common = ''
    if common != abs_upload:
        return {"error": "source_path outside UPLOAD_FOLDER"}, 400
    if not os.path.exists(abs_source):
        return {"error": "source image not found"}, 404

    try:
        from PIL import Image as _PILImage
        img = _PILImage.open(abs_source)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        iw, ih = img.size
        left = int(round(x * iw))
        top = int(round(y * ih))
        right = int(round((x + w) * iw))
        bottom = int(round((y + h) * ih))
        # Clamp to safe bounds.
        left = max(0, min(left, iw - 1))
        top = max(0, min(top, ih - 1))
        right = max(left + 1, min(right, iw))
        bottom = max(top + 1, min(bottom, ih))

        cropped = img.crop((left, top, right, bottom))

        # Cap at 1600 px longest edge — keeps catalog file sizes reasonable
        # without ever upscaling. `thumbnail()` is in-place, no-op if smaller.
        MAX_EDGE = 1600
        if max(cropped.size) > MAX_EDGE:
            cropped.thumbnail((MAX_EDGE, MAX_EDGE), _PILImage.LANCZOS)  # type: ignore[attr-defined]

        model_name = sess.get('model_name') or 'product'
        safe_name = secure_filename(model_name) or 'product'
        filename = f"visual_{safe_name}_crop_{int(time.time())}.jpg"
        out_path = os.path.join(upload_folder, filename)
        cropped.save(out_path, quality=95)
        rel = f"uploads/{filename}"
        return {"path": rel}
    except Exception as e:
        return {"error": f"Crop failed: {e}"}, 500


@marketing_bp.route('/import_proforma/single/extract_from_url', methods=['POST'])
@limiter.limit("15 per minute")
def import_proforma_single_extract_from_url():
    """On-demand: user pastes a specific URL they want images extracted
    from (e.g. they found the exact product page themselves). We scrape
    + download up to 3 images and stream them back as new candidates,
    same shape as the auto-discovered web pipeline.
    """
    if session.get('role') != 'marketing':
        return Response('{"error":"unauthorized"}\n', status=401, mimetype='application/x-ndjson')

    payload = request.get_json(silent=True) or {}
    token = (payload.get('session_token') or '').strip()
    suggested_url = (payload.get('url') or '').strip()

    sess = sw.get_session(token)
    if not sess:
        return Response('{"error":"Wizard session expired"}\n', status=400, mimetype='application/x-ndjson')

    if not suggested_url:
        return Response('{"error":"url required"}\n', status=400, mimetype='application/x-ndjson')

    model_name = sess.get('model_name') or 'product'
    existing_candidates = list(sess.get('image_candidates') or [])
    upload_folder = current_app.config['UPLOAD_FOLDER']

    def stream():
        yield sw.log_info(f"Fetching images from suggested URL: {suggested_url}")
        log_lines: list[str] = []
        def _cb(msg: str) -> None:
            log_lines.append(msg)

        try:
            results = sw.extract_images_from_user_url(
                suggested_url, model_name, upload_folder, 3, _cb,
            ) or []
        except Exception as e:
            yield sw.log_err(f"Suggested-URL fetch failed: {e}")
            yield sw.log_payload(error=str(e))
            return

        for line in log_lines:
            yield sw.log_info(line)

        # Merge new candidates into the session so finalize can persist
        # them to the gallery if the user keeps them.
        new_candidates: list[dict] = []
        for r in results:
            cand = {"path": r["path"], "page_url": r.get("page_url"),
                    "source": "user_url"}
            new_candidates.append(cand)
            yield sw.log_payload(candidate=cand)

        if new_candidates:
            sw.update_session(token,
                              image_candidates=existing_candidates + new_candidates)
            yield sw.log_ok(f"Suggested-URL → {len(new_candidates)} candidate(s).")
        else:
            yield sw.log_warn("Suggested-URL produced no usable images.")

        yield sw.log_payload(candidates=new_candidates, done=True)

    return Response(stream_with_context(stream()), mimetype='application/x-ndjson')  # type: ignore[arg-type]


@marketing_bp.route('/import_proforma/single/nano_isolate', methods=['POST'])
@limiter.limit("10 per minute")
def import_proforma_single_nano_isolate():
    """On-demand nano-banana isolation. Runs Gemini's image-out model on
    the uploaded proforma (or first file) to produce a clean isolated
    product render. Returns the new candidate path. Costs one Gemini
    call per click, which is why it's user-triggered now instead of
    running automatically in Step 4.
    """
    if session.get('role') != 'marketing':
        return {"error": "unauthorized"}, 401

    payload = request.get_json(silent=True) or {}
    token = (payload.get('session_token') or '').strip()

    sess = sw.get_session(token)
    if not sess:
        return {"error": "Wizard session expired"}, 400

    file_paths = sess.get('file_paths') or []
    model_name = sess.get('model_name') or ''
    brand = sess.get('brand') or ''
    model_number = sess.get('model_number') or ''
    upload_folder = current_app.config['UPLOAD_FOLDER']

    if not file_paths:
        return {"error": "no source file in session"}, 400
    if not model_name:
        return {"error": "model_name missing — re-run step 2"}, 400

    try:
        from utils.pdf_processing import extract_isolated_product_with_nano_banana
        rel = extract_isolated_product_with_nano_banana(
            file_paths[0], model_name, upload_folder,
            brand=brand or None, sku=model_number or None,
        )
        if not rel:
            return {"error": "Nano-banana returned no image"}, 502

        cand = {"path": rel, "page_url": None, "source": "nano"}
        existing = sess.get('image_candidates') or []
        sw.update_session(token, image_candidates=existing + [cand])
        return {"candidate": cand}
    except Exception as e:
        return {"error": f"Nano-banana failed: {e}"}, 500


@marketing_bp.route('/import_proforma/single/finalize', methods=['POST'])
@limiter.limit("10 per minute")
def import_proforma_single_finalize():
    """Step 5 — full proforma extraction + Product creation with the
    user-selected image. Reuses the existing single-mode logic from
    `import_proforma()` so content extraction stays untouched."""
    if session.get('role') != 'marketing':
        return Response('{"error":"unauthorized"}\n', status=401, mimetype='application/x-ndjson')

    payload = request.get_json(silent=True) or {}
    token = (payload.get('session_token') or '').strip()
    selected_image = (payload.get('selected_image') or '').strip() or None
    model_name = (payload.get('model_name') or '').strip()
    # Gallery: client sends every candidate it still has on screen. The
    # selected_image becomes Product.image_path; the rest become
    # Product.additional_images (gallery). Accepts a list of relative
    # `uploads/...` paths OR a list of {path, ...} dicts (the same shape
    # the cascade emits). De-duped and filtered against selected_image.
    raw_gallery = payload.get('gallery_images') or []
    gallery_paths: list[str] = []
    seen_gallery: set[str] = set()
    for item in raw_gallery:
        if isinstance(item, str):
            p = item.strip()
        elif isinstance(item, dict):
            p = str(item.get('path') or '').strip()
        else:
            p = ''
        if not p or p == selected_image or p in seen_gallery:
            continue
        seen_gallery.add(p)
        gallery_paths.append(p)

    sess = sw.get_session(token)
    if not sess:
        return Response('{"error":"Wizard session expired"}\n', status=400, mimetype='application/x-ndjson')

    file_paths = sess.get('file_paths') or []
    supplier_url = sess.get('supplier_url')
    brand = sess.get('brand') or ''
    if not model_name:
        model_name = sess.get('model_name') or ''
    if not model_name:
        return Response('{"error":"model_name required"}\n', status=400, mimetype='application/x-ndjson')

    _app = current_app._get_current_object()  # type: ignore[attr-defined]

    def stream():
        yield sw.log_step("Step 5 — Content extraction & save")
        yield sw.log_progress(90, "Running full proforma extraction...")

        # 1) Scrape supplier URL if we have one.
        site_data = {"text": "", "html": ""}
        if supplier_url:
            yield sw.log_info(f"Scraping {supplier_url}")
            try:
                site_data = scrape_url_data(supplier_url)
            except Exception as e:
                yield sw.log_warn(f"Scrape failed (continuing): {e}")

        # 2) Run the canonical single-mode proforma extraction.
        try:
            extracted = generate_proforma_data(
                file_paths=file_paths,
                url_data=site_data,
                extraction_mode='single',
                brand_hint=brand or None,
            )
            if not extracted:
                yield sw.log_err("AI returned no products from this source.")
                yield sw.log_payload(error="AI returned no products from this source.")
                return
        except Exception as e:
            import traceback; traceback.print_exc()
            yield sw.log_err(f"AI extraction failed: {e}")
            yield sw.log_payload(error=f"AI extraction failed: {e}")
            return

        # Strict-fact rule: only the uploaded Proforma counts as verification
        # source (see the bulk path above for rationale).
        raw_doc_text = extract_raw_text_from_files(file_paths) or ""

        raw = extracted[0]
        pis = proforma_to_pis_data(raw, raw_text=raw_doc_text, source_files=file_paths) or {}
        pis.setdefault('header_info', {})
        if not pis['header_info'].get('product_name'):
            pis['header_info']['product_name'] = model_name

        yield sw.log_ok("Content extracted.")
        yield sw.log_progress(95, "Saving product...")

        try:
            with _app.app_context():
                new_product = Product(
                    model_name=model_name,
                    pis_data=pis,
                    image_path=selected_image,
                    additional_images=gallery_paths,
                    seo_keywords=(pis or {}).get('seo_data', {}).get('generated_keywords', ''),
                    workflow_stage='marketing_draft',
                )
                db.session.add(new_product)
                db.session.commit()
                log_event(new_product.id, get_current_username(), 'New Product Added',
                          'Imported via single-item wizard.', 'neutral')
                save_version_snapshot(new_product, label='Initial version', is_major=True)
                product_id = new_product.id
        except Exception as e:
            yield sw.log_err(f"DB save failed: {e}")
            yield sw.log_payload(error=f"DB save failed: {e}")
            return

        sw.drop_session(token)
        clear_pdf_cache()
        redirect_url = url_for('marketing.review_pis_marketing', product_id=product_id)
        yield sw.log_ok(f"Saved product #{product_id}.")
        yield sw.log_progress(100, "Done — redirecting...")
        yield sw.log_payload(redirect=redirect_url)

    # Flask's stubs declare stream_with_context as wanting Iterator[AnyStr]
    # specifically; our generators yield bare str chunks which IS valid at
    # runtime but trips the variance check. Silenced narrowly.
    return Response(stream_with_context(stream()), mimetype='application/x-ndjson')  # type: ignore[arg-type]


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
    _app = current_app._get_current_object()  # type: ignore[attr-defined]

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

    return Response(stream_with_context(generate_bulk_updates()), mimetype='application/x-ndjson')  # type: ignore[arg-type]


# ── REVIEW ROUTES ─────────────────────────────────────────────────────────────

@marketing_bp.route('/verify/<int:product_id>')
def old_verify_redirect(product_id):
    return redirect(url_for('marketing.review_pis_marketing', product_id=product_id))


@marketing_bp.route('/review/marketing/<int:product_id>', methods=['GET', 'POST'])
def review_pis_marketing(product_id):
    product = Product.query.get_or_404(product_id)

    # ── Bulk-batch context ──────────────────────────────────────────────
    # If this product was created by the bulk wizard, pull every sibling in
    # the same batch so the template can render prev/next arrows + counter,
    # and so "Submit all to Director" / "Cancel" can target the whole batch.
    pis_for_batch = product.pis_data or {}
    batch_id = (pis_for_batch.get('_bulk_batch_id') or '').strip() if isinstance(pis_for_batch, dict) else ''
    batch_siblings: list[Product] = []
    batch_index = -1
    prev_id = next_id = None
    if batch_id:
        batch_siblings = Product.query.filter(
            Product.deleted_at.is_(None),
            Product.pis_data['_bulk_batch_id'].astext == batch_id,
        ).order_by(Product.id.asc()).all()
        ids = [s.id for s in batch_siblings]
        if product.id in ids:
            batch_index = ids.index(product.id)
            if batch_index > 0:
                prev_id = ids[batch_index - 1]
            if batch_index < len(ids) - 1:
                next_id = ids[batch_index + 1]

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
        actor = get_current_username()

        if action == 'submit_director_batch' and batch_id and batch_siblings:
            # First, persist the current PIS edits as a draft snapshot.
            save_version_snapshot(product, label='Draft saved (pre-batch submit)', is_major=False)
            _diff_and_log_changes(product.id, old_pis, updated_data, prefix='pis_data')
            # Then move EVERY active draft in the batch (this product included)
            # to pending_director_pis. Stages already past draft are left alone.
            moved = 0
            for s in batch_siblings:
                if s.workflow_stage in ('marketing_draft', 'marketing_in_progress',
                                         'marketing_changes_requested'):
                    s.workflow_stage = 'pending_director_pis'
                    save_version_snapshot(s, label='Submitted for Director review (batch)',
                                          is_major=True)
                    log_event(s.id, actor, 'Sent for Director Review',
                              f'Submitted via Edit-PIS batch action (batch {batch_id[:8]}).',
                              'waiting')
                    moved += 1
            db.session.commit()
            flash(f'Sent {moved} PIS to the Director for review')
            return redirect(url_for('marketing.dashboard_marketing'))

        if action == 'submit_director':
            save_version_snapshot(product, label='Submitted for Director review', is_major=True)
            product.workflow_stage = 'pending_director_pis'
            log_event(product.id, actor, 'Sent for Director Review',
                      'The product sheet has been sent to the Director for approval.', 'waiting')
            flash('Sent to the Director for review')
        else:
            save_version_snapshot(product, label='Draft saved', is_major=False)
            if product.workflow_stage in ('marketing_draft', 'marketing_changes_requested'):
                product.workflow_stage = 'marketing_in_progress'
            log_event(product.id, actor, 'Draft Updated',
                      'The marketing team updated and saved changes to the product sheet.', 'neutral')

            # Save Draft inside a bulk batch persists the WHOLE batch as a
            # checkpoint, not just the current PIS. Siblings haven't been
            # edited (their data isn't in this form) but we mirror the
            # workflow flip + draft snapshot so the batch advances together.
            sibling_count = 0
            if batch_id and batch_siblings:
                for s in batch_siblings:
                    if s.id == product.id:
                        continue
                    if s.workflow_stage in ('marketing_draft', 'marketing_changes_requested'):
                        s.workflow_stage = 'marketing_in_progress'
                    save_version_snapshot(s, label='Draft saved (batch checkpoint)',
                                          is_major=False)
                    log_event(s.id, actor, 'Draft Saved (batch)',
                              f'Batch checkpoint saved alongside sibling #{product.id} '
                              f'(batch {batch_id[:8]}).', 'neutral')
                    sibling_count += 1

            if sibling_count:
                flash(f'Draft saved — batch of {sibling_count + 1} checkpointed')
            else:
                flash('Draft saved successfully')

        _diff_and_log_changes(product.id, old_pis, updated_data, prefix='pis_data')
        db.session.commit()

        # Batch navigation: action=save_and_next | save_and_prev jumps to the
        # adjacent sibling so the user can browse the batch without losing
        # changes. Plain Save Draft (and everything else) falls through to
        # the marketing overview/dashboard — the workspace is only re-entered
        # via the Cancel link or by clicking the batch from the dashboard.
        if action == 'save_and_next' and next_id:
            return redirect(url_for('marketing.review_pis_marketing', product_id=next_id))
        if action == 'save_and_prev' and prev_id:
            return redirect(url_for('marketing.review_pis_marketing', product_id=prev_id))
        return redirect(url_for('marketing.dashboard_marketing'))

    return render_template(
        'verify_marketing.html',
        product=product,
        data=normalize_pis_data(product.pis_data),
        batch_id=batch_id or None,
        batch_index=batch_index,
        batch_size=len(batch_siblings),
        batch_prev_id=prev_id,
        batch_next_id=next_id,
    )


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

@marketing_bp.route('/preview_pis_html/<int:product_id>')
def preview_pis_html(product_id):
    """Phase 2.5: lightweight inline preview of the PIS rendered as HTML
    using the same `pdf_print.html` template the PDF download uses. Lets the
    review page embed an iframe that refreshes after every Save Draft so
    reviewers see exactly what the printed PDF will look like — without
    spinning up Playwright on every render. Returns plain HTML, not a PDF."""
    from datetime import datetime
    product = Product.query.get_or_404(product_id)
    all_images_b64 = _load_images_b64(product)
    return render_template(
        'pdf_print.html',
        data=normalize_pis_data(product.pis_data),
        product=product,
        all_images_b64=all_images_b64,
        date_generated=datetime.now().strftime("%Y-%m-%d"),
    )


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
