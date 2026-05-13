"""Phase: product.last_edited_at — auto-bumped timestamp for sort ordering

Revision ID: phase_last_edited_at
Revises: phase_unified_category
Create Date: 2026-05-13

Adds a `last_edited_at` column that SQLAlchemy's `onupdate=` clause bumps
on every UPDATE statement issued against the row. Used by the marketing/
director/web dashboards to surface the most recently edited product
first — covers silent autosaves that don't emit a ProductHistory row.

Backfill: existing products get last_edited_at = created_at so the
ordering is stable on first deploy.
"""
from alembic import op
import sqlalchemy as sa


revision = 'phase_last_edited_at'
down_revision = 'phase_unified_category'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('product',
        sa.Column('last_edited_at', sa.DateTime(), nullable=True))
    op.create_index('ix_product_last_edited_at', 'product', ['last_edited_at'])

    # Backfill — every existing product starts with last_edited_at equal to
    # created_at so the initial ordering matches the pre-migration view.
    # Any subsequent UPDATE on the row will bump this value via SQLAlchemy's
    # onupdate hook.
    op.execute("UPDATE product SET last_edited_at = created_at WHERE last_edited_at IS NULL")


def downgrade():
    op.drop_index('ix_product_last_edited_at', table_name='product')
    op.drop_column('product', 'last_edited_at')
