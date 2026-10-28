"""add user_settings.external_links

Revision ID: extlinks478
Revises: wesrc477
Create Date: 2026-10-27
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "extlinks478"
down_revision = "wesrc477"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("external_links", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "external_links")
