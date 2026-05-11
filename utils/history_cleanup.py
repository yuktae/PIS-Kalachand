"""History cleanup — Phase 4.

Removes rows older than 6 months from product_history, field_change_log,
and product_version, with one critical invariant:

    Every active product ALWAYS keeps its most-recent major snapshot,
    regardless of age. This is the anchor the reconstruction module
    needs to restore any future version from.

Public API:
  cleanup_expired_history(dry_run=False) -> dict
        Run the cleanup. Returns {history, field_changes, versions, ts}.
  read_cleanup_status() -> dict
        Read the last-run summary (or {} if never run).
"""

import json
import os
from datetime import datetime, timezone

from model import db, ProductHistory, FieldChangeLog, ProductVersion, Product


# Where the "last cleanup" summary is persisted. Plain JSON file so the
# admin panel can read it without a new DB table. Sits alongside the
# uploads folder under static/ so it's easy to locate on disk.
_STATUS_FILENAME = 'history_cleanup_status.json'


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _status_path():
    """Resolved at call-time so this module imports without Flask app context."""
    try:
        from flask import current_app
        root = current_app.root_path
    except Exception:
        root = os.getcwd()
    return os.path.join(root, 'static', _STATUS_FILENAME)


def read_cleanup_status() -> dict:
    """Last-run summary: {ran_at, history, field_changes, versions, dry_run}.
    Returns {} if cleanup has never run."""
    try:
        with open(_status_path(), 'r', encoding='utf-8') as f:
            return json.load(f) or {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_cleanup_status(payload: dict) -> None:
    try:
        path = _status_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        print(f"⚠ Failed to persist cleanup status: {e}")


def _anchor_version_ids() -> set[int]:
    """For every active product, find the id of its most-recent major
    snapshot. Cleanup refuses to delete these even when expired."""
    rows = db.session.query(
        ProductVersion.id, ProductVersion.product_id, ProductVersion.version_num
    ).join(Product, Product.id == ProductVersion.product_id).filter(
        ProductVersion.is_major.is_(True),
    ).order_by(
        ProductVersion.product_id.asc(),
        ProductVersion.version_num.desc(),
    ).all()

    anchors: set[int] = set()
    seen_products: set[int] = set()
    for row_id, product_id, _vn in rows:
        if product_id in seen_products:
            continue
        seen_products.add(product_id)
        anchors.add(row_id)
    return anchors


def cleanup_expired_history(dry_run: bool = False) -> dict:
    """Sweep expired rows from the three audit tables.

    Always preserves each product's most-recent major snapshot regardless
    of its expires_at — this is the restore anchor and must survive
    indefinitely.

    For rows with expires_at = NULL (legacy data created before Phase 1),
    falls back to a "older than 180 days from timestamp" rule so the
    cleanup is deterministic.

    Returns counts: {history, field_changes, versions, ts, dry_run}.
    """
    now = _utcnow()

    # History rows — expired OR (no expiry AND older than 180 days).
    history_q = ProductHistory.query.filter(
        db.or_(
            ProductHistory.expires_at < now,
            db.and_(
                ProductHistory.expires_at.is_(None),
                ProductHistory.timestamp < now - _legacy_window(),
            ),
        )
    )

    # Field-change rows — same rule.
    field_q = FieldChangeLog.query.filter(
        db.or_(
            FieldChangeLog.expires_at < now,
            db.and_(
                FieldChangeLog.expires_at.is_(None),
                FieldChangeLog.timestamp < now - _legacy_window(),
            ),
        )
    )

    # Version rows — same rule, plus protect the anchor set.
    anchors = _anchor_version_ids()
    version_q = ProductVersion.query.filter(
        db.or_(
            ProductVersion.expires_at < now,
            db.and_(
                ProductVersion.expires_at.is_(None),
                ProductVersion.created_at < now - _legacy_window(),
            ),
        )
    )
    if anchors:
        version_q = version_q.filter(~ProductVersion.id.in_(anchors))

    history_count = history_q.count()
    field_count = field_q.count()
    version_count = version_q.count()

    if not dry_run:
        # Use bulk deletes — no ORM cascade hits because none of these
        # tables have dependents.
        history_q.delete(synchronize_session=False)
        field_q.delete(synchronize_session=False)
        version_q.delete(synchronize_session=False)
        db.session.commit()

    payload = {
        'ran_at': now.isoformat(),
        'history': history_count,
        'field_changes': field_count,
        'versions': version_count,
        'dry_run': dry_run,
        'anchors_preserved': len(anchors),
    }
    if not dry_run:
        _write_cleanup_status(payload)
    return payload


def _legacy_window():
    """Centralized: how old a row with NULL expires_at must be before
    cleanup will touch it. Matches HISTORY_TTL_DAYS so behavior is
    identical whether or not the expires_at column is populated."""
    from datetime import timedelta
    from utils.history import HISTORY_TTL_DAYS
    return timedelta(days=HISTORY_TTL_DAYS)
