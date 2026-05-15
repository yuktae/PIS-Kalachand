"""
Smoke tests — the app boots, the DB is reachable, the basic routing works.
If any of these fail, every other test will also fail; check Postgres and
the app factory before chasing other red tests.
"""
from model import db, User, Product, ApiCallLog


def test_app_boots_with_test_config(app):
    assert app.config["TESTING"] is True
    assert app.config["WTF_CSRF_ENABLED"] is False
    assert "pis_system_test" in app.config["SQLALCHEMY_DATABASE_URI"]


def test_database_has_expected_tables(app):
    with app.app_context():
        names = {t.name for t in db.metadata.sorted_tables}
        # Spot-check the load-bearing tables; full schema lives in model.py
        for required in ("user", "product", "product_version",
                         "field_change_log", "job", "api_call_log"):
            assert required in names, f"missing table: {required}"


def test_truncate_actually_resets_between_tests_part1(app, make_user):
    make_user(role="marketing", email="a@test.local", username="a")
    with app.app_context():
        assert User.query.count() == 1


def test_truncate_actually_resets_between_tests_part2(app):
    """If `reset_db` is working, the user created in part1 must be gone."""
    with app.app_context():
        assert User.query.count() == 0
        assert Product.query.count() == 0
        assert ApiCallLog.query.count() == 0


def test_unauthenticated_root_redirects_to_login(client):
    resp = client.get("/", follow_redirects=False)
    # `/` is the login route; an unauthenticated GET should render the login
    # page (200) rather than redirect anywhere else.
    assert resp.status_code == 200
    assert b"login" in resp.data.lower() or b"sign in" in resp.data.lower()


def test_protected_route_redirects_unauthenticated_user(client):
    resp = client.get("/admin/users", follow_redirects=False)
    assert resp.status_code in (301, 302)
    assert "/login" in resp.headers.get("Location", "")
