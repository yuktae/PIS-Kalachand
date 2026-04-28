"""
Auth blueprint — login and logout routes.
"""
from flask import Blueprint, session, redirect, url_for, render_template, request, flash
from model import User
from extensions import limiter, BOOT_TOKEN

auth_bp = Blueprint('auth', __name__)


def _redirect_by_role(role: str):
    destinations = {
        'admin':     'admin.admin_users',
        'marketing': 'marketing.dashboard_marketing',
        'director':  'director.dashboard_director',
        'web':       'web.dashboard_web',
    }
    endpoint = destinations.get(role)
    if endpoint:
        return redirect(url_for(endpoint))
    return redirect(url_for('auth.login'))


@auth_bp.route('/')
def login():
    if session.get('user_id') and session.get('_boot') == BOOT_TOKEN:
        return _redirect_by_role(session.get('role'))
    return render_template('login.html')


@auth_bp.route('/login', methods=['POST'])
@limiter.limit("5 per minute")
def login_post():
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')

    if not email or not password:
        flash('Email and password are required.', 'error')
        return redirect(url_for('auth.login'))

    user = User.query.filter_by(email=email).first()

    # Always call check_password regardless of whether the user exists to
    # prevent timing-based user enumeration.
    valid = user is not None and user.check_password(password)
    if not valid:
        flash('Invalid email or password.', 'error')
        return redirect(url_for('auth.login'))

    if not user.is_active:
        flash('Your account has been deactivated. Contact your administrator.', 'error')
        return redirect(url_for('auth.login'))

    session.clear()  # prevent session fixation
    session.permanent = True
    session['_boot'] = BOOT_TOKEN
    session['user_id'] = user.id
    session['username'] = user.display_name or user.username
    session['role'] = user.role

    return _redirect_by_role(user.role)


@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))
