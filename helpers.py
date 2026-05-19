"""
Shared helper functions used across Flask blueprints.
"""
import os
import re
import copy
import logging

from flask import session
from sqlalchemy.orm.attributes import flag_modified

from model import db, User, Product, ProductVersion, FieldChangeLog
from utils.validation import validate_pis_data, validate_spec_data

logger = logging.getLogger(__name__)


# ================= SESSION HELPERS =================

def get_current_username():
    """Get display name or username of the currently logged-in user. Uses session cache."""
    cached = session.get('username')
    if cached:
        return cached
    user_id = session.get('user_id')
    if user_id:
        user = User.query.get(user_id)
        if user:
            name = user.display_name or user.username
            session['username'] = name
            return name
    return session.get('role', 'System').capitalize()


# ================= VERSION SNAPSHOTS =================

def save_version_snapshot(product, label='Auto-save', is_major=False):
    """Save a snapshot of the product's current state.

    Major snapshots (stage transitions) store full data.
    Minor snapshots store a compact diff against the previous version.

    Phase History v2 — every snapshot is tagged with expires_at = created_at
    + HISTORY_TTL_DAYS so the Phase 4 cleanup can prune old snapshots
    safely. The cleanup guards the most-recent major snapshot per product
    regardless of expiry, so a row's expires_at being in the past does not
    automatically mean it will be deleted.

    Returns the created ProductVersion on success (so callers can pass
    version.id into log_event), or None on failure. Previously returned
    None unconditionally — additive change, no caller breaks.
    """
    try:
        # Validate JSONB structure and log any schema warnings (non-blocking)
        if product.pis_data:
            ok, warnings = validate_pis_data(product.pis_data)
            if not ok or warnings:
                logger.warning("pis_data schema warnings for product %s: %s", product.id, warnings)
        if product.spec_data:
            ok, warnings = validate_spec_data(product.spec_data)
            if not ok or warnings:
                logger.warning("spec_data schema warnings for product %s: %s", product.id, warnings)

        last_version = ProductVersion.query.filter_by(
            product_id=product.id
        ).order_by(ProductVersion.version_num.desc()).first()
        next_num = (last_version.version_num + 1) if last_version else 1

        try:
            user_id = session.get('user_id')
        except RuntimeError:
            user_id = None

        # Centralize the TTL definition. Imported here (not at module top)
        # so this module stays import-cycle-free with utils.history.
        from utils.history import expiry_from
        from datetime import datetime, timezone
        created_at = datetime.now(timezone.utc).replace(tzinfo=None)
        expires_at = expiry_from(created_at)

        if is_major or last_version is None:
            # Full snapshot for major changes or the very first save
            version = ProductVersion(
                product_id=product.id,
                version_num=next_num,
                pis_data=copy.deepcopy(product.pis_data) if product.pis_data else None,
                spec_data=copy.deepcopy(product.spec_data) if product.spec_data else None,
                revision_data=copy.deepcopy(product.revision_data) if product.revision_data else None,
                workflow_stage=product.workflow_stage,
                created_by_id=user_id,
                created_at=created_at,
                expires_at=expires_at,
                label=label,
                is_major=True,
            )
        else:
            # Diff-only snapshot for minor (draft) saves
            prev_pis = last_version.pis_data or {}
            prev_spec = last_version.spec_data or {}
            curr_pis = product.pis_data or {}
            curr_spec = product.spec_data or {}
            version = ProductVersion(
                product_id=product.id,
                version_num=next_num,
                pis_data=_compute_shallow_diff(prev_pis, curr_pis) or None,
                spec_data=_compute_shallow_diff(prev_spec, curr_spec) or None,
                revision_data=copy.deepcopy(product.revision_data) if product.revision_data else None,
                workflow_stage=product.workflow_stage,
                created_by_id=user_id,
                created_at=created_at,
                expires_at=expires_at,
                label=label,
                is_major=False,
            )

        db.session.add(version)
        db.session.commit()
        logger.info("Version %s (%s) saved for product %s: %s",
                    next_num, 'major' if version.is_major else 'minor', product.id, label)
        return version
    except Exception:
        db.session.rollback()
        logger.exception("Failed to save version for product %s", getattr(product, 'id', '?'))
        return None


def _compute_shallow_diff(old: dict, new: dict) -> dict:
    """Return only the top-level keys that changed between old and new."""
    diff = {}
    all_keys = set(list(old.keys()) + list(new.keys()))
    for key in all_keys:
        old_val = old.get(key)
        new_val = new.get(key)
        if old_val != new_val:
            diff[key] = new_val
    return diff


# ================= FIELD DIFF & CHANGE LOG =================

