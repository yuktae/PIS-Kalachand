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
# Current rules (2026-05 — narrowed from the previous escape-hatch model):
#   - Marketing deletes only their initial AI-generated drafts (the
#     "New AI Generated PIS" tab — workflow_stage='marketing_draft'). The
#     moment editing begins (marketing_in_progress) or a director gets
#     involved, the row stays in the audit trail.
#   - Director and Web have NO manual delete option. The product
#     either moves forward through the workflow or, once finalized,
#     gets cleaned up automatically by utils.finalized_cleanup after
#     180 days. This prevents accidental loss of approved work.
#   - Admin manually deletes only FINALIZED products. Older finalized
#     rows are swept by the periodic auto-deletion task; the admin
#     button is for the rare case of an early manual removal.
#
# Previously kept _AnyStage sentinel for admin; removed when admin lost
# the all-stages escape hatch.

_ALL_STAGES = frozenset({
    Stage.MARKETING_DRAFT, Stage.MARKETING_IN_PROGRESS,
    Stage.MARKETING_CHANGES_REQUESTED, Stage.PENDING_DIRECTOR_PIS,
    Stage.READY_FOR_WEB, Stage.SPECSHEET_DRAFT,
    Stage.PENDING_DIRECTOR_SPEC, Stage.WEB_CHANGES_REQUESTED,
    Stage.FINALIZED,
})

DELETE_PERMISSIONS = {
    'marketing': {Stage.MARKETING_DRAFT},
    'director':  set(),
    'web':       set(),
    # Admin keeps a global escape hatch — needed for the dedicated
    # Product Deletion page where individual products at ANY stage
    # can be removed. The 6-month automatic sweep still only touches
    # FINALIZED products (see utils.finalized_cleanup) — this set
    # only governs MANUAL admin deletes.
    'admin':     _ALL_STAGES,
}


def can_delete(role, stage):
    """True iff `role` is allowed to soft-delete a product in `stage`.

    Roles outside the four known names (None, '', stray values) always
    return False — fail-closed so a misconfigured session can't delete.
    """
    return stage in DELETE_PERMISSIONS.get(role, set())


def deletable_stages(role):
    """Return the concrete set of stages `role` may delete.

    Empty set for roles that can't delete anything (director, web,
    unknown). bulk_delete / clear_active use this to build the WHERE
    clause without enumerating per-role cases.
    """
    return frozenset(DELETE_PERMISSIONS.get(role, set()))


# ── DISPLAY LABELS ────────────────────────────────────────────────────────────
#
# Human-friendly names for the workflow_stage values above. The raw values
# (`pending_director_pis`, etc.) capitalize poorly via `.replace('_',' ').title()`
# — "Pis" instead of "PIS", awkward verbosity. Use STAGE_DISPLAY_NAMES anywhere
# you'd otherwise pretty-print a stage string.
STAGE_DISPLAY_NAMES = {
    Stage.MARKETING_DRAFT:              'Marketing Draft',
    Stage.MARKETING_IN_PROGRESS:        'Marketing In Progress',
    Stage.MARKETING_CHANGES_REQUESTED:  'Marketing — Changes Requested',
    Stage.PENDING_DIRECTOR_PIS:         'Awaiting PIS Review',
    Stage.READY_FOR_WEB:                'Ready for Web Team',
    Stage.SPECSHEET_DRAFT:              'SpecSheet Draft',
    Stage.PENDING_DIRECTOR_SPEC:        'Awaiting SpecSheet Review',
    Stage.WEB_CHANGES_REQUESTED:        'Web — Changes Requested',
    Stage.FINALIZED:                    'Finalized',
}


# Pipeline phase grouping — used by the admin Stats dashboard to render the
# Pipeline Snapshot card as a funnel rather than a flat list. Each entry maps
# a phase key to (display label, ordered list of member stages).
STAGE_PHASES = [
    ('marketing', 'Marketing', [
        Stage.MARKETING_DRAFT,
        Stage.MARKETING_IN_PROGRESS,
        Stage.MARKETING_CHANGES_REQUESTED,
    ]),
    ('director', 'Director Review', [
        Stage.PENDING_DIRECTOR_PIS,
        Stage.PENDING_DIRECTOR_SPEC,
    ]),
    ('web', 'Web', [
        Stage.READY_FOR_WEB,
        Stage.SPECSHEET_DRAFT,
        Stage.WEB_CHANGES_REQUESTED,
    ]),
    ('done', 'Done', [
        Stage.FINALIZED,
    ]),
]


def display_stage(stage):
    """Pretty-print a workflow_stage value. Falls back to a title-cased
    replacement of underscores for any unknown/legacy value so the UI never
    shows the raw snake_case string."""
    if not stage:
        return 'Unknown'
    return STAGE_DISPLAY_NAMES.get(stage, stage.replace('_', ' ').title())
