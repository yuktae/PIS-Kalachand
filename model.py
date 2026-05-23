from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.dialects.postgresql import JSONB

db = SQLAlchemy()


def _utcnow_naive() -> datetime:
    """Drop-in replacement for the deprecated datetime.utcnow(). Returns a
    tz-naive UTC datetime so it stays compatible with the existing tz-naive
    `db.DateTime` columns and any callers comparing against historical rows."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ================= USER MODEL =================

class User(db.Model):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # 'admin', 'marketing', 'director', 'web'
    display_name = db.Column(db.String(100))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_utcnow_naive)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<User {self.username} ({self.role})>'


# ================= PRODUCT MODEL =================

class Product(db.Model):
    # Explicit `__init__` so static type-checkers (Pyrefly / Pyright / mypy)
    # see the column-kwarg constructor as valid. SQLAlchemy's declarative
    # base provides `_declarative_constructor` which already accepts these
    # kwargs at runtime; this just publishes the same signature for the
    # type-checking layer. Functionally a no-op — `super().__init__(**kwargs)`
    # delegates to the base constructor unchanged.
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    id = db.Column(db.Integer, primary_key=True)
    model_name = db.Column(db.String(100), nullable=False)

    # Workflow Stage — see utils/workflow.py:Stage for the canonical list.
    # Default kept as a literal (not Stage.MARKETING_DRAFT) to avoid a
    # circular import: utils/__init__.py eagerly pulls in utils.history,
    # which in turn imports from this module. The literal must match
    # Stage.MARKETING_DRAFT exactly.
    workflow_stage = db.Column(db.String(50), default='marketing_draft', index=True)

    created_at = db.Column(db.DateTime, default=_utcnow_naive)
    # Bumped automatically on every UPDATE to this row (autosaves, approvals,
    # category writes, workflow transitions — anything that flushes a change
    # to the DB). Used by the dashboard galleries to surface the most
    # recently touched product first. Indexed for the ORDER BY.
    last_edited_at = db.Column(db.DateTime, default=_utcnow_naive,
                                onupdate=_utcnow_naive, index=True)
    # Soft-delete: set to a timestamp when "deleted", NULL means active
    deleted_at = db.Column(db.DateTime, nullable=True, index=True)

    # Data Fields — JSONB for native PostgreSQL indexing & audit queries
    pis_data = db.Column(JSONB)       # Stores the PIS structure
    spec_data = db.Column(JSONB)      # Stores specific SpecSheet styling/SEO data

    # Stores pending AI revisions & Director section comments
    revision_data = db.Column(JSONB)

    # image_path stores either a relative static path OR a full Azure Blob URL
    image_path = db.Column(db.String(500))
    seo_keywords = db.Column(db.String(255))

    # Approval & Feedback
    director_pis_comments = db.Column(db.Text)
    director_spec_comments = db.Column(db.Text)
    additional_images = db.Column(JSONB, default=list)

    # Per-section comment archive — every comment the director leaves
    # during a revision request is appended here so marketing/web can
    # still review the rationale after accepting or modifying an AI
    # suggestion. Shape: { section_key: [{comment, timestamp, actor}, ...] }
    # Unlike `revision_data` this is never popped on Accept; only cleared
    # at full product reset.
    director_section_comments = db.Column(JSONB, default=dict)

    # Canonical product category — single source of truth. Previously lived
    # in three different JSONB shapes (pis_data.category_data,
    # spec_data.categories, and ghost pis_data.category_A/B/C) that could
    # drift apart. All readers/writers now go through helpers.get_product_category
    # / set_product_category which keep these columns authoritative and
    # mirror to the legacy JSON locations during the transition.
    category_1 = db.Column(db.String(100), nullable=True, index=True)
    category_2 = db.Column(db.String(100), nullable=True)
    category_3 = db.Column(db.String(100), nullable=True)
    magento_category_id = db.Column(db.Integer, nullable=True, index=True)

    # GIN indexes for fast JSONB containment queries (audit trail & search)
    __table_args__ = (
        db.Index('ix_product_pis_data_gin', 'pis_data', postgresql_using='gin'),
        db.Index('ix_product_spec_data_gin', 'spec_data', postgresql_using='gin'),
    )


# ================= PRODUCT HISTORY (EVENT LOG) =================

class ProductHistory(db.Model):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False, index=True)

    actor = db.Column(db.String(50))
    action_title = db.Column(db.String(100))
    description = db.Column(db.Text)
    action_type = db.Column(db.String(20), default='neutral')

    timestamp = db.Column(db.DateTime, default=_utcnow_naive, index=True)

    # Phase History v2 — audit trail context.
    # workflow_stage: which workflow stage the event happened in
    #   (proforma / marketing / director_pis / web / director_spec /
    #    finalized). Indexed for the stage-filter timeline view.
    # actor_role: role of the user at log-time (marketing / director /
    #   web / admin / system). Stored separately from `actor` so the UI
    #   can colour-code rows even when the user's role later changes.
    # version_id: FK to ProductVersion.id when a snapshot was captured
    #   at this moment — lets the UI render a "View at this point" /
    #   "Restore to this version" button.
    # expires_at: timestamp + 180 days. Phase 4 cleanup removes rows
    #   where now() > expires_at (with snapshot-preservation guards).
    workflow_stage = db.Column(db.String(50), index=True, nullable=True)
    actor_role = db.Column(db.String(20), nullable=True)
    version_id = db.Column(
        db.Integer,
        db.ForeignKey('product_version.id', ondelete='SET NULL'),
        index=True, nullable=True,
    )
    expires_at = db.Column(db.DateTime, index=True, nullable=True)

    product = db.relationship('Product', backref=db.backref('history', lazy=True, cascade="all, delete"))
    version = db.relationship('ProductVersion', backref='history_events')


# ================= VERSION SNAPSHOTS =================

class ProductVersion(db.Model):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False, index=True)
    version_num = db.Column(db.Integer, nullable=False)

    # Major snapshots store full JSON; minor (draft) snapshots store only changed keys (diff)
    is_major = db.Column(db.Boolean, default=True, nullable=False)

    # pis_data/spec_data hold full data for major saves, or a diff dict for minor saves
    pis_data = db.Column(JSONB)
    spec_data = db.Column(JSONB)
    revision_data = db.Column(JSONB)
    workflow_stage = db.Column(db.String(50))

    # Metadata
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow_naive)
    label = db.Column(db.String(100))  # e.g. "Before Director Review"

    # Phase History v2 — created_at + 180 days. Phase 4 cleanup uses this
    # for retention, with a guard that always preserves the most-recent
    # major snapshot per product so restore-from-history has an anchor.
    expires_at = db.Column(db.DateTime, index=True, nullable=True)

    product = db.relationship('Product', backref=db.backref('versions', lazy=True, cascade="all, delete"))
    created_by = db.relationship('User', backref='versions')


# ================= FIELD-LEVEL CHANGE LOG =================

class FieldChangeLog(db.Model):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    
    field_name = db.Column(db.String(150))   # e.g. "Brand", "Short Description"
    old_value = db.Column(db.Text)           # Human-readable
    new_value = db.Column(db.Text)           # Human-readable
    version_num = db.Column(db.Integer)      # Which version this change belongs to

    timestamp = db.Column(db.DateTime, default=_utcnow_naive, index=True)

    # Phase History v2 — stage the edit happened in, and TTL for cleanup.
    workflow_stage = db.Column(db.String(50), nullable=True)
    expires_at = db.Column(db.DateTime, index=True, nullable=True)

    product = db.relationship('Product', backref=db.backref('field_changes', lazy=True, cascade="all, delete"))
    user = db.relationship('User', backref='field_changes')


# ================= PROMPT MODEL =================

class Prompt(db.Model):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)  # e.g. "pis_extraction"
    display_name = db.Column(db.String(200))
    description = db.Column(db.Text)
    category = db.Column(db.String(50))
    prompt_text = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow_naive, onupdate=_utcnow_naive)


# ================= JOB MODEL =================

class Job(db.Model):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    id = db.Column(db.String(8), primary_key=True)
    model_name = db.Column(db.String(200))
    status = db.Column(db.String(20), default='queued', index=True)
    message = db.Column(db.String(500))
    progress = db.Column(db.Integer, default=0)
    redirect_url = db.Column(db.String(500), nullable=True)
    dismissed = db.Column(db.Boolean, default=False)
    payload = db.Column(JSONB, nullable=True)
    result = db.Column(JSONB, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow_naive)
    completed_at = db.Column(db.DateTime, nullable=True)

    # Cached aggregates updated by utils.api_metering as calls flow in.
    # Truth lives in ApiCallLog rows; these columns just save a SUM() per
    # job-row render in the admin Recent Jobs list.
    total_cost_usd = db.Column(db.Numeric(12, 6), default=0)
    total_calls    = db.Column(db.Integer, default=0)


# ================= API CALL LOG =================
# One row per external AI / search call. Drives the AI Job Activity admin
# panel: success rate, spend, call volume, per-provider and per-prompt
# breakdowns. job_id is nullable because some calls (verify-marketing
# fixes, compare exports) run outside the Job tracker.

class ApiCallLog(db.Model):
    __tablename__ = 'api_call_log'

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    id            = db.Column(db.Integer, primary_key=True)
    job_id        = db.Column(db.String(8), db.ForeignKey('job.id', ondelete='SET NULL'),
                              nullable=True, index=True)
    prompt_id     = db.Column(db.String(80), index=True)
    provider      = db.Column(db.String(40), nullable=False, index=True)
    model         = db.Column(db.String(80))
    input_tokens  = db.Column(db.Integer, default=0)
    output_tokens = db.Column(db.Integer, default=0)
    cached_tokens = db.Column(db.Integer, default=0)
    image_count   = db.Column(db.Integer, default=0)
    query_count   = db.Column(db.Integer, default=0)
    latency_ms    = db.Column(db.Integer, default=0)
    cost_usd      = db.Column(db.Numeric(12, 6), default=0)
    error         = db.Column(db.String(200), nullable=True)
    created_at    = db.Column(db.DateTime, default=_utcnow_naive, index=True)