FIELD_LABELS = {
    'pis_data.header_info.product_name': 'Product Name',
    'pis_data.header_info.model_number': 'Model Number',
    'pis_data.header_info.brand': 'Brand',
    'pis_data.header_info.price_estimate': 'Price Estimate',
    'pis_data.range_overview': 'Description',
    'pis_data.sales_arguments': 'Key Selling Points',
    'pis_data.technical_specifications': 'Technical Specs',
    'pis_data.warranty_service.period': 'Warranty Period',
    'pis_data.warranty_service.coverage': 'Warranty Coverage',
    'pis_data.seo_data.meta_title': 'Meta Title',
    'pis_data.seo_data.meta_description': 'Meta Description',
    'pis_data.seo_data.generated_keywords': 'SEO Keywords',
    'spec_data.header_info.product_name': 'Product Name',
    'spec_data.header_info.model_number': 'Model Number',
    'spec_data.header_info.brand': 'Brand',
    'spec_data.header_info.price_estimate': 'Price Estimate',
    'spec_data.customer_friendly_description': 'Short Description',
    'spec_data.refined_description': 'Refined Description',
    'spec_data.key_features': 'Key Features',
    'spec_data.seo.meta_title': 'Meta Title',
    'spec_data.seo.meta_description': 'Meta Description',
    'spec_data.seo.keywords': 'SEO Keywords',
    'spec_data.internal_web_keywords': 'Web Keywords',
    'spec_data.categories.category_1': 'Category A',
    'spec_data.categories.category_2': 'Category B',
    'spec_data.categories.category_3': 'Category C',
    'spec_data.technical_specifications': 'Technical Specs',
    'spec_data.warranty.period': 'Warranty Period',
    'spec_data.warranty.coverage': 'Warranty Coverage',
    'spec_data.warranty_service.period': 'Warranty Period',
    'spec_data.warranty_service.coverage': 'Warranty Coverage',
}

FIELD_SECTION_MAP = {
    'Product Name': 'Header', 'Model Number': 'Header', 'Brand': 'Header',
    'Price Estimate': 'Header',
    'Description': 'Description', 'Short Description': 'Description',
    'Refined Description': 'Description',
    'Key Selling Points': 'Sales', 'Key Features': 'Key Features',
    'Technical Specs': 'Specs',
    'Warranty Period': 'Warranty', 'Warranty Coverage': 'Warranty',
    'Meta Title': 'SEO', 'Meta Description': 'SEO', 'SEO Keywords': 'SEO',
    'Web Keywords': 'SEO',
    'Category A': 'Classification', 'Category B': 'Classification',
    'Category C': 'Classification',
}


def _clean_field_name(raw_field):
    if raw_field in FIELD_LABELS:
        return FIELD_LABELS[raw_field]
    if '.technical_specifications.' in raw_field:
        spec_key = raw_field.split('.technical_specifications.')[-1]
        return f"Spec: {spec_key.replace('_', ' ').title()}"
    last = raw_field.split('.')[-1]
    return last.replace('_', ' ').title()


def _get_field_section(field_name):
    if field_name in FIELD_SECTION_MAP:
        return FIELD_SECTION_MAP[field_name]
    if field_name and field_name.startswith('Spec: '):
        return 'Specs'
    return 'Other'


def _format_value(val):
    if val is None:
        return None
    if isinstance(val, list):
        items = [str(v).strip() for v in val if str(v).strip()]
        return '; '.join(items) if items else None
    if isinstance(val, dict):
        parts = [f"{k}: {v}" for k, v in val.items() if v]
        return '; '.join(parts) if parts else None
    return str(val)


def _normalize(val):
    if val is None:
        return None
    if isinstance(val, str):
        s = val.replace('\r\n', '\n').replace('\r', '\n').strip()
        s = re.sub(r'[ \t]+', ' ', s)
        return s
    if isinstance(val, list):
        return [_normalize(x) for x in val]
    return val


def _is_empty(val):
    return val is None or val == '' or val == [] or val == {}


def _make_field_change(*, product_id, user_id, field_name, old_value,
                        new_value, version_num, workflow_stage):
    """Construct a FieldChangeLog row with workflow_stage + expires_at
    populated. Centralizing this keeps the Phase History v2 tagging
    consistent across the 6 insert sites in diff_and_log /
    _diff_and_log_changes. Caller resolves `workflow_stage` once per
    diff so we don't re-query the product row per field."""
    from utils.history import expiry_from
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).replace(tzinfo=None)
    return FieldChangeLog(
        product_id=product_id, user_id=user_id, field_name=field_name,
        old_value=old_value, new_value=new_value,
        version_num=version_num,
        workflow_stage=workflow_stage,
        timestamp=ts, expires_at=expiry_from(ts),
    )


def _resolve_stage_for_diff(product_id):
    """Read the product's current workflow_stage once per diff call.
    Returns None on any failure — FieldChangeLog.workflow_stage is
    nullable so the diff still records correctly."""
    try:
        p = Product.query.get(product_id)
        return p.workflow_stage if p else None
    except Exception:
        return None


