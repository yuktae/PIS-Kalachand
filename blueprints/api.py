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

from model import db, Product, ProductVersion, FieldChangeLog, User, Job
from helpers import (
    get_current_username, save_version_snapshot,
    _get_field_section, load_forbidden_words, save_forbidden_words,
    proforma_to_pis_data, extract_raw_text_from_files,
)
from utils.history import log_event
from utils.web_scraping import scrape_url_data, scrape_url_data_deep
from utils.ai_generation import generate_pis_data, generate_bulk_pis_data
from utils.pdf_processing import extract_specific_image, clear_pdf_cache
from utils.image_processing import (
    find_and_validate_image, find_image_simple, download_web_image,
    find_image_via_screenshot,
)
from utils.storage import store_image

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
    with app.app_context():
        try:
            upload_folder = app.config['UPLOAD_FOLDER']
            _update_job(job_id, status='processing', progress=10, message='Initializing Analysis...')
            site_data = {"text": "", "html": ""}
            if supplier_url:
                _update_job(job_id, progress=20, message='Reading Website Text...')
                site_data = scrape_url_data(supplier_url)

            _update_job(job_id, progress=40, message='Generating PIS Content...')
            ai_data = generate_pis_data(ai_filepaths, model_name, site_data)
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
    with app.app_context():
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


# ── PRODUCT DELETE (soft) ─────────────────────────────────────────────────────

_DELETE_ROLES = ('admin', 'marketing', 'director')


@api_bp.route('/api/product/<int:product_id>/delete', methods=['POST'])
def api_delete_product(product_id):
    """Soft-delete a single product. The row stays in the DB with
    deleted_at set so it can be recovered later if needed; all dashboards
    already filter on deleted_at IS NULL."""
    if session.get('role') not in _DELETE_ROLES:
        return jsonify({"error": "Not authorized"}), 403
    product = Product.query.get_or_404(product_id)
    if product.deleted_at is not None:
        return jsonify({"ok": True, "id": product_id, "already_deleted": True})
    product.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.commit()
    log_event(product.id, get_current_username(), 'Product Deleted',
              'Product was deleted from the dashboard.', 'action')
    return jsonify({"ok": True, "id": product_id})


@api_bp.route('/api/products/bulk_delete', methods=['POST'])
def api_bulk_delete_products():
    """Soft-delete a list of products by id. Body: {"ids": [1, 2, 3]}."""
    if session.get('role') not in _DELETE_ROLES:
        return jsonify({"error": "Not authorized"}), 403
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

_REVIEWER_ROLES = ('admin', 'marketing', 'director')


@api_bp.route('/api/product/<int:product_id>/split_variants', methods=['POST'])
def api_split_variants(product_id):
    """Phase 2.5: explode a draft whose `pis_data['variants']` list has more
    than one entry into N independent draft Products.

    Use case: the AI clustered five wardrobes as variants of one model when
    they're actually distinct products. One click → five drafts. The
    original product is soft-deleted so reviewers don't see duplicates.
    """
    if session.get('role') not in _REVIEWER_ROLES:
        return jsonify({"error": "Not authorized"}), 403
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
    if session.get('role') not in _REVIEWER_ROLES:
        return jsonify({"error": "Not authorized"}), 403

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
def api_clear_active_products():
    """Soft-delete every currently active product. Used by the dashboard
    'Clear All' button. Returns the count cleared."""
    if session.get('role') not in _DELETE_ROLES:
        return jsonify({"error": "Not authorized"}), 403
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

    if 'category_1' in data:
        updated_spec_data.setdefault('categories', {})
        updated_spec_data['categories']['category_1'] = data.get('category_1')
        updated_spec_data['categories']['category_2'] = data.get('category_2')
        updated_spec_data['categories']['category_3'] = data.get('category_3')

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

@api_bp.route('/api/forbidden_words', methods=['GET'])
def api_get_forbidden_words():
    return json.dumps(load_forbidden_words()), 200, {'Content-Type': 'application/json'}


