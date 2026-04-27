"""
Auth blueprint — login and logout routes.
"""
from flask import Blueprint, session, redirect, url_for, render_template, request, flash
from model import db, User
from extensions import limiter

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/')
def login():
    if session.get('user_id'):
        role = session.get('role')
        if role == 'admin':      return redirect(url_for('admin.admin_users'))
        if role == 'marketing':  return redirect(url_for('marketing.dashboard_marketing'))
        if role == 'director':   return redirect(url_for('director.dashboard_director'))
        if role == 'web':        return redirect(url_for('web.dashboard_web'))
    return render_template('login.html')


@auth_bp.route('/login', methods=['POST'])
@limiter.limit("5 per minute")
def login_post():
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')

    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        flash('Invalid email or password.', 'error')
        return redirect(url_for('auth.login'))
    if not user.is_active:
        flash('Your account has been deactivated. Contact admin.', 'error')
        return redirect(url_for('auth.login'))

    session.permanent = True
    session['user_id'] = user.id
    session['username'] = user.display_name or user.username
    session['role'] = user.role

    if user.role == 'admin':      return redirect(url_for('admin.admin_users'))
    if user.role == 'marketing':  return redirect(url_for('marketing.dashboard_marketing'))
    if user.role == 'director':   return redirect(url_for('director.dashboard_director'))
    if user.role == 'web':        return redirect(url_for('web.dashboard_web'))
    return redirect(url_for('auth.login'))


@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))
