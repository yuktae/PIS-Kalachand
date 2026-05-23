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