@api_bp.route('/api/forbidden_words', methods=['POST'])
def api_add_forbidden_word():
    body = request.get_json(force=True)
    category = body.get('category', '').strip()
    word     = body.get('word', '').strip().lower()
    if not category or not word:
        return json.dumps({"error": "Category and word required"}), 400, {'Content-Type': 'application/json'}
    data = load_forbidden_words()
    data.setdefault(category, [])
    if word not in data[category]:
        data[category].append(word)
    save_forbidden_words(data)
    return json.dumps({"ok": True, "words": data[category]}), 200, {'Content-Type': 'application/json'}


@api_bp.route('/api/forbidden_words', methods=['DELETE'])
def api_remove_forbidden_word():
    body = request.get_json(force=True)
    category = body.get('category', '').strip()
    word     = body.get('word', '').strip().lower()
    if not category or not word:
        return json.dumps({"error": "Category and word required"}), 400, {'Content-Type': 'application/json'}
    data = load_forbidden_words()
    if category in data and word in data[category]:
        data[category].remove(word)
        if not data[category]:
            del data[category]
    save_forbidden_words(data)
    return json.dumps({"ok": True, "words": data.get(category, [])}), 200, {'Content-Type': 'application/json'}


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
    save_version_snapshot(product, label=f"Before rolling back to version {version.version_num}", is_major=True)
    product.pis_data      = copy.deepcopy(version.pis_data)
    product.spec_data     = copy.deepcopy(version.spec_data)
    product.revision_data = copy.deepcopy(version.revision_data)
    product.workflow_stage = version.workflow_stage
    db.session.commit()
    log_event(product.id, get_current_username(), 'Rolled Back to Previous Version',
              f'The product was rolled back to version {version.version_num} ({version.label}).', 'action')
    return jsonify({"ok": True, "message": f"Restored to version {version.version_num}"})


# ── FIELD CHANGELOG ───────────────────────────────────────────────────────────

@api_bp.route('/api/product/<int:product_id>/changelog')
def api_product_changelog(product_id):
    changes = FieldChangeLog.query.filter_by(product_id=product_id).order_by(FieldChangeLog.timestamp.desc()).limit(100).all()
    result = [{
        "id": c.id, "field_name": c.field_name,
        "section": _get_field_section(c.field_name) if c.field_name else 'Other',
        "old_value": c.old_value, "new_value": c.new_value,
        "version_num": c.version_num,
        "user": c.user.display_name if c.user else "System",
        "timestamp": c.timestamp.strftime('%d %b %Y, %H:%M')
    } for c in changes]
    return jsonify(result)


@api_bp.route('/api/product/<int:product_id>/changes_at')
def api_product_changes_at(product_id):
    ts_str = request.args.get('date', '')
    tm_str = request.args.get('time', '')
    if not ts_str or not tm_str:
        return jsonify([]), 400
    try:
        target_dt = datetime.strptime(f"{ts_str} {tm_str}", '%Y-%m-%d %H:%M')
    except ValueError:
        return jsonify([]), 400
    window = timedelta(seconds=120)
    changes = FieldChangeLog.query.filter(
        FieldChangeLog.product_id == product_id,
        FieldChangeLog.timestamp >= target_dt - window,
        FieldChangeLog.timestamp <= target_dt + window
    ).order_by(FieldChangeLog.timestamp.asc()).all()
    result = [{
        "field_name": c.field_name,
        "section": _get_field_section(c.field_name) if c.field_name else 'Other',
        "old_value": c.old_value, "new_value": c.new_value,
        "version_num": c.version_num,
        "user": c.user.display_name if c.user else "System",
        "timestamp": c.timestamp.strftime('%d %b %Y, %H:%M')
    } for c in changes]
    return jsonify(result)


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
    with app.app_context():
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
            raw_doc_text = extract_raw_text_from_files(ai_filepaths) or ""
            if site_data and site_data.get('text'):
                raw_doc_text = (raw_doc_text + "\n" + site_data['text']) if raw_doc_text else site_data['text']

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
