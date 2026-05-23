"""
API blueprint — background job queue, product data APIs, version history, images, forbidden words.
"""
import os
import copy
import uuid
import time
import json
import shutil
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

from flask import (
    Blueprint, session, redirect, url_for, request, jsonify,
    current_app, send_from_directory
)


from werkzeug.utils import secure_filename
from sqlalchemy.orm.attributes import flag_modified

from model import db, Product, ProductVersion, FieldChangeLog, User, Job, ProductHistory
from helpers import (
    get_current_username, save_version_snapshot,
    _clean_field_name, _get_field_section,
    load_forbidden_words, save_forbidden_words,
    VALID_SEVERITIES,
    proforma_to_pis_data, extract_raw_text_from_files,
    set_product_category,
)
from utils.decorators import require_role
from utils.history import log_event
from utils.web_scraping import scrape_url_data, scrape_url_data_deep
from utils.ai_generation import generate_pis_data, generate_bulk_pis_data, generate_proforma_data
from utils.pdf_processing import extract_specific_image, clear_pdf_cache
from utils.image_processing import (
    find_and_validate_image, find_image_simple, download_web_image,
    find_image_via_screenshot,
)
from utils.storage import store_image
from utils import single_wizard as sw
from utils import bulk_wizard as bw

api_bp = Blueprint('api', __name__)

pis_executor = ThreadPoolExecutor(max_workers=5)


# ── STATIC ────────────────────────────────────────────────────────────────────

@api_bp.route('/favicon.ico')
def favicon():
    return send_from_directory(current_app.static_folder or 'static', 'favicon.ico', mimetype='image/x-icon')


# ── JOB HELPERS ───────────────────────────────────────────────────────────────

def _update_job(job_id, **kwargs):
    try:
        job = db.session.get(Job, job_id)
        if job:
            if 'completed_at' in kwargs and isinstance(kwargs['completed_at'], str):
                kwargs['completed_at'] = datetime.fromisoformat(kwargs['completed_at'])
            for key, value in kwargs.items():
                setattr(job, key, value)
            db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f'Job update error ({job_id}): {e}')


# ── BACKGROUND WORKERS ────────────────────────────────────────────────────────

