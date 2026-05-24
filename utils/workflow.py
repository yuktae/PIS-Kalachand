"""
Workflow stage constants — single source of truth for the 8 stages a
Product moves through.

Background: workflow_stage values used to live as bare string literals
scattered across blueprints, helpers, and the model default. A typo like
'aproved' or 'pending_director_pis_' would silently compare unequal
forever — no exception, no test failure, just a stuck product. With
Stage.X the typo `Stage.APROVED` raises AttributeError at import time.

Values match the DB exactly (Product.workflow_stage is a string column),
so this is purely a typo-safety layer — no schema change, no migration.
"""


class Stage:
    # Initial PIS creation by marketing
    MARKETING_DRAFT = 'marketing_draft'

    # Legacy / transitional: still referenced by the marketing dashboard
    # metrics aggregation (blueprints/marketing.py) but not in the canonical
    # 8-stage flow documented on Product.workflow_stage. Kept as a constant
    # so the reference is greppable; consider removing after confirming no
    # historical rows still carry this value.
    MARKETING_IN_PROGRESS = 'marketing_in_progress'

    # Awaiting director PIS review
    PENDING_DIRECTOR_PIS = 'pending_director_pis'

    # Director requested changes to the PIS — back to marketing
    MARKETING_CHANGES_REQUESTED = 'marketing_changes_requested'

    # PIS approved, ready for web team to build the specsheet
    READY_FOR_WEB = 'ready_for_web'

    # Web team is editing the specsheet
    SPECSHEET_DRAFT = 'specsheet_draft'

    # Awaiting director specsheet approval
    PENDING_DIRECTOR_SPEC = 'pending_director_spec'

    # Director requested specsheet changes — back to web team
    WEB_CHANGES_REQUESTED = 'web_changes_requested'

    # Complete, published
    FINALIZED = 'finalized'


# ── DELETE PERMISSIONS ────────────────────────────────────────────────────────
#
# Single source of truth for which role can soft-delete a product in which
# stage. Used by both the backend (api.py delete endpoints) and the frontend
# (dashboards hide the trash icon on rows the caller can't delete).
#
# Rules:
#   - Marketing deletes their own early-stage drafts only. Once a PIS has
#     been submitted to a director (pending_director_pis) or sent back with
#     change requests (marketing_changes_requested), it carries director
#     history and stays in the audit trail.
#   - Director deletes only things they have already approved
#     (ready_for_web + finalized). Items awaiting their review
#     (pending_director_pis, pending_director_spec) cannot be deleted —
#     a director should approve or request changes, not silently drop
#     work submitted to them.
#   - Web deletes only finalized specsheets — clean-up of completed work.
#   - Admin can delete any stage (escape hatch; matches today's behavior).
#
# `_ANY` is a sentinel set membership check delegates to.
class _AnyStage:
    def __contains__(self, _stage):
        return True


_ANY = _AnyStage()

DELETE_PERMISSIONS = {
    'marketing': {Stage.MARKETING_DRAFT, Stage.MARKETING_IN_PROGRESS},
    'director':  {Stage.READY_FOR_WEB, Stage.FINALIZED},
    'web':       {Stage.FINALIZED},
    'admin':     _ANY,
}


def can_delete(role, stage):
    """True iff `role` is allowed to soft-delete a product in `stage`.

    Roles outside the four known names (None, '', stray values) always
    return False — fail-closed so a misconfigured session can't delete.
    """
    allowed = DELETE_PERMISSIONS.get(role)
    if allowed is None:
        return False
    return stage in allowed


def deletable_stages(role):
    """Return the concrete set of stages `role` may delete, or None for
    admin (meaning "any stage"). Used by clear_active / bulk_delete to
    build a SQL WHERE clause without enumerating cases per role."""
    allowed = DELETE_PERMISSIONS.get(role)
    if allowed is None:
        return frozenset()      # unknown role → nothing deletable
    if isinstance(allowed, _AnyStage):
        return None             # admin → no stage filter
    return frozenset(allowed)
