"""add playback_progress.sort_at

Revision ID: cwsort479
Revises: extlinks478
Create Date: 2026-10-28
"""

from alembic import op
import sqlalchemy as sa


revision = "cwsort479"
down_revision = "extlinks478"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("playback_progress", sa.Column("sort_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("playback_progress", "sort_at")