def _pis_worker(app, job_id, model_name, supplier_url, ai_filepaths, contains_images, user_name):
    """Background worker that generates a single PIS and updates job status."""
    from utils.api_metering import job_scope
    with app.app_context(), job_scope(job_id):
        try:
            upload_folder = app.config['UPLOAD_FOLDER']
            _update_job(job_id, status='processing', progress=10, message='Initializing Analysis...')

            # Build the web/site context fed to generate_pis_data. Mirrors the
            # bulk-wizard content task: always run Brave so the verify-PIS
            # badges can split Fact vs Web-grounded vs AI; the supplier URL,
            # when given, is a supplement concatenated ahead of Brave's text.
            site_data = {"text": "", "html": ""}
            site_text_parts: list[str] = []
            if supplier_url:
                _update_job(job_id, progress=15, message='Reading supplier website...')
                scraped = scrape_url_data(supplier_url) or {}
                if scraped.get('text'):
                    site_text_parts.append(scraped['text'])
                site_data['html'] = scraped.get('html') or ''

            _update_job(job_id, progress=25, message='Searching the web for product context...')
            web_context = ""
            try:
                from utils.image_processing import gather_web_context_for_content
                web_context = gather_web_context_for_content(model_name) or ""
                if web_context:
                    site_text_parts.append(web_context)
            except Exception as e:
                print(f"⚠ Brave web context fetch failed for '{model_name}': {e}")

            site_data['text'] = "\n\n".join(p for p in site_text_parts if p)

            _update_job(job_id, progress=40, message='Generating PIS Content...')
            ai_data = generate_pis_data(ai_filepaths, model_name, site_data)

            # Origin classification — grep AI fields against the proforma raw
            # text (verified) and web context (web_grounded) so the verify-PIS
            # legend renders Fact / Web / AI-enriched / AI-generated badges
            # instead of falling back to a uniform red ✨ on every field.
            # `_web_context` uses the combined site text (supplier scrape +
            # Brave) so anything sourced from either gets web-grounded.
            try:
                from helpers import (
                    extract_raw_text_from_files,
                    classify_flat_pis_origins,
                )
                raw_doc_text = extract_raw_text_from_files(ai_filepaths) or ""
                combined_web = site_data['text']
                field_origins, spec_origins = classify_flat_pis_origins(
                    ai_data, raw_doc_text, web_context=combined_web or None,
                )
                ai_data['_field_origins'] = field_origins
                ai_data['_spec_origins'] = spec_origins
                if combined_web:
                    ai_data['_web_context'] = combined_web
            except Exception as e:
                print(f"⚠ origin classification failed for single PIS: {e}")

            extracted_image_path = None

            if contains_images and ai_filepaths:
                _update_job(job_id, progress=55, message='Scanning PDF for product image...')
                extracted_image_path = extract_specific_image(ai_filepaths[0], model_name, upload_folder)
                if not extracted_image_path:
                    _update_job(job_id, progress=65, message='PDF scan found nothing, trying web...')
                    ai_found_url = ai_data.get('found_image_url')
                    if ai_found_url and ai_found_url.startswith('http'):
                        extracted_image_path = download_web_image(ai_found_url, model_name, upload_folder)
                if not extracted_image_path:
                    rich_query = _build_query(ai_data, model_name)
                    extracted_image_path = _web_search_image(rich_query, supplier_url, model_name, upload_folder, job_id)
            else:
                ai_found_url = ai_data.get('found_image_url')
                if ai_found_url and ai_found_url.startswith('http'):
                    _update_job(job_id, progress=55, message='AI found a product image — downloading...')
                    extracted_image_path = download_web_image(ai_found_url, model_name, upload_folder)
                if not extracted_image_path:
                    rich_query = _build_query(ai_data, model_name)
                    extracted_image_path = _web_search_image(rich_query, supplier_url, model_name, upload_folder, job_id)

            if not extracted_image_path:
                _update_job(job_id, progress=80, message='Trying DuckDuckGo fallback search...')
                header = ai_data.get('header_info', {})
                simple_query = f"{header.get('brand', '')} {header.get('product_name', '')}".strip() or model_name
                simple_url = find_image_simple(simple_query, supplier_url)
                if simple_url:
                    _update_job(job_id, progress=85, message='Found image via DuckDuckGo!')
                    extracted_image_path = download_web_image(simple_url, model_name, upload_folder)

            # Store to Azure or local
            if extracted_image_path:
                extracted_image_path = store_image(extracted_image_path, model_name)

            _update_job(job_id, progress=90, message='Saving product...')
            new_product = Product(
                model_name=model_name, pis_data=ai_data,
                image_path=extracted_image_path,
                seo_keywords=ai_data.get('seo_data', {}).get('generated_keywords', ''),
                workflow_stage='marketing_draft'
            )
            db.session.add(new_product)
            db.session.commit()
            log_event(new_product.id, user_name, 'New Product Added',
                      'A new product information sheet was created from a single import.', 'neutral')
            save_version_snapshot(new_product, label='Initial version', is_major=True)
            _update_job(job_id, status='completed', progress=100, message='Done!',
                        product_id=new_product.id,
                        redirect_url=f'/review/marketing/{new_product.id}',
                        completed_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

        except Exception as e:
            import traceback; traceback.print_exc()
            _update_job(job_id, status='failed', progress=100,
                        message=f'Generation failed: {str(e)[:100]}',
                        error=str(e), completed_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())


def _bulk_pis_worker(app, job_id, supplier_url, ai_filepaths, contains_images, product_filter, user_name):
    """Background worker that generates multiple PIS from a bulk document."""
    from utils.api_metering import job_scope
    with app.app_context(), job_scope(job_id):
        try:
            upload_folder = app.config['UPLOAD_FOLDER']
            _update_job(job_id, status='processing', progress=5, message='Analyzing document...')
            site_data = {"text": "", "html": ""}
            is_url_only = supplier_url and not ai_filepaths
            if supplier_url:
                if is_url_only:
                    _update_job(job_id, progress=5, message='Deep-scraping supplier website...')
                    site_data = scrape_url_data_deep(supplier_url)
                    sub_count = site_data.get('sub_pages_scraped', 0)
                    _update_job(job_id, progress=12, message=f'Scraped main page + {sub_count} product pages' if sub_count else 'Website scraped, extracting products...')
                else:
                    _update_job(job_id, progress=10, message='Reading Website Text...')
                    site_data = scrape_url_data(supplier_url)

            _update_job(job_id, progress=15, message='Extracting products with AI...')
            products_list = generate_bulk_pis_data(ai_filepaths, site_data, product_filter=product_filter)
            total_items = len(products_list)
            if total_items == 0:
                _update_job(job_id, status='completed', progress=100,
                            message='No products found in document.',
                            redirect_url='/dashboard/marketing',
                            completed_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())
                return

            _update_job(job_id, progress=20, message=f'Found {total_items} products. Processing...')
            ai_filepath = ai_filepaths[0] if ai_filepaths else None

            for idx, p_data in enumerate(products_list):
                header = p_data.get('header_info', {})
                brand     = header.get('brand', '')
                model_id  = header.get('model_number', '')
                prod_name = header.get('product_name', '')
                display_name = prod_name or model_id or f"Item_{idx+1}"
                current_progress = 20 + int(((idx + 1) / total_items) * 70)
                _update_job(job_id, progress=current_progress, message=f'Processing {idx+1}/{total_items}: {display_name}')

                try:
                    search_query = _build_bulk_query(brand, prod_name, model_id, display_name)
                    extracted_image_path = None

                    if ai_filepath:
                        pdf_term = model_id if model_id else display_name
                        extracted_image_path = extract_specific_image(ai_filepath, pdf_term, upload_folder)
                        if not extracted_image_path and model_id and display_name != model_id:
                            extracted_image_path = extract_specific_image(ai_filepath, display_name, upload_folder)

                    if not extracted_image_path:
                        ai_found_url = p_data.get('found_image_url')
                        if ai_found_url and str(ai_found_url).startswith('http'):
                            extracted_image_path = download_web_image(ai_found_url, display_name, upload_folder)
                    if not extracted_image_path:
                        image_url = find_and_validate_image(search_query, supplier_url)
                        if image_url:
                            extracted_image_path = download_web_image(image_url, display_name, upload_folder)
                    if not extracted_image_path:
                        simple_url = find_image_simple(search_query, supplier_url)
                        if simple_url:
                            extracted_image_path = download_web_image(simple_url, display_name, upload_folder)

                    if extracted_image_path:
                        extracted_image_path = store_image(extracted_image_path, display_name)

                    new_product = Product(
                        model_name=display_name, pis_data=p_data,
                        image_path=extracted_image_path,
                        seo_keywords=p_data.get('seo_data', {}).get('generated_keywords', ''),
                        workflow_stage='marketing_draft'
                    )
                    db.session.add(new_product)
                    db.session.commit()
                    log_event(new_product.id, user_name, 'New Product Added',
                              'This product was imported as part of a bulk extraction.', 'neutral')
                    save_version_snapshot(new_product, label='Initial version', is_major=True)

                except Exception as product_err:
                    print(f"⚠️ [ASYNC BULK] Error for '{display_name}': {product_err}")
                    try:
                        fallback = Product(
                            model_name=display_name, pis_data=p_data, image_path=None,
                            seo_keywords=p_data.get('seo_data', {}).get('generated_keywords', ''),
                            workflow_stage='marketing_draft'
                        )
                        db.session.add(fallback)
                        db.session.commit()
                        log_event(fallback.id, user_name, 'PIS Draft Created',
                                  f'Imported via Bulk (image search failed).', 'neutral')
                        save_version_snapshot(fallback, label='Original', is_major=True)
                    except Exception:
                        db.session.rollback()

            clear_pdf_cache()
            _update_job(job_id, status='completed', progress=100,
                        message=f'Bulk import complete — {total_items} products created!',
                        redirect_url='/dashboard/marketing',
                        completed_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

        except Exception as e:
            import traceback; traceback.print_exc()
            _update_job(job_id, status='failed', progress=100,
                        message=f'Bulk import failed: {str(e)[:100]}',
                        error=str(e), completed_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())


# ── ASYNC JOB ROUTES ─────────────────────────────────────────────────────────

@api_bp.route('/api/pis/generate', methods=['POST'])
def api_pis_generate_async():
    model_name    = request.form.get('model_name', '').strip()
    supplier_url  = request.form.get('supplier_url', '').strip()
    ai_files      = request.files.getlist('ai_document')
    contains_images = request.form.get('contains_images') == 'on'

    if not model_name and not supplier_url and not ai_files:
        return jsonify({"error": "Please provide a model name, document, or URL."}), 400

    active_count = Job.query.filter(Job.status.in_(('queued', 'processing'))).count()
    if active_count >= 5:
        return jsonify({"error": "Maximum 5 concurrent generations allowed. Please wait for a slot to free up."}), 429

    ai_filepaths = []
    for ai_file in ai_files:
        if ai_file and ai_file.filename:
            filename = secure_filename(ai_file.filename)
            filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
            ai_file.save(filepath)
            ai_filepaths.append(filepath)

    job_id    = str(uuid.uuid4())[:8]
    user_name = get_current_username()
    _app      = current_app._get_current_object()  # type: ignore[attr-defined]

    db.session.add(Job(
        id=job_id, model_name=model_name or 'Unknown Product',
        status='queued', progress=0, message='Queued — waiting for slot...',
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    ))
    db.session.commit()
    pis_executor.submit(_pis_worker, _app, job_id, model_name, supplier_url, ai_filepaths, contains_images, user_name)
    return jsonify({"ok": True, "job_id": job_id, "message": f"Generation started for '{model_name}'"}), 202


@api_bp.route('/api/pis/jobs', methods=['GET'])
def api_pis_jobs():
    jobs = Job.query.filter_by(dismissed=False).order_by(Job.created_at.asc()).all()
    result = sorted([{
        'id': j.id, 'model_name': j.model_name or '', 'status': j.status,
        'message': j.message or '', 'progress': j.progress or 0,
        'redirect_url': j.redirect_url, 'dismissed': j.dismissed,
    } for j in jobs], key=lambda j: (0 if j['status'] in ('queued', 'processing') else 1))
    resp = jsonify(result)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@api_bp.route('/api/pis/jobs/<job_id>', methods=['DELETE'])
def api_pis_dismiss_job(job_id):
    job = db.session.get(Job, job_id)
    if not job:
        return jsonify({"ok": True})
    if job.status not in ('completed', 'failed', 'preview_ready'):
        return jsonify({"error": "Cannot dismiss an active job"}), 400
    job.dismissed = True
    db.session.commit()
    return jsonify({"ok": True})


# ── PROFORMA ASYNC WORKERS (fire-and-redirect Generate PIS) ──────────────────
#
# The /import_proforma single + bulk flows both end with the user clicking
# "Generate PIS". Historically that call ran synchronously and the user
# watched a streaming progress bar until done. Now the click enqueues a Job
# and the response carries a redirect to /dashboard/marketing — the
# bottom-right tracker widget (base.html) picks up the job and shows
# per-product progress while the user keeps working in the gallery.
#
# Both workers persist `_source_files` / `_bulk_source_filenames` on
# pis_data so the Edit PIS Gallery tab's Crop-from-proforma keeps working
# after generation.


def _single_finalize_worker(app, job_id, token, model_name,
                            selected_image, gallery_paths, user_name):
    """Background worker for the single-mode Generate PIS button.

    Wizard now stops at the "Detected product" step — this worker owns
    everything after the click:

      1. Run the full proforma extraction (content + structured fields).
      2. Image extraction, simplified two-tier strategy:
         • Proforma WITH image  : extract from PDF + smart web search.
         • Proforma WITHOUT image: scrape supplier URL + smart web search.
         (Supplier URL is auto-discovered if the session doesn't have one.)
         No DuckDuckGo / screenshot fallbacks — the user refines from the
         Edit PIS gallery if these come up empty.
      3. Persist Product + initial version snapshot + category classification.

    `selected_image` / `gallery_paths` arguments are kept for backwards
    compatibility but ignored (the old picker UI is gone).
    """
    from utils.api_metering import job_scope
    with app.app_context(), job_scope(job_id):
        try:
            _update_job(job_id, status='processing', progress=5,
                        message='Reading proforma...')

            sess = sw.get_session(token)
            if not sess:
                _update_job(
                    job_id, status='failed', progress=100,
                    message='Wizard session expired — please re-upload.',
                    completed_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                )
                return

            file_paths   = sess.get('file_paths') or []
            supplier_url = (sess.get('supplier_url') or '').strip()
            brand        = (sess.get('brand') or '').strip()

            upload_folder = app.config['UPLOAD_FOLDER']
            if not os.path.isabs(upload_folder):
                upload_folder = os.path.join(app.root_path, upload_folder)

            # ── 1. Content extraction ────────────────────────────────────
            # Always run Brave web search so single-wizard imports get the
            # same Web-grounded coverage as the bulk wizard. The supplier
            # URL scrape (only set in Edit PIS today) is kept as a text
            # supplement when present. Both feed `site_data['text']` so
            # `generate_proforma_data` can ground its output against them.
            site_data = {"text": "", "html": ""}
            site_text_parts: list[str] = []
            if supplier_url:
                _update_job(job_id, progress=15, message='Scraping supplier URL...')
                try:
                    scraped = scrape_url_data(supplier_url) or {}
                    if scraped.get('text'):
                        site_text_parts.append(scraped['text'])
                    site_data['html'] = scraped.get('html') or ''
                except Exception as e:
                    print(f'[single async] supplier scrape failed (continuing): {e}')

            _update_job(job_id, progress=22, message='Searching the web for product context...')
            web_context = ""
            try:
                from utils.image_processing import gather_web_context_for_content
                web_context = gather_web_context_for_content(
                    model_name, brand=brand or None,
                ) or ""
                if web_context:
                    site_text_parts.append(web_context)
            except Exception as e:
                print(f"[single async] Brave web context failed for '{model_name}': {e}")

            site_data['text'] = "\n\n".join(p for p in site_text_parts if p)

            _update_job(job_id, progress=30, message='Extracting content with AI...')
            extracted = generate_proforma_data(
                file_paths=file_paths, url_data=site_data,
                extraction_mode='single', brand_hint=brand or None,
            )
            if not extracted:
                _update_job(
                    job_id, status='failed', progress=100,
                    message='AI returned no products from this source.',
                    completed_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                )
                return

            raw_doc_text = extract_raw_text_from_files(file_paths) or ""
            raw = extracted[0]
            pis = proforma_to_pis_data(raw, raw_text=raw_doc_text,
                                       source_files=file_paths) or {}
            pis.setdefault('header_info', {})
            if not pis['header_info'].get('product_name'):
                pis['header_info']['product_name'] = model_name

            header = pis.get('header_info', {}) or {}
            final_name  = (header.get('product_name') or model_name or '').strip() or model_name
            final_brand = (header.get('brand') or brand or '').strip() or brand

            # Origin classification — grep AI fields against proforma raw
            # text (verified) and combined web context (web_grounded) so
            # the verify-PIS legend renders Fact / Web / AI-enriched / AI
            # badges instead of falling back to a uniform red ✨.
            try:
                from helpers import classify_flat_pis_origins
                combined_web = site_data['text']
                field_origins, spec_origins = classify_flat_pis_origins(
                    pis, raw_doc_text, web_context=combined_web or None,
                )
                pis['_field_origins'] = field_origins
                pis['_spec_origins'] = spec_origins
                if combined_web:
                    pis['_web_context'] = combined_web
            except Exception as e:
                print(f'[single async] origin classification failed: {e}')

            # ── 2. Image extraction — simplified two-tier ────────────────
            candidates: list[str] = []  # relative `uploads/...` paths, deduped

            # Tier 1A: try to pull the image directly from the proforma.
            #          Works only when the PDF actually contains a product photo.
            _update_job(job_id, progress=50, message='Looking for image in proforma...')
            pdf_path = None
            if file_paths:
                try:
                    pdf_path = extract_specific_image(file_paths[0], final_name, upload_folder)
                except Exception as e:
                    print(f'[single async] PDF image extract failed: {e}')
                    pdf_path = None
            if pdf_path:
                candidates.append(pdf_path)

            # Discover a supplier URL if the session didn't give us one —
            # needed for Tier-1B (proforma had no image) and useful as a
            # bias for the Tier-2 smart web search.
            if not supplier_url:
                _update_job(job_id, progress=58, message='Finding supplier page...')
                try:
                    from utils.single_wizard import discover_supplier_url
                    sup = discover_supplier_url(final_name, final_brand or None) or {}
                    supplier_url = (sup.get('url') or '').strip()
                except Exception as e:
                    print(f'[single async] supplier discovery failed: {e}')
                    supplier_url = ''

            # Tier 1B: proforma had no usable image → pull candidates from
            # the supplier URL itself (scrape product page images).
            if not pdf_path and supplier_url:
                _update_job(job_id, progress=65, message='Pulling images from supplier page...')
                try:
                    from utils.single_wizard import extract_images_from_user_url
                    url_results = extract_images_from_user_url(
                        supplier_url, final_name, upload_folder, max_results=3,
                    ) or []
                    for r in url_results:
                        p = r.get('path')
                        if p and p not in candidates:
                            candidates.append(p)
                except Exception as e:
                    print(f'[single async] supplier URL image extract failed: {e}')

            # Tier 2 (both branches): smart web search.
            _update_job(job_id, progress=75, message='Smart web image search...')
            try:
                rich_query = _build_query(pis, final_name)
                web_url = find_and_validate_image(rich_query, supplier_url or None)
                if web_url:
                    web_path = download_web_image(web_url, final_name, upload_folder)
                    if web_path and web_path not in candidates:
                        candidates.append(web_path)
            except Exception as e:
                print(f'[single async] smart web search failed: {e}')

            # First candidate becomes the main image; the rest go to the gallery.
            main_image = None
            additional_images: list[str] = []
            if candidates:
                main_image = store_image(candidates[0], final_name) or candidates[0]
                additional_images = [p for p in candidates[1:] if p]

            # ── 3. Save ─────────────────────────────────────────────────
            _update_job(job_id, progress=85, message='Saving product...')
            new_product = Product(
                model_name=final_name, pis_data=pis,
                image_path=main_image,
                additional_images=additional_images,
                seo_keywords=(pis or {}).get('seo_data', {}).get('generated_keywords', ''),
                workflow_stage='marketing_draft',
            )
            db.session.add(new_product)
            db.session.commit()
            product_id = new_product.id

            log_event(product_id, user_name, 'New Product Added',
                      'Imported via single-item wizard (background).', 'neutral')
            save_version_snapshot(new_product, label='Initial version', is_major=True)

            # Inline category classification — keeps single-import on par with bulk.
            _update_job(job_id, progress=93, message='Classifying product category...')
            try:
                from utils.category_classifier import classify_product_category
                classification = classify_product_category(pis) or {}
                if classification.get('category_1'):
                    p = db.session.get(Product, product_id)
                    if p:
                        set_product_category(
                            p,
                            classification.get('category_1', ''),
                            classification.get('category_2', ''),
                            classification.get('category_3', ''),
                        )
                        db.session.commit()
            except Exception as e:
                print(f'[single async] category classification skipped: {e}')

            sw.drop_session(token)
            clear_pdf_cache()
            _update_job(
                job_id, status='completed', progress=100,
                message=f'Saved {new_product.model_name}',
                product_id=product_id,
                redirect_url='/dashboard/marketing',
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            )
        except Exception as e:
            import traceback; traceback.print_exc()
            _update_job(
                job_id, status='failed', progress=100,
                message=f'Generation failed: {str(e)[:100]}',
                error=str(e),
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            )


def _bulk_extract_worker(app, job_id, token, edited_groups, edited_items,
                         user_name):
    """Background worker for the bulk-mode Generate N PIS button.

    Stages every PIS payload in MEMORY first (stub build + enrichment), then
    persists all Product rows in a single commit at the end. The user's
    Product Gallery therefore never sees half-baked drafts — products only
    show up once every PIS in the batch is fully enriched (content +
    category + image candidates).
    """
    import uuid as _uuid
    from utils.api_metering import job_scope
    with app.app_context(), job_scope(job_id):
        try:
            _update_job(job_id, status='processing', progress=5,
                        message='Preparing drafts...')

            sess = bw.get_session(token)
            if not sess:
                _update_job(
                    job_id, status='failed', progress=100,
                    message='Bulk session expired — please re-upload.',
                    completed_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                )
                return

            origin_hint = sess.get('origin_hint') or 'unknown'
            source_filenames = sess.get('file_names') or []
            triage_summary = (sess.get('triage') or {}).get('summary') or {}

            batch_id = _uuid.uuid4().hex

            upload_folder = app.config['UPLOAD_FOLDER']
            if not os.path.isabs(upload_folder):
                upload_folder = os.path.join(app.root_path, upload_folder)

            # Step 1 — build every stub PIS in memory (no DB writes).
            staged: list[dict] = []
            for cluster_idx, cluster in enumerate(edited_groups):
                pis = bw.build_stub_pis_from_cluster(
                    cluster, edited_items, batch_id, origin_hint,
                    cluster_index=cluster_idx,
                    source_filenames=source_filenames,
                    triage_summary=triage_summary,
                )
                if not pis:
                    continue
                model_name = pis.pop('_bulk_model_name', None) \
                              or (pis.get('header_info', {}).get('product_name') or 'Item')
                staged.append({
                    'pis':           pis,
                    'model_name':    model_name,
                    'cluster_index': cluster_idx,
                })

            total = len(staged)
            if total == 0:
                _update_job(
                    job_id, status='completed', progress=100,
                    message='No active drafts created — every row was skipped.',
                    redirect_url='/dashboard/marketing',
                    completed_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                )
                return

            # Step 2 — enrich each staged PIS in memory (content + category +
            # image). Clusters run in parallel because each one is mostly
            # I/O bound (Gemini calls, HTTP downloads) — sequential was the
            # killer in the pre-Phase-3 run logs (4× the wall time of the
            # slowest cluster).
            #
            # max_workers: 4 — picked to stay well under gemini-2.5-flash
            # paid-tier RPM (~1000) and the typical browser concurrent-HTTP
            # cap. Bump only after confirming Gemini quota headroom.
            # Failures inside a worker keep the stub so the product still
            # gets created at the end — partial > nothing.
            from threading import Lock
            _progress_lock = Lock()
            _done = {'n': 0}

            def _enrich_one(entry: dict) -> None:
                # Worker threads do NOT inherit the parent's Flask app
                # context, so we push a fresh one per worker. Without this,
                # `_update_job`'s db.session.rollback() blows up with
                # "Working outside of application context."
                # ContextVars (incl. job_scope's _current_job_id) ALSO
                # don't propagate from the parent thread to ThreadPoolExecutor
                # children — re-enter the job_scope here so metering rows
                # written from inside enrichment get attributed to this job.
                with app.app_context(), job_scope(job_id):
                    display_name = entry['model_name'] or 'Draft'
                    try:
                        entry['pis'] = bw.enrich_product(
                            entry['pis'], upload_folder,
                            tasks=['content', 'category', 'image'],
                        )
                    except Exception as e:
                        print(f"[bulk async] enrich failed for '{display_name}': {e}")
                    with _progress_lock:
                        _done['n'] += 1
                        pct = 5 + int((_done['n'] / total) * 88)
                        _update_job(job_id, progress=pct,
                                    message=f"Enriching {_done['n']}/{total}: {display_name}")

            with ThreadPoolExecutor(max_workers=min(4, total)) as ex:
                list(ex.map(_enrich_one, staged))

            # Step 3 — persist every Product in ONE transaction. Until this
            # commit lands, the gallery has no idea this batch exists.
            _update_job(job_id, progress=95, message='Saving products...')
            created_ids: list[int] = []
            try:
                for entry in staged:
                    pis = entry['pis']
                    model_name = entry['model_name']
                    image_path = pis.get('_image_path')

                    seo_kw = (pis.get('_seo_keywords_pending')
                              or (pis.get('seo_data') or {}).get('generated_keywords')
                              or '')
                    seo_kw = str(seo_kw).strip()[:255]

                    # additional_images = every extracted candidate other
                    # than the chosen main, so the Edit PIS gallery + the
                    # PDF render show the full picker.
                    extras: list[str] = []
                    for c in (pis.get('_bulk_image_candidates') or []):
                        if not isinstance(c, dict):
                            continue
                        cp = c.get('path')
                        if cp and cp != image_path and cp not in extras:
                            extras.append(cp)

                    new_product = Product(
                        model_name=model_name, pis_data=pis,
                        image_path=image_path,
                        additional_images=extras,
                        seo_keywords=seo_kw,
                        workflow_stage='marketing_draft',
                    )
                    db.session.add(new_product)
                    db.session.flush()

                    # Canonical category columns + magento id lookup.
                    bulk_cats = pis.get('category_data') if isinstance(pis, dict) else None
                    if isinstance(bulk_cats, dict) and (bulk_cats.get('category_1') or '').strip():
                        try:
                            set_product_category(
                                new_product,
                                bulk_cats.get('category_1', ''),
                                bulk_cats.get('category_2', ''),
                                bulk_cats.get('category_3', ''),
                            )
                        except Exception as e:
                            print(f'[bulk async] category write failed for #{new_product.id}: {e}')

                    log_event(
                        new_product.id, user_name, 'New Product Added',
                        f'Imported via bulk wizard (batch {batch_id[:8]}, '
                        f'cluster {entry["cluster_index"] + 1}/{len(edited_groups)}).',
                        'neutral',
                    )
                    save_version_snapshot(
                        new_product,
                        label='Initial bulk draft (enriched)',
                        is_major=True,
                    )
                    created_ids.append(new_product.id)
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                import traceback; traceback.print_exc()
                _update_job(
                    job_id, status='failed', progress=100,
                    message=f'Failed to persist drafts: {str(e)[:100]}',
                    error=str(e),
                    completed_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                )
                return

            bw.update_session(token, batch_id=batch_id,
                              created_product_ids=created_ids)

            clear_pdf_cache()
            _update_job(
                job_id, status='completed', progress=100,
                message=f'Generated {len(created_ids)} product{"s" if len(created_ids) != 1 else ""}.',
                redirect_url='/dashboard/marketing',
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            )
        except Exception as e:
            import traceback; traceback.print_exc()
            _update_job(
                job_id, status='failed', progress=100,
                message=f'Bulk generation failed: {str(e)[:100]}',
                error=str(e),
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            )


@api_bp.route('/api/proforma/single/finalize_async', methods=['POST'])
@require_role('marketing', api=True, status=401)
def api_single_finalize_async():
    """Enqueue the single-mode Generate PIS. Returns 202 + redirect_url so
    the frontend can navigate to /dashboard/marketing immediately."""

    payload = request.get_json(silent=True) or {}
    token = (payload.get('session_token') or '').strip()
    model_name = (payload.get('model_name') or '').strip()
    selected_image = (payload.get('selected_image') or '').strip() or None

    raw_gallery = payload.get('gallery_images') or []
    gallery_paths: list[str] = []
    seen_g: set[str] = set()
    for item in raw_gallery:
        if isinstance(item, str):
            p = item.strip()
        elif isinstance(item, dict):
            p = str(item.get('path') or '').strip()
        else:
            p = ''
        if not p or p == selected_image or p in seen_g:
            continue
        seen_g.add(p)
        gallery_paths.append(p)

    if not token:
        return jsonify({"error": "session_token required"}), 400
    if not model_name:
        return jsonify({"error": "model_name required"}), 400

    active_count = Job.query.filter(Job.status.in_(('queued', 'processing'))).count()
    if active_count >= 5:
        return jsonify({"error": "Maximum 5 concurrent jobs. Please wait."}), 429

    job_id = str(uuid.uuid4())[:8]
    _app = current_app._get_current_object()  # type: ignore[attr-defined]
    user_name = get_current_username()

    db.session.add(Job(
        id=job_id, model_name=model_name,
        status='queued', progress=0,
        message='Queued — waiting for slot...',
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    ))
    db.session.commit()

    pis_executor.submit(_single_finalize_worker, _app, job_id, token,
                        model_name, selected_image, gallery_paths, user_name)
    return jsonify({
        "ok": True, "job_id": job_id,
        "redirect_url": "/dashboard/marketing",
    }), 202


@api_bp.route('/api/proforma/bulk/extract_async', methods=['POST'])
@require_role('marketing', api=True, status=401)
def api_bulk_extract_async():
    """Enqueue the bulk-mode Generate N PIS. Returns 202 + redirect_url."""

    payload = request.get_json(silent=True) or {}
    token = (payload.get('session_token') or '').strip()
    edited_groups = payload.get('cluster_groups') or []
    edited_items = payload.get('items') or []

    if not token:
        return jsonify({"error": "session_token required"}), 400
    if not isinstance(edited_groups, list) or not edited_groups:
        return jsonify({"error": "cluster_groups required"}), 400
    if not isinstance(edited_items, list):
        return jsonify({"error": "items required"}), 400

    active_count = Job.query.filter(Job.status.in_(('queued', 'processing'))).count()
    if active_count >= 5:
        return jsonify({"error": "Maximum 5 concurrent jobs. Please wait."}), 429

    job_id = str(uuid.uuid4())[:8]
    _app = current_app._get_current_object()  # type: ignore[attr-defined]
    user_name = get_current_username()

    active_count_clusters = sum(
        1 for g in edited_groups
        if any(not (edited_items[idx].get('skip') if isinstance(edited_items[idx], dict) else False)
               for idx in (g.get('item_indexes') or [])
               if isinstance(idx, int) and 0 <= idx < len(edited_items))
    )
    label = f'Bulk · {active_count_clusters} item{"s" if active_count_clusters != 1 else ""}'

    db.session.add(Job(
        id=job_id, model_name=label,
        status='queued', progress=0,
        message='Queued — waiting for slot...',
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    ))
    db.session.commit()

    pis_executor.submit(_bulk_extract_worker, _app, job_id, token,
                        edited_groups, edited_items, user_name)
    return jsonify({
        "ok": True, "job_id": job_id,
        "redirect_url": "/dashboard/marketing",
    }), 202


@api_bp.route('/api/pis/generate_bulk', methods=['POST'])
def api_bulk_generate_async():
    supplier_url   = request.form.get('supplier_url', '').strip()
    ai_files       = request.files.getlist('ai_document')
    contains_images = request.form.get('contains_images') == 'on'
    product_filter  = request.form.get('product_filter', '').strip()

    ai_filepaths = []
    for ai_file in ai_files:
        if ai_file and ai_file.filename:
            filename = secure_filename(ai_file.filename)
            filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
            ai_file.save(filepath)
            ai_filepaths.append(filepath)

    if not ai_filepaths and not supplier_url:
        return jsonify({"error": "Please provide at least a document or a supplier URL."}), 400

    active_count = Job.query.filter(Job.status.in_(('queued', 'processing'))).count()
    if active_count >= 5:
        return jsonify({"error": "Maximum 5 concurrent jobs allowed. Please wait for a slot to free up."}), 429

    job_id    = str(uuid.uuid4())[:8]
    user_name = get_current_username()
    doc_names = ', '.join(os.path.basename(f) for f in ai_filepaths[:2])
    job_label = f"Bulk: {doc_names}" if doc_names else "Bulk Import"
    _app      = current_app._get_current_object()  # type: ignore[attr-defined]

    db.session.add(Job(
        id=job_id, model_name=job_label,
        status='queued', progress=0, message='Queued — waiting for slot...',
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    ))
    db.session.commit()
    pis_executor.submit(_bulk_pis_worker, _app, job_id, supplier_url, ai_filepaths, contains_images, product_filter, user_name)
    return jsonify({"ok": True, "job_id": job_id, "message": "Bulk generation started"}), 202


# ── PRODUCT IMAGE APIS ────────────────────────────────────────────────────────

@api_bp.route('/api/product/<int:product_id>/images/upload', methods=['POST'])
def api_upload_image(product_id):
    product = Product.query.get_or_404(product_id)
    files = request.files.getlist('file')
    if not files or all(f.filename == '' for f in files):
        return {"error": "No file provided"}, 400
    try:
        uploaded = []
        for file in files:
            if not file or file.filename == '':
                continue
            filename = secure_filename(f"extra_{product.id}_{int(time.time())}_{file.filename}")
            save_path = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
            file.save(save_path)

            # Move to cloud/local via storage abstraction
            db_path = store_image(save_path, filename)

            if not product.image_path:
                product.image_path = db_path
                is_main = True
            else:
                imgs = list(product.additional_images) if product.additional_images else []
                imgs.append(db_path)
                product.additional_images = imgs
                flag_modified(product, 'additional_images')
                is_main = False

            uploaded.append({'path': db_path, 'is_main': is_main})
            img_type = 'main photo' if is_main else 'gallery photo'
            log_event(product.id, get_current_username(), 'Photo Added',
                      f'A new {img_type} was uploaded: {file.filename}', 'neutral')

        db.session.commit()
        if len(uploaded) == 1:
            return {"status": "success", "path": uploaded[0]['path'], "is_main": uploaded[0]['is_main']}
        return {"status": "success", "count": len(uploaded)}
    except Exception as e:
        return {"error": str(e)}, 500


@api_bp.route('/api/product/<int:product_id>/images/delete', methods=['POST'])
def api_delete_image(product_id):
    product = Product.query.get_or_404(product_id)
    data = request.get_json()
    path_to_delete = data.get('path')
    if not path_to_delete:
        return {"error": "No path provided"}, 400
    try:
        deleted_type = 'image'
        if product.image_path == path_to_delete:
            deleted_type = 'main image'
            product.image_path = None
            imgs = list(product.additional_images) if product.additional_images else []
            if imgs:
                product.image_path = imgs.pop(0)
                product.additional_images = imgs
                flag_modified(product, 'additional_images')
        else:
            imgs = list(product.additional_images) if product.additional_images else []
            if path_to_delete in imgs:
                deleted_type = 'additional image'
                imgs.remove(path_to_delete)
                product.additional_images = imgs
                flag_modified(product, 'additional_images')

        # ── Cascade into the JSON side ─────────────────────────────────────
        # Same path can be referenced in three other JSON locations and they
        # all need to drop it together — otherwise the variant strip and the
        # Edit-PIS gallery's source badges still point at a file that no
        # longer exists in the gallery list.
        #   1. pis_data.variants[*].image_path / image_paths   (per-variant)
        #   2. pis_data._bulk_image_candidates                  (gallery source tags)
        #   3. pis_data._image_path                             (default thumbnail mirror)
        pis = dict(product.pis_data or {})
        pis_changed = False

        variants = pis.get('variants') or []
        for v in variants:
            if not isinstance(v, dict):
                continue
            # Drop from the variant's image_paths list.
            existing = list(v.get('image_paths') or [])
            if path_to_delete in existing:
                existing = [p for p in existing if p != path_to_delete]
                v['image_paths'] = existing
                pis_changed = True
            # Drop the variant's primary image when it matches; promote the
            # first remaining image_paths entry if there is one.
            if v.get('image_path') == path_to_delete:
                v['image_path'] = existing[0] if existing else None
                pis_changed = True
        if pis_changed:
            pis['variants'] = variants

        candidates = pis.get('_bulk_image_candidates') or []
        if isinstance(candidates, list):
            filtered = [
                c for c in candidates
                if not (isinstance(c, dict) and c.get('path') == path_to_delete)
            ]
            if len(filtered) != len(candidates):
                pis['_bulk_image_candidates'] = filtered
                pis_changed = True

        if pis.get('_image_path') == path_to_delete:
            pis['_image_path'] = product.image_path or None
            pis_changed = True

        if pis_changed:
            product.pis_data = pis
            flag_modified(product, 'pis_data')

        fname = path_to_delete.split('/')[-1] if '/' in path_to_delete else path_to_delete
        log_event(product.id, get_current_username(), 'Photo Removed',
                  f'Removed a {deleted_type} photo: {fname}', 'neutral')
        db.session.commit()
        return {"status": "success"}
    except Exception as e:
        return {"error": str(e)}, 500


@api_bp.route('/api/product/<int:product_id>/images/set_main', methods=['POST'])
def api_set_main_image(product_id):
    """Promote an existing gallery image to the main slot. The previous
    main (if any) is demoted into additional_images so nothing is lost.
    Body: {"path": "uploads/..."}."""
    product = Product.query.get_or_404(product_id)
    data = request.get_json(silent=True) or {}
    new_main = (data.get('path') or '').strip()
    if not new_main:
        return {"error": "No path provided"}, 400

    imgs = list(product.additional_images) if product.additional_images else []
    if new_main != product.image_path and new_main not in imgs:
        return {"error": "Path not in this product's gallery"}, 400

    if new_main == product.image_path:
        return {"status": "success", "main_path": new_main, "additional_images": imgs}

    old_main = product.image_path
    if new_main in imgs:
        imgs.remove(new_main)
    if old_main and old_main not in imgs:
        imgs.append(old_main)
    product.image_path = new_main
    product.additional_images = imgs
    flag_modified(product, 'additional_images')

    fname = new_main.split('/')[-1] if '/' in new_main else new_main
    log_event(product.id, get_current_username(), 'Main Photo Changed',
              f'Promoted gallery photo to main: {fname}', 'neutral')
    db.session.commit()
    return {"status": "success", "main_path": new_main,
            "additional_images": list(product.additional_images or [])}


# ── PRODUCT GALLERY IMAGE ACTIONS ─────────────────────────────────────────────
#
# Per-product image-source menu used by the Edit PIS Gallery tab. Mirrors
# the bulk-workspace routes (`/import_proforma/bulk/workspace/<batch>/<id>/image/*`)
# but works on ANY Product — no batch context required.
#
# Each successful action also mirrors the new image into `Product.additional_images`
# so the PIS PDF gallery picks it up automatically. Sources are tagged on
# `pis_data._bulk_image_candidates` so the Gallery tab can show source badges.


def _resolve_product_proforma_paths(product) -> list[str]:
    """Resolve the proforma source files for a single Product back to absolute
    paths on disk. Supports both single-import (`_source_files`) and bulk-import
    (`_bulk_source_filenames`) shapes."""
    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)

    pis = product.pis_data or {}
    seen, abs_paths = set(), []

    # Single-import shape: `_source_files = ["uploads/<filename>", ...]`
    for rel in (pis.get('_source_files') or []):
        if not isinstance(rel, str) or not rel or rel in seen:
            continue
        seen.add(rel)
        basename = rel.split('/')[-1]
        p = os.path.join(upload_folder, basename)
        if os.path.exists(p):
            abs_paths.append(p)

    # Bulk-import shape: `_bulk_source_filenames = ["<filename>", ...]`
    for fn in (pis.get('_bulk_source_filenames') or []):
        if not isinstance(fn, str) or not fn or fn in seen:
            continue
        seen.add(fn)
        p = os.path.join(upload_folder, fn)
        if os.path.exists(p):
            abs_paths.append(p)

    return abs_paths


def _draft_meta_from_product(product) -> dict:
    """Build the routing-meta dict expected by utils/bulk_image_routing.py.
    Shape mirrors `_draft_to_routing_meta` in blueprints/marketing.py."""
    pis = product.pis_data or {}
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
        'id':           product.id,
        'name':         (header.get('product_name') or '').strip()
                          or (pis.get('_bulk_cluster_label') or '').strip()
                          or product.model_name
                          or f'Product #{product.id}',
        'brand':        (header.get('brand') or '').strip(),
        'model_number': (header.get('model_number') or '').strip(),
        'kind':         kind,
        'source_pages': list(pis.get('_bulk_source_pages') or []),
        'variants':     variants_meta,
    }


