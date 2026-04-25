import pytest
from model import db as _db, User


@pytest.fixture(scope='session')
def app():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    flask_app.config['WTF_CSRF_ENABLED'] = False
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def marketing_user(app):
    """Create a fresh marketing user and yield its credentials dict. Auto-cleans up."""
    with app.app_context():
        user = User(username='mkt_pytest', email='mkt_pytest@pis.test',
                    role='marketing', is_active=True)
        user.set_password('pass1234')
        _db.session.add(user)
        _db.session.commit()
        creds = {'email': user.email, 'password': 'pass1234', 'id': user.id}
    yield creds
    with app.app_context():
        u = _db.session.get(User, creds['id'])
        if u:
            _db.session.delete(u)
            _db.session.commit()


@pytest.fixture
def director_user(app):
    with app.app_context():
        user = User(username='dir_pytest', email='dir_pytest@pis.test',
                    role='director', is_active=True)
        user.set_password('pass1234')
        _db.session.add(user)
        _db.session.commit()
        creds = {'email': user.email, 'password': 'pass1234', 'id': user.id}
    yield creds
    with app.app_context():
        u = _db.session.get(User, creds['id'])
        if u:
            _db.session.delete(u)
            _db.session.commit()
