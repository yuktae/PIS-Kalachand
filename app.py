import os
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import secrets
from flask_migrate import Migrate
import json
import re
import time
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect, url_for, Response, stream_with_context, session, flash, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from flask_wtf.csrf import CSRFProtect
from dotenv import load_dotenv
from model import db, Product, ProductHistory, User, ProductVersion, FieldChangeLog, Prompt, Job
import copy
from sqlalchemy.orm.attributes import flag_modified
import csv
import io
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright
import base64

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = secrets.token_hex(32)

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(app.static_folder, 'favicon.ico', mimetype='image/x-icon')

# Database Config — PostgreSQL
basedir = os.path.abspath(os.path.dirname(__file__))
database_url = os.environ.get('DATABASE_URL', 'postgresql://postgres:postgres@localhost:5432/pis_system')
# Fix: EasyPanel/Heroku may give postgres:// but SQLAlchemy requires postgresql://
if database_url.startswith('postgres://') and not database_url.startswith('postgresql://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_recycle': 280,
    'pool_pre_ping': True,
    'pool_size': 10,
    'max_overflow': 20,
}
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000  # 1 year cache for static files
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)
db.init_app(app)
migrate = Migrate(app, db)
csrf = CSRFProtect(app)

# ===== RESPONSE COMPRESSION =====
# Compress all text responses (HTML, JSON, CSS, JS) to reduce payload size
@app.after_request
def add_performance_headers(response):
    # Cache static assets aggressively
    if response.content_type and ('image' in response.content_type or 'font' in response.content_type):
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    elif response.content_type and 'text/html' in response.content_type:
        response.headers['Cache-Control'] = 'no-cache, must-revalidate'
    
    # Security & performance headers
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

# Ensure database and uploads directory exist (Runs even under Gunicorn)
with app.app_context():
    if not os.path.exists('instance'): os.makedirs('instance')
    
    # Create tables safely (handles existing tables on older SQLAlchemy)
    try:
        with db.engine.connect() as conn:
            db.metadata.create_all(bind=conn, checkfirst=True)
            conn.commit()
    except Exception:
        # Fallback: tables already exist, which is fine
        pass
    
    # Install PostgreSQL audit trigger for field-level change tracking
    # Uses advisory lock to prevent race condition when multiple Gunicorn workers boot simultaneously
    try:
        audit_trigger_path = os.path.join(basedir, 'audit_trigger.sql')
        if os.path.exists(audit_trigger_path):
            with open(audit_trigger_path, 'r') as f:
                audit_sql = f.read()
            with db.engine.connect() as conn:
                # Acquire advisory lock — only one worker installs at a time
                conn.execute(db.text("SELECT pg_advisory_lock(42424242)"))
                try:
                    # Execute the entire SQL file as one block (contains $$ delimited function)
                    conn.execute(db.text(audit_sql))
                    conn.commit()
                    print('✅ PostgreSQL audit trigger installed')
                finally:
                    conn.execute(db.text("SELECT pg_advisory_unlock(42424242)"))
    except Exception as e:
        print(f'ℹ️ Audit trigger note: {e}')
    
    if not os.path.exists(app.config['UPLOAD_FOLDER']): os.makedirs(app.config['UPLOAD_FOLDER'])
    
    # Seed default admin account on first run (wrapped in try/except for Gunicorn multi-worker safety)
    try:
        if not User.query.filter_by(role='admin').first():
            admin = User(
                username='admin',
                email='admin@jkalachand.com',
                role='admin',
                display_name='System Admin',
                is_active=True
            )
            admin.set_password('admin123')
            db.session.add(admin)
            db.session.commit()
            print('✅ Default admin account created: admin@jkalachand.com / admin123')
    except Exception:
        db.session.rollback()
        print('ℹ️ Admin account already exists or seed skipped (multi-worker race)')


# Import utility functions from utils package
from utils.image_processing import (
    extract_domain,
    search_google_api,
    clean_search_query,
    ai_validate_image,
    download_image_bytes,
    find_best_images,
    find_and_validate_image,
    find_image_simple,
    download_web_image
)
from utils.web_scraping import scrape_url_data, scrape_url_data_deep
from utils.ai_generation import (
    generate_pis_data,
    generate_comprehensive_spec_data,
    generate_bulk_pis_data,
    generate_specsheet_optimization,
    generate_ai_revision
)
from utils.pdf_processing import extract_specific_image, clear_pdf_cache
from utils.history import log_event


# ================= ASYNC PIS JOB QUEUE =================

pis_executor = ThreadPoolExecutor(max_workers=5)

# Job store is now backed by PostgreSQL via the Job model.


# ================= HELPERS: VERSION & DIFF =================

def get_current_username():
    """Get the display name or username of the currently logged in user.
    Uses session cache to avoid a DB query on every call."""
    # Fast path: use session-cached username (set at login)
    cached = session.get('username')
    if cached:
        return cached
    # Fallback: DB lookup (only if session is missing username)
    user_id = session.get('user_id')
    if user_id:
        user = User.query.get(user_id)
        if user:
            name = user.display_name or user.username
            session['username'] = name  # Cache for next time
            return name
    return session.get('role', 'System').capitalize()


def save_version_snapshot(product, label='Auto-save'):
    """Save a full snapshot of the product's current state."""
    try:
        last_version = ProductVersion.query.filter_by(product_id=product.id).order_by(ProductVersion.version_num.desc()).first()
        next_num = (last_version.version_num + 1) if last_version else 1
        
        # Safely get user_id — may be called from background thread without request context
        try:
            user_id = session.get('user_id')
        except RuntimeError:
            user_id = None
        
        version = ProductVersion(
            product_id=product.id,
            version_num=next_num,
            pis_data=copy.deepcopy(product.pis_data) if product.pis_data else None,
            spec_data=copy.deepcopy(product.spec_data) if product.spec_data else None,
            revision_data=copy.deepcopy(product.revision_data) if product.revision_data else None,
            workflow_stage=product.workflow_stage,
            created_by_id=user_id,
            label=label
        )
        db.session.add(version)
        db.session.commit()
        print(f"📸 Version {next_num} saved for product {product.id}: {label}")
    except Exception as e:
        print(f"❌ Failed to save version: {e}")


def _clean_field_name(raw_field):
    """Convert internal field paths to human-readable labels matching the UI."""
    FIELD_LABELS = {
        # === PIS (Marketing Review) ===
        # Header section
        'pis_data.header_info.product_name': 'Product Name',
        'pis_data.header_info.model_number': 'Model Number',
        'pis_data.header_info.brand': 'Brand',
        'pis_data.header_info.price_estimate': 'Price Estimate',
        # Description section
        'pis_data.range_overview': 'Description',
        # Sales section
        'pis_data.sales_arguments': 'Key Selling Points',
        # Specs section
        'pis_data.technical_specifications': 'Technical Specs',
        # Warranty section
        'pis_data.warranty_service.period': 'Warranty Period',
        'pis_data.warranty_service.coverage': 'Warranty Coverage',
        # SEO section (from PIS)
        'pis_data.seo_data.meta_title': 'Meta Title',
        'pis_data.seo_data.meta_description': 'Meta Description',
        'pis_data.seo_data.generated_keywords': 'SEO Keywords',
        
        # === SpecSheet (Web Team) ===
        # Header section
        'spec_data.header_info.product_name': 'Product Name',
        'spec_data.header_info.model_number': 'Model Number',
        'spec_data.header_info.brand': 'Brand',
        'spec_data.header_info.price_estimate': 'Price Estimate',
        # Short Description section
        'spec_data.customer_friendly_description': 'Short Description',
        'spec_data.refined_description': 'Refined Description',
        # Key Features section
        'spec_data.key_features': 'Key Features',
        # SEO section
        'spec_data.seo.meta_title': 'Meta Title',
        'spec_data.seo.meta_description': 'Meta Description',
        'spec_data.seo.keywords': 'SEO Keywords',
        'spec_data.internal_web_keywords': 'Web Keywords',
        # Classification section
        'spec_data.categories.category_1': 'Category A',
        'spec_data.categories.category_2': 'Category B',
        'spec_data.categories.category_3': 'Category C',
        # Specs section
        'spec_data.technical_specifications': 'Technical Specs',
        # Warranty section
        'spec_data.warranty.period': 'Warranty Period',
        'spec_data.warranty.coverage': 'Warranty Coverage',
        'spec_data.warranty_service.period': 'Warranty Period',
        'spec_data.warranty_service.coverage': 'Warranty Coverage',
    }
    if raw_field in FIELD_LABELS:
        return FIELD_LABELS[raw_field]
    # Handle dynamic tech spec keys like pis_data.technical_specifications.Processor
    if '.technical_specifications.' in raw_field:
        spec_key = raw_field.split('.technical_specifications.')[-1]
        return f"Spec: {spec_key.replace('_', ' ').title()}"
    # Fallback: take last segment and title-case it
    last = raw_field.split('.')[-1]
    return last.replace('_', ' ').title()


# Section grouping for changelog display
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


def _get_field_section(field_name):
    """Get the UI section a field belongs to."""
    if field_name in FIELD_SECTION_MAP:
        return FIELD_SECTION_MAP[field_name]
    if field_name.startswith('Spec: '):
        return 'Specs'
    return 'Other'


def _format_value(val):
    """Format a value for human-readable display in the changelog."""
    if val is None:
        return None
    if isinstance(val, list):
        # Show list items as bullet points
        items = [str(v).strip() for v in val if str(v).strip()]
        return '; '.join(items) if items else None
    if isinstance(val, dict):
        # Show dict as key: value pairs
        parts = [f"{k}: {v}" for k, v in val.items() if v]
        return '; '.join(parts) if parts else None
    return str(val)


def _normalize(val):
    """Normalize a value for comparison — strip strings, normalize whitespace."""
    if val is None:
        return None
    if isinstance(val, str):
        # Normalize newlines (\r\n → \n), collapse multiple spaces, strip
        s = val.replace('\r\n', '\n').replace('\r', '\n').strip()
        s = re.sub(r'[ \t]+', ' ', s)  # collapse spaces/tabs
        return s
    if isinstance(val, list):
        return [_normalize(x) for x in val]
    return val


def _is_empty(val):
    """Check if a value is effectively empty."""
    return val is None or val == '' or val == [] or val == {}


def diff_and_log(product_id, old_data, new_data, prefix='', _version_num=None):
    """Compare two dicts recursively and log only actual field edits."""
    user_id = session.get('user_id')
    
    # Look up current version number (only on first call, not recursive)
    if _version_num is None:
        latest = ProductVersion.query.filter_by(product_id=product_id).order_by(ProductVersion.version_num.desc()).first()
        _version_num = latest.version_num if latest else 1
    
    if old_data is None: old_data = {}
    if new_data is None: new_data = {}
    
    # --- Handle non-dict values (leaf nodes) ---
    if not isinstance(old_data, dict) or not isinstance(new_data, dict):
        # Skip initial population
        if _is_empty(old_data):
            return
        
        # Handle list comparison (Sales Arguments, Key Features, etc.)
        if isinstance(old_data, list) and isinstance(new_data, list):
            old_set = [_normalize(x) for x in old_data if x]
            new_set = [_normalize(x) for x in new_data if x]
            added = [x for x in new_set if x not in old_set]
            removed = [x for x in old_set if x not in new_set]
            
            if not added and not removed:
                return  # No real change
            
            field_name = _clean_field_name(prefix or 'root')
            try:
                if added:
                    entry = FieldChangeLog(
                        product_id=product_id, user_id=user_id,
                        field_name=field_name,
                        old_value=None,
                        new_value=('Added: ' + '; '.join(str(a) for a in added))[:2000],
                        version_num=_version_num
                    )
                    db.session.add(entry)
                if removed:
                    entry = FieldChangeLog(
                        product_id=product_id, user_id=user_id,
                        field_name=field_name,
                        old_value=('Removed: ' + '; '.join(str(r) for r in removed))[:2000],
                        new_value=None,
                        version_num=_version_num
                    )
                    db.session.add(entry)
            except Exception as e:
                print(f"Diff log error: {e}")
            return
        
        # Handle string comparison with normalization
        if _normalize(old_data) == _normalize(new_data):
            return  # Same after stripping whitespace
        
        try:
            entry = FieldChangeLog(
                product_id=product_id, user_id=user_id,
                field_name=_clean_field_name(prefix or 'root'),
                old_value=_format_value(old_data)[:2000] if _format_value(old_data) else None,
                new_value=_format_value(new_data)[:2000] if _format_value(new_data) else None,
                version_num=_version_num
            )
            db.session.add(entry)
        except Exception as e:
            print(f"Diff log error: {e}")
        return
    
    # --- Handle dict comparison (recurse into keys) ---
    all_keys = set(list(old_data.keys()) + list(new_data.keys()))
    for key in all_keys:
        field = f"{prefix}.{key}" if prefix else key
        old_val = old_data.get(key)
        new_val = new_data.get(key)
        
        if isinstance(old_val, dict) and isinstance(new_val, dict):
            diff_and_log(product_id, old_val, new_val, prefix=field, _version_num=_version_num)
        elif isinstance(old_val, list) or isinstance(new_val, list):
            # Delegate to the list handler above
            if _is_empty(old_val):
                continue
            diff_and_log(product_id, old_val or [], new_val or [], prefix=field, _version_num=_version_num)
        elif old_val != new_val:
            # Skip initial population
            if _is_empty(old_val):
                continue
            # Strip-compare strings
            if _normalize(old_val) == _normalize(new_val):
                continue
            try:
                entry = FieldChangeLog(
                    product_id=product_id, user_id=user_id,
                    field_name=_clean_field_name(field),
                    old_value=_format_value(old_val)[:2000] if _format_value(old_val) else None,
                    new_value=_format_value(new_val)[:2000] if _format_value(new_val) else None,
                    version_num=_version_num
                )
                db.session.add(entry)
            except Exception as e:
                print(f"Diff log error: {e}")


def _diff_and_log_changes(product_id, old_data, new_data, prefix=''):
    """
    Compare two dicts and log ALL field changes (including empty→value).
    Unlike diff_and_log, this does NOT skip initial population — it logs
    every difference so the changelog accurately reflects what changed.
    """
    user_id = session.get('user_id')
    
    # Get version number
    latest = ProductVersion.query.filter_by(product_id=product_id).order_by(ProductVersion.version_num.desc()).first()
    version_num = latest.version_num if latest else 1
    
    if old_data is None: old_data = {}
    if new_data is None: new_data = {}
    
    def _recurse(old, new, path):
        if not isinstance(old, dict) or not isinstance(new, dict):
            # Leaf comparison
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
            
            # String/scalar comparison
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
        
        # Dict comparison — recurse
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