def diff_and_log(product_id, old_data, new_data, prefix='', _version_num=None,
                  _workflow_stage=None):
    """Compare two dicts recursively and log only actual field edits.

    `_workflow_stage` is resolved once at the top of the public call and
    threaded through recursion so every emitted FieldChangeLog row gets
    the same stage tag.
    """
    user_id = session.get('user_id')

    if _version_num is None:
        latest = ProductVersion.query.filter_by(product_id=product_id).order_by(ProductVersion.version_num.desc()).first()
        _version_num = latest.version_num if latest else 1

    if _workflow_stage is None:
        _workflow_stage = _resolve_stage_for_diff(product_id)

    if old_data is None: old_data = {}
    if new_data is None: new_data = {}

    if not isinstance(old_data, dict) or not isinstance(new_data, dict):
        if _is_empty(old_data):
            return
        if isinstance(old_data, list) and isinstance(new_data, list):
            old_set = [_normalize(x) for x in old_data if x]
            new_set = [_normalize(x) for x in new_data if x]
            added = [x for x in new_set if x not in old_set]
            removed = [x for x in old_set if x not in new_set]
            if not added and not removed:
                return
            field_name = _clean_field_name(prefix or 'root')
            try:
                if added:
                    db.session.add(_make_field_change(
                        product_id=product_id, user_id=user_id, field_name=field_name,
                        old_value=None,
                        new_value=('Added: ' + '; '.join(str(a) for a in added))[:2000],
                        version_num=_version_num,
                        workflow_stage=_workflow_stage,
                    ))
                if removed:
                    db.session.add(_make_field_change(
                        product_id=product_id, user_id=user_id, field_name=field_name,
                        old_value=('Removed: ' + '; '.join(str(r) for r in removed))[:2000],
                        new_value=None, version_num=_version_num,
                        workflow_stage=_workflow_stage,
                    ))
            except Exception as e:
                logger.exception("Diff log error")
            return
        if _normalize(old_data) == _normalize(new_data):
            return
        try:
            fmt_old = _format_value(old_data)
            fmt_new = _format_value(new_data)
            db.session.add(_make_field_change(
                product_id=product_id, user_id=user_id,
                field_name=_clean_field_name(prefix or 'root'),
                old_value=fmt_old[:2000] if fmt_old else None,
                new_value=fmt_new[:2000] if fmt_new else None,
                version_num=_version_num,
                workflow_stage=_workflow_stage,
            ))
        except Exception as e:
            logger.exception("Diff log error")
        return

    all_keys = set(list(old_data.keys()) + list(new_data.keys()))
    for key in all_keys:
        field = f"{prefix}.{key}" if prefix else key
        old_val = old_data.get(key)
        new_val = new_data.get(key)
        if isinstance(old_val, dict) and isinstance(new_val, dict):
            diff_and_log(product_id, old_val, new_val, prefix=field,
                          _version_num=_version_num,
                          _workflow_stage=_workflow_stage)
        elif isinstance(old_val, list) or isinstance(new_val, list):
            if _is_empty(old_val):
                continue
            diff_and_log(product_id, old_val or [], new_val or [], prefix=field,
                          _version_num=_version_num,
                          _workflow_stage=_workflow_stage)
        elif old_val != new_val:
            if _is_empty(old_val):
                continue
            if _normalize(old_val) == _normalize(new_val):
                continue
            try:
                fmt_old = _format_value(old_val)
                fmt_new = _format_value(new_val)
                db.session.add(_make_field_change(
                    product_id=product_id, user_id=user_id,
                    field_name=_clean_field_name(field),
                    old_value=fmt_old[:2000] if fmt_old else None,
                    new_value=fmt_new[:2000] if fmt_new else None,
                    version_num=_version_num,
                    workflow_stage=_workflow_stage,
                ))
            except Exception as e:
                logger.exception("Diff log error")


def _diff_and_log_changes(product_id, old_data, new_data, prefix=''):
    """Compare two dicts and log ALL field changes (including empty→value)."""
    user_id = session.get('user_id')
    latest = ProductVersion.query.filter_by(product_id=product_id).order_by(ProductVersion.version_num.desc()).first()
    version_num = latest.version_num if latest else 1

    # Phase History v2 — resolve once, capture via closure in _recurse.
    workflow_stage = _resolve_stage_for_diff(product_id)

    if old_data is None: old_data = {}
    if new_data is None: new_data = {}

    def _recurse(old, new, path):
        if not isinstance(old, dict) or not isinstance(new, dict):
            if isinstance(old, list) and isinstance(new, list):
                old_set = [_normalize(x) for x in old if x]
                new_set = [_normalize(x) for x in new if x]
                if old_set == new_set:
                    return
                added = [x for x in new_set if x not in old_set]
                removed = [x for x in old_set if x not in new_set]
                if added or removed:
                    field_name = _clean_field_name(path)
                    try:
                        if added:
                            db.session.add(_make_field_change(
                                product_id=product_id, user_id=user_id,
                                field_name=field_name, old_value=None,
                                new_value=('Added: ' + '; '.join(str(a) for a in added))[:2000],
                                version_num=version_num,
                                workflow_stage=workflow_stage,
                            ))
                        if removed:
                            db.session.add(_make_field_change(
                                product_id=product_id, user_id=user_id,
                                field_name=field_name,
                                old_value=('Removed: ' + '; '.join(str(r) for r in removed))[:2000],
                                new_value=None, version_num=version_num,
                                workflow_stage=workflow_stage,
                            ))
                    except Exception as e:
                        logger.exception("Diff log error")
                return
            if _normalize(old) == _normalize(new):
                return
            try:
                old_str = _format_value(old)
                new_str = _format_value(new)
                db.session.add(_make_field_change(
                    product_id=product_id, user_id=user_id,
                    field_name=_clean_field_name(path),
                    old_value=old_str[:2000] if old_str else None,
                    new_value=new_str[:2000] if new_str else None,
                    version_num=version_num,
                    workflow_stage=workflow_stage,
                ))
            except Exception as e:
                logger.exception("Diff log error")
            return

        for key in set(list(old.keys()) + list(new.keys())):
            child_path = f"{path}.{key}" if path else key
            old_val = old.get(key)
            new_val = new.get(key)
            if isinstance(old_val, dict) and isinstance(new_val, dict):
                _recurse(old_val, new_val, child_path)
            elif isinstance(old_val, list) or isinstance(new_val, list):
                _recurse(old_val or [], new_val or [], child_path)
            elif old_val != new_val:
                if _normalize(old_val) == _normalize(new_val):
                    continue
                try:
                    old_str = _format_value(old_val)
                    new_str = _format_value(new_val)
                    db.session.add(_make_field_change(
                        product_id=product_id, user_id=user_id,
                        field_name=_clean_field_name(child_path),
                        old_value=old_str[:2000] if old_str else None,
                        new_value=new_str[:2000] if new_str else None,
                        version_num=version_num,
                        workflow_stage=workflow_stage,
                    ))
                except Exception as e:
                    logger.exception("Diff log error")

    _recurse(old_data, new_data, prefix)


