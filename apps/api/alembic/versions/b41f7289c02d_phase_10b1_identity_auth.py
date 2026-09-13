"""Phase 10B.1 Identity and Authentication Foundation

Revision ID: b41f7289c02d
Revises: e5a842b109dc
Create Date: 2026-09-13 22:15:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b41f7289c02d"
down_revision: str | None = "e5a842b109dc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Create users table
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("avatar_url", sa.String(length=1024), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=False)

    # 2. Create github_identities table
    op.create_table(
        "github_identities",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("github_user_id", sa.BigInteger(), nullable=False),
        sa.Column("github_login", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_github_identities_user_id"),
        sa.UniqueConstraint("github_user_id", name="uq_github_identities_github_user_id"),
    )
    op.create_index(
        op.f("ix_github_identities_user_id"), "github_identities", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_github_identities_github_user_id"),
        "github_identities",
        ["github_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_github_identities_github_login"),
        "github_identities",
        ["github_login"],
        unique=False,
    )

    # 3. Add creator_id to tasks table
    op.add_column("tasks", sa.Column("creator_id", sa.String(length=36), nullable=True))
    op.create_foreign_key(
        "fk_tasks_creator_id", "tasks", "users", ["creator_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index(op.f("ix_tasks_creator_id"), "tasks", ["creator_id"], unique=False)

    # 4. Create task_github_links table
    op.create_table(
        "task_github_links",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("repository_full_name", sa.String(length=255), nullable=False),
        sa.Column("issue_id", sa.BigInteger(), nullable=True),
        sa.Column("issue_number", sa.Integer(), nullable=True),
        sa.Column("pull_request_id", sa.BigInteger(), nullable=True),
        sa.Column("pull_request_number", sa.Integer(), nullable=True),
        sa.Column("trigger_comment_id", sa.BigInteger(), nullable=True),
        sa.Column("triggering_github_user_id", sa.BigInteger(), nullable=False),
        sa.Column("triggering_github_login", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("task_id", name="uq_task_github_links_task_id"),
    )
    op.create_index(
        op.f("ix_task_github_links_task_id"), "task_github_links", ["task_id"], unique=False
    )
    op.create_index(
        op.f("ix_task_github_links_installation_id"),
        "task_github_links",
        ["installation_id"],
        unique=False,
    )
    op.create_index(
        "ix_task_github_links_repo_issue",
        "task_github_links",
        ["repository_id", "issue_id"],
        unique=False,
    )
    op.create_index(
        "ix_task_github_links_repo_pr",
        "task_github_links",
        ["repository_id", "pull_request_id"],
        unique=False,
    )
    op.create_index(
        "uq_task_github_links_repo_comment",
        "task_github_links",
        ["repository_id", "trigger_comment_id"],
        unique=True,
        postgresql_where=sa.text("trigger_comment_id IS NOT NULL"),
    )


def downgrade() -> None:
    # 4. Drop task_github_links
    op.drop_index("uq_task_github_links_repo_comment", table_name="task_github_links")
    op.drop_index("ix_task_github_links_repo_pr", table_name="task_github_links")
    op.drop_index("ix_task_github_links_repo_issue", table_name="task_github_links")
    op.drop_index(op.f("ix_task_github_links_installation_id"), table_name="task_github_links")
    op.drop_index(op.f("ix_task_github_links_task_id"), table_name="task_github_links")
    op.drop_table("task_github_links")

    # 3. Drop creator_id from tasks
    op.drop_index(op.f("ix_tasks_creator_id"), table_name="tasks")
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS fk_tasks_creator_id")
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_creator_id_fkey")
    op.drop_column("tasks", "creator_id")

    # 2. Drop github_identities
    op.drop_index(op.f("ix_github_identities_github_login"), table_name="github_identities")
    op.drop_index(op.f("ix_github_identities_github_user_id"), table_name="github_identities")
    op.drop_index(op.f("ix_github_identities_user_id"), table_name="github_identities")
    op.drop_table("github_identities")

    # 1. Drop users
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")
