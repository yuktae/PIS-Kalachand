"""Phase History v2 — audit trail expansion

Revision ID: phase_history_v2
Revises: phase1_2_model_changes
Create Date: 2026-05-11

Adds the columns the new audit trail needs:

  product_history
    + workflow_stage   — which stage the event happened in
    + actor_role       — role of the actor at log-time
    + version_id       — FK to product_version (snapshot taken at this moment)
    + expires_at       — when this row becomes eligible for cleanup

  field_change_log
    + workflow_stage   — which stage the field edit happened in
    + expires_at       — when this row becomes eligible for cleanup

  product_version
    + expires_at       — when this snapshot becomes eligible for cleanup
                         (Phase 4 cleanup keeps the most-recent major
                         snapshot per product regardless of this column.)

All new columns are nullable so existing rows survive and existing
callers don't break. Phase 1 helpers backfill these values on every new
insert. A future migration can backfill historical rows if needed.
"""
from alembic import op
import sqlalchemy as sa


revision = 'phase_history_v2'
down_revision = 'phase1_2_model_changes'
branch_labels = None
depends_on = None


def upgrade():
    # ── product_history ────────────────────────────────────────────────────
    op.add_column('product_history',
        sa.Column('workflow_stage', sa.String(50), nullable=True))
    op.add_column('product_history',
        sa.Column('actor_role', sa.String(20), nullable=True))
    op.add_column('product_history',
        sa.Column('version_id', sa.Integer(),
                  sa.ForeignKey('product_version.id', ondelete='SET NULL'),
                  nullable=True))
    op.add_column('product_history',
        sa.Column('expires_at', sa.DateTime(), nullable=True))
    op.create_index('ix_product_history_workflow_stage',
                    'product_history', ['workflow_stage'])
    op.create_index('ix_product_history_expires_at',
                    'product_history', ['expires_at'])
    op.create_index('ix_product_history_version_id',
                    'product_history', ['version_id'])

    # ── field_change_log ───────────────────────────────────────────────────
    op.add_column('field_change_log',
        sa.Column('workflow_stage', sa.String(50), nullable=True))
    op.add_column('field_change_log',
        sa.Column('expires_at', sa.DateTime(), nullable=True))
    op.create_index('ix_field_change_log_expires_at',
                    'field_change_log', ['expires_at'])

    # ── product_version ────────────────────────────────────────────────────
    op.add_column('product_version',
        sa.Column('expires_at', sa.DateTime(), nullable=True))
    op.create_index('ix_product_version_expires_at',
                    'product_version', ['expires_at'])


def downgrade():
    op.drop_index('ix_product_version_expires_at', table_name='product_version')
    op.drop_column('product_version', 'expires_at')

    op.drop_index('ix_field_change_log_expires_at', table_name='field_change_log')
    op.drop_column('field_change_log', 'expires_at')
    op.drop_column('field_change_log', 'workflow_stage')

    op.drop_index('ix_product_history_version_id', table_name='product_history')
    op.drop_index('ix_product_history_expires_at', table_name='product_history')
    op.drop_index('ix_product_history_workflow_stage', table_name='product_history')
    op.drop_column('product_history', 'expires_at')
    op.drop_column('product_history', 'version_id')
    op.drop_column('product_history', 'actor_role')
    op.drop_column('product_history', 'workflow_stage')