# ================= DIRECTOR SECTION COMMENTS =================
#
# Persistent archive of every per-section comment the director leaves
# during revision requests. Built up over time; never cleared by Accept
# (the matching entry in `revision_data` gets popped, but the comment
# survives here for audit + review).
#
# `append_director_comment` is the single write site — called from both
# director PIS review and director SpecSheet review when collecting
# `comment_X` form fields, so the marketing/web team can still inspect
# the rationale after the revision card disappears.

def append_director_comment(product, section, comment, actor=None, audience=None):
    """Append a comment to the persistent per-section archive.

    `audience` tags the entry with the team it was addressed to so the
    macro can scope the popover correctly:
        'marketing' — left during the PIS review (marketing team acts on it)
        'web'       — left during the SpecSheet review (web team acts on it)
    Untagged entries (legacy, pre-audience-split) default to 'marketing'
    at read time so they don't disappear silently.

    Idempotent on dedup: if the most recent entry for this section *and
    audience* has the exact same comment text, skip — protects against
    form re-posts. The audience is part of the dedup key so a marketing
    comment doesn't suppress an identical-text web comment.
    """
    from datetime import datetime, timezone

    if not comment or not str(comment).strip():
        return
    comment = str(comment).strip()

    archive = product.director_section_comments
    if not isinstance(archive, dict):
        archive = {}

    entries = archive.get(section)
    if not isinstance(entries, list):
        entries = []

    # Skip duplicate-of-last-entry — but only when same audience, so the
    # same wording sent to two different teams doesn't get swallowed.
    if entries:
        last = entries[-1]
        if (last.get('comment', '').strip() == comment
                and last.get('audience') == audience):
            return

    entries.append({
        'comment':   comment,
        'timestamp': datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds'),
        'actor':     (actor or get_current_username() or 'Director'),
        'audience':  audience,
    })
    archive[section] = entries
    product.director_section_comments = archive
    flag_modified(product, 'director_section_comments')


def format_comment_timestamp(iso_str):
    """Render a stored ISO timestamp as "13 May · 14:23" for the popup.
    Robust against malformed/missing values so the template doesn't 500
    if a comment is somehow missing its timestamp."""
    if not iso_str:
        return ''
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(str(iso_str).split('+')[0])
        return dt.strftime('%d %b · %H:%M')
    except (ValueError, TypeError):
        return str(iso_str)


# ================= UNIFIED PRODUCT CATEGORY =================
#
# Single source of truth: the Product.category_1/2/3 columns. Reads fall back
# to the legacy JSON locations so products written before the schema migration
# still resolve. Writes update the canonical columns AND mirror to the legacy
# locations so any reader not yet switched to the helper keeps working.
#
# Once every reader/writer has been migrated, the mirror can be dropped
# (Phase F) — set_product_category(..., mirror_to_json=False).

CATEGORY_UNCATEGORISED = 'Uncategorised'


def get_product_category(product):
    """Return the canonical category for a product.

    Resolution order:
      1. Product.category_1/2/3 columns (canonical, post-migration)
      2. spec_data.categories            (legacy web-confirmed)
      3. pis_data.category_data          (legacy AI-assigned)
      4. None — surfaced as `source='uncategorised'` so callers can bucket
         these products without a special case for empty fields.

    Returns a dict so the same shape can feed templates, dashboards, and
    the SpecSheet generator without each call site duplicating fallback
    logic. `source` is informational — useful for debugging during the
    migration but never user-visible.
    """
    if product is None:
        return _empty_category()

    # 1. Canonical columns
    if getattr(product, 'category_1', None):
        return {
            'category_1':           product.category_1,
            'category_2':           product.category_2 or '',
            'category_3':           product.category_3 or '',
            'magento_category_id':  getattr(product, 'magento_category_id', None),
            'source':               'canonical',
        }

    # 2. Legacy spec_data.categories (web-confirmed wins over AI guess)
    spec = getattr(product, 'spec_data', None)
    if isinstance(spec, dict):
        cats = spec.get('categories') or {}
        if isinstance(cats, dict) and (cats.get('category_1') or '').strip():
            return {
                'category_1':           (cats.get('category_1') or '').strip(),
                'category_2':           (cats.get('category_2') or '').strip(),
                'category_3':           (cats.get('category_3') or '').strip(),
                'magento_category_id':  None,
                'source':               'spec_data',
            }

    # 3. Legacy pis_data.category_data (AI-assigned at bulk enrichment)
    pis = getattr(product, 'pis_data', None)
    if isinstance(pis, dict):
        cats = pis.get('category_data') or {}
        if isinstance(cats, dict) and (cats.get('category_1') or '').strip():
            return {
                'category_1':           (cats.get('category_1') or '').strip(),
                'category_2':           (cats.get('category_2') or '').strip(),
                'category_3':           (cats.get('category_3') or '').strip(),
                'magento_category_id':  None,
                'source':               'pis_data',
            }

    return _empty_category()


def _empty_category():
    return {
        'category_1':           '',
        'category_2':           '',
        'category_3':           '',
        'magento_category_id':  None,
        'source':               'uncategorised',
    }


def get_product_category_label(product):
    """Convenience for UI: returns the top-level category name or the
    'Uncategorised' bucket label. Used by dashboard filters where the
    fallback chain has to collapse to one filterable string."""
    cat = get_product_category(product)
    return cat['category_1'] or CATEGORY_UNCATEGORISED


