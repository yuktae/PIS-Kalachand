"""Auth flow tests: login redirect, failed login flash, logout session clear.

All tests in this module require a running PostgreSQL instance (same one the
app is configured for).  They are automatically skipped when the database is
not reachable so CI on machines without Postgres still passes the utility tests.
"""
import os
import pytest


def _postgres_available():
    try:
        import psycopg2, urllib.parse
        from dotenv import load_dotenv
        load_dotenv()
        url = os.environ.get('DATABASE_URL', 'postgresql://postgres:postgres@localhost:5432/pis_system')
        p = urllib.parse.urlparse(url)
        conn = psycopg2.connect(
            host=p.hostname, port=p.port or 5432,
            database=p.path.lstrip('/'),
            user=p.username, password=p.password,
            connect_timeout=2,
        )
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_available(),
    reason='PostgreSQL not reachable — skipping DB tests',
)


def test_login_marketing_redirects_to_dashboard(client, marketing_user):
    resp = client.post('/login', data={
        'email': marketing_user['email'],
        'password': marketing_user['password'],
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert '/dashboard/marketing' in resp.headers['Location']


def test_login_director_redirects_to_dashboard(client, director_user):
    resp = client.post('/login', data={
        'email': director_user['email'],
        'password': director_user['password'],
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert '/dashboard/director' in resp.headers['Location']


def test_login_wrong_password_shows_error(client, marketing_user):
    resp = client.post('/login', data={
        'email': marketing_user['email'],
        'password': 'totally_wrong',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'Invalid email or password' in resp.data


def test_login_unknown_email_shows_error(client):
    resp = client.post('/login', data={
        'email': 'nobody_pytest@test.com',
        'password': 'anything',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'Invalid email or password' in resp.data


def test_login_inactive_user_shows_error(client, app):
    from model import db, User
    with app.app_context():
        user = User(username='inactive_pytest', email='inactive_pytest@test.com',
                    role='marketing', is_active=False)
        user.set_password('pass1234')
        db.session.add(user)
        db.session.commit()
        uid = user.id

    resp = client.post('/login', data={
        'email': 'inactive_pytest@test.com',
        'password': 'pass1234',
    }, follow_redirects=True)
    assert b'deactivated' in resp.data

    with app.app_context():
        u = db.session.get(User, uid)
        if u:
            db.session.delete(u)
            db.session.commit()


def test_logout_clears_session(client, marketing_user):
    client.post('/login', data={
        'email': marketing_user['email'],
        'password': marketing_user['password'],
    })
    resp = client.get('/logout', follow_redirects=False)
    assert resp.status_code == 302

    # After logout the marketing dashboard must redirect back to login (root '/')
    resp2 = client.get('/dashboard/marketing', follow_redirects=False)
    assert resp2.status_code == 302
    assert resp2.headers['Location'] in ('/', 'http://localhost/')
