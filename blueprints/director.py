"""
Director blueprint — director dashboard, PIS review, and SpecSheet review.
"""
import copy
import threading

from flask import (
    Blueprint, session, redirect, url_for, render_template, request, current_app
)
from sqlalchemy.orm.attributes import flag_modified

from model import db, Product, ProductVersion
from helpers import (
    get_current_username, save_version_snapshot,
    _diff_and_log_changes, normalize_pis_data, load_forbidden_words,
    get_product_category, set_product_category,
    get_product_category_label, CATEGORY_UNCATEGORISED,
    append_director_comment,
)
from utils.decorators import require_role
from utils.workflow import Stage
from utils.history import log_event
from utils.ai_generation import generate_ai_revision, generate_comprehensive_spec_data

director_bp = Blueprint('director', __name__)


# ── DASHBOARDS ────────────────────────────────────────────────────────────────

@director_bp.route('/dashboard/director')
@require_role('director')
def dashboard_director():
    pending_pis  = Product.query.filter_by(workflow_stage=Stage.PENDING_DIRECTOR_PIS).filter(Product.deleted_at.is_(None)).all()
    pending_spec = Product.query.filter_by(workflow_stage=Stage.PENDING_DIRECTOR_SPEC).filter(Product.deleted_at.is_(None)).all()

    # Order by `last_edited_at` — bumped on every UPDATE so autosaves,
    # approvals, change-requests, category writes, and stage transitions
    # all surface the product to the top. Fallback to created_at for any
    # row where the column is NULL.
    director_excluded = [Stage.MARKETING_DRAFT, Stage.MARKETING_IN_PROGRESS]
    all_products = (
        Product.query
        .filter(~Product.workflow_stage.in_(director_excluded),
                Product.deleted_at.is_(None))
        .order_by(
            db.func.coalesce(Product.last_edited_at, Product.created_at).desc()
        )
        .all()
    )

    total_products = len(all_products)
    finalized_count = sum(1 for p in all_products if p.workflow_stage == Stage.FINALIZED)
    approved_stages = [Stage.FINALIZED, Stage.READY_FOR_WEB, Stage.SPECSHEET_DRAFT, Stage.PENDING_DIRECTOR_SPEC, Stage.WEB_CHANGES_REQUESTED]
    in_progress_count = sum(1 for p in all_products if p.workflow_stage not in ([Stage.PENDING_DIRECTOR_PIS, Stage.PENDING_DIRECTOR_SPEC] + approved_stages))

    metrics = {
        'total_products': total_products,
        'pending_reviews': len(pending_pis) + len(pending_spec),
        'pending_pis': len(pending_pis),
        'pending_spec': len(pending_spec),
        'finalized': finalized_count,
        'in_progress': in_progress_count
    }

    # Categories actually present in the director's view so the filter
    # dropdown only offers options that will yield results. "Uncategorised"
    # is pinned last so real Magento categories list alphabetically first.
    available_categories = sorted({
        get_product_category_label(p) for p in all_products
    } - {CATEGORY_UNCATEGORISED})
    if any(get_product_category_label(p) == CATEGORY_UNCATEGORISED for p in all_products):
        available_categories.append(CATEGORY_UNCATEGORISED)

    return render_template('dashboard_director.html',
                           pending_pis=pending_pis, pending_spec=pending_spec,
                           all_products=all_products, metrics=metrics,
                           available_categories=available_categories,
                           uncategorised_label=CATEGORY_UNCATEGORISED)


@director_bp.route('/dashboard/director/archive')
@require_role('director')
def director_archive():
    approved_stages = [Stage.FINALIZED, Stage.READY_FOR_WEB, Stage.SPECSHEET_DRAFT, Stage.PENDING_DIRECTOR_SPEC, Stage.WEB_CHANGES_REQUESTED]
    archived_products = Product.query.filter(
        Product.workflow_stage.in_(approved_stages),
        Product.deleted_at.is_(None)
    ).order_by(Product.created_at.desc()).all()
    return render_template('archive_director.html', products=archived_products)


# ── REVIEW: PIS ───────────────────────────────────────────────────────────────

