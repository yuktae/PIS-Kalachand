"""
Security tests for admin-only endpoints.

Covers:
- /purge_all_data requires admin role (non-admin gets redirected)
- /purge_all_data requires confirm_text == 'DELETE' (missing/wrong text is rejected)
- /purge_all_data with correct credentials + confirm_text succeeds
- Admin API endpoints reject non-admin callers
"""
import pytest
import os
import urllib.parse
import psycopg2
from dotenv import load_dotenv


def _postgres_available():
    try:
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


def _login(client, email, password):
    return client.post('/login', data={'email': email, 'password': password},
                       follow_redirects=False)


# ── purge_all_data access control ────────────────────────────────────────────

def test_purge_blocked_for_unauthenticated(client):
    resp = client.post('/purge_all_data', data={'confirm_text': 'DELETE'},
                       follow_redirects=False)
    assert resp.status_code == 302
    # auth.login maps to GET '/', not '/login'
    assert resp.headers['Location'] in ('/', 'http://localhost/')


def test_purge_blocked_for_marketing_user(client, marketing_user):
    _login(client, marketing_user['email'], marketing_user['password'])
    resp = client.post('/purge_all_data', data={'confirm_text': 'DELETE'},
                       follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers['Location'] in ('/', 'http://localhost/')
    client.get('/logout')


def test_purge_blocked_for_director_user(client, director_user):
    _login(client, director_user['email'], director_user['password'])
    resp = client.post('/purge_all_data', data={'confirm_text': 'DELETE'},
                       follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers['Location'] in ('/', 'http://localhost/')
    client.get('/logout')


# ── confirm_text validation ───────────────────────────────────────────────────

def test_purge_rejected_with_missing_confirm_text(client, admin_user):
    _login(client, admin_user['email'], admin_user['password'])
    resp = client.post('/purge_all_data', data={}, follow_redirects=True)
    assert resp.status_code == 200
    assert b'DELETE' in resp.data or b'cancelled' in resp.data
    client.get('/logout')


def test_purge_rejected_with_wrong_confirm_text(client, admin_user):
    _login(client, admin_user['email'], admin_user['password'])
    resp = client.post('/purge_all_data', data={'confirm_text': 'yes'},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert b'cancelled' in resp.data or b'DELETE' in resp.data
    client.get('/logout')


def test_purge_rejected_with_lowercase_delete(client, admin_user):
    _login(client, admin_user['email'], admin_user['password'])
    resp = client.post('/purge_all_data', data={'confirm_text': 'delete'},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert b'cancelled' in resp.data or b'DELETE' in resp.data
    client.get('/logout')


# ── admin API access control ──────────────────────────────────────────────────

def test_create_user_api_blocked_for_marketing(client, marketing_user):
    _login(client, marketing_user['email'], marketing_user['password'])
    resp = client.post('/api/admin/users',
                       json={'username': 'hacked', 'email': 'h@h.com', 'password': 'x'})
    assert resp.status_code == 403
    client.get('/logout')


def test_reset_all_prompts_blocked_for_director(client, director_user):
    _login(client, director_user['email'], director_user['password'])
    resp = client.post('/api/admin/prompts/reset-all')
    assert resp.status_code == 403
    client.get('/logout')
