"""
Shared helper functions used across Flask blueprints.
"""
import os
import re
import copy
import json

from flask import session, current_app
from sqlalchemy.orm.attributes import flag_modified

from model import db, User, Product, ProductVersion, FieldChangeLog
from utils.validation import validate_pis_data, validate_spec_data


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
    """
    try:
        # Validate JSONB structure and log any schema warnings (non-blocking)
        if product.pis_data:
            ok, warnings = validate_pis_data(product.pis_data)
            if not ok or warnings:
                print(f"⚠️  pis_data schema warnings for product {product.id}: {warnings}")
        if product.spec_data:
            ok, warnings = validate_spec_data(product.spec_data)
            if not ok or warnings:
                print(f"⚠️  spec_data schema warnings for product {product.id}: {warnings}")

        last_version = ProductVersion.query.filter_by(
            product_id=product.id
        ).order_by(ProductVersion.version_num.desc()).first()
        next_num = (last_version.version_num + 1) if last_version else 1

        try:
            user_id = session.get('user_id')
        except RuntimeError:
            user_id = None

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
                label=label,
                is_major=False,
            )

        db.session.add(version)
        db.session.commit()
        print(f"📸 Version {next_num} ({'major' if version.is_major else 'minor'}) saved for product {product.id}: {label}")
    except Exception as e:
        db.session.rollback()
        print(f"❌ Failed to save version: {e}")


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


def diff_and_log(product_id, old_data, new_data, prefix='', _version_num=None):
    """Compare two dicts recursively and log only actual field edits."""
    user_id = session.get('user_id')

    if _version_num is None:
        latest = ProductVersion.query.filter_by(product_id=product_id).order_by(ProductVersion.version_num.desc()).first()
        _version_num = latest.version_num if latest else 1

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
                    db.session.add(FieldChangeLog(
                        product_id=product_id, user_id=user_id, field_name=field_name,
                        old_value=None,
                        new_value=('Added: ' + '; '.join(str(a) for a in added))[:2000],
                        version_num=_version_num
                    ))
                if removed:
                    db.session.add(FieldChangeLog(
                        product_id=product_id, user_id=user_id, field_name=field_name,
                        old_value=('Removed: ' + '; '.join(str(r) for r in removed))[:2000],
                        new_value=None, version_num=_version_num
                    ))
            except Exception as e:
                print(f"Diff log error: {e}")
            return
        if _normalize(old_data) == _normalize(new_data):
            return
        try:
            db.session.add(FieldChangeLog(
                product_id=product_id, user_id=user_id,
                field_name=_clean_field_name(prefix or 'root'),
                old_value=_format_value(old_data)[:2000] if _format_value(old_data) else None,
                new_value=_format_value(new_data)[:2000] if _format_value(new_data) else None,
                version_num=_version_num
            ))
        except Exception as e:
            print(f"Diff log error: {e}")
        return

    all_keys = set(list(old_data.keys()) + list(new_data.keys()))
    for key in all_keys:
        field = f"{prefix}.{key}" if prefix else key
        old_val = old_data.get(key)
        new_val = new_data.get(key)
        if isinstance(old_val, dict) and isinstance(new_val, dict):
            diff_and_log(product_id, old_val, new_val, prefix=field, _version_num=_version_num)
        elif isinstance(old_val, list) or isinstance(new_val, list):
            if _is_empty(old_val):
                continue
            diff_and_log(product_id, old_val or [], new_val or [], prefix=field, _version_num=_version_num)
        elif old_val != new_val:
            if _is_empty(old_val):
                continue
            if _normalize(old_val) == _normalize(new_val):
                continue
            try:
                db.session.add(FieldChangeLog(
                    product_id=product_id, user_id=user_id,
                    field_name=_clean_field_name(field),
                    old_value=_format_value(old_val)[:2000] if _format_value(old_val) else None,
                    new_value=_format_value(new_val)[:2000] if _format_value(new_val) else None,
                    version_num=_version_num
                ))
            except Exception as e:
                print(f"Diff log error: {e}")


def _diff_and_log_changes(product_id, old_data, new_data, prefix=''):
    """Compare two dicts and log ALL field changes (including empty→value)."""
    user_id = session.get('user_id')
    latest = ProductVersion.query.filter_by(product_id=product_id).order_by(ProductVersion.version_num.desc()).first()
    version_num = latest.version_num if latest else 1

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
                            db.session.add(FieldChangeLog(
                                product_id=product_id, user_id=user_id,
                                field_name=field_name, old_value=None,
                                new_value=('Added: ' + '; '.join(str(a) for a in added))[:2000],
                                version_num=version_num
                            ))
                        if removed:
                            db.session.add(FieldChangeLog(
                                product_id=product_id, user_id=user_id,
                                field_name=field_name,
                                old_value=('Removed: ' + '; '.join(str(r) for r in removed))[:2000],
                                new_value=None, version_num=version_num
                            ))
                    except Exception as e:
                        print(f"Diff log error: {e}")
                return
            if _normalize(old) == _normalize(new):
                return
            try:
                old_str = _format_value(old)
                new_str = _format_value(new)
                db.session.add(FieldChangeLog(
                    product_id=product_id, user_id=user_id,
                    field_name=_clean_field_name(path),
                    old_value=old_str[:2000] if old_str else None,
                    new_value=new_str[:2000] if new_str else None,
                    version_num=version_num
                ))
            except Exception as e:
                print(f"Diff log error: {e}")
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
                    db.session.add(FieldChangeLog(
                        product_id=product_id, user_id=user_id,
                        field_name=_clean_field_name(child_path),
                        old_value=old_str[:2000] if old_str else None,
                        new_value=new_str[:2000] if new_str else None,
                        version_num=version_num
                    ))
                except Exception as e:
                    print(f"Diff log error: {e}")

    _recurse(old_data, new_data, prefix)


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


# ================= FORBIDDEN WORDS =================

def _forbidden_words_file():
    return os.path.join(current_app.config['BASE_DIR'], 'data', 'forbidden_words.json')


def load_forbidden_words():
    try:
        with open(_forbidden_words_file(), 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_forbidden_words(data):
    fw_file = _forbidden_words_file()
    os.makedirs(os.path.dirname(fw_file), exist_ok=True)
    with open(fw_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_forbidden_words_for_category(category_3):
    return load_forbidden_words().get(category_3, [])