@director_bp.route('/review/director_pis/<int:product_id>', methods=['GET', 'POST'])
def review_director_pis(product_id):
    product = Product.query.get_or_404(product_id)

    if request.method == 'POST':
        action = request.form.get('director_action')
        last_version = ProductVersion.query.filter_by(product_id=product.id).order_by(ProductVersion.version_num.desc()).first()
        old_pis = copy.deepcopy(last_version.pis_data) if last_version and last_version.pis_data else {}
        updated_data = product.pis_data or {}

        if request.form.get('product_name'):
            if 'header_info' not in updated_data: updated_data['header_info'] = {}
            updated_data['header_info']['product_name']   = request.form.get('product_name')
            updated_data['header_info']['model_number']   = request.form.get('model_number')
            updated_data['header_info']['brand']          = request.form.get('brand')
            updated_data['header_info']['price_estimate'] = request.form.get('price_estimate')
        if request.form.get('range_overview'):
            updated_data['range_overview'] = request.form.get('range_overview')
        sales_args = request.form.getlist('sales_argument')
        if sales_args and any(a.strip() for a in sales_args):
            updated_data['sales_arguments'] = [a.strip() for a in sales_args if a.strip()]
        tech_keys = request.form.getlist('tech_spec_key')
        tech_vals = request.form.getlist('tech_spec_value')
        if tech_keys and tech_vals:
            updated_data['technical_specifications'] = dict(zip(tech_keys, tech_vals))
        if request.form.get('warranty_period'):
            if 'warranty_service' not in updated_data: updated_data['warranty_service'] = {}
            updated_data['warranty_service']['period']   = request.form.get('warranty_period')
            updated_data['warranty_service']['coverage'] = request.form.get('warranty_coverage')

        product.pis_data = updated_data
        flag_modified(product, 'pis_data')

        if action == 'review':
            comments_map = {
                'header_info': request.form.get('comment_header_info'),
                'range_overview': request.form.get('comment_range_overview'),
                'sales_arguments': request.form.get('comment_sales_arguments'),
                'technical_specifications': request.form.get('comment_technical_specifications'),
                'warranty_service': request.form.get('comment_warranty_service')
            }
            new_revisions = {}
            sections_to_revise = []
            for section, comment in comments_map.items():
                if comment and comment.strip():
                    original = product.pis_data.get(section)
                    new_revisions[section] = {
                        'comment': comment, 'original': original,
                        'ai_suggestion': None, 'status': 'generating'
                    }
                    sections_to_revise.append((section, original, comment.strip()))
                    # Persist into the per-section archive so marketing
                    # can still review this comment after accepting the
                    # AI suggestion (revision_data gets popped on Accept).
                    # `audience='marketing'` keeps it scoped to the
                    # marketing editor; the web team won't see it.
                    append_director_comment(product, section, comment, audience='marketing')

            product.revision_data = new_revisions
            product.director_pis_comments = request.form.get('director_general_comments')
            product.workflow_stage = Stage.MARKETING_CHANGES_REQUESTED

            section_labels = {
                'header_info': 'Header Info', 'range_overview': 'Description',
                'sales_arguments': 'Sales Arguments',
                'technical_specifications': 'Tech Specs', 'warranty_service': 'Warranty'
            }
            comment_details = [
                f'{section_labels.get(s, s)}: "{c.strip()[:80]}"'
                for s, c in comments_map.items() if c and c.strip()
            ]
            log_desc = f"Director requested changes on {len(new_revisions)} section(s):\n" + "\n".join(f"• {d}" for d in comment_details)
            general = request.form.get('director_general_comments')
            if general and general.strip():
                log_desc += f'\n\nGeneral: "{general.strip()[:100]}"'

            save_version_snapshot(product, label='Before Director requested changes', is_major=True)
            log_event(product.id, get_current_username(), 'Revisions Requested by Director', log_desc, 'action')
            db.session.commit()

            if sections_to_revise:
                pid = product.id
                _app = current_app._get_current_object()  # type: ignore[attr-defined]

                def _generate_revisions(app_ctx, product_id, sections):
                    with app_ctx:
                        try:
                            p = Product.query.get(product_id)
                            if not p or not p.revision_data:
                                return
                            rev = dict(p.revision_data)
                            for section, original, comment in sections:
                                try:
                                    rev[section]['ai_suggestion'] = generate_ai_revision(section, original, comment)
                                    rev[section]['status'] = 'pending'
                                except Exception as e:
                                    print(f"⚠ AI revision failed for {section}: {e}")
                                    rev[section]['ai_suggestion'] = original
                                    rev[section]['status'] = 'pending'
                            p.revision_data = rev
                            flag_modified(p, 'revision_data')
                            db.session.commit()
                        except Exception as e:
                            print(f"❌ Background revision error: {e}")

                t = threading.Thread(
                    target=_generate_revisions,
                    args=(_app.app_context(), pid, sections_to_revise),
                    daemon=True
                )
                t.start()

        elif action == 'approve':
            preserved_image_path = product.image_path
            preserved_additional_images = product.additional_images

            # Seed categories from the canonical column — set by bulk
            # enrichment for imported products, NULL for single-import.
            # In the latter case generate_comprehensive_spec_data's classifier
            # fallback will fill them and we capture back into canonical
            # in the bg thread below.
            seed_cat = get_product_category(product)
            initial_spec_data = {
                'header_info': product.pis_data.get('header_info', {}),
                'customer_friendly_description': product.pis_data.get('seo_data', {}).get('seo_long_description', ''),
                'refined_description': product.pis_data.get('seo_data', {}).get('seo_long_description', ''),
                'key_features': product.pis_data.get('sales_arguments', []),
                'technical_specifications': product.pis_data.get('technical_specifications', {}),
                'long_tail_keywords': '',
                'internal_web_keywords': product.pis_data.get('seo_data', {}).get('generated_keywords', ''),
                'seo': {
                    'meta_title': product.pis_data.get('seo_data', {}).get('meta_title', ''),
                    'meta_description': product.pis_data.get('seo_data', {}).get('meta_description', ''),
                    'keywords': product.pis_data.get('seo_data', {}).get('generated_keywords', '')
                },
                'categories': {
                    'category_1': seed_cat['category_1'],
                    'category_2': seed_cat['category_2'],
                    'category_3': seed_cat['category_3'],
                },
                '_spec_generating': True
            }
            product.spec_data = initial_spec_data
            product.workflow_stage = Stage.READY_FOR_WEB
            product.revision_data = None
            product.image_path = preserved_image_path
            product.additional_images = preserved_additional_images

            log_event(product.id, get_current_username(), 'PIS Approved ✓',
                      'The Director has approved this product sheet. The system is now generating the customer-facing specsheet.',
                      'success')
            save_version_snapshot(product, label='Approved by Director', is_major=True)
            db.session.commit()

            pid = product.id
            pis_data_copy = copy.deepcopy(product.pis_data)
            _app = current_app._get_current_object()  # type: ignore[attr-defined]

            def _generate_specsheet_bg(app_ctx, product_id, pis_data, canonical_cat):
                with app_ctx:
                    try:
                        all_fw = load_forbidden_words()
                        combined_forbidden = list(set(w for words in all_fw.values() for w in words))
                        # Pass canonical category through — prevents the AI
                        # classifier from being re-run when the product was
                        # already classified by bulk enrichment.
                        spec_data_generated = generate_comprehensive_spec_data(
                            pis_data,
                            forbidden_words=combined_forbidden,
                            categories=canonical_cat if canonical_cat.get('category_1') else None,
                        )
                        spec_data_generated['technical_specifications'] = pis_data.get('technical_specifications', {})
                        spec_data_generated['header_info'] = pis_data.get('header_info', {})
                        spec_data_generated.pop('_spec_generating', None)
                        p = Product.query.get(product_id)
                        if p:
                            p.spec_data = spec_data_generated
                            flag_modified(p, 'spec_data')
                            # If the generator's AI classifier filled in
                            # categories (single-import, no canonical yet),
                            # promote them to the canonical column so future
                            # reads/filters see them.
                            if not p.category_1:
                                gen_cats = (spec_data_generated.get('categories') or {})
                                if gen_cats.get('category_1'):
                                    set_product_category(p,
                                        gen_cats.get('category_1', ''),
                                        gen_cats.get('category_2', ''),
                                        gen_cats.get('category_3', ''))
                            save_version_snapshot(p, label='SpecSheet auto-generated', is_major=True)
                            db.session.commit()
                    except Exception as e:
                        print(f"❌ [BG] Specsheet generation failed: {e}")
                        try:
                            p = Product.query.get(product_id)
                            if p and p.spec_data:
                                sd = dict(p.spec_data)
                                sd.pop('_spec_generating', None)
                                p.spec_data = sd
                                flag_modified(p, 'spec_data')
                                db.session.commit()
                        except Exception:
                            pass

            t = threading.Thread(
                target=_generate_specsheet_bg,
                args=(_app.app_context(), pid, pis_data_copy, seed_cat),
                daemon=True
            )
            t.start()

        _diff_and_log_changes(product.id, old_pis, updated_data, prefix='pis_data')
        db.session.commit()
        return redirect(url_for('director.dashboard_director'))

    return render_template('verify_director_pis.html', product=product, data=normalize_pis_data(product.pis_data))