def _mirror_candidate_to_gallery(product, rel_path: str) -> None:
    """Append `rel_path` to product.additional_images (de-duped vs main).
    Does NOT promote to main — caller decides."""
    if not rel_path:
        return
    extras = list(product.additional_images or [])
    if rel_path != product.image_path and rel_path not in extras:
        extras.append(rel_path)
        product.additional_images = extras
        flag_modified(product, 'additional_images')


@api_bp.route('/api/product/<int:product_id>/image/web', methods=['POST'])
@require_role('marketing', 'admin', 'director', api=True, status=401)
def api_product_image_web(product_id):
    """Search the supplier or general web for product images and append
    candidates to the gallery. Query arg `mode=supplier|general`."""
    product = Product.query.get_or_404(product_id)

    mode = (request.args.get('mode') or 'general').strip().lower()
    if mode not in ('general', 'supplier'):
        mode = 'general'

    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)

    from utils import bulk_image_routing as bir
    meta = _draft_meta_from_product(product)
    candidates = bir.regenerate_image_via_web(meta, upload_folder,
                                              max_results=3, mode=mode) or []
    if not candidates:
        return {"candidates": [], "added": [],
                "error": f"no {mode}-mode web results"}, 200

    pis = dict(product.pis_data or {})
    existing = pis.get('_bulk_image_candidates') or []
    seen = {c.get('path') for c in existing if isinstance(c, dict)}
    added: list[str] = []
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
        added.append(path)
        _mirror_candidate_to_gallery(product, path)
    pis['_bulk_image_candidates'] = existing
    if not product.image_path and added:
        product.image_path = added[0]
        pis['_image_path'] = added[0]
    product.pis_data = pis
    flag_modified(product, 'pis_data')

    log_event(product.id, get_current_username(), 'Photo Added',
              f'Added {len(added)} image{"s" if len(added) != 1 else ""} '
              f'from {mode}-mode web search.', 'neutral')
    db.session.commit()

    return {
        "image_path": product.image_path or '',
        "added": added,
        "candidates": existing,
        "additional_images": list(product.additional_images or []),
    }


