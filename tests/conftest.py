import pytest
from model import db as _db, User, Product


@pytest.fixture(scope='session')
def app():
    from app import app as flask_app
    from extensions import limiter
    flask_app.config['TESTING'] = True
    flask_app.config['WTF_CSRF_ENABLED'] = False
    flask_app.config['RATELIMIT_ENABLED'] = False
    # Directly disable the limiter instance — config alone is not enough when the
    # limiter is already initialised and tests share one session-scoped app.
    # 'enabled' is a plain instance attr (not a property) in Flask-Limiter 3.x.
    limiter.enabled = False
    return flask_app


@pytest.fixture(scope='session', autouse=True)
def _scrub_stale_test_users(app):
    """Delete any test users left over from a previously interrupted run."""
    stale_usernames = ['inactive_pytest', 'mkt_pytest', 'dir_pytest', 'adm_pytest']
    with app.app_context():
        for username in stale_usernames:
            u = User.query.filter_by(username=username).first()
            if u:
                _db.session.delete(u)
        _db.session.commit()
    yield


@pytest.fixture
def client(app):
    return app.test_client()


def _make_user(app, username, email, role):
    """Helper: create a user and return credentials dict. Caller handles cleanup."""
    with app.app_context():
        user = User(username=username, email=email, role=role, is_active=True)
        user.set_password('pass1234')
        _db.session.add(user)
        _db.session.commit()
        return {'email': email, 'password': 'pass1234', 'id': user.id}


def _delete_user(app, user_id):
    with app.app_context():
        u = _db.session.get(User, user_id)
        if u:
            _db.session.delete(u)
            _db.session.commit()


@pytest.fixture
def marketing_user(app):
    creds = _make_user(app, 'mkt_pytest', 'mkt_pytest@pis.test', 'marketing')
    yield creds
    _delete_user(app, creds['id'])


@pytest.fixture
def director_user(app):
    creds = _make_user(app, 'dir_pytest', 'dir_pytest@pis.test', 'director')
    yield creds
    _delete_user(app, creds['id'])


@pytest.fixture
def admin_user(app):
    creds = _make_user(app, 'adm_pytest', 'adm_pytest@pis.test', 'admin')
    yield creds
    _delete_user(app, creds['id'])


@pytest.fixture
def sample_product(app):
    """Create a minimal Product in marketing_draft stage. Auto-cleans up."""
    with app.app_context():
        p = Product(
            model_name='Pytest Widget X100',
            workflow_stage='marketing_draft',
            pis_data={
                'header_info': {'product_name': 'Widget X100', 'brand': 'Acme',
                                'model_number': 'X100', 'price_estimate': ''},
                'range_overview': 'A test widget.',
                'sales_arguments': ['Durable', 'Fast'],
                'technical_specifications': {'Weight': '1kg'},
                'warranty_service': {'period': '1 year', 'coverage': 'Full'},
            },
        )
        _db.session.add(p)
        _db.session.commit()
        pid = p.id
    yield pid
    with app.app_context():
        prod = _db.session.get(Product, pid)
        if prod:
            _db.session.delete(prod)
            _db.session.commit()