def set_product_category(product, category_1, category_2='', category_3='',
                         magento_id=None, *, mirror_to_json=True):
    """Write the canonical category for a product.

    Always updates Product.category_1/2/3 (and magento_category_id, looked
    up from the Magento taxonomy if not provided). With `mirror_to_json=True`
    (default during the transition) also writes the legacy
    `spec_data.categories` and `pis_data.category_data` shapes so any code
    still reading from those keeps working.

    Empty strings are stored as NULL — that's what `get_product_category`
    treats as "uncategorised" and what the filter dropdown buckets together.

    No commit — caller controls the transaction boundary."""
    c1 = (category_1 or '').strip() or None
    c2 = (category_2 or '').strip() or None
    c3 = (category_3 or '').strip() or None

    product.category_1 = c1
    product.category_2 = c2
    product.category_3 = c3

    # Resolve Magento ID against the live taxonomy when not supplied. This
    # is best-effort: if Magento is unreachable or the path doesn't exist
    # upstream (e.g. the AI invented a custom category) we just leave the
    # ID NULL so the names still display.
    if magento_id is None and c1:
        try:
            from utils.magento_api import get_category_ids_for_path
            magento_id = get_category_ids_for_path(c1, c2 or '', c3 or '')
        except Exception:
            magento_id = None
    product.magento_category_id = magento_id

    if not mirror_to_json:
        return

    # ── Mirror to legacy JSON locations ───────────────────────────────────
    # Keeps every reader that still consults spec_data.categories or
    # pis_data.category_data in sync with the canonical columns until those
    # readers are switched in Phase D / Phase F removes this mirror.
    if not isinstance(product.spec_data, dict):
        product.spec_data = {}
    product.spec_data.setdefault('categories', {})
    product.spec_data['categories']['category_1'] = c1 or ''
    product.spec_data['categories']['category_2'] = c2 or ''
    product.spec_data['categories']['category_3'] = c3 or ''
    flag_modified(product, 'spec_data')

    if not isinstance(product.pis_data, dict):
        product.pis_data = {}
    product.pis_data['category_data'] = {
        'category_1': c1 or '',
        'category_2': c2 or '',
        'category_3': c3 or '',
    }
    flag_modified(product, 'pis_data')


# ================= PIS DATA NORMALIZATION =================

def normalize_pis_data(data):
    """Ensure all required nested keys exist in PIS data before rendering templates."""
    if not data or not isinstance(data, dict):
        data = {}
    if 'header_info' not in data or not isinstance(data.get('header_info'), dict):
        data['header_info'] = {}
    for key in ('product_name', 'model_number', 'brand', 'price_estimate'):
        data['header_info'].setdefault(key, '')
    data.setdefault('range_overview', '')
    if 'sales_arguments' not in data or not isinstance(data.get('sales_arguments'), list):
        data['sales_arguments'] = []
    else:
        data['sales_arguments'] = [
            re.sub(r'(\*\*|__)(.+?)\1', r'\2', str(x)).strip()
            for x in data['sales_arguments']
            if str(x).strip()
        ]
    if 'technical_specifications' not in data or not isinstance(data.get('technical_specifications'), dict):
        data['technical_specifications'] = {}
    if 'warranty_service' not in data or not isinstance(data.get('warranty_service'), dict):
        data['warranty_service'] = {}
    for key in ('period', 'coverage'):
        data['warranty_service'].setdefault(key, '')
    if 'seo_data' not in data or not isinstance(data.get('seo_data'), dict):
        data['seo_data'] = {}
    for key in ('generated_keywords', 'meta_title', 'meta_description', 'seo_long_description'):
        data['seo_data'].setdefault(key, '')
    return data


# ================= PROFORMA SCHEMA → PIS DATA =================

_IMAGE_OCR_EXTS = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff', '.tif')

# Known install locations for the tesseract binary. Checked in order when
# pytesseract can't find tesseract on PATH (e.g. Windows session that didn't
# refresh PATH after install). Empty strings filter out platform mismatches
# so the list works cross-platform without branching at import time.
_TESSERACT_FALLBACK_PATHS = tuple(p for p in (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    "/usr/bin/tesseract",
    "/usr/local/bin/tesseract",
    "/opt/homebrew/bin/tesseract",
) if p)


def _ensure_tesseract_cmd(pytesseract_mod) -> bool:
    """Make sure pytesseract knows where the tesseract binary is.

    On Windows the installer adds Tesseract-OCR to the system PATH, but
    long-lived shells (and CI runners) often inherit a stale PATH that
    doesn't include it. Probe the well-known install locations and pin
    `pytesseract.tesseract_cmd` to the first one that exists. Returns
    True when a usable binary is reachable, False otherwise."""
    try:
        pytesseract_mod.get_tesseract_version()
        return True
    except Exception:
        pass
    for cand in _TESSERACT_FALLBACK_PATHS:
        if os.path.exists(cand):
            pytesseract_mod.pytesseract.tesseract_cmd = cand
            try:
                pytesseract_mod.get_tesseract_version()
                return True
            except Exception:
                continue
    return False


