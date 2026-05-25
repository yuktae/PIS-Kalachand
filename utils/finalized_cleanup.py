"""Finalized-product cleanup.

Soft-deletes products that have been in `workflow_stage = finalized` for
longer than `FINALIZED_RETENTION_DAYS`. The "finalized timestamp" is the
moment the SpecSheet was approved — derived from the latest
ProductHistory event whose action_title matches '%specsheet approved%'.
If a finalized product has no such event (legacy data), `created_at` is
used as a conservative fallback.

The cleanup performs a SOFT delete (`Product.deleted_at = now`). The row
stays in the DB and any audit-trail / restore flow keeps working — only
the dashboards stop listing it.

Public API:
    cleanup_expired_finalized(dry_run=False) -> dict
        Run the sweep. Returns counts + the cutoff timestamp.
    read_cleanup_status() -> dict
        Last-run summary (or {} if never run).

Triggering:
    - Admin UI exposes a "Run cleanup" button (see admin_users.html).
    - Easypanel can hit POST /api/admin/finalized_cleanup/run via cron
      for unattended runs (see deployment note in the admin panel).
"""

import json
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from model import db, Product, ProductHistory


# Retention window: a finalized product older than this gets auto-deleted.
# 180 days = ~6 months. Defined here so we can bump it without hunting
# through call sites.
FINALIZED_RETENTION_DAYS = 180

