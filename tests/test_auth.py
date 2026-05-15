"""
Login / logout / role-redirect / inactive-user guard.

Auth is the lowest-trust surface in the app — every other endpoint depends
on the session it sets. Keep these tests fast and pure (no AI/HTTP mocks
needed).
"""
from model import User


def test_login_with_valid_credentials_sets_session(client, make_user, app):
    make_user(role="marketing", email="mary@test.local", password="hunter2-strong")
    resp = client.post("/login", data={"email": "mary@test.local",
                                       "password": "hunter2-strong"},
                       follow_redirects=False)
    assert resp.status_code in (301, 302)
    # Marketing users redirect into the marketing dashboard.
    assert "/dashboard" in resp.headers.get("Location", "") \
        or "marketing" in resp.headers.get("Location", "")
    with client.session_transaction() as sess:
        assert sess.get("role") == "marketing"
        assert sess.get("user_id") is not None


def test_login_with_wrong_password_does_not_set_session(client, make_user):
    make_user(role="marketing", email="bob@test.local", password="correct-pw")
    resp = client.post("/login", data={"email": "bob@test.local",
                                       "password": "WRONG"},
                       follow_redirects=False)
    # Redirects back to /login with a flash, never to a dashboard.
    assert resp.status_code in (301, 302)
    assert "/login" in resp.headers.get("Location", "")
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_login_with_unknown_email_returns_same_response_shape(client):
    """Timing-leak guard from auth.py: check_password is called even when
    the user doesn't exist. We can't measure timing here, but we can at
    least pin the response shape so a regression that returns 404 (which
    would leak existence) is caught."""
    resp = client.post("/login", data={"email": "ghost@nowhere.local",
                                       "password": "anything"},
                       follow_redirects=False)
    assert resp.status_code in (301, 302)
    assert "/login" in resp.headers.get("Location", "")


def test_inactive_user_cannot_login(client, make_user):
    make_user(role="marketing", email="frozen@test.local",
              password="pw-test-1234", is_active=False)
    resp = client.post("/login", data={"email": "frozen@test.local",
                                       "password": "pw-test-1234"},
                       follow_redirects=False)
    assert resp.status_code in (301, 302)
    assert "/login" in resp.headers.get("Location", "")
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_logout_clears_session(client, make_user, app):
    make_user(role="marketing", email="leave@test.local", password="pw-test-1234")
    client.post("/login", data={"email": "leave@test.local",
                                "password": "pw-test-1234"})
    with client.session_transaction() as sess:
        assert sess.get("user_id") is not None
    resp = client.get("/logout", follow_redirects=False)
    assert resp.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_login_missing_fields_does_not_500(client):
    resp = client.post("/login", data={}, follow_redirects=False)
    assert resp.status_code in (301, 302)
    assert "/login" in resp.headers.get("Location", "")


def test_each_role_redirects_to_its_own_dashboard(client, make_user):
    """The four roles each have a distinct landing endpoint defined in
    blueprints/auth.py:_redirect_by_role."""
    roles_and_landing = [
        ("admin",     "/admin"),       # admin.admin_users
        ("marketing", "/dashboard"),   # marketing.dashboard_marketing
        ("director",  "/dashboard"),   # director.dashboard_director
        ("web",       "/dashboard"),   # web.dashboard_web
    ]
    for i, (role, landing_prefix) in enumerate(roles_and_landing):
        email = f"{role}-{i}@test.local"
        make_user(role=role, email=email, username=f"{role}{i}")
        resp = client.post("/login", data={"email": email, "password": "pw-test-1234"},
                           follow_redirects=False)
        assert resp.status_code in (301, 302), f"{role}: {resp.status_code}"
        location = resp.headers.get("Location", "")
        assert landing_prefix in location, f"{role} -> {location!r}"
        client.get("/logout")