def _ocr_image_text(fp: str) -> str:
    """Phase 5 Fix #8: run pytesseract on a PNG/JPG proforma so the
    grep-verification pass has source text to match against. Without this,
    every field on an image-only proforma falls back to the AI bucket and
    shows as a red pill even when Gemini read the value correctly.

    Soft dependency: pytesseract + a system-installed tesseract binary. If
    either is missing we return "" and the caller falls through to AI-only
    verification (current behaviour pre-Fix #8). Never raises."""
    try:
        import pytesseract  # type: ignore
        from PIL import Image
    except ImportError:
        logger.info("OCR skipped (pytesseract not installed) for %s",
                    os.path.basename(fp))
        return ""
    if not _ensure_tesseract_cmd(pytesseract):
        logger.info("OCR skipped (tesseract binary not found) for %s",
                    os.path.basename(fp))
        return ""
    try:
        with Image.open(fp) as img:
            text = pytesseract.image_to_string(img)
        # image_to_string is typed bytes|dict|str depending on output_type;
        # we always call with the default output_type=str, so coerce defensively.
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        elif not isinstance(text, str):
            return ""
        return text
    except Exception as e:
        logger.warning("OCR failed for %s: %s", os.path.basename(fp), e)
        return ""


def extract_raw_text_from_files(file_paths) -> str:
    """Phase 2.4: pull plain text out of every supported source file so the
    grep-verification pass can check whether the AI's claimed source_facts
    actually appear on the page. PDF goes through PyMuPDF; .docx through
    python-docx (lazy-imported, optional); image proformas go through
    pytesseract OCR when available (Phase 5 Fix #8) — when the OCR
    dependency is missing the image falls through silently and the file
    contributes no verification text, matching the legacy behaviour.
    """
    if not file_paths:
        return ""
    if isinstance(file_paths, str):
        file_paths = [file_paths]
    chunks = []
    for fp in file_paths:
        if not fp or not os.path.exists(fp):
            continue
        ext = os.path.splitext(fp)[1].lower()
        try:
            if ext == '.pdf':
                import fitz  # type: ignore
                doc = fitz.open(fp)  # type: ignore[attr-defined]
                for page in doc:
                    chunks.append(page.get_text("text"))
                doc.close()
            elif ext in ('.docx',):
                try:
                    import docx  # type: ignore  (python-docx)
                    d = docx.Document(fp)
                    for p in d.paragraphs:
                        if p.text:
                            chunks.append(p.text)
                except ImportError:
                    pass  # python-docx not installed; skip
            elif ext in _IMAGE_OCR_EXTS:
                ocr_text = _ocr_image_text(fp)
                if ocr_text:
                    chunks.append(ocr_text)
        except Exception as e:
            logger.warning("raw-text extraction failed for %s: %s", os.path.basename(fp), e)
    return "\n".join(chunks)


def _value_appears_in_text(value, raw_text_lower: str) -> bool:
    """Loose substring match: case-insensitive, whitespace-collapsed. For
    numeric values (like a model number or price), also tries a digit-only
    comparison so '12,000' matches '12000' and 'Rs. 12 000'."""
    if not value or not raw_text_lower:
        return False
    s = str(value).strip().lower()
    if len(s) < 2:
        return False
    if s in raw_text_lower:
        return True
    s_norm = re.sub(r'\s+', ' ', s)
    if s_norm in raw_text_lower:
        return True
    # Digit-only fallback for prices, model numbers with separators
    digits = re.sub(r'\D', '', s)
    if digits and len(digits) >= 3:
        raw_digits = re.sub(r'\D', '', raw_text_lower)
        if digits in raw_digits:
            return True
    return False


# Currency symbol / code → ISO 4217 mapping.
_CURRENCY_PATTERNS = [
    (re.compile(r'\bMUR\b', re.IGNORECASE), 'MUR'),
    (re.compile(r'\bRs\.?\b', re.IGNORECASE), 'MUR'),
    (re.compile(r'₨'), 'MUR'),
    (re.compile(r'\bUSD\b', re.IGNORECASE), 'USD'),
    (re.compile(r'\$'), 'USD'),
    (re.compile(r'\bEUR\b', re.IGNORECASE), 'EUR'),
    (re.compile(r'€'), 'EUR'),
    (re.compile(r'\bGBP\b', re.IGNORECASE), 'GBP'),
    (re.compile(r'£'), 'GBP'),
    (re.compile(r'\bINR\b', re.IGNORECASE), 'INR'),
    (re.compile(r'₹'), 'INR'),
    (re.compile(r'\b(RMB|CNY)\b', re.IGNORECASE), 'CNY'),
    (re.compile(r'¥'), 'CNY'),
    (re.compile(r'\bAED\b', re.IGNORECASE), 'AED'),
    (re.compile(r'\bSGD\b', re.IGNORECASE), 'SGD'),
    (re.compile(r'\bZAR\b', re.IGNORECASE), 'ZAR'),
]


def parse_price_currency(price_str: str) -> dict:
    """Phase 2.4: extract currency code + numeric amount from a free-form
    price string. Mauritian rupee (MUR / Rs / ₨) is the home currency —
    everything else gets `is_foreign=True` so the UI can flag it for accounts.
    Returns `{'amount': str, 'currency': str, 'is_foreign': bool}`."""
    out = {'amount': '', 'currency': '', 'is_foreign': False}
    if not price_str or not isinstance(price_str, str):
        return out
    s = price_str.strip()
    for pattern, code in _CURRENCY_PATTERNS:
        if pattern.search(s):
            out['currency'] = code
            break
    m = re.search(r'[\d][\d,\s]*(?:\.\d+)?', s)
    if m:
        out['amount'] = re.sub(r'[\s,]', '', m.group(0))
    out['is_foreign'] = out['currency'] not in ('', 'MUR')
    return out


