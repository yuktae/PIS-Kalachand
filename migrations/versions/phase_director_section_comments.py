"""Phase: product.director_section_comments — persistent per-section comment log

Revision ID: phase_director_section_comments
Revises: phase_last_edited_at
Create Date: 2026-05-13

Adds a JSONB archive that accumulates the director's per-section feedback
across revision cycles. Existing `revision_data` only holds the CURRENT
pending revision — it gets popped on Accept and cleared on submit-to-
director — so the director's original comment vanished as soon as the
marketing/web user resolved it. The new column preserves every comment
so the team can still review the rationale after acting on it.

Shape:
    {
      "range_overview": [
        {"comment": "make it shorter",
         "timestamp": "2026-05-13T14:23:00",
         "actor": "John Doe"},
        ...
      ],
      "sales_arguments": [...]
    }
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = 'phase_director_section_comments'
down_revision = 'phase_last_edited_at'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('product',
        sa.Column('director_section_comments', JSONB(), nullable=True,
                  server_default=sa.text("'{}'::jsonb")))


def downgrade():
    op.drop_column('product', 'director_section_comments')
