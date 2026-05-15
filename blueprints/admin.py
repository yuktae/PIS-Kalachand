"""
Admin blueprint — user management and prompt management routes.
"""
import json

from flask import Blueprint, session, redirect, url_for, render_template, request, jsonify, flash

from model import db, User, ProductVersion, FieldChangeLog, Product, ProductHistory, Job, ApiCallLog
from utils.prompt_manager import (
    load_all_prompts, save_prompt as save_prompt_to_db,
    reset_prompt as reset_prompt_to_default, reset_all_prompts,
    DEFAULT_PROMPTS, get_default_prompt,
)

admin_bp = Blueprint('admin', __name__)


def _require_admin():
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 403
    return None


# ── USER MANAGEMENT ───────────────────────────────────────────────────────────

@admin_bp.route('/admin/users')
def admin_users():
    if session.get('role') != 'admin':
        return redirect(url_for('auth.login'))
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin_users.html', users=users)


@admin_bp.route('/api/admin/users', methods=['POST'])
def api_create_user():
    err = _require_admin()
    if err: return err
    data = request.get_json(force=True)
    username = data.get('username', '').strip().lower()
    email    = data.get('email', '').strip().lower()
    password = data.get('password', '')
    role     = data.get('role', 'marketing')
    display_name = data.get('display_name', '').strip()
    if not username or not email or not password:
        return jsonify({"error": "Username, email and password are required"}), 400
    if User.query.filter((User.username == username) | (User.email == email)).first():
        return jsonify({"error": "Username or email already exists"}), 400
    user = User(username=username, email=email, role=role,
                display_name=display_name or username, is_active=True)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return jsonify({"ok": True, "id": user.id, "message": f"User {username} created"})


@admin_bp.route('/api/admin/users/<int:user_id>', methods=['PUT'])
def api_update_user(user_id):
    err = _require_admin()
    if err: return err
    user = User.query.get_or_404(user_id)
    data = request.get_json(force=True)
    if 'display_name' in data:
        user.display_name = data['display_name'].strip()
    if 'username' in data:
        new_username = data['username'].strip().lower()
        if new_username and new_username != user.username:
            clash = User.query.filter(User.username == new_username, User.id != user.id).first()
            if clash:
                return jsonify({"error": "Username already taken"}), 400
            user.username = new_username
    if 'email' in data:
        new_email = data['email'].strip().lower()
        if new_email and new_email != user.email:
            clash = User.query.filter(User.email == new_email, User.id != user.id).first()
            if clash:
                return jsonify({"error": "Email already taken"}), 400
            user.email = new_email
    if 'role' in data and data['role'] in ('admin', 'marketing', 'director', 'web'):
        user.role = data['role']
    if 'is_active' in data:
        user.is_active = bool(data['is_active'])
    if 'password' in data and data['password']:
        user.set_password(data['password'])
    db.session.commit()
    return jsonify({"ok": True, "message": f"User {user.username} updated"})


@admin_bp.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
def api_delete_user(user_id):
    err = _require_admin()
    if err: return err
    user = User.query.get_or_404(user_id)
    if user.id == session.get('user_id'):
        return jsonify({"error": "Cannot delete your own account"}), 400
    ProductVersion.query.filter_by(created_by_id=user.id).update({"created_by_id": None})
    FieldChangeLog.query.filter_by(user_id=user.id).update({"user_id": None})
    db.session.delete(user)
    db.session.commit()
    return jsonify({"ok": True, "message": f"User {user.username} permanently deleted"})


# ── STATS & ANALYTICS ─────────────────────────────────────────────────────────