def _normalize_mur_price(price_str: str, parsed: dict) -> str:
    """If the price was detected as MUR (or carries no currency at all and a
    plain amount), return a canonical 'Rs N,NNN' / 'Rs N,NNN.NN' form so the
    UI is consistent. Foreign currencies and unparseable strings are returned
    unchanged so we don't drop information the reviewer needs."""
    if not parsed or parsed.get('is_foreign'):
        return price_str
    amount = parsed.get('amount') or ''
    if not amount:
        return price_str
    try:
        n = float(amount)
    except (ValueError, TypeError):
        return price_str
    formatted = f"{int(n):,}" if n.is_integer() else f"{n:,.2f}"
    return f"Rs {formatted}"


def proforma_to_pis_data(product_obj: dict, raw_text: str | None = None,
                         source_files: list | None = None) -> dict:
    """Flatten the Phase-2 source_facts / ai_enriched_details schema into the
    legacy `pis_data` structure consumed by the UI and version system.

    Keeps the original nodes (`source_facts`, `ai_enriched_details`,
    `variants`) inside pis_data so the verify UI can mark AI-enriched fields
    with a sparkle indicator. Also writes a flat `_field_origins` map so
    template logic doesn't need to walk nested objects.

    Phase 2.4 — when `raw_text` is provided, runs a deterministic grep pass
    on every claimed source_facts value and tags origins as one of:
        verified    — claimed in source_facts AND found in raw doc text
        discrepancy — claimed in source_facts but NOT found (likely AI mis-read)
        inferred    — value came from inferred_specs
        ai          — narrative composed by the model
    Without raw_text, falls back to the legacy {'source','ai'} two-state.
    """
    if not isinstance(product_obj, dict):
        return {}

    src = product_obj.get('source_facts') or {}
    ai  = product_obj.get('ai_enriched_details') or {}
    variants = product_obj.get('variants') or []

    documented_specs = src.get('documented_specs') if isinstance(src.get('documented_specs'), dict) else {}
    inferred_specs   = ai.get('inferred_specs')   if isinstance(ai.get('inferred_specs'),   dict) else {}

    merged_specs = {}
    if isinstance(documented_specs, dict):
        merged_specs.update(documented_specs)
    if isinstance(inferred_specs, dict):
        for k, v in inferred_specs.items():
            if k not in merged_specs:
                merged_specs[k] = v

    seo = ai.get('seo_data') or {}

    pis = {
        'header_info': {
            'product_name':  src.get('product_name')  or '',
            'model_number':  src.get('model_number')  or '',
            'brand':         src.get('brand')         or '',
            'price_estimate':src.get('price_estimate')or '',
        },
        'range_overview': ai.get('range_overview') or '',
        'sales_arguments': ai.get('sales_arguments') if isinstance(ai.get('sales_arguments'), list) else [],
        'technical_specifications': merged_specs,
        'warranty_service': {
            'period':   src.get('warranty_period')   or '',
            'coverage': src.get('warranty_coverage') or '',
        },
        'seo_data': {
            'generated_keywords':   seo.get('generated_keywords')   or '',
            'meta_title':           seo.get('meta_title')           or '',
            'meta_description':     seo.get('meta_description')     or '',
            'seo_long_description': seo.get('seo_long_description') or '',
        },
        'found_image_url': ai.get('found_image_url'),
        'variants': variants,
        'source_facts': src,
        'ai_enriched_details': ai,
    }

    # ── 4-state origin classification ─────────────────────────────────────
    raw_lower = (raw_text or '').lower()
    have_text = bool(raw_lower)

    def _classify_source(value) -> str:
        """For a field claimed in source_facts: did the value appear on the page?

        Strict-fact rule: only values literally present in the Proforma's raw
        text qualify as facts. If we couldn't extract raw text (image-only
        PDF, etc.), the AI's source claim is unverifiable, so it falls into
        the AI-generated bucket rather than a presumed fact."""
        if not value:
            return 'ai'      # empty source value = no claim, treat as ai
        if not have_text:
            return 'ai'      # no raw text to verify against → not a fact
        return 'verified' if _value_appears_in_text(value, raw_lower) else 'discrepancy'

    origins = {
        'header_info.product_name':  _classify_source(src.get('product_name'))  if src.get('product_name')  else 'ai',
        'header_info.model_number':  _classify_source(src.get('model_number'))  if src.get('model_number')  else 'ai',
        'header_info.brand':         _classify_source(src.get('brand'))         if src.get('brand')         else 'ai',
        'header_info.price_estimate':_classify_source(src.get('price_estimate'))if src.get('price_estimate')else 'ai',
        'range_overview':            'ai',
        'sales_arguments':           'ai',
        'warranty_service.period':   _classify_source(src.get('warranty_period'))   if src.get('warranty_period')   else 'ai',
        'warranty_service.coverage': _classify_source(src.get('warranty_coverage')) if src.get('warranty_coverage') else 'ai',
        'seo_data.generated_keywords':   'ai',
        'seo_data.meta_title':           'ai',
        'seo_data.meta_description':     'ai',
        'seo_data.seo_long_description': 'ai',
    }

    spec_origins = {}
    for k, v in merged_specs.items():
        is_documented = k in documented_specs
        if not have_text:
            # Strict-fact rule: without raw text we can't grep-verify the
            # spec, so it doesn't qualify as a fact. Treat as inferred (which
            # renders as AI-generated on the UI).
            spec_origins[k] = 'inferred'
        elif not is_documented:
            spec_origins[k] = 'inferred'
        # A documented spec is "verified" if EITHER the spec key OR its
        # value appears on the page. This is permissive on purpose:
        # boolean-shaped values like "Yes" / "Standard" / "Available" rarely
        # appear in isolation, but their KEY ("Mesh back", "Bluetooth") will.
        # The check still catches pure hallucinations — when the AI invents
        # an entire spec, neither key nor value will be in the raw text.
        elif _value_appears_in_text(v, raw_lower) or _value_appears_in_text(k, raw_lower):
            spec_origins[k] = 'verified'
        else:
            spec_origins[k] = 'discrepancy'

    pis['_field_origins'] = origins
    pis['_spec_origins'] = spec_origins

    # Confidence scores (Phase 2.4) — surfaced from ai_enriched_details if AI
    # provided them. Used by the UI to colour the sparkle by certainty.
    confidence = ai.get('confidence_scores')
    if isinstance(confidence, dict):
        pis['_confidence'] = confidence

    # Currency normalisation (Phase 2.4) — flag foreign-currency prices for
    # the accounts team. Phase 2.5: also rewrite local-currency prices into
    # the canonical "Rs N,NNN" form so the UI doesn't have to do per-render
    # formatting and stays consistent across imports.
    pis['_price_meta'] = parse_price_currency(pis['header_info']['price_estimate'])
    pis['header_info']['price_estimate'] = _normalize_mur_price(
        pis['header_info']['price_estimate'], pis['_price_meta']
    )

    # Phase 2.5: source-file references for the inline viewer on
    # verify_marketing.html. Stored as web-relative paths (e.g.
    # "uploads/foo.pdf") so the template can pass them to url_for('static').
    if source_files:
        rel = []
        for fp in source_files:
            if not fp:
                continue
            base = os.path.basename(str(fp))
            if base:
                rel.append(f"uploads/{base}")
        if rel:
            pis['_source_files'] = rel

    return pis


