"""
Auth decorators — single source of truth for session-based role gating.

Replaces the ~49 hand-written `if session.get('role') != 'X': ...` checks
that previously lived in every blueprint. A single change here (logging,
new role, switching from session to tokens) now propagates everywhere.

Two decorators:
  - @require_role(*roles)        gates on session['role'] membership
  - @require_login               any logged-in role

HTML routes redirect unauthenticated/unauthorized users to /login.
JSON/API routes return a JSON error with a configurable status code
(default 403; the two endpoints that historically returned 401 keep
that code by passing status=401 explicitly).
"""
from functools import wraps
from flask import session, redirect, url_for, jsonify, Response


def _unauthorized_response(api: bool, ndjson: bool, status: int):
    """Build the failure response shape that matches what the hand-written
    checks returned. Three shapes exist in this codebase:
      - HTML routes:  302 redirect to /login
      - JSON routes:  jsonify({...}), status  (Content-Type: application/json)
      - NDJSON streams: Response with mimetype='application/x-ndjson' and a
        single trailing-newline JSON object (so streaming clients can parse it)
    """
    if ndjson:
        return Response('{"error":"unauthorized"}\n', status=status,
                        mimetype='application/x-ndjson')
    if api:
        return jsonify({"error": "Unauthorized"}), status
    return redirect(url_for('auth.login'))


def require_role(*roles, api=False, ndjson=False, status=403):
    """Gate a route on the caller's session role.

    Args:
        *roles: One or more role names from {'admin','marketing','director','web'}.
                Caller is allowed iff session['role'] is one of these.
        api:    True for JSON endpoints — emits jsonify(...) on failure.
                False (default) for HTML — redirects to the login page.
        ndjson: True for streaming NDJSON endpoints — emits a single-line
                JSON error with mimetype='application/x-ndjson'. Implies api.
        status: HTTP status code for the JSON/NDJSON failure response.
                Ignored when api/ndjson are both False. Defaults to 403; pass
                401 at the api.py / marketing.py sites that historically used 401.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if session.get('role') not in roles:
                return _unauthorized_response(api, ndjson, status)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def require_login(fn):
    """Allow any logged-in role. Used by routes that don't restrict by
    role but still require a valid session (e.g. /compare, history pages)."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('role'):
            return redirect(url_for('auth.login'))
        return fn(*args, **kwargs)
    return wrapper