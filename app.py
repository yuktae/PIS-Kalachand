"""
PIS Application — entry point.

Gunicorn CMD: gunicorn ... app:app
The `app` variable at module level is created by create_app() so Gunicorn can import it.
"""
import os
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from datetime import timedelta, datetime

from flask import Flask
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from dotenv import load_dotenv

from model import db, User, Job

load_dotenv()


def create_app() -> Flask:
    application = Flask(__name__)

    # ── CONFIG ──────────────────────────────────────────────────────────────
    basedir = os.path.abspath(os.path.dirname(__file__))
    application.config['BASE_DIR'] = basedir

    _secret = os.environ.get('SECRET_KEY') or os.environ.get('FLASK_SECRET_KEY')
    if not _secret:
        if os.environ.get('FLASK_ENV') == 'production':
            raise RuntimeError(
                "SECRET_KEY environment variable must be set in production. "
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        _secret = 'dev-insecure-key-set-SECRET_KEY-before-deploying'
        print('⚠️  SECRET_KEY not set — using insecure dev default. Set SECRET_KEY in .env before deploying.')
    application.config['SECRET_KEY'] = _secret

    database_url = os.environ.get('DATABASE_URL', 'postgresql://postgres:postgres@localhost:5432/pis_system')
    if database_url.startswith('postgres://') and not database_url.startswith('postgresql://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    application.config['SQLALCHEMY_DATABASE_URI'] = database_url
    application.config['UPLOAD_FOLDER'] = 'static/uploads'
    application.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    application.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_recycle': 280,
        'pool_pre_ping': True,
        'pool_size': 10,
        'max_overflow': 20,
    }
    application.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000
    application.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)

    # ── EXTENSIONS ──────────────────────────────────────────────────────────
    db.init_app(application)
    Migrate(application, db)
    CSRFProtect(application)

    from extensions import limiter
    limiter.init_app(application)

    # ── PERFORMANCE HEADERS ─────────────────────────────────────────────────
    @application.after_request
    def add_performance_headers(response):
        if response.content_type and ('image' in response.content_type or 'font' in response.content_type):
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        elif response.content_type and 'text/html' in response.content_type:
            response.headers['Cache-Control'] = 'no-cache, must-revalidate'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    # ── TEMPLATE GLOBALS ────────────────────────────────────────────────────
    from utils.storage import get_image_url
    application.jinja_env.globals['get_image_url'] = get_image_url

    # ── BLUEPRINTS ───────────────────────────────────────────────────────────
    from blueprints.auth      import auth_bp
    from blueprints.marketing import marketing_bp
    from blueprints.director  import director_bp
    from blueprints.web       import web_bp
    from blueprints.admin     import admin_bp
    from blueprints.api       import api_bp

    application.register_blueprint(auth_bp)
    application.register_blueprint(marketing_bp)
    application.register_blueprint(director_bp)
    application.register_blueprint(web_bp)
    application.register_blueprint(admin_bp)
    application.register_blueprint(api_bp)

    # ── DATABASE INIT (runs once per worker boot) ────────────────────────────
    with application.app_context():
        if not os.path.exists('instance'):
            os.makedirs('instance')

        try:
            with db.engine.connect() as conn:
                db.metadata.create_all(bind=conn, checkfirst=True)
                conn.commit()
        except Exception:
            pass

        # Install PostgreSQL audit trigger (uses advisory lock for multi-worker safety)
        try:
            audit_trigger_path = os.path.join(basedir, 'audit_trigger.sql')
            if os.path.exists(audit_trigger_path):
                with open(audit_trigger_path, 'r') as f:
                    audit_sql = f.read()
                with db.engine.connect() as conn:
                    conn.execute(db.text("SELECT pg_advisory_lock(42424242)"))
                    try:
                        conn.execute(db.text(audit_sql))
                        conn.commit()
                        print('✅ PostgreSQL audit trigger installed')
                    finally:
                        conn.execute(db.text("SELECT pg_advisory_unlock(42424242)"))
        except Exception as e:
            print(f'ℹ️ Audit trigger note: {e}')

        if not os.path.exists(application.config['UPLOAD_FOLDER']):
            os.makedirs(application.config['UPLOAD_FOLDER'])

        # Reset any jobs stuck in 'processing' from a previous crashed/restarted run
        try:
            stuck_jobs = Job.query.filter_by(status='processing').all()
            if stuck_jobs:
                for j in stuck_jobs:
                    j.status = 'failed'
                    j.message = 'App restarted — job was interrupted. Please re-submit.'
                    j.completed_at = datetime.utcnow()
                db.session.commit()
                print(f'⚠️  Reset {len(stuck_jobs)} interrupted job(s) to failed state')
        except Exception:
            db.session.rollback()

        # Seed default admin account on first run
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

    return application


# Module-level `app` so Gunicorn can do `gunicorn ... app:app`
app = create_app()


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