@admin_bp.route('/admin/stats')
def admin_stats():
    if session.get('role') != 'admin':
        return redirect(url_for('auth.login'))

    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func, or_

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    week_ago = now - timedelta(days=7)

    # ── Section 1: Overview ───────────────────────────────────────────────
    total_products  = Product.query.filter(Product.deleted_at.is_(None)).count()
    finalized_count = Product.query.filter(Product.workflow_stage == 'finalized',
                                           Product.deleted_at.is_(None)).count()
    in_progress     = total_products - finalized_count
    active_users    = User.query.filter_by(is_active=True).count()
    products_this_week = Product.query.filter(Product.created_at >= week_ago,
                                              Product.deleted_at.is_(None)).count()

    # ── Section 2: Workflow Pipeline ──────────────────────────────────────
    stage_rows = (db.session.query(Product.workflow_stage, func.count(Product.id))
                  .filter(Product.deleted_at.is_(None))
                  .group_by(Product.workflow_stage).all())
    stage_breakdown = [{'stage': s or 'unknown', 'count': c} for s, c in stage_rows]
    stage_breakdown.sort(key=lambda x: x['count'], reverse=True)

    # Approvals this week — count "Approved" history entries
    approvals_this_week = ProductHistory.query.filter(
        ProductHistory.timestamp >= week_ago,
        ProductHistory.action_title.ilike('%approved%')
    ).count()

    # Avg time draft → finalized: use the last "SpecSheet Approved" timestamp per product
    avg_finalize_days = 0.0
    finalized_products = Product.query.filter(Product.workflow_stage == 'finalized',
                                              Product.deleted_at.is_(None)).all()
    if finalized_products:
        deltas = []
        for p in finalized_products:
            final_evt = (ProductHistory.query
                         .filter(ProductHistory.product_id == p.id,
                                 ProductHistory.action_title.ilike('%specsheet approved%'))
                         .order_by(ProductHistory.timestamp.desc()).first())
            if final_evt and p.created_at:
                deltas.append((final_evt.timestamp - p.created_at).total_seconds() / 86400)
        if deltas:
            avg_finalize_days = round(sum(deltas) / len(deltas), 1)

    # Daily product creation — last 14 days
    daily_counts = []
    for i in range(13, -1, -1):
        day = now - timedelta(days=i)
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end   = day_start + timedelta(days=1)
        cnt = Product.query.filter(Product.created_at >= day_start,
                                   Product.created_at < day_end,
                                   Product.deleted_at.is_(None)).count()
        daily_counts.append({'label': day.strftime('%d %b'), 'short': day.strftime('%a'), 'value': cnt})

    # ── Section 3: AI / Job Activity ──────────────────────────────────────
    # Period scope — admin can pass ?ai_period=7|30|all (default 30 days).
    # Spend/calls/breakdowns all use the same window so the section reads
    # coherently. Success Rate stays all-time because it's a quality metric.
    period_arg = (request.args.get('ai_period') or '30').lower()
    if period_arg == '7':
        period_days = 7
        period_label = 'Last 7 days'
    elif period_arg in ('all', '0'):
        period_days = None
        period_label = 'All time'
    else:
        period_days = 30
        period_label = 'Last 30 days'

    period_start = (now - timedelta(days=period_days)) if period_days else None

    # Success rate uses ALL completed/failed jobs — health, not spend.
    total_jobs     = Job.query.count()
    completed_jobs = Job.query.filter_by(status='completed').count()
    failed_jobs    = Job.query.filter_by(status='failed').count()
    success_rate   = round((completed_jobs / total_jobs * 100), 1) if total_jobs else 0

    # Spend / call aggregates — period-scoped on ApiCallLog.
    log_q = ApiCallLog.query
    if period_start is not None:
        log_q = log_q.filter(ApiCallLog.created_at >= period_start)

    period_calls = log_q.count()
    period_cost  = (db.session.query(func.coalesce(func.sum(ApiCallLog.cost_usd), 0))
                    .filter(ApiCallLog.created_at >= period_start)
                    .scalar() if period_start is not None else
                    db.session.query(func.coalesce(func.sum(ApiCallLog.cost_usd), 0)).scalar())
    period_cost = float(period_cost or 0)

    # Spend per completed job in window — use window-scoped completed jobs
    # so the avg matches the rest of the panel.
    if period_start is not None:
        period_jobs_done = Job.query.filter(Job.status == 'completed',
                                            Job.created_at >= period_start).count()
    else:
        period_jobs_done = completed_jobs
    avg_cost_per_job = (period_cost / period_jobs_done) if period_jobs_done else 0.0

    # Provider breakdown — one row per (provider, model) pair, sorted by spend.
    provider_rows_q = (db.session.query(
            ApiCallLog.provider,
            ApiCallLog.model,
            func.count(ApiCallLog.id).label('calls'),
            func.coalesce(func.sum(ApiCallLog.input_tokens), 0).label('in_tok'),
            func.coalesce(func.sum(ApiCallLog.output_tokens), 0).label('out_tok'),
            func.coalesce(func.sum(ApiCallLog.image_count), 0).label('images'),
            func.coalesce(func.sum(ApiCallLog.query_count), 0).label('queries'),
            func.coalesce(func.sum(ApiCallLog.cost_usd), 0).label('cost'),
        )
        .group_by(ApiCallLog.provider, ApiCallLog.model))
    if period_start is not None:
        provider_rows_q = provider_rows_q.filter(ApiCallLog.created_at >= period_start)
    provider_rows_raw = provider_rows_q.all()

    def _provider_label(provider, model):
        # Friendly labels for the UI.
        if provider == 'gemini':
            return f"Gemini {model.replace('gemini-', '')}" if model else 'Gemini'
        return {
            'google_cse':   'Google Custom Search',
            'brave_search': 'Brave Search',
            'duckduckgo':   'DuckDuckGo',
            'web_scraper':  'Web scraper',
        }.get(provider, provider.title())

    provider_breakdown = []
    for r in provider_rows_raw:
        cost = float(r.cost or 0)
        usage_bits = []
        if r.in_tok or r.out_tok:
            usage_bits.append(f"{int(r.in_tok):,} in / {int(r.out_tok):,} out tokens")
        if r.images:
            usage_bits.append(f"{int(r.images):,} images")
        if r.queries:
            usage_bits.append(f"{int(r.queries):,} queries")
        provider_breakdown.append({
            'label': _provider_label(r.provider, r.model),
            'provider': r.provider,
            'calls':  int(r.calls or 0),
            'usage':  ' · '.join(usage_bits) or '—',
            'cost':   cost,
            'share':  (cost / period_cost * 100) if period_cost > 0 else 0,
        })
    provider_breakdown.sort(key=lambda x: x['cost'], reverse=True)
    providers_count = len(provider_breakdown)

    # Top prompts by spend.
    prompt_rows_q = (db.session.query(
            ApiCallLog.prompt_id,
            func.count(ApiCallLog.id).label('calls'),
            func.coalesce(func.sum(ApiCallLog.cost_usd), 0).label('cost'),
        )
        .filter(ApiCallLog.prompt_id.isnot(None))
        .group_by(ApiCallLog.prompt_id))
    if period_start is not None:
        prompt_rows_q = prompt_rows_q.filter(ApiCallLog.created_at >= period_start)
    top_prompts = []
    for r in prompt_rows_q.all():
        cost = float(r.cost or 0)
        calls = int(r.calls or 0)
        top_prompts.append({
            'prompt_id': r.prompt_id,
            'calls':     calls,
            'cost':      cost,
            'avg_cost':  (cost / calls) if calls else 0.0,
        })
    top_prompts.sort(key=lambda x: x['cost'], reverse=True)
    top_prompts = top_prompts[:5]
    top_prompt_label = str(top_prompts[0]['prompt_id']).replace('_', ' ').title() if top_prompts else None
    top_prompt_cost  = top_prompts[0]['cost'] if top_prompts else 0.0

    # 14-day spend trend (oldest → newest).
    trend = []
    for i in range(13, -1, -1):
        day = now - timedelta(days=i)
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end   = day_start + timedelta(days=1)
        day_cost = (db.session.query(func.coalesce(func.sum(ApiCallLog.cost_usd), 0))
                    .filter(ApiCallLog.created_at >= day_start,
                            ApiCallLog.created_at < day_end).scalar()) or 0
        day_calls = (db.session.query(func.count(ApiCallLog.id))
                     .filter(ApiCallLog.created_at >= day_start,
                             ApiCallLog.created_at < day_end).scalar()) or 0
        trend.append({
            'label': day.strftime('%d %b'),
            'short': day.strftime('%a'),
            'cost':  float(day_cost),
            'calls': int(day_calls),
        })
    trend_max_cost = max((d['cost'] for d in trend), default=0.0)

    # ── Section 4: Team Activity ──────────────────────────────────────────
    user_activity = (db.session.query(
            User.id, User.display_name, User.username, User.role,
            func.count(FieldChangeLog.id).label('edits'),
        )
        .outerjoin(FieldChangeLog, FieldChangeLog.user_id == User.id)
        .group_by(User.id, User.display_name, User.username, User.role)
        .order_by(func.count(FieldChangeLog.id).desc())
        .all())

    user_activity_list = [{
        'id': r.id,
        'display_name': r.display_name or r.username,
        'username': r.username,
        'role': r.role,
        'edits': r.edits or 0,
    } for r in user_activity]

    role_activity_rows = (db.session.query(User.role, func.count(FieldChangeLog.id))
                          .join(FieldChangeLog, FieldChangeLog.user_id == User.id)
                          .group_by(User.role).all())
    role_activity = [{'role': r or 'unknown', 'count': c} for r, c in role_activity_rows]

    # Most active user (top by edits, at least 1 edit)
    most_active_user = next((u for u in user_activity_list if u['edits'] > 0), None)

    return render_template('admin_stats.html',
        total_products=total_products,
        in_progress=in_progress,
        finalized_count=finalized_count,
        active_users=active_users,
        products_this_week=products_this_week,
        stage_breakdown=stage_breakdown,
        approvals_this_week=approvals_this_week,
        avg_finalize_days=avg_finalize_days,
        daily_counts=daily_counts,
        total_jobs=total_jobs,
        completed_jobs=completed_jobs,
        failed_jobs=failed_jobs,
        success_rate=success_rate,
        # AI / Job Activity — period-scoped panel
        ai_period=period_arg,
        ai_period_label=period_label,
        period_calls=period_calls,
        period_cost=period_cost,
        period_jobs_done=period_jobs_done,
        avg_cost_per_job=avg_cost_per_job,
        providers_count=providers_count,
        provider_breakdown=provider_breakdown,
        top_prompts=top_prompts,
        top_prompt_label=top_prompt_label,
        top_prompt_cost=top_prompt_cost,
        spend_trend=trend,
        trend_max_cost=trend_max_cost,
        user_activity=user_activity_list,
        role_activity=role_activity,
        most_active_user=most_active_user,
    )


