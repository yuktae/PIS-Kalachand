"""Phase 1.2 model changes: soft-delete, version retention flag, image URL support

Revision ID: phase1_2_model_changes
Revises:
Create Date: 2026-04-27

Changes:
  - product.deleted_at        (DateTime, nullable) — soft-delete support
  - product.image_path        (String 200 → 500) — allows full Azure Blob URLs
  - product_version.is_major  (Boolean, default True) — major vs minor snapshot flag
"""
from alembic import op
import sqlalchemy as sa


revision = 'phase1_2_model_changes'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # ── product: add deleted_at column ──────────────────────────────────────
    op.add_column('product',
        sa.Column('deleted_at', sa.DateTime(), nullable=True)
    )
    op.create_index('ix_product_deleted_at', 'product', ['deleted_at'])

    # ── product: widen image_path to 500 chars (supports Azure blob URLs) ───
    op.alter_column('product', 'image_path',
        existing_type=sa.String(200),
        type_=sa.String(500),
        existing_nullable=True
    )

    # ── product_version: add is_major flag ───────────────────────────────────
    op.add_column('product_version',
        sa.Column('is_major', sa.Boolean(), nullable=False,
                  server_default=sa.true())
    )


def downgrade():
    op.drop_column('product_version', 'is_major')
    op.alter_column('product', 'image_path',
        existing_type=sa.String(500),
        type_=sa.String(200),
        existing_nullable=True
    )
    op.drop_index('ix_product_deleted_at', table_name='product')
    op.drop_column('product', 'deleted_at')