@api_bp.route('/api/product/<int:product_id>/image/extract_from_url',
              methods=['POST'])
@require_role('marketing', 'admin', 'director', api=True, status=401)
def api_product_image_extract_from_url(product_id):
    """Pull up to 3 images from a user-supplied URL. Body: {"url": "..."}."""
    product = Product.query.get_or_404(product_id)

    payload = request.get_json(silent=True) or {}
    suggested_url = (payload.get('url') or '').strip()
    if not suggested_url:
        return {"error": "url required"}, 400

    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)

    from utils.single_wizard import extract_images_from_user_url
    meta = _draft_meta_from_product(product)
    model_name = (meta.get('name') or meta.get('model_number') or 'product').strip()
    try:
        results = extract_images_from_user_url(
            suggested_url, model_name, upload_folder, max_results=3,
        ) or []
    except Exception as e:
        return {"error": f"URL fetch failed: {e}"}, 502

    if not results:
        return {"candidates": [], "added": [],
                "error": "no usable images on the suggested page"}, 200

    pis = dict(product.pis_data or {})
    existing = pis.get('_bulk_image_candidates') or []
    seen = {c.get('path') for c in existing if isinstance(c, dict)}
    added: list[str] = []
    for r in results:
        path = r.get('path')
        if not path or path in seen:
            continue
        existing.append({
            'path': path, 'source': 'user_url',
            'page_url': suggested_url,
            'variant_sku': '', 'matched_label': '', 'confidence': 'medium',
        })
        seen.add(path)
        added.append(path)
        _mirror_candidate_to_gallery(product, path)
    pis['_bulk_image_candidates'] = existing
    if not product.image_path and added:
        product.image_path = added[0]
        pis['_image_path'] = added[0]
    product.pis_data = pis
    flag_modified(product, 'pis_data')

    log_event(product.id, get_current_username(), 'Photo Added',
              f'Added {len(added)} image{"s" if len(added) != 1 else ""} '
              f'from user-supplied URL.', 'neutral')
    db.session.commit()

    return {
        "image_path": product.image_path or '',
        "added": added,
        "candidates": existing,
        "additional_images": list(product.additional_images or []),
    }


@api_bp.route('/api/product/<int:product_id>/image/ai', methods=['POST'])
@require_role('marketing', 'admin', 'director', api=True, status=401)
def api_product_image_ai(product_id):
    """Re-run nano-banana on the source proforma to isolate the product."""
    product = Product.query.get_or_404(product_id)

    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)

    file_paths = _resolve_product_proforma_paths(product)
    if not file_paths:
        return {"error": "no proforma source on file for this product"}, 400

    from utils import bulk_image_routing as bir
    meta = _draft_meta_from_product(product)
    rel = bir.regenerate_image_via_ai(meta, file_paths, upload_folder)
    if not rel:
        return {"error": "AI generation returned no image"}, 502

    pis = dict(product.pis_data or {})
    candidates = pis.get('_bulk_image_candidates') or []
    if not any(c.get('path') == rel for c in candidates if isinstance(c, dict)):
        candidates.append({
            'path': rel, 'source': 'ai',
            'variant_sku': '', 'matched_label': '', 'confidence': 'medium',
        })
    pis['_bulk_image_candidates'] = candidates
    pis['_image_path'] = rel
    product.pis_data = pis
    flag_modified(product, 'pis_data')
    _mirror_candidate_to_gallery(product, rel)

    log_event(product.id, get_current_username(), 'Photo Added',
              'Added an AI-isolated image from the proforma.', 'neutral')
    db.session.commit()

    return {
        "image_path": rel,
        "added": [rel],
        "candidates": candidates,
        "additional_images": list(product.additional_images or []),
    }


# ── Phase 3.2 — Retouch (gemini image-out) and Generate (Imagen) ────────────
# `/image/ai` above remains for the legacy "isolate from proforma" path; the
# two endpoints below back the new "Retouch with AI" + "Generate with AI"
# buttons in the Edit PIS image dropdown.

@api_bp.route('/api/product/<int:product_id>/image/enhance', methods=['POST'])
@require_role('marketing', 'admin', 'director', api=True, status=401)
def api_product_image_enhance(product_id):
    """Retouch an existing gallery image — clean background, fix artifacts,
    keep the product pixel-faithful. Body: {"source_path": "uploads/...",
    "user_note": "remove the white line"}."""
    product = Product.query.get_or_404(product_id)

    payload = request.get_json(silent=True) or {}
    source_path = (payload.get('source_path') or '').strip()
    user_note = (payload.get('user_note') or '').strip()
    if not source_path:
        return {"error": "source_path required"}, 400

    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)

    from utils.image_processing import enhance_image_with_gemini
    meta = _draft_meta_from_product(product)
    model_name = (meta.get('name') or meta.get('model_number') or 'product').strip()
    rel = enhance_image_with_gemini(
        source_path, model_name, upload_folder, user_note=user_note or None,
    )
    if not rel:
        return {"error": "retouch returned no image"}, 502

    pis = dict(product.pis_data or {})
    candidates = pis.get('_bulk_image_candidates') or []
    if not any(c.get('path') == rel for c in candidates if isinstance(c, dict)):
        candidates.append({
            'path': rel, 'source': 'ai_enhanced',
            'page_url': source_path,  # source image acts as the "origin" link
            'variant_sku': '', 'matched_label': '',
            'user_note': user_note or '',
            'confidence': 'medium',
        })
    pis['_bulk_image_candidates'] = candidates
    product.pis_data = pis
    flag_modified(product, 'pis_data')
    _mirror_candidate_to_gallery(product, rel)

    note_msg = f' (note: "{user_note[:50]}…")' if user_note else ''
    log_event(product.id, get_current_username(), 'Photo Added',
              f'AI-retouched an existing gallery image{note_msg}.', 'neutral')
    db.session.commit()

    return {
        "image_path": product.image_path or '',
        "added": [rel],
        "candidates": candidates,
        "additional_images": list(product.additional_images or []),
    }


@api_bp.route('/api/product/<int:product_id>/image/generate', methods=['POST'])
@require_role('marketing', 'admin', 'director', api=True, status=401)
def api_product_image_generate(product_id):
    """Synthesize a brand-new product photo from the PIS description via
    Imagen 4. Body: {"user_note": "modern living room background"}."""
    product = Product.query.get_or_404(product_id)

    payload = request.get_json(silent=True) or {}
    user_note = (payload.get('user_note') or '').strip()

    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)

    meta = _draft_meta_from_product(product)
    model_name = (meta.get('name') or meta.get('model_number') or 'product').strip()
    brand = (meta.get('brand') or '').strip()
    # Pull the AI-extracted description from the PIS for the Imagen prompt.
    pis_for_desc = product.pis_data or {}
    description = (pis_for_desc.get('range_overview') or
                   pis_for_desc.get('customer_friendly_description') or '').strip()

    from utils.image_processing import generate_image_with_imagen
    rel = generate_image_with_imagen(
        product_name=model_name, upload_folder=upload_folder,
        brand=brand or None,
        description=description or None,
        user_note=user_note or None,
    )
    if not rel:
        return {"error": "Imagen returned no image"}, 502

    pis = dict(product.pis_data or {})
    candidates = pis.get('_bulk_image_candidates') or []
    if not any(c.get('path') == rel for c in candidates if isinstance(c, dict)):
        candidates.append({
            'path': rel, 'source': 'ai_synthetic',
            'variant_sku': '', 'matched_label': '',
            'user_note': user_note or '',
            'confidence': 'low',  # synthetic — caller should verify
        })
    pis['_bulk_image_candidates'] = candidates
    product.pis_data = pis
    flag_modified(product, 'pis_data')
    _mirror_candidate_to_gallery(product, rel)

    note_msg = f' (note: "{user_note[:50]}…")' if user_note else ''
    log_event(product.id, get_current_username(), 'Photo Added',
              f'AI-generated a synthetic product image{note_msg}.', 'neutral')
    db.session.commit()

    return {
        "image_path": product.image_path or '',
        "added": [rel],
        "candidates": candidates,
        "additional_images": list(product.additional_images or []),
    }


@api_bp.route('/api/product/<int:product_id>/image/upload_to_gallery',
              methods=['POST'])
@require_role('marketing', 'admin', 'director', api=True, status=401)
def api_product_image_upload_to_gallery(product_id):
    """Per-product manual upload that records the candidate source as
    `upload` on `_bulk_image_candidates` (so the gallery's source badge
    is correct). Kept separate from the legacy `/images/upload` endpoint
    used by the marketing review form to avoid disturbing that path."""
    product = Product.query.get_or_404(product_id)

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
    filename = f"produp_{product.id}_{int(time.time())}_{safe_stem}{ext}"
    save_path = os.path.join(upload_folder, filename)
    f.save(save_path)
    rel = f"uploads/{filename}"

    pis = dict(product.pis_data or {})
    candidates = pis.get('_bulk_image_candidates') or []
    candidates.append({
        'path': rel, 'source': 'upload',
        'variant_sku': '', 'matched_label': '', 'confidence': 'high',
    })
    pis['_bulk_image_candidates'] = candidates
    if not product.image_path:
        product.image_path = rel
        pis['_image_path'] = rel
    product.pis_data = pis
    flag_modified(product, 'pis_data')
    _mirror_candidate_to_gallery(product, rel)

    log_event(product.id, get_current_username(), 'Photo Added',
              f'Uploaded a new image to the gallery: {f.filename}', 'neutral')
    db.session.commit()

    return {
        "image_path": product.image_path or rel,
        "added": [rel],
        "candidates": candidates,
        "additional_images": list(product.additional_images or []),
    }


@api_bp.route('/api/product/<int:product_id>/image/page_preview',
              methods=['GET'])
@require_role('marketing', 'admin', 'director', api=True, status=401)
def api_product_image_page_preview(product_id):
    """Render ONE page of the source proforma to a static-servable PNG so
    the crop modal has something to display. `?page=N` selects the page."""
    product = Product.query.get_or_404(product_id)
    file_paths = _resolve_product_proforma_paths(product)
    if not file_paths:
        return {"error": "no proforma source on file for this product"}, 400
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

    page_count = 1
    src = file_paths[0]
    if os.path.splitext(src)[1].lower() == '.pdf':
        try:
            import fitz
            with fitz.open(src) as _doc:  # type: ignore[attr-defined]
                page_count = len(_doc) or 1
        except Exception:
            page_count = 1
    return {"path": rel, "page": page_index, "page_count": page_count}


def _is_safe_product_preview_path(rel_path: str, upload_folder: str) -> tuple[bool, str]:
    """Same defense as the bulk-workspace `_is_safe_preview_path`, but for
    the per-product crop preview file prefix (`prodpreview_`)."""
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
    if not basename.startswith('prodpreview_'):
        return False, 'preview_path must be a prodpreview_* file'
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


@api_bp.route('/api/product/<int:product_id>/image/crop', methods=['POST'])
@require_role('marketing', 'admin', 'director', api=True, status=401)
def api_product_image_crop(product_id):
    """Two-step manual crop. With `?preview=1` the crop is saved as a
    `prodpreview_*.jpg` and the path returned for the user to confirm.
    Without it, the crop is committed straight to the gallery (legacy).
    Body: {source_path, crop: {x,y,w,h in [0,1]}}."""
    product = Product.query.get_or_404(product_id)

    payload = request.get_json(silent=True) or {}
    source_path = (payload.get('source_path') or '').strip()
    crop = payload.get('crop') or {}
    try:
        x = float(crop.get('x', 0)); y = float(crop.get('y', 0))
        w = float(crop.get('w', 0)); h = float(crop.get('h', 0))
    except (TypeError, ValueError):
        return {"error": "crop must contain x, y, w, h as numbers in [0,1]"}, 400
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1):
        return {"error": "crop out of range"}, 400
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
        target = (header.get('product_name') or '').strip() \
                  or product.model_name or f'product_{product.id}'
        safe_name = secure_filename(target) or 'product'
        prefix = 'prodpreview' if is_preview else 'prodcrop'
        filename = f"{prefix}_{safe_name}_{int(time.time() * 1000)}.jpg"
        out_path = os.path.join(upload_folder, filename)
        cropped.save(out_path, quality=95)
        rel = f"uploads/{filename}"

        if is_preview:
            return {"preview_path": rel, "image_path": rel}

        # Direct-save path (no preview/confirm).
        pis = dict(product.pis_data or {})
        candidates = pis.get('_bulk_image_candidates') or []
        candidates.append({
            'path': rel, 'source': 'crop',
            'variant_sku': '', 'matched_label': '', 'confidence': 'high',
        })
        pis['_bulk_image_candidates'] = candidates
        pis['_image_path'] = pis.get('_image_path') or rel
        product.pis_data = pis
        flag_modified(product, 'pis_data')
        _mirror_candidate_to_gallery(product, rel)
        if not product.image_path:
            product.image_path = rel
        log_event(product.id, get_current_username(), 'Photo Added',
                  'Added an image manually cropped from the proforma.', 'neutral')
        db.session.commit()
        return {"image_path": product.image_path or rel,
                "added": [rel],
                "candidates": candidates,
                "additional_images": list(product.additional_images or [])}
    except Exception as e:
        return {"error": f"crop failed: {e}"}, 500


