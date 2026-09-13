"""Phase 10A Approval Core

Revision ID: e5a842b109dc
Revises: 7a0d42125023
Create Date: 2026-09-13 19:15:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e5a842b109dc'
down_revision: str | None = '7a0d42125023'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add Phase 10A fields to tasks table
    op.add_column('tasks', sa.Column('parent_task_id', sa.String(length=36), nullable=True))
    op.create_foreign_key('fk_tasks_parent_task_id', 'tasks', 'tasks', ['parent_task_id'], ['task_id'], ondelete='SET NULL')
    op.create_index(op.f('ix_tasks_parent_task_id'), 'tasks', ['parent_task_id'], unique=False)
    op.add_column('tasks', sa.Column('approval_config', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('tasks', sa.Column('pr_metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_index(op.f('ix_tasks_repository'), 'tasks', ['repository'], unique=False)

    # 2. Relax jobs.task_id uniqueness to allow resumed jobs per task while indexing
    op.drop_constraint('jobs_task_id_key', 'jobs', type_='unique')
    op.create_index(op.f('ix_jobs_task_id'), 'jobs', ['task_id'], unique=False)

    # 3. Create task_approvals table
    op.create_table(
        'task_approvals',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('task_id', sa.String(length=36), nullable=False),
        sa.Column('approval_type', sa.String(length=50), nullable=False, server_default='PLAN'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='PENDING'),
        sa.Column('requested_by', sa.String(length=255), nullable=True),
        sa.Column('approved_by', sa.String(length=255), nullable=True),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('requested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('responded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.task_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_task_approvals_task_id'), 'task_approvals', ['task_id'], unique=False)
    op.create_index(op.f('ix_task_approvals_status'), 'task_approvals', ['status'], unique=False)
    op.create_index(op.f('ix_task_approvals_expires_at'), 'task_approvals', ['expires_at'], unique=False)


def downgrade() -> None:
    # 3. Drop task_approvals
    op.drop_index(op.f('ix_task_approvals_expires_at'), table_name='task_approvals')
    op.drop_index(op.f('ix_task_approvals_status'), table_name='task_approvals')
    op.drop_index(op.f('ix_task_approvals_task_id'), table_name='task_approvals')
    op.drop_table('task_approvals')

    # 2. Restore jobs.task_id unique constraint
    op.drop_index(op.f('ix_jobs_task_id'), table_name='jobs')
    op.create_unique_constraint('jobs_task_id_key', 'jobs', ['task_id'])

    # 1. Revert tasks table
    op.drop_index(op.f('ix_tasks_repository'), table_name='tasks')
    op.drop_column('tasks', 'pr_metadata')
    op.drop_column('tasks', 'approval_config')
    op.drop_index(op.f('ix_tasks_parent_task_id'), table_name='tasks')
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS fk_tasks_parent_task_id")
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_parent_task_id_fkey")
    op.drop_column('tasks', 'parent_task_id')
