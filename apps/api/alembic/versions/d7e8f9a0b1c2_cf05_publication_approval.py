"""CF-05 publication approval: artifacts and approval binding

Revision ID: d7e8f9a0b1c2
Revises: c1f2e3d4a5b6
Create Date: 2026-10-20 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d7e8f9a0b1c2"
down_revision: str | None = "c1f2e3d4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_publication_artifacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("repository", sa.String(length=255), nullable=False),
        sa.Column("base_branch", sa.String(length=255), nullable=False),
        sa.Column("base_sha", sa.String(length=40), nullable=False),
        sa.Column("commit_message", sa.Text(), nullable=False),
        sa.Column("pull_request_title", sa.Text(), nullable=False),
        sa.Column("pull_request_body", sa.Text(), nullable=False),
        sa.Column("files", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("total_bytes", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("commit_sha", sa.String(length=40), nullable=True),
        sa.Column("pull_request_number", sa.Integer(), nullable=True),
        sa.Column("pull_request_url", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id"),
    )
    op.add_column("task_approvals", sa.Column("artifact_digest", sa.String(length=64), nullable=True))
    op.add_column("task_approvals", sa.Column("approved_by_user_id", sa.String(length=36), nullable=True))
    op.create_foreign_key(
        "fk_task_approvals_approved_by_user_id",
        "task_approvals",
        "users",
        ["approved_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_task_approvals_approved_by_user_id", "task_approvals", type_="foreignkey")
    op.drop_column("task_approvals", "approved_by_user_id")
    op.drop_column("task_approvals", "artifact_digest")
    op.drop_table("task_publication_artifacts")