@api_bp.route('/api/product/<int:product_id>/image/crop_commit',
              methods=['POST'])
@require_role('marketing', 'admin', 'director', api=True, status=401)
def api_product_image_crop_commit(product_id):
    """Promote a `prodpreview_*.jpg` from /image/crop?preview=1 into the
    permanent gallery. Body: {preview_path: "uploads/prodpreview_..."}."""
    product = Product.query.get_or_404(product_id)

    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)

    payload = request.get_json(silent=True) or {}
    preview_path = (payload.get('preview_path') or '').strip()
    ok, info = _is_safe_product_preview_path(preview_path, upload_folder)
    if not ok:
        return {"error": info}, 400
    abs_preview = info

    basename = os.path.basename(abs_preview)
    perm_basename = basename.replace('prodpreview_', 'prodcrop_', 1)
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
            'path': rel, 'source': 'crop',
            'variant_sku': '', 'matched_label': '', 'confidence': 'high',
        })
    pis['_bulk_image_candidates'] = candidates
    if not pis.get('_image_path'):
        pis['_image_path'] = rel
    product.pis_data = pis
    flag_modified(product, 'pis_data')
    _mirror_candidate_to_gallery(product, rel)
    if not product.image_path:
        product.image_path = rel

    log_event(product.id, get_current_username(), 'Photo Added',
              'Added an image manually cropped from the proforma.', 'neutral')
    db.session.commit()
    return {
        "image_path": product.image_path or rel,
        "added_path": rel,
        "candidates": candidates,
        "additional_images": list(product.additional_images or []),
    }


@api_bp.route('/api/product/<int:product_id>/image/reassign',
              methods=['POST'])
@require_role('marketing', 'admin', 'director', api=True, status=401)
def api_product_image_reassign(product_id):
    """Reassign a gallery image to a specific variant SKU.
    Body: {"path": "uploads/...", "variant_sku": "MODEL-SKU"}.

    • Updates the candidate's `variant_sku` tag in `_bulk_image_candidates`
    • Strips `path` from every other variant's image_paths to avoid dupes
    • Prepends `path` to the target variant's image_paths
    """
    product = Product.query.get_or_404(product_id)

    payload = request.get_json(silent=True) or {}
    path = (payload.get('path') or '').strip()
    variant_sku = (payload.get('variant_sku') or '').strip()
    if not path:
        return {"error": "path is required"}, 400

    pis = dict(product.pis_data or {})

    candidates = pis.get('_bulk_image_candidates') or []
    for c in candidates:
        if isinstance(c, dict) and c.get('path') == path:
            c['variant_sku'] = variant_sku
            break

    variants = pis.get('variants') or []
    for v in variants:
        if not isinstance(v, dict):
            continue
        existing = list(v.get('image_paths') or [])
        if v.get('image_path') == path:
            v.pop('image_path', None)
        if path in existing:
            existing.remove(path)
        v['image_paths'] = existing
        if not v.get('image_path') and existing:
            v['image_path'] = existing[0]

    if variant_sku:
        for v in variants:
            if not isinstance(v, dict):
                continue
            if (v.get('model_number') or '').strip() == variant_sku:
                paths = list(v.get('image_paths') or [])
                if path not in paths:
                    paths.insert(0, path)
                v['image_paths'] = paths
                v['image_path'] = paths[0]
                break

    pis['_bulk_image_candidates'] = candidates
    pis['variants'] = variants
    product.pis_data = pis
    flag_modified(product, 'pis_data')
    db.session.commit()

    return {"candidates": candidates, "variants": variants}


@api_bp.route('/api/product/<int:product_id>/image/crop_discard',
              methods=['POST'])
@require_role('marketing', 'admin', 'director', api=True, status=401)
def api_product_image_crop_discard(product_id):
    """Throw away an unwanted crop preview. Body: {preview_path}."""
    Product.query.get_or_404(product_id)

    upload_folder = current_app.config['UPLOAD_FOLDER']
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(current_app.root_path, upload_folder)

    payload = request.get_json(silent=True) or {}
    preview_path = (payload.get('preview_path') or '').strip()
    ok, info = _is_safe_product_preview_path(preview_path, upload_folder)
    if not ok:
        if info == 'preview file not found':
            return {"discarded": False}, 200
        return {"error": info}, 400
    try:
        os.remove(info)
    except OSError as e:
        return {"error": f"could not discard preview: {e}"}, 500
    return {"discarded": True}


# ── PRODUCT DELETE (soft) ─────────────────────────────────────────────────────

@api_bp.route('/api/product/<int:product_id>/delete', methods=['POST'])
@require_role('admin', 'marketing', 'director', api=True)
def api_delete_product(product_id):
    """Soft-delete a single product. The row stays in the DB with
    deleted_at set so it can be recovered later if needed; all dashboards
    already filter on deleted_at IS NULL."""
    product = Product.query.get_or_404(product_id)
    if product.deleted_at is not None:
        return jsonify({"ok": True, "id": product_id, "already_deleted": True})
    product.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.commit()
    log_event(product.id, get_current_username(), 'Product Deleted',
              'Product was deleted from the dashboard.', 'action')
    return jsonify({"ok": True, "id": product_id})


@api_bp.route('/api/products/bulk_delete', methods=['POST'])
@require_role('admin', 'marketing', 'director', api=True)
def api_bulk_delete_products():
    """Soft-delete a list of products by id. Body: {"ids": [1, 2, 3]}."""
    body = request.get_json(silent=True) or {}
    raw_ids = body.get('ids')
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"error": "ids must be a non-empty list"}), 400
    try:
        ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        return jsonify({"error": "ids must be integers"}), 400
    affected = Product.query.filter(
        Product.id.in_(ids),
        Product.deleted_at.is_(None),
    ).update({'deleted_at': datetime.now(timezone.utc).replace(tzinfo=None)}, synchronize_session=False)
    db.session.commit()
    return jsonify({"ok": True, "deleted_count": affected})


# ── PHASE 2.5: SPLIT VARIANTS / MERGE DRAFTS ─────────────────────────────

@api_bp.route('/api/product/<int:product_id>/split_variants', methods=['POST'])
@require_role('admin', 'marketing', 'director', api=True)
def api_split_variants(product_id):
    """Phase 2.5: explode a draft whose `pis_data['variants']` list has more
    than one entry into N independent draft Products.

    Use case: the AI clustered five wardrobes as variants of one model when
    they're actually distinct products. One click → five drafts. The
    original product is soft-deleted so reviewers don't see duplicates.
    """
    product = Product.query.get_or_404(product_id)
    pis = product.pis_data or {}
    variants = pis.get('variants') or []
    if not variants:
        return jsonify({"error": "This product has no variants to split."}), 400

    user_name = get_current_username()
    created_ids = []

    try:
        for v in variants:
            if not isinstance(v, dict):
                continue
            label = (v.get('label') or v.get('model_number') or 'Variant').strip() or 'Variant'
            new_pis = copy.deepcopy(pis)
            # Reset variants — each child is now a standalone product
            new_pis['variants'] = []
            # Override the header_info so the new draft is uniquely identifiable
            hi = new_pis.setdefault('header_info', {})
            hi['product_name'] = label
            if v.get('model_number'):
                hi['model_number'] = v['model_number']
            if v.get('price'):
                hi['price_estimate'] = v['price']
                # Re-parse currency for the new value
                from helpers import parse_price_currency, _normalize_mur_price
                pm = parse_price_currency(v['price'])
                new_pis['_price_meta'] = pm
                hi['price_estimate'] = _normalize_mur_price(v['price'], pm)

            # Drop the per-variant proforma block that no longer applies
            if isinstance(new_pis.get('source_facts'), dict):
                new_pis['source_facts'] = {**new_pis['source_facts']}
                new_pis['source_facts']['product_name'] = label
                if v.get('model_number'):
                    new_pis['source_facts']['model_number'] = v['model_number']

            new_product = Product(
                model_name=label,
                pis_data=new_pis,
                image_path=product.image_path,
                seo_keywords=(new_pis.get('seo_data') or {}).get('generated_keywords', ''),
                workflow_stage='marketing_draft',
            )
            db.session.add(new_product)
            db.session.commit()
            log_event(new_product.id, user_name, 'New Product Added',
                      f'Split out from product #{product.id} ({product.model_name}) variants.', 'neutral')
            save_version_snapshot(new_product, label='Initial version (split)', is_major=True)
            created_ids.append(new_product.id)

        # Soft-delete the original — its variants now live as separate drafts
        product.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.commit()
        log_event(product.id, user_name, 'Product Split',
                  f'Split into {len(created_ids)} separate drafts: {created_ids}.', 'action')

        return jsonify({"ok": True, "created_ids": created_ids,
                        "redirect_url": "/dashboard/marketing"})
    except Exception as e:
        db.session.rollback()
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@api_bp.route('/api/products/merge', methods=['POST'])
@require_role('admin', 'marketing', 'director', api=True)
def api_merge_products():
    """Phase 2.5: merge multiple drafts into a single base product whose
    `variants` list captures the others.

    Body: { "primary_id": int, "secondary_ids": [int, ...] }
        - primary_id    → kept as the base; its pis_data is preserved
        - secondary_ids → soft-deleted; their header_info/price get
                          appended to primary's `variants` list and
                          `pis_data['variants']` updated.
    Use case: AI created 3 separate drafts for "Black", "White", "Grey" of
    the same wardrobe. Reviewer ticks the 3 cards on the dashboard → Merge.
    """

    body = request.get_json(silent=True) or {}
    raw_primary = body.get('primary_id')
    if raw_primary is None:
        return jsonify({"error": "primary_id is required"}), 400
    try:
        primary_id = int(raw_primary)
        secondary_ids = [int(x) for x in (body.get('secondary_ids') or [])]
    except (TypeError, ValueError):
        return jsonify({"error": "primary_id and secondary_ids must be integers"}), 400
    if not secondary_ids:
        return jsonify({"error": "Pick at least 2 products to merge."}), 400
    if primary_id in secondary_ids:
        return jsonify({"error": "primary_id cannot also be in secondary_ids"}), 400

    primary = Product.query.get(primary_id)
    if not primary or primary.deleted_at:
        return jsonify({"error": "Primary product not found"}), 404
    secondaries = Product.query.filter(
        Product.id.in_(secondary_ids),
        Product.deleted_at.is_(None),
    ).all()
    if len(secondaries) != len(secondary_ids):
        return jsonify({"error": "Some secondary products not found / already deleted"}), 404

    user_name = get_current_username()
    try:
        primary_pis = copy.deepcopy(primary.pis_data or {})
        existing_variants = primary_pis.get('variants') or []
        if not isinstance(existing_variants, list):
            existing_variants = []

        for s in secondaries:
            sh = (s.pis_data or {}).get('header_info', {}) or {}
            existing_variants.append({
                'label': sh.get('product_name') or s.model_name,
                'model_number': sh.get('model_number') or '',
                'price': sh.get('price_estimate') or '',
            })
            s.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)

        primary_pis['variants'] = existing_variants
        primary.pis_data = primary_pis
        flag_modified(primary, 'pis_data')

        db.session.commit()
        log_event(primary.id, user_name, 'Products Merged',
                  f'Merged {len(secondaries)} drafts ({[s.id for s in secondaries]}) '
                  f'into this product as variants.', 'action')
        for s in secondaries:
            log_event(s.id, user_name, 'Product Merged',
                      f'Merged into product #{primary.id} ({primary.model_name}).', 'action')
        save_version_snapshot(primary, label=f'Merged {len(secondaries)} draft(s)', is_major=True)

        return jsonify({"ok": True,
                        "primary_id": primary.id,
                        "absorbed_ids": [s.id for s in secondaries],
                        "variant_count": len(existing_variants),
                        "redirect_url": f"/review/marketing/{primary.id}"})
    except Exception as e:
        db.session.rollback()
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@api_bp.route('/api/products/clear_active', methods=['POST'])
@require_role('admin', 'marketing', 'director', api=True)
def api_clear_active_products():
    """Soft-delete every currently active product. Used by the dashboard
    'Clear All' button. Returns the count cleared."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    affected = Product.query.filter(Product.deleted_at.is_(None)).update(
        {'deleted_at': now}, synchronize_session=False
    )
    db.session.commit()
    return jsonify({"ok": True, "deleted_count": affected})


@api_bp.route('/api/product/<int:product_id>/save_draft', methods=['POST'])
def api_save_draft(product_id):
    product = Product.query.get_or_404(product_id)
    data = request.get_json()
    if not data:
        return {"error": "No data provided"}, 400

    updated_pis_data  = product.pis_data  or {}
    updated_spec_data = product.spec_data or {}

    if 'product_name' in data:
        h_info = {
            'product_name':   data.get('product_name'),
            'model_number':   data.get('model_number'),
            'brand':          data.get('brand'),
            'price_estimate': data.get('price_estimate')
        }
        updated_pis_data['header_info']  = h_info
        updated_spec_data['header_info'] = h_info

    if 'range_overview' in data:
        desc = data.get('range_overview')
        updated_pis_data['range_overview']                    = desc
        updated_spec_data['customer_friendly_description']    = desc
        updated_spec_data['refined_description']              = desc

    if 'customer_friendly_description' in data:
        desc = data.get('customer_friendly_description')
        updated_spec_data['customer_friendly_description'] = desc
        updated_spec_data['refined_description']           = desc
        updated_pis_data['range_overview']                 = desc

    features = data.get('key_features') or data.get('sales_argument') or data.get('sales_arguments')
    if features is not None and isinstance(features, list):
        clean = [f.strip() for f in features if f.strip()]
        updated_pis_data['sales_arguments']   = clean
        updated_spec_data['key_features']     = clean

    tech_specs = data.get('technical_specifications')
    if tech_specs is not None and isinstance(tech_specs, dict):
        updated_pis_data['technical_specifications']  = tech_specs
        updated_spec_data['technical_specifications'] = tech_specs

    if 'warranty_period' in data:
        for d in (updated_pis_data, updated_spec_data):
            d.setdefault('warranty_service', {})
            d['warranty_service']['period']   = data.get('warranty_period')
            d['warranty_service']['coverage'] = data.get('warranty_coverage')

    if 'seo_meta_title' in data:
        updated_spec_data.setdefault('seo', {})
        updated_spec_data['seo']['meta_title']       = data.get('seo_meta_title')
        updated_spec_data['seo']['meta_description'] = data.get('seo_meta_description')
        updated_spec_data['seo']['keywords']         = data.get('seo_keywords') or data.get('seo_meta_keywords')

    if 'internal_web_keywords' in data:
        updated_spec_data['internal_web_keywords'] = data.get('internal_web_keywords')

    # Categories are written via set_product_category once the assignments
    # below are complete — keeps the canonical column + JSON mirror in sync
    # with whatever the inline editor / autosave just submitted.
    _pending_categories = None
    if 'category_1' in data:
        _pending_categories = (
            data.get('category_1'),
            data.get('category_2') or '',
            data.get('category_3') or '',
        )

    if 'director_general_comments' in data:
        comments = data.get('director_general_comments')
        if 'pending_director_pis' in product.workflow_stage or 'marketing_changes' in product.workflow_stage:
            product.director_pis_comments = comments
        elif 'pending_director_spec' in product.workflow_stage or 'web_changes' in product.workflow_stage:
            product.director_spec_comments = comments

    accepted = data.get('accepted_revisions')
    if accepted and isinstance(accepted, list) and product.revision_data:
        rev = dict(product.revision_data)
        key_map = {
            'header': 'header_info', 'overview': 'range_overview',
            'sales': 'sales_arguments', 'specs': 'technical_specifications',
            'warranty': 'warranty_service'
        }
        for section_key in accepted:
            rev.pop(key_map.get(section_key, section_key), None)
        product.revision_data = rev if rev else None
        flag_modified(product, 'revision_data')

    product.pis_data  = updated_pis_data
    product.spec_data = updated_spec_data
    flag_modified(product, 'pis_data')
    flag_modified(product, 'spec_data')

    # Apply category last so the helper sees the fresh spec_data/pis_data
    # state when it mirrors the canonical value into the JSON shapes.
    if _pending_categories is not None:
        set_product_category(product, *_pending_categories)

    db.session.commit()
    return {"status": "success"}


# ── STATUS POLLS ──────────────────────────────────────────────────────────────

@api_bp.route('/api/spec_status/<int:product_id>', methods=['GET'])
def api_spec_status(product_id):
    product = Product.query.get_or_404(product_id)
    sd = product.spec_data or {}
    is_generating = sd.get('_spec_generating', False)
    return json.dumps({'ready': not is_generating}), 200, {'Content-Type': 'application/json'}


@api_bp.route('/api/revision_status/<int:product_id>', methods=['GET'])
def api_revision_status(product_id):
    product = Product.query.get_or_404(product_id)
    rev = product.revision_data or {}
    statuses = {}
    all_ready = True
    for section, data in rev.items():
        status = data.get('status', 'pending')
        statuses[section] = status
        if status == 'generating':
            all_ready = False
    return json.dumps({'statuses': statuses, 'all_ready': all_ready}), 200, {'Content-Type': 'application/json'}


# ── CATEGORY / MAGENTO ────────────────────────────────────────────────────────

@api_bp.route('/api/magento_categories', methods=['GET'])
def api_magento_categories():
    try:
        from utils.magento_api import get_category_tree
        tree = get_category_tree()
        return json.dumps(tree), 200, {'Content-Type': 'application/json'}
    except Exception as e:
        return json.dumps({'error': str(e)}), 500, {'Content-Type': 'application/json'}


# ── FORBIDDEN WORDS ───────────────────────────────────────────────────────────
#
# The on-disk shape is normalized on every read so the API always returns
# objects of the form { word, replace_with, severity, reason?, added_by?,
# added_at? }. Legacy string entries written by older versions are upgraded
# transparently on first read.

def _fw_json(payload, status=200):
    """Compact JSON responder for the forbidden-words endpoints. Centralized
    so we keep one consistent content-type and don't drift."""
    return json.dumps(payload), status, {'Content-Type': 'application/json'}