# ── EXPORT USERS ──────────────────────────────────────────────────────────────

@admin_bp.route('/api/admin/users/export', methods=['POST'])
def api_export_users():
    err = _require_admin()
    if err: return err
    import io, csv
    from flask import Response

    data = request.get_json(force=True) or {}
    roles = data.get('roles') or []
    valid_roles = {'marketing', 'director', 'web'}
    selected = [r for r in roles if r in valid_roles]
    if not selected:
        return jsonify({"error": "Select at least one role"}), 400

    users = User.query.filter(User.role.in_(selected)).order_by(User.role, User.created_at.desc()).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['Display Name', 'Username', 'Email', 'Role', 'Status', 'Created Date'])
    for u in users:
        writer.writerow([
            u.display_name or '',
            u.username,
            u.email,
            u.role,
            'Active' if u.is_active else 'Inactive',
            u.created_at.strftime('%Y-%m-%d') if u.created_at else '',
        ])
    csv_bytes = buf.getvalue().encode('utf-8-sig')
    filename = f"users_export_{datetime_safe_now()}.csv"
    return Response(
        csv_bytes,
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


def datetime_safe_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y%m%d_%H%M%S')


# ── PROMPT MANAGEMENT ─────────────────────────────────────────────────────────

@admin_bp.route('/admin/prompts')
def admin_prompts():
    if session.get('role') != 'admin':
        return redirect(url_for('auth.login'))
    prompts  = load_all_prompts()
    defaults = [{"id": d["id"], "prompt": d["prompt"]} for d in DEFAULT_PROMPTS]
    return render_template('admin_prompts.html',
                           prompts=prompts,
                           prompts_json=json.dumps(prompts, ensure_ascii=False),
                           defaults_json=json.dumps(defaults, ensure_ascii=False))


@admin_bp.route('/api/admin/prompts/<string:prompt_id>', methods=['PUT'])
def api_update_prompt(prompt_id):
    err = _require_admin()
    if err: return err
    data = request.get_json(force=True)
    new_text = data.get('prompt', '').strip()
    if not new_text:
        return jsonify({"error": "Prompt text cannot be empty"}), 400
    if save_prompt_to_db(prompt_id, new_text):
        return jsonify({"ok": True, "message": f"Prompt '{prompt_id}' saved"})
    return jsonify({"error": "Failed to save prompt"}), 500


@admin_bp.route('/api/admin/prompts/<string:prompt_id>/reset', methods=['POST'])
def api_reset_prompt(prompt_id):
    err = _require_admin()
    if err: return err
    if reset_prompt_to_default(prompt_id):
        default_text = get_default_prompt(prompt_id)
        return jsonify({"ok": True, "prompt": default_text, "message": f"Prompt '{prompt_id}' reset to default"})
    return jsonify({"error": "Prompt not found or failed to reset"}), 400


@admin_bp.route('/api/admin/prompts/reset-all', methods=['POST'])
def api_reset_all_prompts():
    err = _require_admin()
    if err: return err
    if reset_all_prompts():
        return jsonify({"ok": True, "message": "All prompts reset to defaults"})
    return jsonify({"error": "Failed to reset prompts"}), 500


# ── PURGE ─────────────────────────────────────────────────────────────────────

@admin_bp.route('/purge_all_data', methods=['POST'])
def purge_all_data():
    import os, shutil
    from flask import current_app

    if session.get('role') != 'admin':
        return redirect(url_for('auth.login'))

    confirm_text = request.form.get('confirm_text', '').strip()
    if confirm_text != 'DELETE':
        flash("Purge cancelled — you must type DELETE exactly to confirm.", "error")
        return redirect(request.referrer or url_for('admin.admin_users'))

    try:
        FieldChangeLog.query.delete()
        ProductVersion.query.delete()
        ProductHistory.query.delete()
        Product.query.delete()
        upload_folder = current_app.config['UPLOAD_FOLDER']
        if os.path.exists(upload_folder):
            for filename in os.listdir(upload_folder):
                file_path = os.path.join(upload_folder, filename)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)
                except Exception as e:
                    print(f'Failed to delete {file_path}: {e}')
        Job.query.delete()
        db.session.commit()
        flash("All system data has been successfully cleared.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error purging data: {str(e)}", "error")
    referrer = request.referrer or url_for('auth.login')
    return redirect(referrer)


# ── PHASE 4: HISTORY CLEANUP ─────────────────────────────────────────────────

@admin_bp.route('/api/admin/history_cleanup/status')
def admin_history_cleanup_status():
    """Return the last-run summary for the cleanup job."""
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 403
    from utils.history_cleanup import read_cleanup_status
    return jsonify(read_cleanup_status())


@admin_bp.route('/api/admin/history_cleanup/run', methods=['POST'])
def admin_history_cleanup_run():
    """Run the 6-month cleanup. POST {"dry_run": true} to preview without
    deleting. Returns the counts of rows affected."""
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 403
    body = request.get_json(silent=True) or {}
    dry_run = bool(body.get('dry_run'))
    from utils.history_cleanup import cleanup_expired_history
    result = cleanup_expired_history(dry_run=dry_run)
    return jsonify(result)
