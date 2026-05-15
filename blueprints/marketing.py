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
    set_product_category, get_product_category_label, CATEGORY_UNCATEGORISED,
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

    # Order by `last_edited_at` — bumped by SQLAlchemy on every UPDATE so
    # autosaves, approvals, category writes, and stage transitions all
    # surface the product to the top of the gallery. Falls back to
    # created_at for safety on any row where the column is NULL.
    active_pipeline = (
        Product.query
        .filter(Product.workflow_stage.in_(marketing_stages),
                Product.deleted_at.is_(None))
        .order_by(
            db.func.coalesce(Product.last_edited_at, Product.created_at).desc()
        )
        .all()
    )

    metrics = {
        'total_active': len(active_pipeline),
        'drafts': sum(1 for p in active_pipeline if p.workflow_stage == 'marketing_draft'),
        'changes': sum(1 for p in active_pipeline if p.workflow_stage == 'marketing_changes_requested'),
        'need_review': sum(1 for p in active_pipeline if p.workflow_stage == 'pending_director_pis'),
        'in_process': sum(1 for p in active_pipeline if p.workflow_stage == 'marketing_in_progress'),
        'approved': sum(1 for p in active_pipeline if p.workflow_stage in approved_stages)
    }

    # Build the list of categories actually present in the current pipeline
    # so the filter dropdown only ever offers selections that will return
    # results. "Uncategorised" is pinned last so it doesn't clutter the
    # alphabetical run of real Magento top-level categories.
    available_categories = sorted({
        get_product_category_label(p) for p in active_pipeline
    } - {CATEGORY_UNCATEGORISED})
    if any(get_product_category_label(p) == CATEGORY_UNCATEGORISED for p in active_pipeline):
        available_categories.append(CATEGORY_UNCATEGORISED)

    return render_template(
        'dashboard_marketing.html',
        products=active_pipeline, metrics=metrics,
        available_categories=available_categories,
        uncategorised_label=CATEGORY_UNCATEGORISED,
    )


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

    # Full product lifecycle filter mapping — covers PIS creation,
    # PIS approval, SpecSheet creation, SpecSheet approval. Each
    # workflow_stage gets bucketed into one of these audit-log buckets so
    # the History Log can be filtered end-to-end (Marketing → Director →
    # Web → Director → Finalized).
    STAGE_FILTER_MAP = {
        'marketing_draft':              'PIS DRAFT',
        'marketing_in_progress':        'PIS DRAFT',
        'marketing_changes_requested':  'PIS CHANGES',
        'pending_director_pis':         'PIS REVIEW',
        'ready_for_web':                'PIS APPROVED',
        'specsheet_draft':              'SPEC DRAFT',
        'web_changes_requested':        'SPEC CHANGES',
        'pending_director_spec':        'SPEC REVIEW',
        'finalized':                    'FINALIZED',
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
        filter_status = STAGE_FILTER_MAP.get(stage, 'PIS DRAFT')

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
        triage_feedback  — optional free-text hint applied to the rework
                           prompt (Phase A: only used as instruction context;
                           the actual rework endpoint is Phase B).

    The AI triage scan auto-detects whether the document is an external
    supplier proforma or an internal Kalachand doc — no user toggle. The
    `origin_hint` passed downstream is always `'unknown'` so the prompt
    has no bias to override the document evidence.

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

    # Origin hint removed from the UI — the AI's triage scan detects this
    # from the document itself (returns it in `summary.origin`). We pass
    # 'unknown' so the prompt has no user bias to trust over the evidence.
    origin_hint = 'unknown'

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
        # Generated PIS are now managed individually from the Product Gallery
        # — there's no batch workspace anymore. The async endpoint
        # /api/proforma/bulk/extract_async is the primary path; this sync
        # route is kept as a fallback and sends the user to the same place.
        "redirect":     url_for('marketing.dashboard_marketing'),
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

        # Classify against the Magento taxonomy so single-import products
        # have a canonical category from day one — same end state as bulk
        # import. Runs inline (user is already watching the progress bar)
        # and writes through `set_product_category`, which also mirrors to
        # the legacy JSON shapes. Failures are non-fatal: the product is
        # already saved and SpecSheet generation has its own classifier
        # fallback if this one didn't land.
        yield sw.log_progress(97, "Classifying product category...")
        try:
            from utils.category_classifier import classify_product_category
            classification = classify_product_category(pis) or {}
            if classification.get('category_1'):
                with _app.app_context():
                    p = Product.query.get(product_id)
                    if p:
                        set_product_category(
                            p,
                            classification.get('category_1', ''),
                            classification.get('category_2', ''),
                            classification.get('category_3', ''),
                        )
                        db.session.commit()
                yield sw.log_ok(
                    f"Category: {classification.get('category_1','')} → "
                    f"{classification.get('category_2','')} → "
                    f"{classification.get('category_3','')}"
                )
            else:
                yield sw.log_warn("Category classifier returned no result — leaving uncategorised.")
        except Exception as e:
            yield sw.log_warn(f"Category classification skipped: {e}")

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

        action = request.form.get('action')
        actor = get_current_username()

        # Only clear the director's revision suggestions when the user
        # explicitly resubmits — Save Draft must preserve them so marketing
        # can keep working on the requested changes across sessions.
        if action == 'submit_director' and product.revision_data:
            product.revision_data = None

        flag_modified(product, 'pis_data')
        flag_modified(product, 'revision_data')

        if action == 'submit_director':
            save_version_snapshot(product, label='Submitted for Director review', is_major=True)
            product.workflow_stage = 'pending_director_pis'
            log_event(product.id, actor, 'Sent for Director Review',
                      'The product sheet has been sent to the Director for approval.', 'waiting')
            flash('Sent to the Director for review')
        else:
            save_version_snapshot(product, label='Draft saved', is_major=False)
            # Only flip a brand-new draft to "in progress". Products that
            # came back as `marketing_changes_requested` from a director
            # review STAY in changes-requested while the marketing team
            # works on the revisions — saving a draft must not clear the
            # change-request status.
            if product.workflow_stage == 'marketing_draft':
                product.workflow_stage = 'marketing_in_progress'
            log_event(product.id, actor, 'Draft Updated',
                      'The marketing team updated and saved changes to the product sheet.', 'neutral')
            flash('Draft saved successfully')

        _diff_and_log_changes(product.id, old_pis, updated_data, prefix='pis_data')
        db.session.commit()

        # Save Draft AND Need Review both land back on the Product Gallery.
        # PIS-level management is now individual — batch nav was removed.
        return redirect(url_for('marketing.dashboard_marketing'))

    return render_template(
        'verify_marketing.html',
        product=product,
        data=normalize_pis_data(product.pis_data),
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
    from flask import Response as FlaskResponse
    from playwright.sync_api import sync_playwright
    from datetime import datetime
    product = Product.query.get_or_404(product_id)
    all_images_b64 = _load_images_b64(product)
    date_generated = datetime.now().strftime("%Y-%m-%d")
    html = render_template('pdf_print.html',
                           data=product.pis_data, product=product,
                           all_images_b64=all_images_b64,
                           date_generated=date_generated)

    # Chromium-native footer on every physical page. Same pattern as the
    # SpecSheet route — uses the special `pageNumber` / `totalPages`
    # placeholders so multi-page exports get correct page numbering.
    footer_template = f"""
    <div style="font-family: 'Inter', sans-serif; font-size: 8px;
                width: 100%; padding: 6px 14mm 0 14mm; box-sizing: border-box;
                background: #1e293b; color: #94a3b8;
                display: flex; justify-content: space-between; align-items: center;
                -webkit-print-color-adjust: exact; print-color-adjust: exact;">
        <div>
            <span style="color: #ffffff; font-weight: 700; letter-spacing: 1px;">J. KALACHAND</span>
            &nbsp;|&nbsp; PRODUCT INFORMATION SHEET
        </div>
        <div>
            REF: PIS-{product.id} &nbsp;|&nbsp; GENERATED ON {date_generated}
            &nbsp;|&nbsp; PAGE <span class="pageNumber"></span> / <span class="totalPages"></span>
        </div>
    </div>
    """
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
                margin={"top": "18mm", "right": "14mm", "bottom": "22mm", "left": "14mm"},
            )
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