def _fw_extract_payload(body):
    """Pull the editable entry fields out of a JSON body. Returns a dict
    suitable for handing straight to _normalize_word_entry (it ignores
    keys it doesn't recognize, so this is intentionally generous)."""
    return {
        'word':         (body.get('word') or '').strip().lower(),
        'replace_with': (body.get('replace_with') or '').strip(),
        'severity':     (body.get('severity') or 'block').strip().lower(),
        'reason':       (body.get('reason') or '').strip()[:120],
    }


@api_bp.route('/api/forbidden_words', methods=['GET'])
def api_get_forbidden_words():
    """Return the full forbidden-words map. Shape:
        { "<category>": [{ word, replace_with, severity, ... }, ...] }
    Plus the reserved "__global__" key for site-wide rules."""
    return _fw_json(load_forbidden_words())


@api_bp.route('/api/forbidden_words', methods=['POST'])
def api_add_forbidden_word():
    """Add (or upsert) a forbidden-word entry in a category. The category
    may be a regular leaf-category name or the reserved "__global__" key."""
    body = request.get_json(force=True) or {}
    category = (body.get('category') or '').strip()
    payload  = _fw_extract_payload(body)
    if not category or not payload['word']:
        return _fw_json({'error': 'Category and word required'}, 400)
    if payload['severity'] not in VALID_SEVERITIES:
        return _fw_json({'error': f'severity must be one of {list(VALID_SEVERITIES)}'}, 400)

    # Auto-stamp governance fields so the manager UI can show who added what.
    payload['added_by'] = get_current_username()
    payload['added_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')

    data = load_forbidden_words()
    bucket = list(data.get(category) or [])
    # Upsert: if the word already exists in this category, replace the entry;
    # otherwise append. Keeps governance fields fresh on re-add.
    replaced = False
    for i, existing in enumerate(bucket):
        if existing.get('word') == payload['word']:
            bucket[i] = payload
            replaced = True
            break
    if not replaced:
        bucket.append(payload)
    data[category] = bucket
    save_forbidden_words(data)
    # Re-load so the response reflects what's actually on disk after
    # normalization (consistent with what the GET endpoint would return).
    fresh = load_forbidden_words()
    return _fw_json({'ok': True, 'words': fresh.get(category, [])})


@api_bp.route('/api/forbidden_words', methods=['DELETE'])
def api_remove_forbidden_word():
    """Remove a single word from a category. If the category becomes empty
    it is dropped from the map so the file stays tidy."""
    body = request.get_json(force=True) or {}
    category = (body.get('category') or '').strip()
    word     = (body.get('word') or '').strip().lower()
    if not category or not word:
        return _fw_json({'error': 'Category and word required'}, 400)

    data = load_forbidden_words()
    bucket = [e for e in (data.get(category) or []) if e.get('word') != word]
    if bucket:
        data[category] = bucket
    elif category in data:
        del data[category]
    save_forbidden_words(data)
    fresh = load_forbidden_words()
    return _fw_json({'ok': True, 'words': fresh.get(category, [])})


# ── VERSION HISTORY ───────────────────────────────────────────────────────────

@api_bp.route('/api/product/<int:product_id>/versions')
def api_product_versions(product_id):
    versions = ProductVersion.query.filter_by(product_id=product_id).order_by(ProductVersion.version_num.desc()).all()
    result = [{
        "id": v.id, "version_num": v.version_num, "label": v.label,
        "workflow_stage": v.workflow_stage, "is_major": v.is_major,
        "created_by": v.created_by.display_name if v.created_by else "System",
        "created_at": v.created_at.strftime('%d %b %Y, %H:%M')
    } for v in versions]
    return jsonify(result)


@api_bp.route('/api/product/<int:product_id>/versions/<int:version_id>/restore', methods=['POST'])
def api_restore_version(product_id, version_id):
    product = Product.query.get_or_404(product_id)
    version = ProductVersion.query.get_or_404(version_id)
    if version.product_id != product_id:
        return jsonify({"error": "Version does not belong to this product"}), 400

    # Phase 3 — use the reconstruction helper so we can restore EVEN IF the
    # target version is a minor (diff-only) snapshot. The helper walks back
    # to the nearest major and applies forward diffs.
    from utils.version_reconstruction import reconstruct_version_data
    reconstructed = reconstruct_version_data(product_id, version.version_num)
    if reconstructed is None:
        return jsonify({"error": "Could not reconstruct version data"}), 500

    # Capture a fresh anchor of the CURRENT state so the user can undo.
    pre_restore_version = save_version_snapshot(
        product,
        label=f"Before rolling back to version {version.version_num}",
        is_major=True,
    )

    product.pis_data      = copy.deepcopy(reconstructed.get('pis_data'))
    product.spec_data     = copy.deepcopy(reconstructed.get('spec_data'))
    product.revision_data = copy.deepcopy(reconstructed.get('revision_data'))
    product.workflow_stage = reconstructed.get('workflow_stage') or product.workflow_stage
    db.session.commit()

    log_event(
        product.id, get_current_username(),
        'Rolled Back to Previous Version',
        f'The product was rolled back to version {version.version_num} ({version.label}).',
        'action',
        version_id=pre_restore_version.id if pre_restore_version else None,
    )
    return jsonify({"ok": True, "message": f"Restored to version {version.version_num}"})


# ── PHASE 3: read-only preview at a past version ─────────────────────────────

def _build_phase_diff(before_data, after_data, after_stage=None):
    """Phase-aware field-by-field diff between two product snapshots.

    `before_data` and `after_data` are dicts shaped like
    `{'pis_data': {...}, 'spec_data': {...}, ...}` — the same shape
    returned by `reconstruct_version_data` and exposed on `Product`.
    `after_stage` is the workflow_stage of the "after" snapshot and
    chooses the PIS vs SpecSheet whitelist; unknown stages fall through
    to PIS so we never silently hide every field.

    Returns `(fields, phase, changed_count)` where each entry in
    `fields` is `{path, field_name, section, current_value,
    target_value, changed}` in editor reading order. Both endpoints
    (`/compare` and `/changes_at`) share this so the version-restore
    modal and the history-event popup always agree on which fields
    matter, how they're labelled, and what order they appear in.
    """
    from helpers import _clean_field_name, _format_value, _get_field_section

    fields: list[dict] = []
    seen_paths: set[str] = set()

    def _push(path, cur_val, tgt_val):
        if path in seen_paths:
            return
        seen_paths.add(path)
        last_seg = path.split('.')[-1]
        if last_seg.startswith('_'):
            return
        name = _clean_field_name(path)
        section = _get_field_section(name)
        cur_str = _format_value(cur_val)
        tgt_str = _format_value(tgt_val)
        if not cur_str and not tgt_str:
            return
        fields.append({
            'path': path,
            'field_name': name,
            'section': section,
            'current_value': cur_str,
            'target_value':  tgt_str,
            'changed': (cur_str or '') != (tgt_str or ''),
        })

    def _walk(prefix, cur, tgt):
        if isinstance(cur, dict) or isinstance(tgt, dict):
            cur_d = cur if isinstance(cur, dict) else {}
            tgt_d = tgt if isinstance(tgt, dict) else {}
            for key in sorted(set(list(cur_d.keys()) + list(tgt_d.keys()))):
                path = f"{prefix}.{key}" if prefix else key
                _walk(path, cur_d.get(key), tgt_d.get(key))
            return
        _push(prefix, cur, tgt)

    _walk('pis_data',  (before_data or {}).get('pis_data')  or {}, (after_data or {}).get('pis_data')  or {})
    _walk('spec_data', (before_data or {}).get('spec_data') or {}, (after_data or {}).get('spec_data') or {})

    SPEC_PHASE_STAGES = {
        'ready_for_web', 'specsheet_draft', 'pending_director_spec',
        'web_changes_requested', 'finalized',
    }
    PIS_ALLOW_EXACT = {
        'pis_data.header_info.product_name',
        'pis_data.header_info.model_number',
        'pis_data.header_info.brand',
        'pis_data.header_info.price_estimate',
        'pis_data.range_overview',
        'pis_data.sales_arguments',
    }
    PIS_ALLOW_PREFIX = (
        'pis_data.technical_specifications.',
        'pis_data.warranty_service.',
        'pis_data.warranty.',
    )
    SPEC_ALLOW_EXACT = {
        'spec_data.header_info.product_name',
        'spec_data.header_info.model_number',
        'spec_data.header_info.brand',
        'spec_data.header_info.price_estimate',
        'spec_data.customer_friendly_description',
        'spec_data.key_features',
        'spec_data.seo.meta_title',
        'spec_data.seo.meta_description',
        'spec_data.seo.keywords',
        'spec_data.seo_data.meta_title',
        'spec_data.seo_data.meta_description',
        'spec_data.seo_data.generated_keywords',
        'spec_data.categories.category_1',
        'spec_data.categories.category_2',
        'spec_data.categories.category_3',
    }
    SPEC_ALLOW_PREFIX = (
        'spec_data.technical_specifications.',
        'spec_data.warranty_service.',
        'spec_data.warranty.',
    )

    if after_stage in SPEC_PHASE_STAGES:
        allow_exact, allow_prefix = SPEC_ALLOW_EXACT, SPEC_ALLOW_PREFIX
        phase = 'specsheet'
    else:
        allow_exact, allow_prefix = PIS_ALLOW_EXACT, PIS_ALLOW_PREFIX
        phase = 'pis'

    def _is_visible(path: str) -> bool:
        if path in allow_exact:
            return True
        return any(path.startswith(p) for p in allow_prefix)

    fields = [f for f in fields if _is_visible(f['path'])]

    deduped: list[dict] = []
    by_label: dict[tuple[str, str], int] = {}
    for f in fields:
        key = (f['field_name'], f['section'])
        if key not in by_label:
            by_label[key] = len(deduped)
            deduped.append(f)
        else:
            existing_idx = by_label[key]
            existing = deduped[existing_idx]
            if f['path'].startswith('spec_data') and existing['path'].startswith('pis_data'):
                deduped[existing_idx] = f

    PIS_SECTION_BUCKET = {
        'Header': 0, 'Description': 1, 'Sales': 2,
        'Specs': 3, 'Warranty': 4,
    }
    SPEC_SECTION_BUCKET = {
        'Header': 0, 'Description': 1, 'Key Features': 2,
        'SEO': 3, 'Classification': 4,
        'Specs': 5, 'Warranty': 6,
    }
    EXPLICIT_NAME_ORDER = [
        'Product Name', 'Model Number', 'Brand', 'Price Estimate',
        'Description',
        'Short Description', 'Refined Description',
        'Key Selling Points', 'Key Features',
        'Meta Title', 'Meta Description', 'SEO Keywords', 'Web Keywords',
        'Category A', 'Category B', 'Category C',
        'Warranty Period', 'Warranty Coverage',
    ]
    section_bucket = SPEC_SECTION_BUCKET if phase == 'specsheet' else PIS_SECTION_BUCKET

    def _sort_key(f):
        bucket = section_bucket.get(f['section'], 99)
        if f['field_name'] in EXPLICIT_NAME_ORDER:
            sub_priority = (0, EXPLICIT_NAME_ORDER.index(f['field_name']))
        else:
            sub_priority = (1, f['field_name'].lower())
        return (bucket, sub_priority)

    deduped.sort(key=_sort_key)
    changed_count = sum(1 for f in deduped if f.get('changed'))
    return deduped, phase, changed_count


