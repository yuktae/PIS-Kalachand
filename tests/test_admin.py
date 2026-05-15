"""
Admin blueprint — user CRUD + the new _json_body() guard rail.

Most admin endpoints just wrap SQLAlchemy operations, so the goal here
isn't exhaustive coverage; it's to pin the security-critical paths:
role-guard, malformed-JSON 400 (the Tier-2 fix), and the "cannot delete
your own account" guard.
"""
import json

from model import User


# ── _json_body 400 path (Tier-2 fix) ────────────────────────────────────────


def test_create_user_rejects_empty_body(admin_client):
    resp = admin_client.post("/api/admin/users",
                             data="",
                             content_type="application/json")
    assert resp.status_code == 400


def test_create_user_rejects_non_object_json(admin_client):
    resp = admin_client.post("/api/admin/users",
                             data="[]",
                             content_type="application/json")
    assert resp.status_code == 400


def test_update_user_rejects_empty_body(admin_client, make_user):
    target = make_user(role="marketing", email="targ@test.local",
                       username="targ")
    resp = admin_client.put(f"/api/admin/users/{target.id}",
                            data="",
                            content_type="application/json")
    assert resp.status_code == 400


# ── Role guard ──────────────────────────────────────────────────────────────


def test_create_user_endpoint_requires_admin(marketing_client):
    resp = marketing_client.post(
        "/api/admin/users",
        data=json.dumps({"username": "x", "email": "x@y.z", "password": "pw"}),
        content_type="application/json",
    )
    assert resp.status_code == 403


def test_create_user_endpoint_blocks_unauthenticated(client):
    resp = client.post(
        "/api/admin/users",
        data=json.dumps({"username": "x", "email": "x@y.z", "password": "pw"}),
        content_type="application/json",
    )
    # Unauthenticated requests bounce to /login via the global session guard
    # before they ever hit the role-check.
    assert resp.status_code in (301, 302, 403)


# ── User CRUD happy paths ───────────────────────────────────────────────────


def test_create_user_persists_with_hashed_password(admin_client, app):
    resp = admin_client.post(
        "/api/admin/users",
        data=json.dumps({"username": "newbie", "email": "new@test.local",
                         "password": "fresh-pw-1234", "role": "marketing"}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.data
    body = json.loads(resp.data)
    assert body["ok"] is True

    with app.app_context():
        u = User.query.filter_by(email="new@test.local").first()
        assert u is not None
        assert u.role == "marketing"
        # Stored as a hash, not the plaintext.
        assert u.password_hash != "fresh-pw-1234"
        assert u.check_password("fresh-pw-1234") is True


def test_create_user_rejects_missing_required_fields(admin_client):
    resp = admin_client.post(
        "/api/admin/users",
        data=json.dumps({"username": "only-name"}),
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_create_user_rejects_duplicate_email(admin_client, make_user):
    make_user(role="marketing", email="dup@test.local", username="dup")
    resp = admin_client.post(
        "/api/admin/users",
        data=json.dumps({"username": "dup2", "email": "dup@test.local",
                         "password": "pw-1234", "role": "marketing"}),
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_update_user_can_change_role_and_display_name(admin_client, make_user, app):
    target = make_user(role="marketing", email="upd@test.local",
                       username="upd")
    resp = admin_client.put(
        f"/api/admin/users/{target.id}",
        data=json.dumps({"role": "director", "display_name": "Updated Name"}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.data
    with app.app_context():
        u = User.query.get(target.id)
        assert u is not None
        assert u.role == "director"
        assert u.display_name == "Updated Name"


def test_update_user_ignores_unknown_role(admin_client, make_user, app):
    """The endpoint only writes a new role if it's one of the four valid
    values — junk should be silently dropped, not raise."""
    target = make_user(role="marketing", email="r@test.local", username="r")
    resp = admin_client.put(
        f"/api/admin/users/{target.id}",
        data=json.dumps({"role": "superuser"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    with app.app_context():
        u = User.query.get(target.id)
        assert u is not None
        assert u.role == "marketing"


# ── Self-delete guard ───────────────────────────────────────────────────────


def test_admin_cannot_delete_own_account(admin_client, app):
    with app.app_context():
        me = User.query.filter_by(role="admin").first()
        assert me is not None
        my_id = me.id

    resp = admin_client.delete(f"/api/admin/users/{my_id}")
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert "own account" in body.get("error", "").lower()


def test_admin_can_delete_other_user(admin_client, make_user, app):
    target = make_user(role="marketing", email="del@test.local",
                       username="del")
    target_id = target.id
    resp = admin_client.delete(f"/api/admin/users/{target_id}")
    assert resp.status_code == 200
    with app.app_context():
        assert User.query.get(target_id) is None
