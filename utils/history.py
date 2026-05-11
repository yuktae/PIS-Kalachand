"""
Event logging utilities for PIS System
Handles product history and event logging.

Phase History v2 — every log entry now carries:
  • workflow_stage  — the stage the event happened in
  • actor_role      — role of the user (resolved via User table by username)
  • version_id      — optional FK to the snapshot taken at this moment
  • expires_at      — timestamp + 180 days, used by the cleanup process

All four are keyword-only with sensible defaults, so legacy callers that
only pass (product_id, actor, title, description, action_type) continue
to work unchanged.
"""

from datetime import datetime, timedelta, timezone
from model import db, ProductHistory, Product, User


# Records expire 6 months (180 days) after creation. The Phase 4 cleanup
# wipes expired rows but always preserves the most-recent major snapshot
# per product so the restore-from-history feature has an anchor.
HISTORY_TTL_DAYS = 180


def _utcnow():
    """Tz-naive UTC datetime — matches the existing DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def expiry_from(ts):
    """Compute the expiry timestamp for a row created at `ts`. Exposed at
    module level so other modules (helpers.save_version_snapshot,
    helpers.diff_and_log, the Phase 4 cleanup) share one TTL definition."""
    return ts + timedelta(days=HISTORY_TTL_DAYS)


def _resolve_role(actor):
    """Look up a user's role by username. Returns None for unknown users
    so the caller can override via the `actor_role` kwarg. 'System' is
    a sentinel we use for automated actions (e.g. AI auto-extraction)."""
    if not actor:
        return None
    if actor == 'System':
        return 'system'
    try:
        u = User.query.filter_by(username=actor).first()
        return u.role if u else None
    except Exception:
        return None


def _resolve_stage(product_id):
    """Best-effort: pull the product's current workflow_stage so callers
    don't have to look it up. Returns None on any failure."""
    try:
        p = Product.query.get(product_id)
        return p.workflow_stage if p else None
    except Exception:
        return None


def log_event(product_id, actor, title, description, action_type='neutral',
              *, workflow_stage=None, actor_role=None, version_id=None):
    """Logs an event to the ProductHistory table.

    Positional args (backward-compatible):
        action_type: 'neutral' (gray), 'waiting' (blue), 'action' (red),
                     'success' (green).

    Keyword-only extras (all optional, all auto-resolved when omitted):
        workflow_stage  — stage the event happened in. When omitted,
                          falls back to the product's current stage.
        actor_role      — role of the user. When omitted, resolved from
                          the User table by username.
        version_id      — ProductVersion.id if a snapshot was captured
                          at this moment. Lets the UI render a Restore
                          button next to this row.

    expires_at is always set to now + HISTORY_TTL_DAYS so the cleanup
    process has a deterministic retention deadline.
    """
    try:
        ts = _utcnow()
        stage = workflow_stage if workflow_stage is not None else _resolve_stage(product_id)
        role = actor_role if actor_role is not None else _resolve_role(actor)
        event = ProductHistory(
            product_id=product_id,
            actor=actor,
            action_title=title,
            description=description,
            action_type=action_type,
            timestamp=ts,
            workflow_stage=stage,
            actor_role=role,
            version_id=version_id,
            expires_at=expiry_from(ts),
        )
        db.session.add(event)
        db.session.commit()
    except Exception as e:
        print(f"Failed to log history: {e}")