@api_bp.route('/api/product/<int:product_id>/versions/<int:version_id>/compare')
def api_compare_version(product_id, version_id):
    """Diff CURRENT product data against a past version. Returns a flat
    list of fields that differ, each with human-readable name, section,
    current value, and target-version value — ready for a comparison
    table UI. Empty `fields` array means the version is identical to
    the current state (nothing would change on restore)."""
    product = Product.query.get_or_404(product_id)
    version = ProductVersion.query.get_or_404(version_id)
    if version.product_id != product_id:
        return jsonify({"error": "Version does not belong to this product"}), 400

    from utils.version_reconstruction import reconstruct_version_data
    target = reconstruct_version_data(product_id, version.version_num)
    if target is None:
        return jsonify({"error": "Could not reconstruct version data"}), 500

    # The compare modal reads as "live → past version" — left column is
    # the user's CURRENT live state, right column is what they'd land on
    # if they restored. The helper outputs current_value/target_value
    # keys that map directly to those columns.
    before_data = {'pis_data': product.pis_data, 'spec_data': product.spec_data}
    deduped, phase, changed_count = _build_phase_diff(
        before_data, target, after_stage=target.get('workflow_stage')
    )

    return jsonify({
        'version_num': version.version_num,
        'label':       version.label,
        'is_major':    version.is_major,
        'workflow_stage': target.get('workflow_stage'),
        'phase':       phase,
        'changed_count': changed_count,
        'created_at':  version.created_at.strftime('%d %b %Y, %H:%M') if version.created_at else None,
        'created_by':  version.created_by.display_name if version.created_by else 'System',
        'fields':      deduped,
    })


@api_bp.route('/api/product/<int:product_id>/versions/<int:version_id>/preview')
def api_preview_version(product_id, version_id):
    """View the product state at a past version without applying it.
    Reconstructs from the nearest major snapshot + forward-applied diffs.
    Returns pis_data + spec_data the UI can render in a read-only view."""
    product = Product.query.get_or_404(product_id)
    version = ProductVersion.query.get_or_404(version_id)
    if version.product_id != product_id:
        return jsonify({"error": "Version does not belong to this product"}), 400

    from utils.version_reconstruction import reconstruct_version_data
    data = reconstruct_version_data(product_id, version.version_num)
    if data is None:
        return jsonify({"error": "Could not reconstruct version data"}), 500

    return jsonify({
        "version_num": version.version_num,
        "label": version.label,
        "workflow_stage": data.get('workflow_stage'),
        "pis_data": data.get('pis_data'),
        "spec_data": data.get('spec_data'),
        "revision_data": data.get('revision_data'),
        "created_at": version.created_at.strftime('%d %b %Y, %H:%M') if version.created_at else None,
        "created_by": version.created_by.display_name if version.created_by else 'System',
        "is_major": version.is_major,
    })


# ── PHASE 5: TIMELINE (grouped by date, with stage + role + snapshot flag) ──

# Maps internal workflow_stage values onto the 5 high-level "swim lanes"
# the timeline UI groups by. Any unknown value falls into 'other'.
_STAGE_SWIM_LANE = {
    None: 'proforma',
    '': 'proforma',
    'marketing_draft': 'marketing',
    'marketing_in_progress': 'marketing',
    'marketing_changes_requested': 'marketing',
    'pending_director_pis': 'director_pis',
    'ready_for_web': 'web',
    'specsheet_draft': 'web',
    'web_changes_requested': 'web',
    'pending_director_spec': 'director_spec',
    'finalized': 'finalized',
}


def _swim_lane(stage):
    if stage in _STAGE_SWIM_LANE:
        return _STAGE_SWIM_LANE[stage]
    return 'other'


@api_bp.route('/api/product/<int:product_id>/timeline')
def api_product_timeline(product_id):
    """Phase 5 — restructured history endpoint.

    Returns events for one product, grouped by calendar date, each event
    enriched with workflow_stage / swim_lane / actor_role and flags
    indicating whether a snapshot + field changes exist at that point.

    Query params:
      ?stage=marketing | director_pis | web | director_spec | finalized | proforma
            Filter to one swim lane.
      ?from=YYYY-MM-DD  ?to=YYYY-MM-DD
            Inclusive date range.
      ?page=1  ?per_page=50
            Pagination. Pagination is applied AFTER grouping so each
            page is a complete set of date buckets.

    Response shape:
      {
        "product_id": int,
        "page": int, "per_page": int, "total_events": int,
        "groups": [
          { "date": "2026-05-11",
            "events": [ {id, time, action, description, type, stage,
                         swim_lane, actor, actor_role, version_id,
                         version_num, has_field_changes, field_change_count
                        }, ... ] },
          ...
        ]
      }
    """
    # 1. Pull events (apply DB-level filters first)
    q = ProductHistory.query.filter_by(product_id=product_id)

    swim_filter = (request.args.get('stage') or '').strip().lower()
    if swim_filter:
        # Reverse-map the swim_lane to the underlying workflow_stage values.
        matching_stages = [k for k, v in _STAGE_SWIM_LANE.items() if v == swim_filter]
        if matching_stages:
            q = q.filter(ProductHistory.workflow_stage.in_(matching_stages))

    def _parse_date(s):
        try:
            return datetime.strptime(s, '%Y-%m-%d')
        except (TypeError, ValueError):
            return None

    d_from = _parse_date(request.args.get('from'))
    d_to = _parse_date(request.args.get('to'))
    if d_from:
        q = q.filter(ProductHistory.timestamp >= d_from)
    if d_to:
        q = q.filter(ProductHistory.timestamp < d_to + timedelta(days=1))

    events = q.order_by(ProductHistory.timestamp.desc()).all()

    # 2. Pre-fetch field-change counts per version so the UI can show
    #    "5 fields changed" badges without per-event subqueries.
    version_ids = {e.version_id for e in events if e.version_id}
    field_change_counts: dict[int, int] = {}
    if version_ids:
        rows = db.session.query(
            ProductVersion.id,
            db.func.count(FieldChangeLog.id),
        ).join(
            FieldChangeLog,
            FieldChangeLog.version_num == ProductVersion.version_num,
        ).filter(
            ProductVersion.id.in_(version_ids),
            FieldChangeLog.product_id == product_id,
        ).group_by(ProductVersion.id).all()
        field_change_counts = {vid: cnt for vid, cnt in rows}

    # Pre-fetch version_num for each linked version so UI can render
    # "v12" labels without N round-trips.
    version_nums: dict[int, int] = {}
    if version_ids:
        for v in ProductVersion.query.filter(ProductVersion.id.in_(version_ids)).all():
            version_nums[v.id] = v.version_num

    # 3. Group events by calendar date.
    groups: list[dict] = []
    bucket: dict | None = None
    for e in events:
        date_str = e.timestamp.strftime('%Y-%m-%d')
        if bucket is None or bucket['date'] != date_str:
            bucket = {'date': date_str, 'events': []}
            groups.append(bucket)
        bucket['events'].append({
            'id':                  e.id,
            'time':                e.timestamp.strftime('%H:%M'),
            'timestamp':           e.timestamp.isoformat(),
            'action':              e.action_title,
            'description':         e.description or '',
            'type':                e.action_type or 'neutral',
            'stage':               e.workflow_stage,
            'swim_lane':           _swim_lane(e.workflow_stage),
            'actor':               e.actor,
            'actor_role':          e.actor_role,
            'version_id':          e.version_id,
            'version_num':         version_nums.get(e.version_id),
            'has_snapshot':        e.version_id is not None,
            'has_field_changes':   field_change_counts.get(e.version_id, 0) > 0,
            'field_change_count':  field_change_counts.get(e.version_id, 0),
        })

    # 4. Pagination — pages are full date buckets (so a single date never
    #    splits across pages).
    try:
        page = max(1, int(request.args.get('page', 1)))
        per_page = max(1, min(200, int(request.args.get('per_page', 50))))
    except (TypeError, ValueError):
        page, per_page = 1, 50

    # Flatten -> count events for `total_events`, then slice by date bucket.
    total_events = sum(len(g['events']) for g in groups)
    start = (page - 1) * per_page
    end = start + per_page

    paginated_groups: list[dict] = []
    flat_index = 0
    for g in groups:
        bucket_start = flat_index
        bucket_end = flat_index + len(g['events'])
        flat_index = bucket_end
        if bucket_end <= start or bucket_start >= end:
            continue
        sliced = g['events'][max(0, start - bucket_start): max(0, end - bucket_start)]
        if sliced:
            paginated_groups.append({'date': g['date'], 'events': sliced})

    # 5. Summary bar — created/updated timestamps, total versions, current stage.
    product = Product.query.get(product_id)
    summary = None
    if product:
        latest_version = ProductVersion.query.filter_by(
            product_id=product_id
        ).order_by(ProductVersion.version_num.desc()).first()
        version_count = ProductVersion.query.filter_by(product_id=product_id).count()
        summary = {
            'created_at':  product.created_at.strftime('%d %b %Y') if product.created_at else None,
            'last_event':  events[0].timestamp.strftime('%d %b %Y, %H:%M') if events else None,
            'version_count': version_count,
            'current_stage': product.workflow_stage,
            'current_swim_lane': _swim_lane(product.workflow_stage),
            'latest_version_num': latest_version.version_num if latest_version else None,
        }

    return jsonify({
        'product_id':   product_id,
        'page':         page,
        'per_page':     per_page,
        'total_events': total_events,
        'summary':      summary,
        'groups':       paginated_groups,
    })


# ── FIELD CHANGELOG ───────────────────────────────────────────────────────────

@api_bp.route('/api/product/<int:product_id>/changelog')
def api_product_changelog(product_id):
    changes = FieldChangeLog.query.filter_by(product_id=product_id).order_by(FieldChangeLog.timestamp.desc()).limit(100).all()
    result = [{
        "id": c.id,
        # Translate the raw dotted path (e.g. `pis_data.range_overview`) into
        # the same user-facing label shown in the marketing/web editors
        # (e.g. "Description"). Falls back to the raw key for fields that
        # aren't mapped in FIELD_LABELS.
        "field_name": _clean_field_name(c.field_name) if c.field_name else c.field_name,
        "section": _get_field_section(_clean_field_name(c.field_name)) if c.field_name else 'Other',
        "old_value": c.old_value, "new_value": c.new_value,
        "version_num": c.version_num,
        "user": c.user.display_name if c.user else "System",
        "timestamp": c.timestamp.strftime('%d %b %Y, %H:%M')
    } for c in changes]
    return jsonify(result)


@api_bp.route('/api/product/<int:product_id>/changes_at')
def api_product_changes_at(product_id):
    """Full editor surface at a past event in time.

    Pinpoints the ProductVersion that captured the event (via the
    FieldChangeLog rows logged within a 2-minute window of the
    requested timestamp), then reconstructs THAT version as the
    "after" state and `version_num - 1` as the "before". Walks both
    via `_build_phase_diff` so the popup shows the same full editor
    surface — every Header / Description / Sales (or Short Desc / Key
    Features / SEO / Classification) / Spec / Warranty field — that
    the version-restore modal shows, with changed values highlighted
    and unchanged values rendered as plain context.
    """
    ts_str = request.args.get('date', '')
    tm_str = request.args.get('time', '')
    if not ts_str or not tm_str:
        return jsonify({'fields': [], 'changed_count': 0}), 400
    try:
        target_dt = datetime.strptime(f"{ts_str} {tm_str}", '%Y-%m-%d %H:%M')
    except ValueError:
        return jsonify({'fields': [], 'changed_count': 0}), 400

    window = timedelta(seconds=120)
    changes = FieldChangeLog.query.filter(
        FieldChangeLog.product_id == product_id,
        FieldChangeLog.timestamp >= target_dt - window,
        FieldChangeLog.timestamp <= target_dt + window
    ).order_by(FieldChangeLog.timestamp.asc()).all()
    if not changes:
        return jsonify({'fields': [], 'changed_count': 0, 'version_num': None})

    # All rows in this window belong to the same workflow action and
    # therefore the same version_num. Take the first non-null one.
    version_num = next((c.version_num for c in changes if c.version_num), None)
    if not version_num:
        return jsonify({'fields': [], 'changed_count': 0, 'version_num': None})

    from utils.version_reconstruction import reconstruct_version_data
    after = reconstruct_version_data(product_id, version_num)
    if after is None:
        return jsonify({'fields': [], 'changed_count': 0, 'version_num': version_num})

    # `before` = the state immediately prior to this event. For v1 there
    # is no predecessor, so we treat "before" as an empty product.
    before = (reconstruct_version_data(product_id, version_num - 1)
              if version_num > 1 else None) or {'pis_data': {}, 'spec_data': {}}

    fields, phase, changed_count = _build_phase_diff(
        before, after, after_stage=after.get('workflow_stage')
    )

    # The history popup's UI uses `old_value` / `new_value`; the
    # compare popup uses `current_value` / `target_value`. Same data,
    # different historical naming — translate at the boundary so
    # neither UI has to change for the other.
    rows = [{
        'field_name':   f['field_name'],
        'section':      f['section'],
        'old_value':    f['current_value'],
        'new_value':    f['target_value'],
        'changed':      f['changed'],
        'version_num':  version_num,
    } for f in fields]

    return jsonify({
        'fields':        rows,
        'changed_count': changed_count,
        'phase':         phase,
        'version_num':   version_num,
        'user':          changes[0].user.display_name if changes[0].user else 'System',
        'timestamp':     changes[0].timestamp.strftime('%d %b %Y, %H:%M'),
    })


# ── IMAGE HEALTH CHECK ────────────────────────────────────────────────────────

