"""job_retry_and_dead_letter

Revision ID: 002_job_retry
Revises: 001_initial
Create Date: 2026-10-31 01:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "002_job_retry"
down_revision: str | None = "001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "extraction_jobs",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "extraction_jobs",
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
    )
    op.add_column(
        "extraction_attempts",
        sa.Column("error_message", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("extraction_attempts", "error_message")
    op.drop_column("extraction_jobs", "max_attempts")
    op.drop_column("extraction_jobs", "attempt_count")
