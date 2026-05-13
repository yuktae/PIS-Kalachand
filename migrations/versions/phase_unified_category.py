"""Phase: Unified product category — canonical columns on Product

Revision ID: phase_unified_category
Revises: phase_history_v2
Create Date: 2026-05-13

Lifts category from JSONB into first-class columns so there is one
source of truth. Until now category lived in three different shapes
(`pis_data.category_data`, `spec_data.categories`, and ghost
`pis_data.category_A/B/C` references) which could drift independently.

Adds:
  product.category_1            — top-level Magento category (indexed for filtering)
  product.category_2            — mid-level Magento category
  product.category_3            — leaf-level Magento category
  product.magento_category_id   — stable Magento ID so renames don't break the link

Backfill of existing rows runs as a one-off data migration in the same
revision so the columns are populated immediately after upgrade. The
priority order matches the legacy fallback chain: spec_data.categories
wins (web-confirmed) over pis_data.category_data (AI-assigned).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = 'phase_unified_category'
down_revision = 'phase_history_v2'
branch_labels = None
depends_on = None


def upgrade():
    # ── product: add canonical category columns ────────────────────────────
    op.add_column('product',
        sa.Column('category_1', sa.String(100), nullable=True))
    op.add_column('product',
        sa.Column('category_2', sa.String(100), nullable=True))
    op.add_column('product',
        sa.Column('category_3', sa.String(100), nullable=True))
    op.add_column('product',
        sa.Column('magento_category_id', sa.Integer(), nullable=True))

    op.create_index('ix_product_category_1', 'product', ['category_1'])
    op.create_index('ix_product_magento_category_id',
                    'product', ['magento_category_id'])

    # ── data backfill: copy from JSONB into new columns ────────────────────
    # spec_data.categories takes priority (web-confirmed value) over
    # pis_data.category_data (AI-assigned guess). Products with neither
    # remain NULL — the helper layer surfaces these as "Uncategorised".
    conn = op.get_bind()
    conn.execute(sa.text("""
        UPDATE product
        SET category_1 = COALESCE(
                NULLIF(spec_data->'categories'->>'category_1', ''),
                NULLIF(pis_data->'category_data'->>'category_1', '')
            ),
            category_2 = COALESCE(
                NULLIF(spec_data->'categories'->>'category_2', ''),
                NULLIF(pis_data->'category_data'->>'category_2', '')
            ),
            category_3 = COALESCE(
                NULLIF(spec_data->'categories'->>'category_3', ''),
                NULLIF(pis_data->'category_data'->>'category_3', '')
            )
        WHERE category_1 IS NULL
    """))


def downgrade():
    op.drop_index('ix_product_magento_category_id', table_name='product')
    op.drop_index('ix_product_category_1', table_name='product')
    op.drop_column('product', 'magento_category_id')
    op.drop_column('product', 'category_3')
    op.drop_column('product', 'category_2')
    op.drop_column('product', 'category_1')