@api_bp.route('/api/images/cleanup', methods=['GET'])
def api_cleanup_images():
    products = Product.query.filter(Product.deleted_at.is_(None)).all()
    fixed = []
    for p in products:
        changed = False
        if p.image_path and not p.image_path.startswith('http'):
            full_path = os.path.join('static', p.image_path)
            if not os.path.exists(full_path) or os.path.getsize(full_path) < 500:
                fixed.append({'id': p.id, 'model': p.model_name, 'type': 'main_image',
                               'broken_path': p.image_path,
                               'reason': 'file_missing' if not os.path.exists(full_path) else 'file_corrupt'})
                p.image_path = None
                changed = True
        if p.additional_images:
            clean_imgs = []
            for img in p.additional_images:
                if img.startswith('http'):
                    clean_imgs.append(img)
                    continue
                full_path = os.path.join('static', img)
                if os.path.exists(full_path) and os.path.getsize(full_path) >= 500:
                    clean_imgs.append(img)
                else:
                    fixed.append({'id': p.id, 'model': p.model_name, 'type': 'additional_image',
                                   'broken_path': img,
                                   'reason': 'file_missing' if not os.path.exists(full_path) else 'file_corrupt'})
            if len(clean_imgs) != len(p.additional_images):
                p.additional_images = clean_imgs
                flag_modified(p, 'additional_images')
                changed = True
        if not p.image_path and p.additional_images:
            p.image_path = p.additional_images.pop(0)
            flag_modified(p, 'additional_images')
            changed = True
    if fixed:
        db.session.commit()
    return {'status': 'success', 'total_products': len(products),
            'broken_paths_fixed': len(fixed), 'details': fixed}


# ── PRIVATE HELPERS ───────────────────────────────────────────────────────────

def _build_query(ai_data, fallback):
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
    words, seen = [], set()
    for w in ' '.join(q_parts).split():
        if w.lower() not in seen:
            words.append(w); seen.add(w.lower())
    return ' '.join(words) if words else fallback


def _build_bulk_query(brand, prod_name, model_id, fallback):
    q_parts = []
    if brand:     q_parts.append(brand)
    if prod_name: q_parts.append(prod_name)
    is_real = model_id and (any(c.isalpha() for c in model_id) or '-' in model_id)
    if is_real and model_id not in (prod_name or ''):
        q_parts.append(model_id)
    words, seen = [], set()
    for w in ' '.join(q_parts).split():
        if w.lower() not in seen:
            words.append(w); seen.add(w.lower())
    return ' '.join(words) if words else fallback


def _web_search_image(query, supplier_url, name, upload_folder, job_id):
    _update_job(job_id, progress=60, message='Searching Google Images...')
    public_url = find_and_validate_image(query, supplier_url)
    if public_url:
        _update_job(job_id, progress=70, message='Downloading Image...')
        return download_web_image(public_url, name, upload_folder)
    return None


# ══════════════════════════════════════════════════════════════════════════
# Phase 2 — Unified Proforma Import (extract → preview → commit/rework)
# ══════════════════════════════════════════════════════════════════════════

def _display_name_for(product_obj: dict, idx: int) -> str:
    src = product_obj.get('source_facts') or {}
    return (
        src.get('product_name')
        or src.get('model_number')
        or f"Item_{idx+1}"
    )


def _resolve_image_for_product(product_obj, ai_filepath, supplier_url, upload_folder):
    """Best-effort image resolution mirroring the legacy bulk worker:
    PDF scan → AI-found URL → Google Images → DuckDuckGo fallback.
    Mutates nothing; returns the static-relative path or None.
    """
    src      = product_obj.get('source_facts') or {}
    ai_block = product_obj.get('ai_enriched_details') or {}
    brand    = src.get('brand', '')
    p_name   = src.get('product_name', '')
    model_id = src.get('model_number', '')
    display_name = p_name or model_id or 'Product'
    query = _build_bulk_query(brand, p_name, model_id, display_name)

    extracted = None
    if ai_filepath:
        pdf_term = model_id or display_name
        try:
            extracted = extract_specific_image(ai_filepath, pdf_term, upload_folder)
        except Exception:
            extracted = None

    if not extracted:
        ai_url = ai_block.get('found_image_url')
        if ai_url and str(ai_url).startswith('http'):
            extracted = download_web_image(ai_url, display_name, upload_folder)

    if not extracted:
        public_url = find_and_validate_image(query, supplier_url)
        if public_url:
            extracted = download_web_image(public_url, display_name, upload_folder)

    if not extracted:
        simple_url = find_image_simple(query, supplier_url)
        if simple_url:
            extracted = download_web_image(simple_url, display_name, upload_folder)

    # Phase 2.2: last-resort fallback for "no-image" proformas — open the
    # supplier/Google result page in a headless browser, screenshot it, and
    # let the AI crop the product photo out (bypasses anti-hotlink blocks).
    # Phase 2.3: pass `brand` so the SERP scraper can lock onto the official
    # brand domain when available.
    if not extracted:
        extracted = find_image_via_screenshot(
            display_name, supplier_url, upload_folder, brand=brand
        )

    if extracted:
        extracted = store_image(extracted, display_name)
    return extracted


def _proforma_extract_worker(app, job_id, ai_filepaths, supplier_url,
                             extraction_mode, contains_images, brand_hint,
                             feedback=None, prior_products=None):
    """Background worker that runs AI extraction + image search and parks
    the result in Job.payload with status='preview_ready'. Used for both the
    initial extract and the rework flow (when feedback is provided)."""
    from utils.ai_generation import generate_proforma_data
    from utils.api_metering import job_scope
    with app.app_context(), job_scope(job_id):
        try:
            upload_folder = app.config['UPLOAD_FOLDER']
            _update_job(job_id, status='processing', progress=10,
                        message='Reading proforma...')

            site_data = {"text": "", "html": ""}
            if supplier_url:
                _update_job(job_id, progress=20, message='Scraping supplier URL...')
                site_data = scrape_url_data(supplier_url)

            stage_msg = 'Re-extracting with feedback...' if feedback else 'Extracting products with AI...'
            _update_job(job_id, progress=35, message=stage_msg)

            products = generate_proforma_data(
                file_paths=ai_filepaths,
                url_data=site_data,
                extraction_mode=extraction_mode,
                brand_hint=brand_hint,
                prior_data=prior_products,
                feedback=feedback,
            )

            if not products:
                _update_job(job_id, status='failed', progress=100,
                            message='No products detected in document.',
                            error='AI returned no products.',
                            completed_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())
                return

            # Phase 2.4: capture raw document text once and stash it in the
            # payload — committed-later products run through proforma_to_pis_data
            # with this same text so origins (verified vs discrepancy) are
            # consistent regardless of when commit happens.
            # Strict-fact rule: only the uploaded Proforma counts as the
            # verification source — supplier-page scrapes don't qualify as
            # Proforma facts.
            raw_doc_text = extract_raw_text_from_files(ai_filepaths) or ""

            ai_filepath = ai_filepaths[0] if ai_filepaths else None
            total = len(products)
            for idx, p_obj in enumerate(products):
                disp = _display_name_for(p_obj, idx)
                pct = 35 + int(((idx + 1) / total) * 55)
                _update_job(job_id, progress=pct,
                            message=f'Finding image {idx+1}/{total}: {disp}')
                try:
                    img_path = _resolve_image_for_product(
                        p_obj, ai_filepath, supplier_url, upload_folder
                    )
                except Exception as e:
                    print(f'[proforma extract] image error for {disp}: {e}')
                    img_path = None
                p_obj['_image_path'] = img_path
                p_obj['_display_name'] = disp

            payload = {
                'type': 'proforma_preview',
                'ai_filepaths': ai_filepaths,
                'supplier_url': supplier_url,
                'extraction_mode': extraction_mode,
                'contains_images': contains_images,
                'brand_hint': brand_hint,
                'products': products,
                'raw_doc_text': raw_doc_text,
            }
            _update_job(
                job_id,
                status='preview_ready', progress=100,
                message=f'{total} product{"s" if total != 1 else ""} ready for review.',
                payload=payload,
                redirect_url=None,
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            )
        except Exception as e:
            import traceback; traceback.print_exc()
            _update_job(job_id, status='failed', progress=100,
                        message=f'Extraction failed: {str(e)[:120]}',
                        error=str(e),
                        completed_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())


@api_bp.route('/api/proforma/extract', methods=['POST'])
def api_proforma_extract():
    """Kick off proforma extraction. Returns a job_id; poll /api/pis/jobs
    until status='preview_ready', then GET /api/proforma/preview/<job_id>."""
    extraction_mode = (request.form.get('extraction_mode') or 'auto').strip().lower()
    if extraction_mode not in ('auto', 'single', 'multiple'):
        extraction_mode = 'auto'
    supplier_url    = request.form.get('supplier_url', '').strip()
    brand_hint      = request.form.get('brand_hint', '').strip() or None
    contains_images = request.form.get('contains_images') == 'on'
    ai_files        = request.files.getlist('ai_document')

    if not ai_files and not supplier_url:
        return jsonify({"error": "Please provide a document or a supplier URL."}), 400

    active_count = Job.query.filter(Job.status.in_(('queued', 'processing'))).count()
    if active_count >= 5:
        return jsonify({"error": "Maximum 5 concurrent jobs. Please wait."}), 429

    ai_filepaths = []
    for ai_file in ai_files:
        if ai_file and ai_file.filename:
            filename = secure_filename(ai_file.filename)
            filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
            ai_file.save(filepath)
            ai_filepaths.append(filepath)

    job_id = str(uuid.uuid4())[:8]
    label_doc = ', '.join(os.path.basename(f) for f in ai_filepaths[:2]) or 'Proforma Import'
    _app = current_app._get_current_object()  # type: ignore[attr-defined]

    db.session.add(Job(
        id=job_id, model_name=f"Proforma: {label_doc}",
        status='queued', progress=0,
        message='Queued — waiting for slot...',
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    ))
    db.session.commit()

    pis_executor.submit(
        _proforma_extract_worker, _app, job_id, ai_filepaths, supplier_url,
        extraction_mode, contains_images, brand_hint
    )
    return jsonify({"ok": True, "job_id": job_id}), 202


@api_bp.route('/api/proforma/preview/<job_id>', methods=['GET'])
def api_proforma_preview(job_id):
    job = db.session.get(Job, job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job.status != 'preview_ready':
        return jsonify({"status": job.status, "message": job.message,
                        "progress": job.progress}), 202

    payload = job.payload or {}
    products = payload.get('products') or []
    preview = []
    for idx, p in enumerate(products):
        src = p.get('source_facts') or {}
        ai  = p.get('ai_enriched_details') or {}
        preview.append({
            'index': idx,
            'display_name':  p.get('_display_name') or _display_name_for(p, idx),
            'image_path':    p.get('_image_path'),
            'product_name':  src.get('product_name') or '',
            'brand':         src.get('brand') or '',
            'model_number':  src.get('model_number') or '',
            'price_estimate':src.get('price_estimate') or '',
            'summary':       (ai.get('range_overview') or '')[:240],
            'variants':      p.get('variants') or [],
            'has_variants':  bool(p.get('variants')),
            'notes':         ai.get('notes') or '',
        })
    return jsonify({
        "status": "preview_ready",
        "job_id": job_id,
        "extraction_mode": payload.get('extraction_mode'),
        "products": preview,
    })


@api_bp.route('/api/proforma/commit/<job_id>', methods=['POST'])
def api_proforma_commit(job_id):
    """Persist the staged products into Product rows."""
    job = db.session.get(Job, job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job.status != 'preview_ready' or not job.payload:
        return jsonify({"error": "Job is not ready for commit"}), 400

    body = request.get_json(silent=True) or {}
    accepted = body.get('accepted_indices')   # optional: subset of indices to commit

    payload = dict(job.payload)
    products = payload.get('products') or []
    if accepted is not None:
        try:
            accepted_set = {int(i) for i in accepted}
            products = [p for i, p in enumerate(products) if i in accepted_set]
        except (TypeError, ValueError):
            return jsonify({"error": "accepted_indices must be a list of integers"}), 400

    if not products:
        return jsonify({"error": "No products selected to commit"}), 400

    raw_doc_text = payload.get('raw_doc_text') or ""
    src_files    = payload.get('ai_filepaths') or []
    user_name = get_current_username()
    created_ids = []
    try:
        for idx, p_obj in enumerate(products):
            display_name = p_obj.get('_display_name') or _display_name_for(p_obj, idx)
            pis_data = proforma_to_pis_data(p_obj, raw_text=raw_doc_text,
                                            source_files=src_files)
            new_product = Product(
                model_name=display_name,
                pis_data=pis_data,
                image_path=p_obj.get('_image_path'),
                seo_keywords=(p_obj.get('ai_enriched_details') or {}).get('seo_data', {}).get('generated_keywords', ''),
                workflow_stage='marketing_draft',
            )
            db.session.add(new_product)
            db.session.commit()
            log_event(
                new_product.id, user_name, 'New Product Added',
                'Imported through the Proforma Review workflow.',
                'neutral'
            )
            save_version_snapshot(new_product, label='Initial version', is_major=True)
            created_ids.append(new_product.id)

        # Mark job as fully completed; if a single product, redirect to its review page.
        if len(created_ids) == 1:
            redirect_url = f'/review/marketing/{created_ids[0]}'
        else:
            redirect_url = '/dashboard/marketing'
        job.status = 'completed'
        job.message = f'Imported {len(created_ids)} product(s).'
        job.redirect_url = redirect_url
        job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.commit()
        clear_pdf_cache()
        return jsonify({"ok": True, "created_ids": created_ids,
                        "redirect_url": redirect_url})
    except Exception as e:
        db.session.rollback()
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@api_bp.route('/api/proforma/rework/<job_id>', methods=['POST'])
def api_proforma_rework(job_id):
    """Re-run AI extraction with reviewer feedback. The previous staging
    payload (uploaded files, URL, mode) is reused so the AI keeps context.
    """
    job = db.session.get(Job, job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if not job.payload:
        return jsonify({"error": "No staged payload to rework"}), 400

    body = request.get_json(silent=True) or {}
    feedback = (body.get('feedback') or '').strip()
    if not feedback:
        return jsonify({"error": "Feedback text is required"}), 400

    payload = dict(job.payload)

    job.status = 'queued'
    job.progress = 0
    job.message = 'Reworking with feedback...'
    job.completed_at = None
    db.session.commit()

    _app = current_app._get_current_object()  # type: ignore[attr-defined]
    pis_executor.submit(
        _proforma_extract_worker, _app, job_id,
        payload.get('ai_filepaths') or [],
        payload.get('supplier_url') or '',
        payload.get('extraction_mode') or 'auto',
        payload.get('contains_images', False),
        payload.get('brand_hint'),
        feedback,
        payload.get('products') or [],
    )
    return jsonify({"ok": True, "job_id": job_id}), 202
