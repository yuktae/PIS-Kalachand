"""
Admin blueprint — user management and prompt management routes.
"""
import json

from flask import Blueprint, session, redirect, url_for, render_template, request, jsonify, flash

from model import db, User, ProductVersion, FieldChangeLog, Product, ProductHistory, Job
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