# ================= ROUTES =================

@app.route('/')
def login():
    if session.get('user_id'):
        role = session.get('role')
        if role == 'admin': return redirect(url_for('admin_users'))
        if role == 'marketing': return redirect(url_for('dashboard_marketing'))
        if role == 'director': return redirect(url_for('dashboard_director'))
        if role == 'web': return redirect(url_for('dashboard_web'))
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login_post():
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')
    
    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        flash('Invalid email or password.', 'error')
        return redirect(url_for('login'))
    if not user.is_active:
        flash('Your account has been deactivated. Contact admin.', 'error')
        return redirect(url_for('login'))
    
    session.permanent = True
    session['user_id'] = user.id
    session['username'] = user.display_name or user.username
    session['role'] = user.role

    if user.role == 'admin': return redirect(url_for('admin_users'))
    if user.role == 'marketing': return redirect(url_for('dashboard_marketing'))
    if user.role == 'director': return redirect(url_for('dashboard_director'))
    if user.role == 'web': return redirect(url_for('dashboard_web'))
    return redirect(url_for('login'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# --- DASHBOARDS ---

@app.route('/dashboard/marketing')
def dashboard_marketing():
    if session.get('role') != 'marketing': return redirect(url_for('login'))
    
    # 1. Only fetch products in active marketing stages (not ALL products)
    approved_stages = ['ready_for_web', 'specsheet_draft', 'pending_director_spec', 'web_changes_requested', 'finalized']
    marketing_stages = ['marketing_draft', 'marketing_in_progress', 'marketing_changes_requested', 'pending_director_pis'] + approved_stages
    
    active_pipeline = Product.query.filter(
        Product.workflow_stage.in_(marketing_stages)
    ).order_by(Product.created_at.desc()).all()
    
    # 2. Calculate metrics from the already-filtered list (no extra queries)
    metrics = {
        'total_active': len(active_pipeline),
        'drafts': sum(1 for p in active_pipeline if p.workflow_stage == 'marketing_draft'),
        'changes': sum(1 for p in active_pipeline if p.workflow_stage == 'marketing_changes_requested'),
        'need_review': sum(1 for p in active_pipeline if p.workflow_stage == 'pending_director_pis'),
        'in_process': sum(1 for p in active_pipeline if p.workflow_stage == 'marketing_in_progress'),
        'approved': sum(1 for p in active_pipeline if p.workflow_stage in approved_stages)
    }
    
    return render_template('dashboard_marketing.html', 
                         products=active_pipeline, 
                         metrics=metrics)

@app.route('/dashboard/history')
@app.route('/dashboard/marketing/history')
def history_marketing():
    if not session.get('role'): return redirect(url_for('login'))
    
    # ===== OPTIMIZED: Batch queries instead of N+1 =====
    # 1. Fetch all products in ONE query (lightweight — no JSON data needed for listing)
    all_products = Product.query.order_by(Product.created_at.desc()).all()
    product_ids = [p.id for p in all_products]
    
    # 2. Batch-fetch ALL history events in ONE query, keyed by product_id
    all_history = ProductHistory.query.filter(
        ProductHistory.product_id.in_(product_ids)
    ).order_by(ProductHistory.timestamp.desc()).all() if product_ids else []
    
    history_by_product = {}
    for event in all_history:
        history_by_product.setdefault(event.product_id, []).append(event)
    
    # 3. Batch-fetch LATEST field changes (limit per product via Python slicing)
    #    Only fetch the last 50 overall for the summary — detail is lazy-loaded via AJAX
    
    # Icon map for different event types
    ICON_MAP = {
        'Created': 'M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z',
        'Submitted': 'M12 19l9 2-9-18-9 18 9-2zm0 0v-8',
        'Approved': 'M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z',
        'Changes': 'M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z',
        'Updated': 'M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15',
        'Generated': 'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z',
        'Restored': 'M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15',
        'Image': 'M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z',
    }
    
    def get_icon(title):
        for key, icon in ICON_MAP.items():
            if key.lower() in title.lower():
                return icon
        return 'M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z'
    
    # Map workflow stages to filter-friendly labels
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
        # Get pre-fetched history events (already sorted desc)
        history_events = history_by_product.get(p.id, [])
        
        timeline = [{
            'date': event.timestamp.strftime('%Y-%m-%d'),
            'time': event.timestamp.strftime('%H:%M'),
            'title': event.action_title,
            'description': event.description or '',
            'actor': event.actor,
            'status': event.action_type or 'neutral',
            'icon': get_icon(event.action_title)
        } for event in history_events]
        
        # If no history exists, add a creation event from product date
        if not timeline:
            timeline.append({
                'date': p.created_at.strftime('%Y-%m-%d'),
                'time': p.created_at.strftime('%H:%M'),
                'title': 'PIS Draft Created',
                'description': 'Product data imported.',
                'actor': 'System',
                'status': 'neutral',
                'icon': ICON_MAP['Created']
            })
        
        # Determine current PIS status (human-readable)
        stage = p.workflow_stage or ''
        pis_approved_stages = ['ready_for_web', 'specsheet_draft', 'pending_director_spec', 'web_changes_requested', 'finalized']
        current_pis_status = 'Draft'
        if 'pending_director_pis' in stage: current_pis_status = 'Pending Review'
        elif 'marketing_changes_requested' in stage: current_pis_status = 'Changes Requested'
        elif any(s in stage for s in pis_approved_stages): current_pis_status = 'Approved'
        
        # Extract latest actor and event for main-view display
        latest_actor = timeline[0]['actor'] if timeline else 'System'
        latest_event = timeline[0]['title'] if timeline else 'Created'
        latest_date = timeline[0]['date'] if timeline else p.created_at.strftime('%Y-%m-%d')
        latest_time = timeline[0]['time'] if timeline else p.created_at.strftime('%H:%M')
        
        # Filter-friendly status label
        filter_status = STAGE_FILTER_MAP.get(stage, 'DRAFT PIS')
        
        products_with_history.append({
            'product': p,
            'pis_status': current_pis_status,
            'filter_status': filter_status,
            'latest_actor': latest_actor,
            'latest_event': latest_event,
            'latest_date': latest_date,
            'latest_time': latest_time,
            'timeline': timeline,
            'changelog': []  # Lazy-loaded via AJAX endpoint
        })
    
    products_json = json.dumps([{
        'id': item['product'].id,
        'model_name': item['product'].model_name,
        'brand': item['product'].pis_data.get('header_info', {}).get('brand', 'Unknown') if item['product'].pis_data else 'Unknown',
        'image_path': url_for('static', filename=item['product'].image_path) if item['product'].image_path else None,
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



@app.route('/dashboard/marketing/archive')
def marketing_archive():
    if session.get('role') != 'marketing': return redirect(url_for('login'))
    
    # Marketing archive should show all approved/finalized products
    approved_stages = ['finalized', 'ready_for_web', 'specsheet_draft', 'pending_director_spec', 'web_changes_requested']
    archived_products = Product.query.filter(Product.workflow_stage.in_(approved_stages)).order_by(Product.created_at.desc()).all()
    
    return render_template('archive_marketing.html', products=archived_products)


@app.route('/dashboard/director')
def dashboard_director():
    if session.get('role') != 'director': return redirect(url_for('login'))
    
    # 1. Fetch Action Items (these are needed for display)
    pending_pis = Product.query.filter_by(workflow_stage='pending_director_pis').all()
    pending_spec = Product.query.filter_by(workflow_stage='pending_director_spec').all()
    
    # 2. Fetch products relevant to the Director (exclude early marketing drafts)
    director_excluded = ['marketing_draft', 'marketing_in_progress']
    all_products = Product.query.filter(
        ~Product.workflow_stage.in_(director_excluded)
    ).order_by(Product.created_at.desc()).all()
    
    # 3. Calculate Metrics from the filtered list
    total_products = len(all_products)
    finalized_count = sum(1 for p in all_products if p.workflow_stage == 'finalized')
    approved_stages = ['finalized', 'ready_for_web', 'specsheet_draft', 'pending_director_spec', 'web_changes_requested']
    in_progress_count = sum(1 for p in all_products if p.workflow_stage not in (['pending_director_pis', 'pending_director_spec'] + approved_stages))
    
    metrics = {
        'total_products': total_products,
        'pending_reviews': len(pending_pis) + len(pending_spec),
        'finalized': finalized_count,
        'in_progress': in_progress_count
    }
    
    return render_template('dashboard_director.html', 
                         pending_pis=pending_pis, 
                         pending_spec=pending_spec,
                         all_products=all_products,
                         metrics=metrics)

@app.route('/dashboard/director/archive')
def director_archive():
    if session.get('role') != 'director': return redirect(url_for('login'))
    
    # Fetch only finalized/approved products for the archive
    # Stages: 'finalized' (Spec approved) or 'ready_for_web' (PIS approved but Spec pending, technically has PIS PDF)
    # Adjust list based on strictness. Here we show anything that has at least passed PIS approval.
    approved_stages = ['finalized', 'ready_for_web', 'specsheet_draft', 'pending_director_spec', 'web_changes_requested']
    archived_products = Product.query.filter(Product.workflow_stage.in_(approved_stages)).order_by(Product.created_at.desc()).all()
    
    return render_template('archive_director.html', products=archived_products)

@app.route('/dashboard/web')
def dashboard_web():
    # ---- ACCESS CONTROL ----
    if session.get('role') != 'web':
        return redirect(url_for('login'))

    # ---- FETCH TASKS FOR WEB TEAM ----
    # We fetch everything related to the web pipeline:
    # 1. New from PIS (ready_for_web)
    # 2. Sent back by Director (web_changes_requested)
    # 3. Drafts saved by web team (specsheet_draft)
    # 4. Sent for approval (pending_director_spec)
    # 5. Approved/Finalized (finalized)
    tasks = (
        Product.query
        .filter(Product.workflow_stage.in_([
            'ready_for_web',
            'web_changes_requested',
            'specsheet_draft',
            'pending_director_spec',
            'finalized'
        ]))
        .order_by(Product.created_at.desc())
        .all()
    )

    # ---- BUILD JSON-SAFE PRODUCT PAYLOAD ----
    products_json = []
    for p in tasks:
        products_json.append({
            "id": p.id,
            "model_name": p.model_name or "",
            "brand": (
                p.pis_data.get("header_info", {}).get("brand", "Unknown")
                if p.pis_data else "Unknown"
            ),
            "image": (
                url_for("static", filename=p.image_path)
                if p.image_path else ""
            ),
            "date": p.created_at.strftime("%d %b"),
            "stage": p.workflow_stage,
            "action_url": url_for("create_specsheet", product_id=p.id)
        })

    # ---- METRICS (SERVER-SIDE, TRUSTED) ----
    metrics = {
        "total_tasks": len(tasks),
        "new_specsheets": sum(1 for p in tasks if p.workflow_stage == "ready_for_web"),
        "changes_requested": sum(1 for p in tasks if p.workflow_stage == "web_changes_requested"),
        "need_review": sum(1 for p in tasks if p.workflow_stage == "pending_director_spec"),
        "approved": sum(1 for p in tasks if p.workflow_stage == "finalized"),
        "in_process": sum(1 for p in tasks if p.workflow_stage == "specsheet_draft"),
    }

    # ---- RENDER DASHBOARD ----
    return render_template(
        "dashboard_web.html",
        tasks=tasks,                 # used only for metrics/debug
        products_json=products_json, # used by Alpine (IMPORTANT)
        metrics=metrics
    )



@app.route('/dashboard/web/archive')
def web_archive():
    if session.get('role') != 'web': return redirect(url_for('login'))
    
    # Fetch finalized products that have completed the full SpecSheet cycle
    finalized_products = Product.query.filter_by(workflow_stage='finalized').order_by(Product.created_at.desc()).all()
    
    return render_template('archive_web.html', products=finalized_products)


@app.route('/dashboard/web/forbidden-words')
def web_forbidden_words():
    if session.get('role') != 'web':
        return redirect(url_for('login'))
    
    # Use Magento API categories (falls back to static JSON automatically)
    try:
        from utils.magento_api import get_category_tree
        category_tree = get_category_tree()
    except Exception:
        from utils.category_classifier import load_categories
        raw_categories = load_categories()
        category_tree = {}
        for cat in raw_categories:
            a, b, c = cat['cat_A'], cat['cat_B'], cat['cat_C']
            if a not in category_tree:
                category_tree[a] = {}
            if b not in category_tree[a]:
                category_tree[a][b] = []
            if c not in category_tree[a][b]:
                category_tree[a][b].append(c)
    
    return render_template('forbidden_words.html', category_tree=category_tree)


@app.route('/create', methods=['GET', 'POST'])
def create_pis():
    if request.method == 'GET':
        return render_template('create.html')
    
    if request.method == 'POST':
        model_name = request.form.get('model_name')
        supplier_url = request.form.get('supplier_url')
        ai_files = request.files.getlist('ai_document')
        
        # --- NEW: Capture toggle value ---
        # Toggle is 'on' if checked, otherwise None
        contains_images = request.form.get('contains_images') == 'on'
        
        # Save all uploaded files and collect their paths
        ai_filepaths = []
        for ai_file in ai_files:
            if ai_file and ai_file.filename:
                filename = secure_filename(ai_file.filename)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                ai_file.save(filepath)
                ai_filepaths.append(filepath)

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

                # When toggle is ON: PDF first, then web fallback
                # When toggle is OFF: Web first (AI url → Google)
                if contains_images and ai_filepaths:
                    # --- PDF FIRST: User says images are in the document ---
                    yield json.dumps({"progress": 55, "message": "Scanning PDF for product image..."}) + "\n"
                    yield " " + "\n"  # Heartbeat
                    extracted_image_path = extract_specific_image(ai_filepaths[0], model_name, app.config['UPLOAD_FOLDER'])
                    yield " " + "\n"  # Heartbeat

                    # Fallback to web if PDF scan found nothing
                    if not extracted_image_path:
                        yield json.dumps({"progress": 65, "message": "PDF scan found nothing, trying web..."}) + "\n"
                        ai_found_url = ai_data.get('found_image_url')
                        if ai_found_url and ai_found_url.startswith('http'):
                            extracted_image_path = download_web_image(ai_found_url, model_name, app.config['UPLOAD_FOLDER'])

                    if not extracted_image_path:
                        header = ai_data.get('header_info', {})
                        brand = header.get('brand', '')
                        m_num = header.get('model_number', '')
                        p_name = header.get('product_name', '')
                        q_parts = []
                        if brand: q_parts.append(brand)
                        if p_name: q_parts.append(p_name)
                        if m_num and (any(c.isalpha() for c in m_num) or '-' in m_num):
                            if m_num not in (p_name or ''):
                                q_parts.append(m_num)
                        full_str = " ".join(q_parts)
                        unique_words = []
                        [unique_words.append(x) for x in full_str.split() if x.lower() not in [y.lower() for y in unique_words]]
                        rich_query = " ".join(unique_words) if q_parts else model_name
                        yield " " + "\n"
                        public_url = find_and_validate_image(rich_query, supplier_url)
                        if public_url:
                            extracted_image_path = download_web_image(public_url, model_name, app.config['UPLOAD_FOLDER'])

                else:
                    # --- WEB FIRST: No toggle, use online search ---
                    ai_found_url = ai_data.get('found_image_url')
                    if ai_found_url and ai_found_url.startswith('http'):
                        yield json.dumps({"progress": 55, "message": "AI found a product image — downloading..."}) + "\n"
                        extracted_image_path = download_web_image(ai_found_url, model_name, app.config['UPLOAD_FOLDER'])

                    if not extracted_image_path:
                        yield json.dumps({"progress": 60, "message": "Searching Google Images..."}) + "\n"
                        header = ai_data.get('header_info', {})
                        brand = header.get('brand', '')
                        m_num = header.get('model_number', '')
                        p_name = header.get('product_name', '')
                        q_parts = []
                        if brand: q_parts.append(brand)
                        if p_name: q_parts.append(p_name)
                        if m_num and (any(c.isalpha() for c in m_num) or '-' in m_num):
                            if m_num not in (p_name or ''):
                                q_parts.append(m_num)
                        full_str = " ".join(q_parts)
                        unique_words = []
                        [unique_words.append(x) for x in full_str.split() if x.lower() not in [y.lower() for y in unique_words]]
                        rich_query = " ".join(unique_words) if q_parts else model_name
                        yield " " + "\n"
                        public_url = find_and_validate_image(rich_query, supplier_url)
                        if public_url:
                            yield json.dumps({"progress": 70, "message": "Downloading Image..."}) + "\n"
                            extracted_image_path = download_web_image(public_url, model_name, app.config['UPLOAD_FOLDER'])

                yield " " + "\n"  # Heartbeat

                # --- LAST RESORT: DuckDuckGo simple search (no AI validation) ---
                if not extracted_image_path:
                    yield json.dumps({"progress": 80, "message": "Trying DuckDuckGo fallback search..."}) + "\n"
                    yield " " + "\n"
                    header = ai_data.get('header_info', {})
                    brand = header.get('brand', '')
                    p_name = header.get('product_name', '')
                    simple_query = f"{brand} {p_name}".strip() or model_name
                    simple_url = find_image_simple(simple_query, supplier_url)
                    if simple_url:
                        yield json.dumps({"progress": 85, "message": "Found image via DuckDuckGo!"}) + "\n"
                        extracted_image_path = download_web_image(simple_url, model_name, app.config['UPLOAD_FOLDER'])

                if extracted_image_path:
                    yield json.dumps({"progress": 90, "message": "Visual Acquired."}) + "\n"
                else:
                    yield json.dumps({"progress": 90, "message": "No visual found."}) + "\n"

                with app.app_context():
                    new_product = Product(
                        model_name=model_name, 
                        pis_data=ai_data,
                        image_path=extracted_image_path,
                        seo_keywords=ai_data.get('seo_data', {}).get('generated_keywords', ''),
                        workflow_stage='marketing_draft'
                    )
                    db.session.add(new_product)
                    db.session.commit()
                    log_event(new_product.id, get_current_username(), 'New Product Added', 'A new product information sheet was created from a single import.', 'neutral')
                    save_version_snapshot(new_product, label='Initial version')
                    
                    yield json.dumps({"progress": 100, "message": "Done!", "redirect": url_for('review_pis_marketing', product_id=new_product.id)}) + "\n"

            except Exception as e:
                yield json.dumps({"error": str(e)}) + "\n"

        return Response(stream_with_context(generate_updates()), mimetype='application/x-ndjson')
    



@app.route('/create_bulk', methods=['GET', 'POST'])
def create_bulk():
    if request.method == 'GET':
        return render_template('create_bulk.html')

    if request.method == 'POST':
        supplier_url = request.form.get('supplier_url')
        ai_files = request.files.getlist('ai_document')
        
        # --- Capture toggle value ---
        contains_images = request.form.get('contains_images') == 'on'
        
        # --- Capture product filter (specific products to extract) ---
        product_filter = request.form.get('product_filter', '').strip()
        
        # Save all uploaded files and collect their paths
        ai_filepaths = []
        for ai_file in ai_files:
            if ai_file and ai_file.filename:
                filename = secure_filename(ai_file.filename)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                ai_file.save(filepath)
                ai_filepaths.append(filepath)
        
        if not ai_filepaths and not supplier_url:
            return "Please provide at least a document or a supplier URL.", 400

        # Keep first filepath for PDF scan fallback (if any files were uploaded)
        ai_filepath = ai_filepaths[0] if ai_filepaths else None

        def generate_bulk_updates():
            yield json.dumps({"progress": 10, "message": "Analyzing Invoice..."}) + "\n"
            
            site_data = {"text": "", "html": ""}
            if supplier_url:
                if not ai_filepaths:
                    # URL-only: use deep scraping
                    site_data = scrape_url_data_deep(supplier_url)
                else:
                    site_data = scrape_url_data(supplier_url)
            
            try:
                products_list = generate_bulk_pis_data(ai_filepaths, site_data, product_filter=product_filter)
                total_items = len(products_list)
                
                # Yield the list of product names to the frontend early
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

                with app.app_context():
                    processed_count = 0
                    for idx, p_data in enumerate(products_list):
                        header = p_data.get('header_info', {})
                        brand = header.get('brand', '')
                        model_id = header.get('model_number', '') 
                        prod_name = header.get('product_name', '')
                        
                        display_name = prod_name if prod_name else (model_id if model_id else f"Item_{idx+1}")
                        
                        processed_count += 1
                        current_progress = 20 + int((processed_count / total_items) * 75) 
                        
                        yield json.dumps({
                            "progress": current_progress, 
                            "message": f"Processing: {display_name}",
                            "item_update": {"name": display_name, "status": "searching"}
                        }) + "\n"

                        # --- Per-product try/except: one failure won't kill the batch ---
                        try:
                            query_parts = []
                            if brand: query_parts.append(brand)
                            if prod_name: query_parts.append(prod_name)
                            
                            is_real_model = model_id and (any(c.isalpha() for c in model_id) or '-' in model_id)
                            if is_real_model and (model_id not in (prod_name or '')):
                                query_parts.append(model_id)

                            seen_words = set()
                            unique_words = []
                            for w in " ".join(query_parts).split():
                                if w.lower() not in seen_words:
                                    unique_words.append(w)
                                    seen_words.add(w.lower())
                            
                            search_query = " ".join(unique_words) if unique_words else display_name
                            
                            extracted_image_path = None

                            # When toggle is ON: PDF first, then web fallback
                            # When toggle is OFF: Web first (AI url → Google)
                            if contains_images:
                                # --- PDF FIRST (only if a file was uploaded) ---
                                if ai_filepath:
                                    yield " " + "\n"
                                    extracted_image_path = extract_specific_image(ai_filepath, model_id, app.config['UPLOAD_FOLDER'])
                                yield " " + "\n"

                                # Fallback to web if PDF found nothing
                                if not extracted_image_path:
                                    ai_found_url = p_data.get('found_image_url')
                                    if ai_found_url and str(ai_found_url).startswith('http'):
                                        yield " " + "\n"
                                        extracted_image_path = download_web_image(ai_found_url, display_name, app.config['UPLOAD_FOLDER'])
                                        yield " " + "\n"

                                if not extracted_image_path:
                                    yield " " + "\n"
                                    image_url = find_and_validate_image(search_query, supplier_url)
                                    yield " " + "\n"
                                    if image_url:
                                        yield " " + "\n"
                                        extracted_image_path = download_web_image(image_url, display_name, app.config['UPLOAD_FOLDER'])
                                        yield " " + "\n"
                            else:
                                # --- WEB FIRST ---
                                ai_found_url = p_data.get('found_image_url')
                                if ai_found_url and str(ai_found_url).startswith('http'):
                                    yield " " + "\n"
                                    extracted_image_path = download_web_image(ai_found_url, display_name, app.config['UPLOAD_FOLDER'])
                                    yield " " + "\n"

                                if not extracted_image_path:
                                    yield " " + "\n"
                                    image_url = find_and_validate_image(search_query, supplier_url)
                                    yield " " + "\n"
                                    if image_url:
                                        yield " " + "\n"
                                        extracted_image_path = download_web_image(image_url, display_name, app.config['UPLOAD_FOLDER'])
                                        yield " " + "\n"

                            # --- LAST RESORT: DuckDuckGo simple search ---
                            if not extracted_image_path:
                                yield " " + "\n"
                                simple_url = find_image_simple(search_query, supplier_url)
                                if simple_url:
                                    extracted_image_path = download_web_image(simple_url, display_name, app.config['UPLOAD_FOLDER'])
                                yield " " + "\n"

                            new_product = Product(
                                model_name=display_name,
                                pis_data=p_data,
                                image_path=extracted_image_path, 
                                seo_keywords=p_data.get('seo_data', {}).get('generated_keywords', ''),
                                workflow_stage='marketing_draft'
                            )
                            db.session.add(new_product)
                            db.session.commit()
                            log_event(new_product.id, get_current_username(), 'New Product Added', 'This product was imported as part of a bulk extraction.', 'neutral')
                            save_version_snapshot(new_product, label='Initial version')

                            yield json.dumps({
                                "item_update": {"name": display_name, "status": "completed"}
                            }) + "\n"

                        except Exception as product_err:
                            print(f"⚠️ Bulk import error for '{display_name}': {product_err}")
                            # Save the product anyway (without image) so data isn't lost
                            try:
                                fallback_product = Product(
                                    model_name=display_name,
                                    pis_data=p_data,
                                    image_path=None,
                                    seo_keywords=p_data.get('seo_data', {}).get('generated_keywords', ''),
                                    workflow_stage='marketing_draft'
                                )
                                db.session.add(fallback_product)
                                db.session.commit()
                                log_event(fallback_product.id, get_current_username(), 'New Product Added', f'Imported via bulk extraction (product image could not be found automatically).', 'neutral')
                                save_version_snapshot(fallback_product, label='Initial version')
                            except Exception:
                                db.session.rollback()

                            yield json.dumps({
                                "item_update": {"name": display_name, "status": "completed"},
                                "message": f"Saved {display_name} (image skipped)"
                            }) + "\n"

                yield json.dumps({"progress": 100, "message": "Bulk Import Complete!", "redirect": url_for('dashboard_marketing')}) + "\n"
                
                # Free cached PDF images from memory
                clear_pdf_cache()
            
            except Exception as e:
                yield json.dumps({"error": str(e)}) + "\n"

        return Response(stream_with_context(generate_bulk_updates()), mimetype='application/x-ndjson')

# --- Compatibility Route ---
@app.route('/verify/<int:product_id>')
def old_verify_redirect(product_id):
    return redirect(url_for('review_pis_marketing', product_id=product_id))

# --- DATA NORMALIZATION HELPER ---
def _normalize_pis_data(data):
    """Ensure all required nested keys exist in PIS data before rendering templates.
    
    The AI may return different JSON structures, so we need to guarantee
    the template can safely access data.header_info.product_name etc.
    """
    if not data or not isinstance(data, dict):
        data = {}
    
    # Ensure header_info exists with all required sub-keys
    if 'header_info' not in data or not isinstance(data.get('header_info'), dict):
        data['header_info'] = {}
    for key in ('product_name', 'model_number', 'brand', 'price_estimate'):
        data['header_info'].setdefault(key, '')
    
    # Ensure other required top-level keys exist
    data.setdefault('range_overview', '')
    
    if 'sales_arguments' not in data or not isinstance(data.get('sales_arguments'), list):
        data['sales_arguments'] = []
    
    if 'technical_specifications' not in data or not isinstance(data.get('technical_specifications'), dict):
        data['technical_specifications'] = {}
    
    if 'warranty_service' not in data or not isinstance(data.get('warranty_service'), dict):
        data['warranty_service'] = {}
    for key in ('period', 'coverage'):
        data['warranty_service'].setdefault(key, '')
    
    # Ensure SEO data exists
    if 'seo_data' not in data or not isinstance(data.get('seo_data'), dict):
        data['seo_data'] = {}
    for key in ('generated_keywords', 'meta_title', 'meta_description', 'seo_long_description'):
        data['seo_data'].setdefault(key, '')
    
    return data

# --- REVIEW ROUTES ---
@app.route('/review/marketing/<int:product_id>', methods=['GET', 'POST'])
def review_pis_marketing(product_id):
    product = Product.query.get_or_404(product_id)
    
    if request.method == 'POST':
        # Capture old data BEFORE applying form changes
        # Use deepcopy of product.pis_data (which may have been auto-saved)
        # compared against what the FORM submits
        last_version = ProductVersion.query.filter_by(product_id=product.id).order_by(ProductVersion.version_num.desc()).first()
        old_pis = copy.deepcopy(last_version.pis_data) if last_version and last_version.pis_data else {}
        
        updated_data = product.pis_data or {}
        
        if 'header_info' not in updated_data: updated_data['header_info'] = {}
        updated_data['header_info']['product_name'] = request.form.get('product_name')
        updated_data['header_info']['model_number'] = request.form.get('model_number')
        updated_data['header_info']['brand'] = request.form.get('brand')
        updated_data['header_info']['price_estimate'] = request.form.get('price_estimate')
        
        updated_data['range_overview'] = request.form.get('range_overview')
        updated_data['sales_arguments'] = request.form.getlist('sales_arguments')
        
        spec_names = request.form.getlist('spec_name')
        spec_values = request.form.getlist('spec_value')
        updated_data['technical_specifications'] = dict(zip(spec_names, spec_values))
        
        if 'warranty_service' not in updated_data: updated_data['warranty_service'] = {}
        updated_data['warranty_service']['period'] = request.form.get('warranty_period')
        updated_data['warranty_service']['coverage'] = request.form.get('warranty_coverage')
        
        product.pis_data = updated_data
        
        # CRITICAL: Clear revision_data so accepted director changes don't reappear on reload
        # The accepted values are already merged into pis_data via the form inputs
        if product.revision_data:
            product.revision_data = None
        
        # CRITICAL: Flag the json fields as modified so SQLAlchemy saves them
        flag_modified(product, 'pis_data')
        flag_modified(product, 'revision_data')
        
        # Create version snapshot FIRST so diff_and_log picks up the new version number
        if request.form.get('action') == 'submit_director':
            save_version_snapshot(product, label='Submitted for Director review')
            product.workflow_stage = 'pending_director_pis'
            log_event(product.id, get_current_username(), 'Sent for Director Review', 'The product sheet has been sent to the Director for approval.', 'waiting')
            flash('Sent to the Director for review ✓')
        else:
            save_version_snapshot(product, label='Draft saved')
            if product.workflow_stage in ('marketing_draft', 'marketing_changes_requested'):
                product.workflow_stage = 'marketing_in_progress'
            log_event(product.id, get_current_username(), 'Draft Updated', 'The marketing team updated and saved changes to the product sheet.', 'neutral')
            flash('Draft saved successfully ✓')
        
        # Log field-level diffs (after snapshot so version_num is correct)
        # Use allow_initial=True so that changes from empty→value are also tracked
        _diff_and_log_changes(product.id, old_pis, updated_data, prefix='pis_data')
            
            
        db.session.commit()
        return redirect(url_for('dashboard_marketing'))
        
    return render_template('verify_marketing.html', product=product, data=_normalize_pis_data(product.pis_data))


@app.route('/review/director_pis/<int:product_id>', methods=['GET', 'POST'])
def review_director_pis(product_id):
    product = Product.query.get_or_404(product_id)
    if request.method == 'POST':
        action = request.form.get('director_action')
        
        # Compare against the LAST VERSION SNAPSHOT (auto-saves may have updated product.pis_data)
        last_version = ProductVersion.query.filter_by(product_id=product.id).order_by(ProductVersion.version_num.desc()).first()
        old_pis = copy.deepcopy(last_version.pis_data) if last_version and last_version.pis_data else {}
        updated_data = product.pis_data or {}
        
        # Update Header Info if edited
        if request.form.get('product_name'):
            if 'header_info' not in updated_data: updated_data['header_info'] = {}
            updated_data['header_info']['product_name'] = request.form.get('product_name')
            updated_data['header_info']['model_number'] = request.form.get('model_number')
            updated_data['header_info']['brand'] = request.form.get('brand')
            updated_data['header_info']['price_estimate'] = request.form.get('price_estimate')
        
        # Update SHORT DESCRIPTION if edited
        if request.form.get('range_overview'):
            updated_data['range_overview'] = request.form.get('range_overview')
        
        # Update Sales Arguments if edited
        sales_args = request.form.getlist('sales_argument')
        if sales_args and any(arg.strip() for arg in sales_args):
            updated_data['sales_arguments'] = [arg.strip() for arg in sales_args if arg.strip()]
        
        # Update Technical Specifications if edited
        tech_spec_keys = request.form.getlist('tech_spec_key')
        tech_spec_values = request.form.getlist('tech_spec_value')
        if tech_spec_keys and tech_spec_values:
            updated_data['technical_specifications'] = dict(zip(tech_spec_keys, tech_spec_values))
        
        # Update Warranty if edited
        if request.form.get('warranty_period'):
            if 'warranty_service' not in updated_data: updated_data['warranty_service'] = {}
            updated_data['warranty_service']['period'] = request.form.get('warranty_period')
            updated_data['warranty_service']['coverage'] = request.form.get('warranty_coverage')
        
        # Save updated data
        product.pis_data = updated_data
        
        # CRITICAL: Flag the JSON field as modified so SQLAlchemy saves it
        flag_modified(product, 'pis_data')
        
        if action == 'review':
            # Updated Map with ALL sections
            comments_map = {
                'header_info': request.form.get('comment_header_info'),
                'range_overview': request.form.get('comment_range_overview'),
                'sales_arguments': request.form.get('comment_sales_arguments'),
                'technical_specifications': request.form.get('comment_technical_specifications'),
                'warranty_service': request.form.get('comment_warranty_service')
            }
            
            # FAST PATH: Save comments immediately, defer AI to background thread
            new_revisions = {}
            sections_to_revise = []
            
            for section, comment in comments_map.items():
                if comment and comment.strip():
                    original = product.pis_data.get(section)
                    new_revisions[section] = {
                        'comment': comment,
                        'original': original,
                        'ai_suggestion': None,  # Will be filled by background thread
                        'status': 'generating'   # Frontend shows spinner
                    }
                    sections_to_revise.append((section, original, comment.strip()))
            
            product.revision_data = new_revisions
            product.director_pis_comments = request.form.get('director_general_comments')
            product.workflow_stage = 'marketing_changes_requested'
            
            # Build a rich log description with section-specific comments
            comment_details = []
            section_labels = {
                'header_info': 'Header Info',
                'range_overview': 'Description',
                'sales_arguments': 'Sales Arguments',
                'technical_specifications': 'Tech Specs',
                'warranty_service': 'Warranty'
            }
            for section, comment in comments_map.items():
                if comment and comment.strip():
                    label = section_labels.get(section, section)
                    comment_details.append(f'{label}: "{comment.strip()[:80]}"')
            
            log_desc = f"Director requested changes on {len(new_revisions)} section(s):\n" + "\n".join(f"• {detail}" for detail in comment_details)
            
            general_comments = request.form.get('director_general_comments')
            if general_comments and general_comments.strip():
                log_desc += f'\n\nGeneral: "{general_comments.strip()[:100]}"'
            
            save_version_snapshot(product, label='Before Director requested changes')
            log_event(product.id, get_current_username(), 'Revisions Requested by Director', log_desc, 'action')
            
            # Commit immediately so the page can redirect fast
            db.session.commit()
            
            # Background AI generation
            if sections_to_revise:
                pid = product.id
                def _generate_revisions(app_ctx, product_id, sections):
                    with app_ctx:
                        try:
                            p = Product.query.get(product_id)
                            if not p or not p.revision_data:
                                return
                            rev = dict(p.revision_data)  # copy
                            for section, original, comment in sections:
                                try:
                                    ai_suggestion = generate_ai_revision(section, original, comment)
                                    rev[section]['ai_suggestion'] = ai_suggestion
                                    rev[section]['status'] = 'pending'
                                except Exception as e:
                                    print(f"⚠ AI revision failed for {section}: {e}")
                                    rev[section]['ai_suggestion'] = original  # fallback to original
                                    rev[section]['status'] = 'pending'
                            p.revision_data = rev
                            flag_modified(p, 'revision_data')
                            db.session.commit()
                            print(f"✅ Background AI revisions complete for product {product_id}")
                        except Exception as e:
                            print(f"❌ Background revision error: {e}")
                
                import threading
                t = threading.Thread(
                    target=_generate_revisions,
                    args=(app.app_context(), pid, sections_to_revise),
                    daemon=True
                )
                t.start()

        elif action == 'approve':
            print("\n" + "="*80)
            print("📋 DIRECTOR APPROVED PIS - FAST APPROVE + BACKGROUND SPECSHEET")
            print("="*80)
            
            # CRITICAL: Preserve image data before any modifications
            preserved_image_path = product.image_path
            preserved_additional_images = product.additional_images
            print(f"📸 Preserving image_path: {preserved_image_path}")
            print(f"📸 Preserving additional_images: {preserved_additional_images}")
            
            # FAST PATH: Create initial spec_data from PIS data immediately
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
                    'category_1': '',
                    'category_2': '',
                    'category_3': ''
                },
                '_spec_generating': True  # Flag for frontend to show generating state
            }
            product.spec_data = initial_spec_data
            product.workflow_stage = 'ready_for_web'
            product.revision_data = None
            
            # CRITICAL: Re-assert image fields to prevent any accidental clearing
            product.image_path = preserved_image_path
            product.additional_images = preserved_additional_images
            
            log_event(product.id, get_current_username(), 'PIS Approved ✓', 'The Director has approved this product sheet. The system is now generating the customer-facing specsheet.', 'success')
            save_version_snapshot(product, label='Approved by Director')
            
            # Commit immediately so the page redirects fast
            db.session.commit()
            
            # Background AI specsheet generation
            pid = product.id
            pis_data_copy = copy.deepcopy(product.pis_data)
            
            def _generate_specsheet_bg(app_ctx, product_id, pis_data):
                with app_ctx:
                    try:
                        print(f"🤖 [BG] Starting specsheet generation for product {product_id}...")
                        
                        # Load forbidden words
                        all_fw = load_forbidden_words()
                        combined_forbidden = list(set(w for words in all_fw.values() for w in words))
                        if combined_forbidden:
                            print(f"🚫 [BG] Enforcing {len(combined_forbidden)} forbidden words")
                        
                        spec_data_generated = generate_comprehensive_spec_data(pis_data, forbidden_words=combined_forbidden)
                        
                        # Sync from PIS
                        spec_data_generated['technical_specifications'] = pis_data.get('technical_specifications', {})
                        spec_data_generated['header_info'] = pis_data.get('header_info', {})
                        
                        # Remove generating flag
                        spec_data_generated.pop('_spec_generating', None)
                        
                        p = Product.query.get(product_id)
                        if p:
                            p.spec_data = spec_data_generated
                            flag_modified(p, 'spec_data')
                            save_version_snapshot(p, label='SpecSheet auto-generated')
                            db.session.commit()
                            print(f"✅ [BG] SpecSheet generated for product {product_id}")
                            if 'categories' in spec_data_generated:
                                print(f"   - Categories: {spec_data_generated['categories']}")
                    except Exception as e:
                        print(f"❌ [BG] Specsheet generation failed: {e}")
                        import traceback
                        traceback.print_exc()
                        # Remove the generating flag even on failure
                        try:
                            p = Product.query.get(product_id)
                            if p and p.spec_data:
                                sd = dict(p.spec_data)
                                sd.pop('_spec_generating', None)
                                p.spec_data = sd
                                flag_modified(p, 'spec_data')
                                db.session.commit()
                        except:
                            pass
            
            import threading
            t = threading.Thread(
                target=_generate_specsheet_bg,
                args=(app.app_context(), pid, pis_data_copy),
                daemon=True
            )
            t.start()
        
        # Log field-level diffs AFTER snapshot so version_num is correct
        _diff_and_log_changes(product.id, old_pis, updated_data, prefix='pis_data')
            
        db.session.commit()
        return redirect(url_for('dashboard_director'))
        
    return render_template('verify_director_pis.html', product=product, data=_normalize_pis_data(product.pis_data))


@app.route('/create_specsheet/<int:product_id>', methods=['GET', 'POST'])
def create_specsheet(product_id):
    product = Product.query.get_or_404(product_id)
    
    # Initialize spec_data if it doesn't exist (first time viewing)
    if not product.spec_data:
        # Use PIS sales_arguments as initial key_features
        initial_spec_data = {
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
        product.spec_data = initial_spec_data
        db.session.commit()
    
    # Valid list check for Alpine.js (ensures front-end doesn't break if data is missing)
    if not product.spec_data.get("key_features") or not isinstance(product.spec_data["key_features"], list):
        product.spec_data["key_features"] = product.spec_data.get("key_features", []) if isinstance(product.spec_data.get("key_features"), list) else []
        # Note: We don't overwrite with pis_data anymore to prevent manual edits being lost
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        # Capture old state from LAST VERSION SNAPSHOT for diff logging
        # (auto-save may have already updated product.spec_data / product.pis_data)
        last_version = ProductVersion.query.filter_by(product_id=product.id).order_by(ProductVersion.version_num.desc()).first()
        old_spec = copy.deepcopy(last_version.spec_data) if last_version and last_version.spec_data else {}
        old_pis = copy.deepcopy(last_version.pis_data) if last_version and last_version.pis_data else {}
        spec_data = product.spec_data or {}
        
        # Save Header Info (Cross-Sync with PIS)
        if 'header_info' not in spec_data: spec_data['header_info'] = {}
        if 'header_info' not in product.pis_data: product.pis_data['header_info'] = {}
        
        h_info = {
            'product_name': request.form.get('product_name'),
            'model_number': request.form.get('model_number'),
            'brand': request.form.get('brand'),
            'price_estimate': request.form.get('price_estimate')
        }
        spec_data['header_info'] = h_info
        product.pis_data['header_info'] = h_info # keep PIS in sync for PDF

        # Save New Fields
        spec_data['customer_friendly_description'] = request.form.get('customer_friendly_description')
        
        # Save Key Features (handle as list)
        features_raw = request.form.getlist('key_features')
        spec_data['key_features'] = [f.strip() for f in features_raw if f.strip()]
        
        # Save SEO Data
        if 'seo' not in spec_data: spec_data['seo'] = {}
        spec_data['seo']['meta_title'] = request.form.get('seo_meta_title')
        spec_data['seo']['meta_description'] = request.form.get('seo_meta_description')
        spec_data['seo']['keywords'] = request.form.get('seo_keywords')
        spec_data['internal_web_keywords'] = request.form.get('internal_web_keywords')
        
        # Save Categories (handle custom dropdown values)
        cat1 = request.form.get('category_1', '')
        cat2 = request.form.get('category_2', '')
        cat3 = request.form.get('category_3', '')
        # If user selected "Custom...", use the custom text input instead
        if cat1 == '__custom__':
            cat1 = request.form.get('category_1_custom', '').strip()
        if cat2 == '__custom__':
            cat2 = request.form.get('category_2_custom', '').strip()
        if cat3 == '__custom__':
            cat3 = request.form.get('category_3_custom', '').strip()
        if cat1:
            if 'categories' not in spec_data:
                spec_data['categories'] = {}
            spec_data['categories']['category_1'] = cat1
            spec_data['categories']['category_2'] = cat2
            spec_data['categories']['category_3'] = cat3
        
        # Save Technical Specifications (from JSON)
        tech_specs_json = request.form.get('technical_specifications')
        if tech_specs_json:
            try:
                spec_data['technical_specifications'] = json.loads(tech_specs_json)
            except:
                # Fallback to PIS data if JSON parse fails
                spec_data['technical_specifications'] = product.pis_data.get('technical_specifications', {})

        # Save Warranty (Cross-Sync with PIS)
        warranty_period = request.form.get('warranty_period')
        warranty_coverage = request.form.get('warranty_coverage')
        if warranty_period is not None or warranty_coverage is not None:
            if 'warranty_service' not in spec_data: spec_data['warranty_service'] = {}
            if 'warranty_service' not in product.pis_data: product.pis_data['warranty_service'] = {}
            spec_data['warranty_service']['period'] = warranty_period
            spec_data['warranty_service']['coverage'] = warranty_coverage
            product.pis_data['warranty_service']['period'] = warranty_period
            product.pis_data['warranty_service']['coverage'] = warranty_coverage

        product.spec_data = spec_data
        
        # CRITICAL: Flag the JSON fields as modified so SQLAlchemy saves them
        flag_modified(product, 'spec_data')
        flag_modified(product, 'pis_data')
        
        # Handle workflow + versioning based on action
        if action == 'submit_director':
            product.workflow_stage = 'pending_director_spec'
            save_version_snapshot(product, label='SpecSheet sent for review')
            log_event(product.id, get_current_username(), 'SpecSheet Sent for Review', 'The specsheet has been submitted to the Director for final review.', 'waiting')
        else:
            if product.workflow_stage == 'ready_for_web':
                product.workflow_stage = 'specsheet_draft'
            save_version_snapshot(product, label='SpecSheet draft saved')
            log_event(product.id, get_current_username(), 'SpecSheet Draft Saved', 'Changes to the specsheet have been saved as a draft.', 'neutral')
        
        # Log field-level diffs AFTER snapshot so version_num is correct
        _diff_and_log_changes(product.id, old_spec, spec_data, prefix='spec_data')
        _diff_and_log_changes(product.id, old_pis, product.pis_data, prefix='pis_data')
        
        db.session.commit()
        return redirect(url_for('dashboard_web'))
        
    return render_template('edit_specsheet.html', product=product, spec_data=product.spec_data or {})



@app.route('/review/director_spec/<int:product_id>', methods=['GET', 'POST'])
def review_director_spec(product_id):
    product = Product.query.get_or_404(product_id)
    
    if request.method == 'POST':
        action = request.form.get('director_action')
        
        # Compare against the LAST VERSION SNAPSHOT (auto-saves may have updated product data)
        last_version = ProductVersion.query.filter_by(product_id=product.id).order_by(ProductVersion.version_num.desc()).first()
        old_pis = copy.deepcopy(last_version.pis_data) if last_version and last_version.pis_data else {}
        old_spec = copy.deepcopy(last_version.spec_data) if last_version and last_version.spec_data else {}
        updated_pis_data = product.pis_data or {}
        updated_spec_data = product.spec_data or {}
        
        # Update Header Info if edited (from PIS data)
        if request.form.get('product_name'):
            if 'header_info' not in updated_pis_data: updated_pis_data['header_info'] = {}
            updated_pis_data['header_info']['product_name'] = request.form.get('product_name')
            updated_pis_data['header_info']['model_number'] = request.form.get('model_number')
            updated_pis_data['header_info']['brand'] = request.form.get('brand')
            updated_pis_data['header_info']['price_estimate'] = request.form.get('price_estimate')
        
        # Update SHORT DESCRIPTION if edited
        if request.form.get('range_overview'):
            updated_pis_data['range_overview'] = request.form.get('range_overview')
        
        # Update Sales Arguments if edited (Sync to SpecSheet key_features)
        sales_args = request.form.getlist('sales_argument')
        if sales_args and any(arg.strip() for arg in sales_args):
            clean_args = [arg.strip() for arg in sales_args if arg.strip()]
            updated_pis_data['sales_arguments'] = clean_args
            updated_spec_data['key_features'] = clean_args
        
        # Update Technical Specifications if edited (Sync to SpecSheet)
        tech_spec_keys = request.form.getlist('tech_spec_key')
        tech_spec_values = request.form.getlist('tech_spec_value')
        if tech_spec_keys and tech_spec_values:
            specs_dict = dict(zip(tech_spec_keys, tech_spec_values))
            updated_pis_data['technical_specifications'] = specs_dict
            updated_spec_data['technical_specifications'] = specs_dict
        
        # Update Warranty if edited (Sync to SpecSheet)
        if request.form.get('warranty_period'):
            if 'warranty_service' not in updated_pis_data: updated_pis_data['warranty_service'] = {}
            if 'warranty_service' not in updated_spec_data: updated_spec_data['warranty_service'] = {}
            updated_pis_data['warranty_service']['period'] = request.form.get('warranty_period')
            updated_pis_data['warranty_service']['coverage'] = request.form.get('warranty_coverage')
            updated_spec_data['warranty_service']['period'] = request.form.get('warranty_period')
            updated_spec_data['warranty_service']['coverage'] = request.form.get('warranty_coverage')
        
        # Update SpecSheet-specific fields
        if request.form.get('refined_description'):
            updated_spec_data['refined_description'] = request.form.get('refined_description')
            updated_spec_data['customer_friendly_description'] = request.form.get('refined_description')
        
        # Update SEO Keywords if edited
        if request.form.get('seo_keywords'):
            product.seo_keywords = request.form.get('seo_keywords')
        
        if request.form.get('internal_web_keywords'):
            updated_spec_data['internal_web_keywords'] = request.form.get('internal_web_keywords')
        
        # Update Categories if edited
        if request.form.get('category_1'):
            if 'categories' not in updated_spec_data:
                updated_spec_data['categories'] = {}
            updated_spec_data['categories']['category_1'] = request.form.get('category_1')
            updated_spec_data['categories']['category_2'] = request.form.get('category_2')
            updated_spec_data['categories']['category_3'] = request.form.get('category_3')
        
        # Save updated data
        product.pis_data = updated_pis_data
        product.spec_data = updated_spec_data
        
        # CRITICAL: Flag the JSON fields as modified so SQLAlchemy saves them
        flag_modified(product, 'pis_data')
        flag_modified(product, 'spec_data')
        
        if action == 'review':
            # Section-specific comments map
            comments_map = {
                'seo_optimization': request.form.get('comment_seo_optimization'),
                'internal_web_keywords': request.form.get('comment_internal_web_keywords'),
                'product_classification': request.form.get('comment_product_classification'),
                'header_info': request.form.get('comment_header_info'),
                'range_overview': request.form.get('comment_range_overview'),
                'sales_arguments': request.form.get('comment_sales_arguments'),
                'technical_specifications': request.form.get('comment_technical_specifications'),
                'warranty_service': request.form.get('comment_warranty_service')
            }
            
            new_revisions = {}
            
            for section, comment in comments_map.items():
                if comment and comment.strip():
                    # Get original content based on section
                    if section in ['seo_optimization', 'internal_web_keywords', 'product_classification']:
                        # For SpecSheet specific fields
                        if section == 'seo_optimization':
                            original = product.spec_data.get('seo') if product.spec_data else {}
                            # Include the description as well if available
                            if product.spec_data and 'customer_friendly_description' in product.spec_data:
                                original['refined_description'] = product.spec_data['customer_friendly_description']
                        elif section == 'product_classification':
                            original = product.spec_data.get('categories') if product.spec_data else {}
                        else:
                            original = product.spec_data.get('internal_web_keywords') if product.spec_data else ''
                    else:
                        # For other sections, use PIS data
                        original = product.pis_data.get(section)
                    
                    # Generate AI suggestion
                    ai_suggestion = generate_ai_revision(section, original, comment)
                    
                    # Store revision
                    new_revisions[section] = {
                        'comment': comment,
                        'original': original,
                        'ai_suggestion': ai_suggestion,
                        'status': 'pending'
                    }
            
            # Store in spec_revision_data (reusing revision_data field)
            product.revision_data = new_revisions
            
            # Store general comments
            general_comments = request.form.get('director_general_comments')
            product.director_spec_comments = general_comments
            
            product.workflow_stage = 'web_changes_requested'
            
            # Build rich log with section-specific comments
            section_labels = {
                'seo_optimization': 'SEO',
                'internal_web_keywords': 'Internal Keywords',
                'product_classification': 'Categories',
                'header_info': 'Header Info',
                'range_overview': 'Description',
                'sales_arguments': 'Sales Arguments',
                'technical_specifications': 'Tech Specs',
                'warranty_service': 'Warranty'
            }
            comment_details = []
            for section, comment in comments_map.items():
                if comment and comment.strip():
                    label = section_labels.get(section, section)
                    comment_details.append(f'{label}: "{comment.strip()[:80]}"')
            
            log_desc = f"Director requested SpecSheet changes on {len(new_revisions)} section(s):\n" + "\n".join(f"• {detail}" for detail in comment_details)
            if general_comments and general_comments.strip():
                log_desc += f'\n\nGeneral: "{general_comments.strip()[:100]}"'
            
            save_version_snapshot(product, label='Before Director requested SpecSheet changes')
            log_event(product.id, get_current_username(), 'SpecSheet Revisions Requested', log_desc, 'action')
            
        elif action == 'approve':
            product.workflow_stage = 'finalized'
            product.revision_data = None
            save_version_snapshot(product, label='Final approved version')
            log_event(
                product.id, 
                get_current_username(), 
                'SpecSheet Approved ✓', 
                'The specsheet has been finalized and approved. This product is now ready for publication.', 
                'success'
            )
        
        # Log field-level diffs AFTER snapshot so version_num is correct
        _diff_and_log_changes(product.id, old_pis, updated_pis_data, prefix='pis_data')
        
        # For spec_data, exclude fields that are mirrors of pis_data to avoid duplicates
        synced_keys = {'key_features', 'technical_specifications', 'customer_friendly_description', 'refined_description', 'header_info', 'warranty_service'}
        spec_diff_old = {k: v for k, v in old_spec.items() if k not in synced_keys}
        spec_diff_new = {k: v for k, v in updated_spec_data.items() if k not in synced_keys}
        if spec_diff_old != spec_diff_new:
            _diff_and_log_changes(product.id, spec_diff_old, spec_diff_new, prefix='spec_data')
            
        db.session.commit()
        return redirect(url_for('dashboard_director'))
        
    return render_template('verify_specsheet.html', product=product, spec_data=product.spec_data)

# --- NEW ROUTE: Marketing PIS PDF Download ---
@app.route('/download_pis_pdf/<int:product_id>')
def download_pis_pdf(product_id):
    product = Product.query.get_or_404(product_id)
    
    # 1. Process ALL Images to Base64 (Main image + Additional images)
    all_images_b64 = []
    
    # Collect all image paths
    image_paths = []
    if product.image_path:
        image_paths.append(product.image_path)
    if product.additional_images:
        image_paths.extend(product.additional_images)
        
    for path in image_paths:
        try:
            img_abs_path = os.path.join(app.root_path, 'static', path.replace('/', os.sep))
            if os.path.exists(img_abs_path):
                with open(img_abs_path, "rb") as img_file:
                    ext = os.path.splitext(img_abs_path)[1].lower().replace('.', '')
                    if ext == 'jpg': ext = 'jpeg'
                    b64_data = base64.b64encode(img_file.read()).decode('utf-8')
                    all_images_b64.append(f"data:image/{ext};base64,{b64_data}")
        except Exception as e:
            print(f"Image processing error for {path}: {e}")

    # 2. Render Template
    html = render_template('pdf_print.html', 
                           data=product.pis_data, 
                           product=product, 
                           all_images_b64=all_images_b64, # List of images
                           date_generated=datetime.now().strftime("%Y-%m-%d"))
    
    # 3. Generate PDF using Playwright
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            
            # Load HTML content — wait for fonts & base64 images to fully render
            page.set_content(html, wait_until='networkidle')
            page.wait_for_timeout(1500)  # Extra buffer for base64 image decoding
            
            # Generate PDF (A4, print background graphics)
            pdf_bytes = page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "15mm", "right": "15mm", "bottom": "15mm", "left": "15mm"}
            )
            browser.close()
            
        return Response(pdf_bytes, mimetype='application/pdf', 
                        headers={"Content-Disposition": f"attachment;filename=PIS_{secure_filename(product.model_name)}.pdf"})
                        
    except Exception as e:
        return f"Error generating PDF with Playwright: {str(e)}"
    
@app.route('/download_specsheet/<int:product_id>')
def download_specsheet(product_id):
    product = Product.query.get_or_404(product_id)
    
    # 1. Process ALL Images to Base64 (Main image + Additional images)
    all_images_b64 = []
    
    # Collect all image paths
    image_paths = []
    if product.image_path:
        image_paths.append(product.image_path)
    if product.additional_images:
        image_paths.extend(product.additional_images)
        
    for path in image_paths:
        try:
            img_abs_path = os.path.join(app.root_path, 'static', path.replace('/', os.sep))
            if os.path.exists(img_abs_path):
                with open(img_abs_path, "rb") as img_file:
                    ext = os.path.splitext(img_abs_path)[1].lower().replace('.', '')
                    if ext == 'jpg': ext = 'jpeg'
                    b64_data = base64.b64encode(img_file.read()).decode('utf-8')
                    all_images_b64.append(f"data:image/{ext};base64,{b64_data}")
        except Exception as e:
            print(f"Image processing error for {path}: {e}")

    # 2. Render Template
    html = render_template('specsheet_pdf.html', 
                           data=product.pis_data, 
                           spec_data=product.spec_data or {}, 
                           product=product, 
                           all_images_b64=all_images_b64, # List of images
                           date_generated=datetime.now().strftime("%Y-%m-%d"))

    # 3. Generate with Playwright
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html, wait_until='networkidle')
            page.wait_for_timeout(1500)  # Extra buffer for base64 image decoding
            pdf_bytes = page.pdf(
                format="A4", 
                print_background=True, 
                margin={"top": "15mm", "right": "15mm", "bottom": "15mm", "left": "15mm"}
            )
            browser.close()
        return Response(pdf_bytes, mimetype='application/pdf', headers={"Content-Disposition": f"attachment;filename=SpecSheet_{secure_filename(product.model_name)}.pdf"})
    except Exception as e:
        print(f"SpecSheet PDF Error: {e}")
        return f"Error generating PDF: {e}"


# --- Spec Key → Short-Code map for additional_attributes column ---
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
    """Convert a spec key like 'Brand' or 'Operating System' to its short code."""
    k_lower = key.strip().lower()
    # Direct match
    if k_lower in SPEC_KEY_ABBR:
        return SPEC_KEY_ABBR[k_lower]
    # Partial match
    for phrase, abbr in SPEC_KEY_ABBR.items():
        if phrase in k_lower or k_lower in phrase:
            return abbr
    # Fallback: first 3 chars uppercase
    return key.strip()[:3].upper()


@app.route('/download_specsheet_csv/<int:product_id>')
def download_specsheet_csv(product_id):
    """Export a single product's specsheet as a Magento-format semicolon-delimited CSV."""
    product = Product.query.get_or_404(product_id)
    pis = product.pis_data or {}
    spec = product.spec_data or {}
    header = spec.get('header_info') or pis.get('header_info') or {}
    seo = spec.get('seo') or {}
    cats = spec.get('categories') or {}
    warranty = pis.get('warranty_service') or spec.get('warranty_service') or {}
    tech_specs = spec.get('technical_specifications') or pis.get('technical_specifications') or {}

    # --- Build each column ---
    sku = header.get('model_number', product.model_name or '')
    product_name = header.get('product_name', product.model_name or '')

    # Categories: "Category/A,Category/A/B,Category/A/B/C"
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
    categories = ','.join(category_parts)

    # Description: key features as HTML <ul><li>
    features = spec.get('key_features') or pis.get('sales_arguments') or []
    if features:
        li_items = ''.join(f'<li>{f}</li>' for f in features if f)
        description = f'<ul>{li_items}</ul>'
    else:
        description = ''

    # Price: extract digits only
    raw_price = str(header.get('price_estimate', ''))
    price = re.sub(r'[^\d.]', '', raw_price) or '0'

    # Additional attributes: KEY=VAL pairs from tech specs + brand + warranty + origin
    attr_parts = []
    # Add brand first if present
    brand = header.get('brand', '')
    if brand:
        attr_parts.append(f"BRD={brand}")

    for key, val in tech_specs.items():
        if not key or not val:
            continue
        abbr = _abbreviate_spec_key(key)
        # Skip brand if already added
        if abbr == 'BRD' and brand:
            continue
        attr_parts.append(f"{abbr}={val}")

    # Add warranty if present and not already in specs
    if warranty.get('period') and not any(p.startswith('GUA=') for p in attr_parts):
        attr_parts.append(f"GUA={warranty['period']}")

    additional_attributes = ','.join(attr_parts)

    # URL key: name~sku with spaces as tildes
    url_key = f"{product_name}~{sku}".replace(' ', '~')

    # Short description
    short_desc = spec.get('customer_friendly_description') or spec.get('refined_description') or pis.get('range_overview') or ''

    # SEO
    meta_title = seo.get('meta_title', '')
    meta_desc = seo.get('meta_description', '')

    # Attribute set code
    attribute_set_code = cat3 or cat2 or cat1 or 'Default'

    # --- Build CSV ---
    csv_columns = [
        'sku', 'attribute_set_code', 'product_type', 'categories', 'product_websites',
        'name', 'description', 'is_in_stock', 'weight', 'product_online',
        'tax_class_name', 'visibility', 'price', 'display_product_options_in',
        'additional_attributes', 'qty', 'url_key', 'short_description',
        'meta_title', 'meta_description'
    ]

    row = {
        'sku': sku,
        'attribute_set_code': attribute_set_code,
        'product_type': 'simple',
        'categories': categories,
        'product_websites': 'base',
        'name': product_name,
        'description': description,
        'is_in_stock': '1',
        'weight': '1',
        'product_online': '1',
        'tax_class_name': 'Taxable Goods',
        'visibility': 'Catalog, Search',
        'price': price,
        'display_product_options_in': 'Block after Info Column',
        'additional_attributes': additional_attributes,
        'qty': '0',
        'url_key': url_key,
        'short_description': short_desc,
        'meta_title': meta_title,
        'meta_description': meta_desc,
    }

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=csv_columns, delimiter=';',
                            quoting=csv.QUOTE_MINIMAL, lineterminator='\r\n')
    writer.writeheader()
    writer.writerow(row)

    csv_content = output.getvalue()
    output.close()

    filename = f"SpecSheet_{secure_filename(product.model_name)}.csv"
    return Response(
        csv_content,
        mimetype='text/csv',
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route('/retry_revision/<int:product_id>/<section>', methods=['POST'])
def retry_revision(product_id, section):
    product = Product.query.get_or_404(product_id)

    if not product.revision_data or section not in product.revision_data:
        return {"error": "No revision data"}, 400

    revision = product.revision_data[section]

    original_content = revision.get("original")
    director_comment = revision.get("comment")

    new_ai_suggestion = generate_ai_revision(
        section_name=section,
        original_content=original_content,
        director_comment=director_comment
    )

    # Update only the AI suggestion
    product.revision_data[section]["ai_suggestion"] = new_ai_suggestion
    
    flag_modified(product, 'revision_data')

    db.session.commit()

    return {
        "ai_suggestion": new_ai_suggestion
    }


@app.route('/api/product/<int:product_id>/images/upload', methods=['POST'])
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
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(save_path)
            db_path = f"uploads/{filename}"
            
            # Logic: If main image is empty, fill it. Otherwise, add to additional_images.
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
            
            # Log the upload to history
            img_type = 'main photo' if is_main else 'gallery photo'
            log_event(product.id, get_current_username(), 'Photo Added',
                f'A new {img_type} was uploaded: {file.filename}', 'neutral')
        
        db.session.commit()
        
        if len(uploaded) == 1:
            return {"status": "success", "path": uploaded[0]['path'], "is_main": uploaded[0]['is_main']}
        return {"status": "success", "count": len(uploaded)}
        
    except Exception as e:
        return {"error": str(e)}, 500

@app.route('/api/product/<int:product_id>/images/delete', methods=['POST'])
def api_delete_image(product_id):
    product = Product.query.get_or_404(product_id)
    data = request.get_json()
    path_to_delete = data.get('path')
    
    if not path_to_delete:
        return {"error": "No path provided"}, 400

    
    try:
        deleted_type = 'image'
        # Check if it's the main image
        if product.image_path == path_to_delete:
            deleted_type = 'main image'
            product.image_path = None
            # Optional: Promote the first additional image to main if exists
            imgs = list(product.additional_images) if product.additional_images else []
            if imgs:
                product.image_path = imgs.pop(0)
                product.additional_images = imgs
                flag_modified(product, 'additional_images')
        else:
            # Check additional images
            imgs = list(product.additional_images) if product.additional_images else []
            if path_to_delete in imgs:
                deleted_type = 'additional image'
                imgs.remove(path_to_delete)
                product.additional_images = imgs
                flag_modified(product, 'additional_images')
        
        # Log the deletion
        fname = path_to_delete.split('/')[-1] if '/' in path_to_delete else path_to_delete
        log_event(product.id, get_current_username(), 'Photo Removed',
            f'Removed a {deleted_type} photo: {fname}', 'neutral')
        
        db.session.commit()
        return {"status": "success"}
        
    except Exception as e:
        return {"error": str(e)}, 500





@app.route('/api/product/<int:product_id>/save_draft', methods=['POST'])
def api_save_draft(product_id):
    product = Product.query.get_or_404(product_id)
    data = request.get_json()
    if not data:
        return {"error": "No data provided"}, 400

    updated_pis_data = product.pis_data or {}
    updated_spec_data = product.spec_data or {}
    old_pis = copy.deepcopy(updated_pis_data)
    old_spec = copy.deepcopy(updated_spec_data)

    # 1. Update Header Info (Shared)
    if 'product_name' in data:
        if 'header_info' not in updated_pis_data: updated_pis_data['header_info'] = {}
        if 'header_info' not in updated_spec_data: updated_spec_data['header_info'] = {}
        
        h_info = {
            'product_name': data.get('product_name'),
            'model_number': data.get('model_number'),
            'brand': data.get('brand'),
            'price_estimate': data.get('price_estimate')
        }
        updated_pis_data['header_info'] = h_info
        updated_spec_data['header_info'] = h_info

    # 2. Update SHORT DESCRIPTION / Description (Cross-Sync)
    if 'range_overview' in data:
        desc = data.get('range_overview')
        updated_pis_data['range_overview'] = desc
        updated_spec_data['customer_friendly_description'] = desc # Sync to SpecSheet
        updated_spec_data['refined_description'] = desc
    
    if 'customer_friendly_description' in data:
        desc = data.get('customer_friendly_description')
        updated_spec_data['customer_friendly_description'] = desc
        updated_spec_data['refined_description'] = desc
        updated_pis_data['range_overview'] = desc # Sync back to PIS

    # 3. Update Sales Arguments / Key Features (Sync)
    # Note: frontend sends 'key_features' for specsheet, 'sales_argument' for verify_spec, 'sales_arguments' for marketing
    features = data.get('key_features') or data.get('sales_argument') or data.get('sales_arguments')
    if features is not None:
        if isinstance(features, list):
            clean_features = [f.strip() for f in features if f.strip()]
            updated_pis_data['sales_arguments'] = clean_features
            updated_spec_data['key_features'] = clean_features

    # 4. Update Technical Specifications (Sync)
    tech_specs = data.get('technical_specifications')
    if tech_specs is not None:
        if isinstance(tech_specs, dict):
            updated_pis_data['technical_specifications'] = tech_specs
            updated_spec_data['technical_specifications'] = tech_specs

    # 5. Update Warranty (Cross-Sync PIS + SpecSheet)
    if 'warranty_period' in data:
        if 'warranty_service' not in updated_pis_data: updated_pis_data['warranty_service'] = {}
        if 'warranty_service' not in updated_spec_data: updated_spec_data['warranty_service'] = {}
        updated_pis_data['warranty_service']['period'] = data.get('warranty_period')
        updated_pis_data['warranty_service']['coverage'] = data.get('warranty_coverage')
        updated_spec_data['warranty_service']['period'] = data.get('warranty_period')
        updated_spec_data['warranty_service']['coverage'] = data.get('warranty_coverage')

    # 6. Update SEO Meta
    if 'seo_meta_title' in data:
        if 'seo' not in updated_spec_data: updated_spec_data['seo'] = {}
        updated_spec_data['seo']['meta_title'] = data.get('seo_meta_title')
        updated_spec_data['seo']['meta_description'] = data.get('seo_meta_description')
        updated_spec_data['seo']['keywords'] = data.get('seo_keywords') or data.get('seo_meta_keywords')
    
    # Internal keywords
    if 'internal_web_keywords' in data:
        updated_spec_data['internal_web_keywords'] = data.get('internal_web_keywords')

    # 7. Update Categories
    if 'category_1' in data:
        if 'categories' not in updated_spec_data: updated_spec_data['categories'] = {}
        updated_spec_data['categories']['category_1'] = data.get('category_1')
        updated_spec_data['categories']['category_2'] = data.get('category_2')
        updated_spec_data['categories']['category_3'] = data.get('category_3')

    # 8. Update Director General Comments (Internal auto-save from review modals)
    if 'director_general_comments' in data:
        comments = data.get('director_general_comments')
        if 'pending_director_pis' in product.workflow_stage or 'marketing_changes' in product.workflow_stage:
            product.director_pis_comments = comments
        elif 'pending_director_spec' in product.workflow_stage or 'web_changes' in product.workflow_stage:
            product.director_spec_comments = comments

    # 9. Handle accepted revision sections (clear from revision_data)
    accepted = data.get('accepted_revisions')
    if accepted and isinstance(accepted, list) and product.revision_data:
        rev = dict(product.revision_data)
        for section_key in accepted:
            # Map frontend section names to revision_data keys
            key_map = {
                'header': 'header_info',
                'overview': 'range_overview',
                'sales': 'sales_arguments',
                'specs': 'technical_specifications',
                'warranty': 'warranty_service'
            }
            rev_key = key_map.get(section_key, section_key)
            rev.pop(rev_key, None)
        
        if rev:
            product.revision_data = rev
        else:
            product.revision_data = None
        flag_modified(product, 'revision_data')

    # Save
    product.pis_data = updated_pis_data
    product.spec_data = updated_spec_data
    
    flag_modified(product, 'pis_data')
    flag_modified(product, 'spec_data')
    
    # NOTE: No diff_and_log here — changelog entries are only created
    # on explicit Save button clicks (form POST routes), not auto-saves.
    
    db.session.commit()
    return {"status": "success"}


# --- NEW: SpecSheet AI Generation API ---
@app.route('/api/generate_specsheet/<int:product_id>', methods=['POST'])
def api_generate_specsheet(product_id):
    product = Product.query.get_or_404(product_id)
    is_rework = request.args.get('rework') == '1'
    
    def generate():
        if is_rework:
            yield json.dumps({"progress": 10, "message": "Loading forbidden words..."}) + "\n"
            time.sleep(0.3)
            yield json.dumps({"progress": 25, "message": "Analyzing existing content..."}) + "\n"
        else:
            yield json.dumps({"progress": 20, "message": "Analyzing PIS Data..."}) + "\n"
        time.sleep(0.5) # UI visual pacing
        
        yield json.dumps({"progress": 50, "message": "Rewriting Customer Content..."}) + "\n"
        
        try:
            # Generate comprehensive content with forbidden words enforcement
            all_fw = load_forbidden_words()
            combined_forbidden = list(set(w for words in all_fw.values() for w in words))
            
            if is_rework and combined_forbidden:
                yield json.dumps({"progress": 55, "message": f"Enforcing {len(combined_forbidden)} forbidden words..."}) + "\n"
            
            spec_data = generate_comprehensive_spec_data(product.pis_data, forbidden_words=combined_forbidden)
            
            yield json.dumps({"progress": 80, "message": "Optimizing SEO Metadata..."}) + "\n"
            
            with app.app_context():
                # Re-fetch to ensure session context
                p = Product.query.get(product_id)
                p.spec_data = spec_data
                p.workflow_stage = 'specsheet_draft'
                flag_modified(p, 'spec_data')
                save_version_snapshot(p, label='SpecSheet regenerated' if is_rework else 'SpecSheet auto-generated')
                db.session.commit()
                
                if is_rework:
                    log_event(p.id, get_current_username(), 'SpecSheet Regenerated', 
                        f'The system regenerated content based on the Director\'s feedback ({len(combined_forbidden)} restricted terms applied).', 'neutral')
                else:
                    log_event(p.id, 'System', 'SpecSheet Auto-Generated', 
                        'The system automatically created customer-facing product descriptions and SEO keywords.', 'neutral')
            
            yield json.dumps({"progress": 100, "message": "Generation Complete!", "redirect": url_for('create_specsheet', product_id=product.id)}) + "\n"
            
        except Exception as e:
            print(f"Error: {e}")
            yield json.dumps({"error": "AI Generation Failed. Please try again."}) + "\n"

    return Response(stream_with_context(generate()), mimetype='application/x-ndjson')


# ================= ASYNC PIS GENERATION API =================

def _update_job(job_id, **kwargs):
    """Update a job's fields in the database. Safe to call from background threads."""
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


def _pis_worker(job_id, model_name, supplier_url, ai_filepaths, contains_images, user_name):
    """Background worker that generates a single PIS and updates job status."""
    with app.app_context():
        try:
            _update_job(job_id, status='processing', progress=10, message='Initializing Analysis...')
            
            site_data = {"text": "", "html": ""}
            if supplier_url:
                _update_job(job_id, progress=20, message='Reading Website Text...')
                site_data = scrape_url_data(supplier_url)
            
            _update_job(job_id, progress=40, message='Generating PIS Content...')
            ai_data = generate_pis_data(ai_filepaths, model_name, site_data)
            
            extracted_image_path = None
            
            # Image search logic (same as synchronous flow)
            if contains_images and ai_filepaths:
                _update_job(job_id, progress=55, message='Scanning PDF for product image...')
                extracted_image_path = extract_specific_image(ai_filepaths[0], model_name, app.config['UPLOAD_FOLDER'])
                
                if not extracted_image_path:
                    _update_job(job_id, progress=65, message='PDF scan found nothing, trying web...')
                    ai_found_url = ai_data.get('found_image_url')
                    if ai_found_url and ai_found_url.startswith('http'):
                        extracted_image_path = download_web_image(ai_found_url, model_name, app.config['UPLOAD_FOLDER'])
                
                if not extracted_image_path:
                    header = ai_data.get('header_info', {})
                    brand = header.get('brand', '')
                    m_num = header.get('model_number', '')
                    p_name = header.get('product_name', '')
                    q_parts = []
                    if brand: q_parts.append(brand)
                    if p_name: q_parts.append(p_name)
                    if m_num and (any(c.isalpha() for c in m_num) or '-' in m_num):
                        if m_num not in (p_name or ''):
                            q_parts.append(m_num)
                    full_str = " ".join(q_parts)
                    unique_words = []
                    [unique_words.append(x) for x in full_str.split() if x.lower() not in [y.lower() for y in unique_words]]
                    rich_query = " ".join(unique_words) if q_parts else model_name
                    public_url = find_and_validate_image(rich_query, supplier_url)
                    if public_url:
                        extracted_image_path = download_web_image(public_url, model_name, app.config['UPLOAD_FOLDER'])
            else:
                ai_found_url = ai_data.get('found_image_url')
                if ai_found_url and ai_found_url.startswith('http'):
                    _update_job(job_id, progress=55, message='AI found a product image — downloading...')
                    extracted_image_path = download_web_image(ai_found_url, model_name, app.config['UPLOAD_FOLDER'])
                
                if not extracted_image_path:
                    _update_job(job_id, progress=60, message='Searching Google Images...')
                    header = ai_data.get('header_info', {})
                    brand = header.get('brand', '')
                    m_num = header.get('model_number', '')
                    p_name = header.get('product_name', '')
                    q_parts = []
                    if brand: q_parts.append(brand)
                    if p_name: q_parts.append(p_name)
                    if m_num and (any(c.isalpha() for c in m_num) or '-' in m_num):
                        if m_num not in (p_name or ''):
                            q_parts.append(m_num)
                    full_str = " ".join(q_parts)
                    unique_words = []
                    [unique_words.append(x) for x in full_str.split() if x.lower() not in [y.lower() for y in unique_words]]
                    rich_query = " ".join(unique_words) if q_parts else model_name
                    public_url = find_and_validate_image(rich_query, supplier_url)
                    if public_url:
                        _update_job(job_id, progress=70, message='Downloading Image...')
                        extracted_image_path = download_web_image(public_url, model_name, app.config['UPLOAD_FOLDER'])
            
            # DuckDuckGo fallback
            if not extracted_image_path:
                _update_job(job_id, progress=80, message='Trying DuckDuckGo fallback search...')
                header = ai_data.get('header_info', {})
                brand = header.get('brand', '')
                p_name = header.get('product_name', '')
                simple_query = f"{brand} {p_name}".strip() or model_name
                simple_url = find_image_simple(simple_query, supplier_url)
                if simple_url:
                    _update_job(job_id, progress=85, message='Found image via DuckDuckGo!')
                    extracted_image_path = download_web_image(simple_url, model_name, app.config['UPLOAD_FOLDER'])
            
            _update_job(job_id, progress=90, message='Saving product...')
            
            new_product = Product(
                model_name=model_name,
                pis_data=ai_data,
                image_path=extracted_image_path,
                seo_keywords=ai_data.get('seo_data', {}).get('generated_keywords', ''),
                workflow_stage='marketing_draft'
            )
            db.session.add(new_product)
            db.session.commit()
            log_event(new_product.id, user_name, 'New Product Added', 'A new product information sheet was created from a single import.', 'neutral')
            save_version_snapshot(new_product, label='Initial version')
            
            _update_job(job_id,
                status='completed',
                progress=100,
                message='Done!',
                product_id=new_product.id,
                redirect_url=f'/review/marketing/{new_product.id}',
                completed_at=datetime.utcnow().isoformat()
            )
            print(f"✅ [ASYNC] PIS generated for '{model_name}' (product #{new_product.id})")
            
        except Exception as e:
            print(f"❌ [ASYNC] PIS generation failed for '{model_name}': {e}")
            import traceback
            traceback.print_exc()
            _update_job(job_id,
                status='failed',
                progress=100,
                message=f'Generation failed: {str(e)[:100]}',
                error=str(e),
                completed_at=datetime.utcnow().isoformat()
            )


@app.route('/api/pis/generate', methods=['POST'])
def api_pis_generate_async():
    """Submit a PIS generation job to the background queue."""
    model_name = request.form.get('model_name', '').strip()
    supplier_url = request.form.get('supplier_url', '').strip()
    ai_files = request.files.getlist('ai_document')
    contains_images = request.form.get('contains_images') == 'on'
    
    if not model_name and not supplier_url and not ai_files:
        return jsonify({"error": "Please provide a model name, document, or URL."}), 400
    
    # Check active job count
    active_count = Job.query.filter(Job.status.in_(('queued', 'processing'))).count()
    if active_count >= 5:
        return jsonify({"error": "Maximum 5 concurrent generations allowed. Please wait for a slot to free up."}), 429

    # Save uploaded files
    ai_filepaths = []
    for ai_file in ai_files:
        if ai_file and ai_file.filename:
            filename = secure_filename(ai_file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            ai_file.save(filepath)
            ai_filepaths.append(filepath)

    # Create job
    job_id = str(uuid.uuid4())[:8]
    user_name = get_current_username()

    db.session.add(Job(
        id=job_id,
        model_name=model_name or 'Unknown Product',
        status='queued',
        progress=0,
        message='Queued — waiting for slot...',
        created_at=datetime.utcnow(),
    ))
    db.session.commit()

    # Submit to thread pool
    pis_executor.submit(
        _pis_worker, job_id, model_name, supplier_url, ai_filepaths, contains_images, user_name
    )
    
    return jsonify({"ok": True, "job_id": job_id, "message": f"Generation started for '{model_name}'"}), 202


@app.route('/api/pis/jobs', methods=['GET'])
def api_pis_jobs():
    """Return all active and recently completed jobs."""
    jobs = Job.query.filter_by(dismissed=False).order_by(Job.created_at.asc()).all()
    result = sorted([{
        'id': j.id,
        'model_name': j.model_name or '',
        'status': j.status,
        'message': j.message or '',
        'progress': j.progress or 0,
        'redirect_url': j.redirect_url,
        'dismissed': j.dismissed,
    } for j in jobs], key=lambda j: (0 if j['status'] in ('queued', 'processing') else 1))
    resp = jsonify(result)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@app.route('/api/pis/jobs/<job_id>', methods=['DELETE'])
def api_pis_dismiss_job(job_id):
    """Dismiss a completed/failed job from the tracker."""
    job = db.session.get(Job, job_id)
    if not job:
        return jsonify({"ok": True})
    if job.status not in ('completed', 'failed'):
        return jsonify({"error": "Cannot dismiss an active job"}), 400
    job.dismissed = True
    db.session.commit()
    return jsonify({"ok": True})


# ================= ASYNC BULK PIS GENERATION =================

def _bulk_pis_worker(job_id, supplier_url, ai_filepaths, contains_images, product_filter, user_name):
    """Background worker that generates multiple PIS from a bulk document."""
    with app.app_context():
        try:
            _update_job(job_id, status='processing', progress=5, message='Analyzing document...')

            site_data = {"text": "", "html": ""}
            is_url_only = supplier_url and not ai_filepaths

            if supplier_url:
                if is_url_only:
                    # URL-only mode: use deep scraping (Jina/Firecrawl + sub-page crawling)
                    _update_job(job_id, progress=5, message='Deep-scraping supplier website...')
                    site_data = scrape_url_data_deep(supplier_url)
                    sub_count = site_data.get('sub_pages_scraped', 0)
                    if sub_count > 0:
                        _update_job(job_id, progress=12, message=f'Scraped main page + {sub_count} product pages')
                    else:
                        _update_job(job_id, progress=12, message='Website scraped, extracting products...')
                else:
                    # Document + URL mode: standard scrape for supplemental data
                    _update_job(job_id, progress=10, message='Reading Website Text...')
                    site_data = scrape_url_data(supplier_url)

            _update_job(job_id, progress=15, message='Extracting products with AI...')
            products_list = generate_bulk_pis_data(ai_filepaths, site_data, product_filter=product_filter)
            total_items = len(products_list)

            if total_items == 0:
                _update_job(job_id, status='completed', progress=100,
                    message='No products found in document.',
                    redirect_url='/dashboard/marketing',
                    completed_at=datetime.utcnow().isoformat())
                return

            _update_job(job_id, progress=20, message=f'Found {total_items} products. Processing...')

            ai_filepath = ai_filepaths[0] if ai_filepaths else None
            processed_count = 0

            for idx, p_data in enumerate(products_list):
                header = p_data.get('header_info', {})
                brand = header.get('brand', '')
                model_id = header.get('model_number', '')
                prod_name = header.get('product_name', '')
                display_name = prod_name if prod_name else (model_id if model_id else f"Item_{idx+1}")

                processed_count += 1
                current_progress = 20 + int((processed_count / total_items) * 70)
                _update_job(job_id, progress=current_progress, message=f'Processing {processed_count}/{total_items}: {display_name}')

                try:
                    # Build search query
                    query_parts = []
                    if brand: query_parts.append(brand)
                    if prod_name: query_parts.append(prod_name)
                    is_real_model = model_id and (any(c.isalpha() for c in model_id) or '-' in model_id)
                    if is_real_model and (model_id not in (prod_name or '')):
                        query_parts.append(model_id)
                    seen_words = set()
                    unique_words = []
                    for w in " ".join(query_parts).split():
                        if w.lower() not in seen_words:
                            unique_words.append(w)
                            seen_words.add(w.lower())
                    search_query = " ".join(unique_words) if unique_words else display_name

                    extracted_image_path = None

                    # Image search logic — always try PDF first when we have a document
                    if ai_filepath:
                        # Try model_id first, then display_name for PDF extraction
                        pdf_search_term = model_id if model_id else display_name
                        extracted_image_path = extract_specific_image(ai_filepath, pdf_search_term, app.config['UPLOAD_FOLDER'])
                        
                        # If model_id didn't work but we have a display_name, try that too
                        if not extracted_image_path and model_id and display_name and display_name != model_id:
                            extracted_image_path = extract_specific_image(ai_filepath, display_name, app.config['UPLOAD_FOLDER'])

                    # Try AI-found URL from the extraction response
                    if not extracted_image_path:
                        ai_found_url = p_data.get('found_image_url')
                        if ai_found_url and str(ai_found_url).startswith('http'):
                            extracted_image_path = download_web_image(ai_found_url, display_name, app.config['UPLOAD_FOLDER'])

                    # Web image search
                    if not extracted_image_path:
                        image_url = find_and_validate_image(search_query, supplier_url)
                        if image_url:
                            extracted_image_path = download_web_image(image_url, display_name, app.config['UPLOAD_FOLDER'])

                    # DuckDuckGo fallback
                    if not extracted_image_path:
                        simple_url = find_image_simple(search_query, supplier_url)
                        if simple_url:
                            extracted_image_path = download_web_image(simple_url, display_name, app.config['UPLOAD_FOLDER'])

                    new_product = Product(
                        model_name=display_name,
                        pis_data=p_data,
                        image_path=extracted_image_path,
                        seo_keywords=p_data.get('seo_data', {}).get('generated_keywords', ''),
                        workflow_stage='marketing_draft'
                    )
                    db.session.add(new_product)
                    db.session.commit()
                    log_event(new_product.id, user_name, 'New Product Added', 'This product was imported as part of a bulk extraction.', 'neutral')
                    save_version_snapshot(new_product, label='Initial version')

                except Exception as product_err:
                    print(f"⚠️ [ASYNC BULK] Error for '{display_name}': {product_err}")
                    try:
                        fallback_product = Product(
                            model_name=display_name,
                            pis_data=p_data,
                            image_path=None,
                            seo_keywords=p_data.get('seo_data', {}).get('generated_keywords', ''),
                            workflow_stage='marketing_draft'
                        )
                        db.session.add(fallback_product)
                        db.session.commit()
                        log_event(fallback_product.id, user_name, 'PIS Draft Created',
                            f'Imported via Bulk (image search failed: {str(product_err)[:80]}).', 'neutral')
                        save_version_snapshot(fallback_product, label='Original')
                    except Exception:
                        db.session.rollback()

            # Free cached PDF images
            clear_pdf_cache()

            _update_job(job_id,
                status='completed',
                progress=100,
                message=f'Bulk import complete — {total_items} products created!',
                redirect_url='/dashboard/marketing',
                completed_at=datetime.utcnow().isoformat()
            )
            print(f"✅ [ASYNC BULK] Completed: {total_items} products imported")

        except Exception as e:
            print(f"❌ [ASYNC BULK] Bulk generation failed: {e}")
            import traceback
            traceback.print_exc()
            _update_job(job_id,
                status='failed',
                progress=100,
                message=f'Bulk import failed: {str(e)[:100]}',
                error=str(e),
                completed_at=datetime.utcnow().isoformat()
            )


@app.route('/api/pis/generate_bulk', methods=['POST'])
def api_bulk_generate_async():
    """Submit a bulk PIS generation job to the background queue."""
    supplier_url = request.form.get('supplier_url', '').strip()
    ai_files = request.files.getlist('ai_document')
    contains_images = request.form.get('contains_images') == 'on'
    product_filter = request.form.get('product_filter', '').strip()

    # Save uploaded files
    ai_filepaths = []
    for ai_file in ai_files:
        if ai_file and ai_file.filename:
            filename = secure_filename(ai_file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            ai_file.save(filepath)
            ai_filepaths.append(filepath)

    if not ai_filepaths and not supplier_url:
        return jsonify({"error": "Please provide at least a document or a supplier URL."}), 400

    # Check active job count
    active_count = Job.query.filter(Job.status.in_(('queued', 'processing'))).count()
    if active_count >= 5:
        return jsonify({"error": "Maximum 5 concurrent jobs allowed. Please wait for a slot to free up."}), 429

    job_id = str(uuid.uuid4())[:8]
    user_name = get_current_username()
    doc_names = ', '.join(os.path.basename(f) for f in ai_filepaths[:2])
    job_label = f"Bulk: {doc_names}" if doc_names else "Bulk Import"

    db.session.add(Job(
        id=job_id,
        model_name=job_label,
        status='queued',
        progress=0,
        message='Queued — waiting for slot...',
        created_at=datetime.utcnow(),
    ))
    db.session.commit()

    pis_executor.submit(
        _bulk_pis_worker, job_id, supplier_url, ai_filepaths, contains_images, product_filter, user_name
    )

    return jsonify({"ok": True, "job_id": job_id, "message": "Bulk generation started"}), 202


# ================= FORBIDDEN WORDS API =================

FORBIDDEN_WORDS_FILE = os.path.join(basedir, 'data', 'forbidden_words.json')

def load_forbidden_words():
    """Load forbidden words from JSON file."""
    try:
        with open(FORBIDDEN_WORDS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_forbidden_words(data):
    """Save forbidden words to JSON file."""
    os.makedirs(os.path.dirname(FORBIDDEN_WORDS_FILE), exist_ok=True)
    with open(FORBIDDEN_WORDS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_forbidden_words_for_category(category_3):
    """Get forbidden words list for a specific cat_C category."""
    data = load_forbidden_words()
    return data.get(category_3, [])


@app.route('/api/forbidden_words', methods=['GET'])
def api_get_forbidden_words():
    return json.dumps(load_forbidden_words()), 200, {'Content-Type': 'application/json'}


@app.route('/api/spec_status/<int:product_id>', methods=['GET'])
def api_spec_status(product_id):
    """Poll endpoint for background specsheet generation status."""
    product = Product.query.get_or_404(product_id)
    sd = product.spec_data or {}
    is_generating = sd.get('_spec_generating', False)
    return json.dumps({'ready': not is_generating}), 200, {'Content-Type': 'application/json'}


@app.route('/api/magento_categories', methods=['GET'])
def api_magento_categories():
    """Return the Magento category tree for frontend dropdowns."""
    try:
        from utils.magento_api import get_category_tree
        tree = get_category_tree()
        return json.dumps(tree), 200, {'Content-Type': 'application/json'}
    except Exception as e:
        return json.dumps({'error': str(e)}), 500, {'Content-Type': 'application/json'}


@app.route('/api/revision_status/<int:product_id>', methods=['GET'])
def api_revision_status(product_id):
    """Poll endpoint for background AI revision generation status."""
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


@app.route('/api/forbidden_words', methods=['POST'])
def api_add_forbidden_word():
    body = request.get_json(force=True)
    category = body.get('category', '').strip()
    word = body.get('word', '').strip().lower()
    if not category or not word:
        return json.dumps({"error": "Category and word required"}), 400, {'Content-Type': 'application/json'}
    
    data = load_forbidden_words()
    if category not in data:
        data[category] = []
    if word not in data[category]:
        data[category].append(word)
    save_forbidden_words(data)
    return json.dumps({"ok": True, "words": data[category]}), 200, {'Content-Type': 'application/json'}


@app.route('/api/forbidden_words', methods=['DELETE'])
def api_remove_forbidden_word():
    body = request.get_json(force=True)
    category = body.get('category', '').strip()
    word = body.get('word', '').strip().lower()
    if not category or not word:
        return json.dumps({"error": "Category and word required"}), 400, {'Content-Type': 'application/json'}
    
    data = load_forbidden_words()
    if category in data and word in data[category]:
        data[category].remove(word)
        if not data[category]:
            del data[category]
    save_forbidden_words(data)
    return json.dumps({"ok": True, "words": data.get(category, [])}), 200, {'Content-Type': 'application/json'}


# ================= ADMIN: USER MANAGEMENT =================

@app.route('/admin/users')
def admin_users():
    if session.get('role') != 'admin':
        return redirect(url_for('login'))
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin_users.html', users=users)


# ================= ADMIN: PROMPT MANAGEMENT =================

from utils.prompt_manager import (
    load_all_prompts, get_prompt, save_prompt as save_prompt_to_file,
    reset_prompt as reset_prompt_to_default, reset_all_prompts,
    DEFAULT_PROMPTS
)

@app.route('/admin/prompts')
def admin_prompts():
    if session.get('role') != 'admin':
        return redirect(url_for('login'))
    
    prompts = load_all_prompts()
    
    # Build defaults list (just id + prompt text for comparison in JS)
    defaults = [{"id": d["id"], "prompt": d["prompt"]} for d in DEFAULT_PROMPTS]
    
    return render_template('admin_prompts.html', 
                         prompts=prompts,
                         prompts_json=json.dumps(prompts, ensure_ascii=False),
                         defaults_json=json.dumps(defaults, ensure_ascii=False))


@app.route('/api/admin/prompts/<string:prompt_id>', methods=['PUT'])
def api_update_prompt(prompt_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 403
    
    data = request.get_json(force=True)
    new_text = data.get('prompt', '').strip()
    
    if not new_text:
        return jsonify({"error": "Prompt text cannot be empty"}), 400
    
    if save_prompt_to_file(prompt_id, new_text):
        return jsonify({"ok": True, "message": f"Prompt '{prompt_id}' saved"})
    else:
        return jsonify({"error": "Failed to save prompt"}), 500


@app.route('/api/admin/prompts/<string:prompt_id>/reset', methods=['POST'])
def api_reset_prompt(prompt_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 403
    
    if reset_prompt_to_default(prompt_id):
        # Return the default prompt text so the UI can update
        from utils.prompt_manager import get_default_prompt
        default_text = get_default_prompt(prompt_id)
        return jsonify({"ok": True, "prompt": default_text, "message": f"Prompt '{prompt_id}' reset to default"})
    else:
        return jsonify({"error": "Prompt not found or failed to reset"}), 400


@app.route('/api/admin/prompts/reset-all', methods=['POST'])
def api_reset_all_prompts():
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 403
    
    if reset_all_prompts():
        return jsonify({"ok": True, "message": "All prompts reset to defaults"})
    else:
        return jsonify({"error": "Failed to reset prompts"}), 500



@app.route('/api/admin/users', methods=['POST'])
def api_create_user():
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 403
    
    data = request.get_json(force=True)
    username = data.get('username', '').strip().lower()
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')
    role = data.get('role', 'marketing')
    display_name = data.get('display_name', '').strip()
    
    if not username or not email or not password:
        return jsonify({"error": "Username, email and password are required"}), 400
    
    if User.query.filter((User.username == username) | (User.email == email)).first():
        return jsonify({"error": "Username or email already exists"}), 400
    
    user = User(
        username=username,
        email=email,
        role=role,
        display_name=display_name or username,
        is_active=True
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    
    return jsonify({"ok": True, "id": user.id, "message": f"User {username} created"})


@app.route('/api/admin/users/<int:user_id>', methods=['PUT'])
def api_update_user(user_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 403
    
    user = User.query.get_or_404(user_id)
    data = request.get_json(force=True)
    
    if 'display_name' in data:
        user.display_name = data['display_name'].strip()
    if 'role' in data and data['role'] in ('admin', 'marketing', 'director', 'web'):
        user.role = data['role']
    if 'is_active' in data:
        user.is_active = bool(data['is_active'])
    if 'password' in data and data['password']:
        user.set_password(data['password'])
    
    db.session.commit()
    return jsonify({"ok": True, "message": f"User {user.username} updated"})


@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
def api_delete_user(user_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 403
    
    user = User.query.get_or_404(user_id)
    if user.id == session.get('user_id'):
        return jsonify({"error": "Cannot delete your own account"}), 400
    
    # Nullify foreign key references before hard-deleting
    ProductVersion.query.filter_by(created_by_id=user.id).update({"created_by_id": None})
    FieldChangeLog.query.filter_by(user_id=user.id).update({"user_id": None})
    db.session.delete(user)
    db.session.commit()
    return jsonify({"ok": True, "message": f"User {user.username} permanently deleted"})


# ================= VERSION HISTORY API =================

@app.route('/api/product/<int:product_id>/versions')
def api_product_versions(product_id):
    versions = ProductVersion.query.filter_by(product_id=product_id).order_by(ProductVersion.version_num.desc()).all()
    result = []
    for v in versions:
        result.append({
            "id": v.id,
            "version_num": v.version_num,
            "label": v.label,
            "workflow_stage": v.workflow_stage,
            "created_by": v.created_by.display_name if v.created_by else "System",
            "created_at": v.created_at.strftime('%d %b %Y, %H:%M')
        })
    return jsonify(result)


@app.route('/api/product/<int:product_id>/versions/<int:version_id>/restore', methods=['POST'])
def api_restore_version(product_id, version_id):
    product = Product.query.get_or_404(product_id)
    version = ProductVersion.query.get_or_404(version_id)
    
    if version.product_id != product_id:
        return jsonify({"error": "Version does not belong to this product"}), 400
    
    # Save current state as a snapshot before restoring
    save_version_snapshot(product, label=f"Before rolling back to version {version.version_num}")
    
    # Restore
    product.pis_data = copy.deepcopy(version.pis_data)
    product.spec_data = copy.deepcopy(version.spec_data)
    product.revision_data = copy.deepcopy(version.revision_data)
    product.workflow_stage = version.workflow_stage
    
    db.session.commit()
    
    log_event(product.id, get_current_username(), 'Rolled Back to Previous Version', 
              f'The product was rolled back to version {version.version_num} ({version.label}).', 'action')
    
    return jsonify({"ok": True, "message": f"Restored to version {version.version_num}"})


# ================= FIELD CHANGE LOG API =================

@app.route('/api/product/<int:product_id>/changelog')
def api_product_changelog(product_id):
    changes = FieldChangeLog.query.filter_by(product_id=product_id).order_by(FieldChangeLog.timestamp.desc()).limit(100).all()
    result = []
    for c in changes:
        section = _get_field_section(c.field_name) if c.field_name else 'Other'
        result.append({
            "id": c.id,
            "field_name": c.field_name,
            "section": section,
            "old_value": c.old_value,
            "new_value": c.new_value,
            "version_num": c.version_num,
            "user": c.user.display_name if c.user else "System",
            "timestamp": c.timestamp.strftime('%d %b %Y, %H:%M')
        })
    return jsonify(result)


@app.route('/api/product/<int:product_id>/changes_at')
def api_product_changes_at(product_id):
    """Return all field-level changes near a given timestamp (±120s window).
    Used by the timeline to show inline diffs when clicking on 'Draft Updated' etc.
    """
    ts_str = request.args.get('date', '')  # expected: YYYY-MM-DD
    tm_str = request.args.get('time', '')  # expected: HH:MM
    if not ts_str or not tm_str:
        return jsonify([]), 400

    try:
        target_dt = datetime.strptime(f"{ts_str} {tm_str}", '%Y-%m-%d %H:%M')
    except ValueError:
        return jsonify([]), 400

    window = timedelta(seconds=120)
    start = target_dt - window
    end = target_dt + window

    changes = FieldChangeLog.query.filter(
        FieldChangeLog.product_id == product_id,
        FieldChangeLog.timestamp >= start,
        FieldChangeLog.timestamp <= end
    ).order_by(FieldChangeLog.timestamp.asc()).all()

    result = []
    for c in changes:
        section = _get_field_section(c.field_name) if c.field_name else 'Other'
        result.append({
            "field_name": c.field_name,
            "section": section,
            "old_value": c.old_value,
            "new_value": c.new_value,
            "version_num": c.version_num,
            "user": c.user.display_name if c.user else "System",
            "timestamp": c.timestamp.strftime('%d %b %Y, %H:%M')
        })
    return jsonify(result)


# ================= IMAGE HEALTH CHECK =================

@app.route('/api/images/cleanup', methods=['GET'])
def api_cleanup_images():
    """Scan all products for broken image paths and clean them up."""
    products = Product.query.all()
    fixed = []
    
    for p in products:
        changed = False
        
        # Check main image
        if p.image_path:
            full_path = os.path.join('static', p.image_path)
            if not os.path.exists(full_path) or os.path.getsize(full_path) < 500:
                fixed.append({
                    'id': p.id,
                    'model': p.model_name,
                    'type': 'main_image',
                    'broken_path': p.image_path,
                    'reason': 'file_missing' if not os.path.exists(full_path) else 'file_corrupt'
                })
                p.image_path = None
                changed = True
        
        # Check additional images
        if p.additional_images:
            clean_imgs = []
            for img in p.additional_images:
                full_path = os.path.join('static', img)
                if os.path.exists(full_path) and os.path.getsize(full_path) >= 500:
                    clean_imgs.append(img)
                else:
                    fixed.append({
                        'id': p.id,
                        'model': p.model_name,
                        'type': 'additional_image',
                        'broken_path': img,
                        'reason': 'file_missing' if not os.path.exists(full_path) else 'file_corrupt'
                    })
            
            if len(clean_imgs) != len(p.additional_images):
                p.additional_images = clean_imgs
                flag_modified(p, 'additional_images')
                changed = True
        
        # If main image is None but we have additional images, promote the first one
        if not p.image_path and p.additional_images and len(p.additional_images) > 0:
            p.image_path = p.additional_images.pop(0)
            p.additional_images = p.additional_images
            flag_modified(p, 'additional_images')
            changed = True
    
    if fixed:
        db.session.commit()
    
    return {
        'status': 'success',
        'total_products': len(products),
        'broken_paths_fixed': len(fixed),
        'details': fixed
    }


# ================= PURGE =================

@app.route('/purge_all_data', methods=['POST'])
def purge_all_data():
    """Nuclear option: Clear all products, history, and uploaded images."""
    try:
        FieldChangeLog.query.delete()
        ProductVersion.query.delete()
        ProductHistory.query.delete()
        Product.query.delete()
        
        upload_folder = app.config['UPLOAD_FOLDER']
        if os.path.exists(upload_folder):
            import shutil
            for filename in os.listdir(upload_folder):
                file_path = os.path.join(upload_folder, filename)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)
                except Exception as e:
                    print(f'Failed to delete {file_path}. Reason: {e}')
        
        db.session.commit()
        
        # Clear background job tracker
        Job.query.delete()
        
        flash("All system data has been successfully cleared.", "success")
        
    except Exception as e:
        db.session.rollback()
        flash(f"Error purging data: {str(e)}", "error")
    
    referrer = request.referrer or url_for('login')
    return redirect(referrer)


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)