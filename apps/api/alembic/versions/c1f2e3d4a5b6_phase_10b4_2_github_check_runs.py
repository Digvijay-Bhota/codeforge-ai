"""Phase 10B.4.2 GitHub Check Run & Comment Lifecycle

Revision ID: c1f2e3d4a5b6
Revises: b41f7289c02d
Create Date: 2026-09-14 12:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1f2e3d4a5b6"
down_revision: str | None = "b41f7289c02d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("task_github_links", sa.Column("check_run_id", sa.BigInteger(), nullable=True))
    op.add_column("task_github_links", sa.Column("check_run_status", sa.String(length=50), nullable=True))
    op.add_column("task_github_links", sa.Column("check_run_conclusion", sa.String(length=50), nullable=True))
    op.add_column("task_github_links", sa.Column("head_sha", sa.String(length=40), nullable=True))
    op.add_column("task_github_links", sa.Column("acknowledgement_comment_id", sa.BigInteger(), nullable=True))
    op.create_index(op.f("ix_task_github_links_check_run_id"), "task_github_links", ["check_run_id"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_task_github_links_check_run_id"), table_name="task_github_links")
    op.drop_column("task_github_links", "acknowledgement_comment_id")
    op.drop_column("task_github_links", "head_sha")
    op.drop_column("task_github_links", "check_run_conclusion")
    op.drop_column("task_github_links", "check_run_status")
    op.drop_column("task_github_links", "check_run_id")