# Where the "last cleanup" summary is persisted. Plain JSON file so the
# admin panel can read it without a new DB table. Sits next to the
# history_cleanup_status.json file under static/ for consistency.
_STATUS_FILENAME = 'finalized_cleanup_status.json'


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
    """Last-run summary: {ran_at, would_delete, deleted, cutoff_iso, dry_run}.
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
        print(f"⚠ Failed to persist finalized-cleanup status: {e}")


def _finalized_approval_times(product_ids: list[int]) -> dict[int, datetime]:
    """For each product in `product_ids`, return the timestamp of its
    most-recent 'SpecSheet Approved' history event. Products with no
    such event are absent from the result — callers fall back to
    `created_at`."""
    if not product_ids:
        return {}
    rows = (db.session.query(
                ProductHistory.product_id,
                func.max(ProductHistory.timestamp))
            .filter(ProductHistory.product_id.in_(product_ids),
                    ProductHistory.action_title.ilike('%specsheet approved%'))
            .group_by(ProductHistory.product_id)
            .all())
    return {pid: ts for pid, ts in rows}


def cleanup_expired_finalized(dry_run: bool = False) -> dict:
    """Soft-delete every finalized product that's been finalized for
    longer than FINALIZED_RETENTION_DAYS.

    Returns a summary:
        {
            'ran_at':       ISO-8601 UTC timestamp of this run,
            'cutoff_iso':   the boundary — products finalized BEFORE this
                            were swept,
            'eligible':     how many products matched the rule,
            'deleted':      how many we actually flipped (==eligible when
                            dry_run=False, 0 when dry_run=True),
            'dry_run':      echoed back so the UI knows whether anything
                            actually changed,
            'retention_days': the window applied, for transparency,
        }

    SOFT DELETE only. `Product.deleted_at` is set to `now`; the row
    stays in the DB so audit-trail and restore continue to work.
    """
    from utils.workflow import Stage

    now = _utcnow()
    cutoff = now - timedelta(days=FINALIZED_RETENTION_DAYS)

    # Candidate set: not-already-deleted finalized products.
    candidates = (Product.query
                  .filter(Product.workflow_stage == Stage.FINALIZED,
                          Product.deleted_at.is_(None))
                  .all())

    approval_ts = _finalized_approval_times([p.id for p in candidates])

    eligible_ids: list[int] = []
    for p in candidates:
        # Prefer the SpecSheet approval timestamp; fall back to
        # created_at for legacy products without a logged event.
        ref_ts = approval_ts.get(p.id) or p.created_at
        if ref_ts and ref_ts < cutoff:
            eligible_ids.append(p.id)

    deleted = 0
    if eligible_ids and not dry_run:
        deleted = (Product.query
                   .filter(Product.id.in_(eligible_ids))
                   .update({'deleted_at': now}, synchronize_session=False))
        db.session.commit()

    payload = {
        'ran_at':         now.isoformat(),
        'cutoff_iso':     cutoff.isoformat(),
        'eligible':       len(eligible_ids),
        'deleted':        deleted,
        'dry_run':        dry_run,
        'retention_days': FINALIZED_RETENTION_DAYS,
    }
    if not dry_run:
        _write_cleanup_status(payload)
    return payload


# ── Background scheduler ─────────────────────────────────────────────────────
#
# Runs cleanup_expired_finalized() automatically — no admin button needed.
# Implementation is a daemon thread that wakes up every hour and runs the
# sweep when the last real (non-dry) run is older than the daily interval.
#
# Why a background thread (not APScheduler / Celery)?
#   - The Dockerfile uses gunicorn with `--workers 1 --threads 4`. With a
#     single worker process, a daemon thread inside the app is the simplest
#     reliable pattern; no new dependency, no fork-orphaning concerns.
#   - The cleanup is idempotent. Even if a future deploy bumps to multiple
#     workers, status-file dedup means only the first worker per day will
#     actually do the work — concurrent threads see "ran_at < 24h ago" and
#     skip.
#
# Check cadence:
#   - Sleeps 1 hour between checks (CHECK_INTERVAL_SECONDS). At each tick
#     it re-reads the status file; if the last real run is older than
#     DAILY_INTERVAL_SECONDS it runs again. Frequent checks make the loop
#     responsive after container restarts — you don't have to wait a full
#     24h after a deploy for the first cleanup to fire.

CHECK_INTERVAL_SECONDS = 60 * 60          # 1 hour
DAILY_INTERVAL_SECONDS = 24 * 60 * 60     # 1 day


def _due_to_run() -> bool:
    """True when the cleanup hasn't successfully run within the last
    DAILY_INTERVAL_SECONDS. Reads the persisted status file so this
    decision survives app restarts — a fresh container won't re-run a
    sweep that already happened yesterday."""
    status = read_cleanup_status() or {}
    ran_at = status.get('ran_at')
    if not ran_at:
        return True
    try:
        last = datetime.fromisoformat(ran_at)
    except (TypeError, ValueError):
        return True
    if status.get('dry_run'):
        # Dry-run writes don't count as a real sweep.
        return True
    return (_utcnow() - last).total_seconds() >= DAILY_INTERVAL_SECONDS


def _scheduler_loop(app) -> None:
    """Background-thread main loop. Re-enters the Flask app context on
    each tick so SQLAlchemy / current_app helpers work normally. Any
    exception is caught and logged so a transient failure doesn't kill
    the thread."""
    import time

    # Small initial delay so the cleanup doesn't fire during the first
    # second of boot — keeps startup logs readable and lets the rest of
    # the app finish initializing first.
    time.sleep(30)

    while True:
        try:
            with app.app_context():
                if _due_to_run():
                    print("⏰ Running scheduled finalized-product cleanup…")
                    result = cleanup_expired_finalized(dry_run=False)
                    print(f"⏰ Finalized cleanup: deleted={result['deleted']} "
                          f"eligible={result['eligible']} "
                          f"cutoff={result['cutoff_iso']}")
        except Exception as e:
            print(f"⚠ Scheduled finalized-cleanup tick failed: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)


def start_scheduler(app) -> None:
    """Spawn the background cleanup thread.

    Call once from the app factory after the database is initialized.
    Idempotent: a second call no-ops (guarded by app.extensions). Skip
    when TESTING=True so the test suite doesn't get a rogue thread.
    """
    if app.config.get('TESTING'):
        return
    # Avoid double-spawn if the factory runs twice (some test harnesses
    # do this; gunicorn doesn't but cheap to guard).
    if app.extensions.get('_finalized_cleanup_scheduler_running'):
        return
    app.extensions['_finalized_cleanup_scheduler_running'] = True

    import threading
    t = threading.Thread(
        target=_scheduler_loop, args=(app,),
        daemon=True, name='finalized-cleanup-scheduler',
    )
    t.start()