def classify_flat_pis_origins(
    pis_data: dict,
    raw_text: str | None,
    web_context: str | None = None,
) -> tuple[dict, dict]:
    """Produce (_field_origins, _spec_origins) maps for a FLAT pis_data shape
    — the one the bulk wizard's content task fills in via generate_pis_data,
    which doesn't split into source_facts / ai_enriched_details.

    When `web_context` is provided (the frozen Brave supplier-page text the
    generator saw), classification is 4-state:
        verified      — value appears in the Proforma raw text
        web_grounded  — value appears in the Brave web_context
        inferred      — spec from the AI's inferred_specs path (legacy fallback)
        hallucinated  — value found in NEITHER source (AI invented it)

    Without web_context, falls back to legacy 3-state ('verified' / 'ai' /
    'inferred') so any caller that hasn't been updated keeps working.

    Why: lets the eval harness count hallucinations directly instead of
    lumping web-grounded specs and invented specs into the same 'inferred'
    bucket. Narrative fields (range_overview, sales_arguments, seo_*) stay
    'ai' here — they're paragraphs the grep can't usefully match; Layer 3
    LLM judging covers them.
    """
    if not isinstance(pis_data, dict):
        return ({}, {})

    raw_lower = (raw_text or '').lower()
    web_lower = (web_context or '').lower()
    have_text = bool(raw_lower)
    have_web = bool(web_lower)
    new_mode = have_web  # 4-state only when there's web text to ground against

    def _classify_field(value) -> str:
        if not value:
            return 'ai'
        if have_text and _value_appears_in_text(value, raw_lower):
            return 'verified'
        if new_mode:
            if _value_appears_in_text(value, web_lower):
                return 'web_grounded'
            return 'hallucinated'
        return 'ai'

    header = pis_data.get('header_info') or {}
    warranty = pis_data.get('warranty_service') or {}
    specs = pis_data.get('technical_specifications') or {}

    field_origins = {
        'header_info.product_name':   _classify_field(header.get('product_name')),
        'header_info.model_number':   _classify_field(header.get('model_number')),
        'header_info.brand':          _classify_field(header.get('brand')),
        'header_info.price_estimate': _classify_field(header.get('price_estimate')),
        'warranty_service.period':    _classify_field(warranty.get('period')),
        'warranty_service.coverage':  _classify_field(warranty.get('coverage')),
        'range_overview':             'ai',
        'sales_arguments':            'ai',
        'seo_data.generated_keywords':   'ai',
        'seo_data.meta_title':           'ai',
        'seo_data.meta_description':     'ai',
        'seo_data.seo_long_description': 'ai',
    }

    spec_origins = {}
    if isinstance(specs, dict):
        for k, v in specs.items():
            if have_text and (_value_appears_in_text(v, raw_lower) or _value_appears_in_text(k, raw_lower)):
                spec_origins[k] = 'verified'
            elif new_mode and (_value_appears_in_text(v, web_lower) or _value_appears_in_text(k, web_lower)):
                spec_origins[k] = 'web_grounded'
            elif new_mode:
                spec_origins[k] = 'hallucinated'
            else:
                spec_origins[k] = 'inferred'

    return (field_origins, spec_origins)


# ================= FORBIDDEN WORDS =================
# Storage / normalization / category-merge moved to utils.forbidden_words.
# Re-exported here so existing `from helpers import ...` callers keep working
# without a flag-day update across blueprints. The `as name` form marks each
# import as an intentional re-export so type-checkers don't flag it unused.
from utils.forbidden_words import (
    GLOBAL_CATEGORY_KEY as GLOBAL_CATEGORY_KEY,
    VALID_SEVERITIES as VALID_SEVERITIES,
    load_forbidden_words as load_forbidden_words,
    save_forbidden_words as save_forbidden_words,
    get_forbidden_words_for_category as get_forbidden_words_for_category,
    get_forbidden_words_flat as get_forbidden_words_flat,
)


