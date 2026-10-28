"""add media_server_connections.stremio_poster_template

Revision ID: stposter480
Revises: cwsort479
Create Date: 2026-10-28
"""

from alembic import op
import sqlalchemy as sa


revision = "stposter480"
down_revision = "cwsort479"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "media_server_connections",
        sa.Column("stremio_poster_template", sa.String(1000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("media_server_connections", "stremio_poster_template")