# ── REVIEW: SPECSHEET ─────────────────────────────────────────────────────────

@director_bp.route('/review/director_spec/<int:product_id>', methods=['GET', 'POST'])
def review_director_spec(product_id):
    product = Product.query.get_or_404(product_id)

    if request.method == 'POST':
        action = request.form.get('director_action')
        last_version = ProductVersion.query.filter_by(product_id=product.id).order_by(ProductVersion.version_num.desc()).first()
        old_pis  = copy.deepcopy(last_version.pis_data)  if last_version and last_version.pis_data  else {}
        old_spec = copy.deepcopy(last_version.spec_data) if last_version and last_version.spec_data else {}
        updated_pis_data  = product.pis_data  or {}
        updated_spec_data = product.spec_data or {}

        if request.form.get('product_name'):
            if 'header_info' not in updated_pis_data: updated_pis_data['header_info'] = {}
            updated_pis_data['header_info']['product_name']   = request.form.get('product_name')
            updated_pis_data['header_info']['model_number']   = request.form.get('model_number')
            updated_pis_data['header_info']['brand']          = request.form.get('brand')
            updated_pis_data['header_info']['price_estimate'] = request.form.get('price_estimate')
        if request.form.get('range_overview'):
            updated_pis_data['range_overview'] = request.form.get('range_overview')
        sales_args = request.form.getlist('sales_argument')
        if sales_args and any(a.strip() for a in sales_args):
            clean_args = [a.strip() for a in sales_args if a.strip()]
            updated_pis_data['sales_arguments'] = clean_args
            updated_spec_data['key_features']   = clean_args
        tech_keys = request.form.getlist('tech_spec_key')
        tech_vals = request.form.getlist('tech_spec_value')
        if tech_keys and tech_vals:
            specs_dict = dict(zip(tech_keys, tech_vals))
            updated_pis_data['technical_specifications']  = specs_dict
            updated_spec_data['technical_specifications'] = specs_dict
        if request.form.get('warranty_period'):
            for d in (updated_pis_data, updated_spec_data):
                if 'warranty_service' not in d: d['warranty_service'] = {}
                d['warranty_service']['period']   = request.form.get('warranty_period')
                d['warranty_service']['coverage'] = request.form.get('warranty_coverage')
        if request.form.get('refined_description'):
            updated_spec_data['refined_description'] = request.form.get('refined_description')
            updated_spec_data['customer_friendly_description'] = request.form.get('refined_description')
        if request.form.get('seo_keywords'):
            product.seo_keywords = request.form.get('seo_keywords')
        if request.form.get('internal_web_keywords'):
            updated_spec_data['internal_web_keywords'] = request.form.get('internal_web_keywords')
        product.pis_data  = updated_pis_data
        product.spec_data = updated_spec_data
        flag_modified(product, 'pis_data')
        flag_modified(product, 'spec_data')

        # Canonical category write — helper updates Product.category_1/2/3
        # and mirrors to spec_data.categories + pis_data.category_data so
        # legacy readers stay in sync. Skipped when the form didn't submit
        # category fields (e.g. PIS review where the section is absent).
        if request.form.get('category_1'):
            set_product_category(
                product,
                request.form.get('category_1'),
                request.form.get('category_2', ''),
                request.form.get('category_3', ''),
            )
            updated_spec_data = product.spec_data
            updated_pis_data = product.pis_data

        if action == 'review':
            comments_map = {
                'seo_optimization':       request.form.get('comment_seo_optimization'),
                'internal_web_keywords':  request.form.get('comment_internal_web_keywords'),
                'product_classification': request.form.get('comment_product_classification'),
                'header_info':            request.form.get('comment_header_info'),
                'range_overview':         request.form.get('comment_range_overview'),
                'sales_arguments':        request.form.get('comment_sales_arguments'),
                'technical_specifications': request.form.get('comment_technical_specifications'),
                'warranty_service':       request.form.get('comment_warranty_service')
            }
            new_revisions = {}
            for section, comment in comments_map.items():
                if comment and comment.strip():
                    if section == 'seo_optimization':
                        # `spec_data.get('seo')` can be None when the key is
                        # absent — coerce to {} so the `refined_description`
                        # write below always lands on a real dict.
                        original = (product.spec_data.get('seo') if product.spec_data else None) or {}
                        if product.spec_data and 'customer_friendly_description' in product.spec_data:
                            original['refined_description'] = product.spec_data['customer_friendly_description']
                    elif section == 'product_classification':
                        original = product.spec_data.get('categories') if product.spec_data else {}
                    elif section == 'internal_web_keywords':
                        original = product.spec_data.get('internal_web_keywords') if product.spec_data else ''
                    else:
                        original = product.pis_data.get(section)
                    new_revisions[section] = {
                        'comment': comment,
                        'original': original,
                        'ai_suggestion': generate_ai_revision(section, original, comment),
                        'status': 'pending'
                    }
                    # Persist into the per-section archive so the web team
                    # can still review this comment after accepting the AI
                    # suggestion (revision_data gets popped on Accept).
                    # `audience='web'` keeps it scoped to the web editor;
                    # the marketing team won't see it.
                    append_director_comment(product, section, comment, audience='web')

            product.revision_data = new_revisions
            product.director_spec_comments = request.form.get('director_general_comments')
            product.workflow_stage = Stage.WEB_CHANGES_REQUESTED

            section_labels = {
                'seo_optimization': 'SEO', 'internal_web_keywords': 'Internal Keywords',
                'product_classification': 'Categories', 'header_info': 'Header Info',
                'range_overview': 'Description', 'sales_arguments': 'Sales Arguments',
                'technical_specifications': 'Tech Specs', 'warranty_service': 'Warranty'
            }
            comment_details = [
                f'{section_labels.get(s, s)}: "{c.strip()[:80]}"'
                for s, c in comments_map.items() if c and c.strip()
            ]
            log_desc = f"Director requested SpecSheet changes on {len(new_revisions)} section(s):\n" + "\n".join(f"• {d}" for d in comment_details)
            general = request.form.get('director_general_comments')
            if general and general.strip():
                log_desc += f'\n\nGeneral: "{general.strip()[:100]}"'

            save_version_snapshot(product, label='Before Director requested SpecSheet changes', is_major=True)
            log_event(product.id, get_current_username(), 'SpecSheet Revisions Requested', log_desc, 'action')

        elif action == 'approve':
            product.workflow_stage = Stage.FINALIZED
            product.revision_data = None
            save_version_snapshot(product, label='Final approved version', is_major=True)
            log_event(product.id, get_current_username(), 'SpecSheet Approved ✓',
                      'The specsheet has been finalized and approved. This product is now ready for publication.',
                      'success')

        _diff_and_log_changes(product.id, old_pis, updated_pis_data, prefix='pis_data')
        synced_keys = {'key_features', 'technical_specifications', 'customer_friendly_description', 'refined_description', 'header_info', 'warranty_service'}
        spec_diff_old = {k: v for k, v in old_spec.items() if k not in synced_keys}
        spec_diff_new = {k: v for k, v in updated_spec_data.items() if k not in synced_keys}
        if spec_diff_old != spec_diff_new:
            _diff_and_log_changes(product.id, spec_diff_old, spec_diff_new, prefix='spec_data')

        db.session.commit()
        return redirect(url_for('director.dashboard_director'))

    return render_template('verify_specsheet.html', product=product, spec_data=product.spec_data)
