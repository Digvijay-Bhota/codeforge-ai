"""Phase 8 Observability

Revision ID: 7a0d42125023
Revises: 1abea910462d
Create Date: 2026-09-05 21:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '7a0d42125023'
down_revision: str | None = '1abea910462d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    # Add execution_id to jobs
    op.add_column('jobs', sa.Column('execution_id', sa.String(length=36), nullable=True))
    op.execute("UPDATE jobs SET execution_id = substring(md5(random()::text) from 1 for 36)")
    op.alter_column('jobs', 'execution_id', nullable=False)
    op.create_unique_constraint(None, 'jobs', ['execution_id'])

    # Create audit_events
    op.create_table('audit_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('task_id', sa.String(length=36), nullable=True),
    sa.Column('execution_id', sa.String(length=36), nullable=True),
    sa.Column('actor_type', sa.String(length=50), nullable=False),
    sa.Column('actor_id', sa.String(length=255), nullable=True),
    sa.Column('event_type', sa.String(length=50), nullable=False),
    sa.Column('resource_type', sa.String(length=50), nullable=True),
    sa.Column('resource_id', sa.String(length=255), nullable=True),
    sa.Column('result', sa.String(length=50), nullable=True),
    sa.Column('metadata_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_events_created_at'), 'audit_events', ['created_at'], unique=False)
    op.create_index(op.f('ix_audit_events_event_type'), 'audit_events', ['event_type'], unique=False)
    op.create_index(op.f('ix_audit_events_execution_id'), 'audit_events', ['execution_id'], unique=False)
    op.create_index(op.f('ix_audit_events_task_id'), 'audit_events', ['task_id'], unique=False)

    # Create observability_events
    op.create_table('observability_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('task_id', sa.String(length=36), nullable=False),
    sa.Column('job_id', sa.Integer(), nullable=True),
    sa.Column('execution_id', sa.String(length=36), nullable=True),
    sa.Column('trace_id', sa.String(length=36), nullable=True),
    sa.Column('parent_event_id', sa.Integer(), nullable=True),
    sa.Column('event_type', sa.String(length=50), nullable=False),
    sa.Column('component', sa.String(length=50), nullable=True),
    sa.Column('stage', sa.String(length=50), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=True),
    sa.Column('metadata_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error_code', sa.String(length=50), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['parent_event_id'], ['observability_events.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_observability_events_created_at'), 'observability_events', ['created_at'], unique=False)
    op.create_index(op.f('ix_observability_events_event_type'), 'observability_events', ['event_type'], unique=False)
    op.create_index(op.f('ix_observability_events_execution_id'), 'observability_events', ['execution_id'], unique=False)
    op.create_index(op.f('ix_observability_events_task_id'), 'observability_events', ['task_id'], unique=False)

    # Create task_evaluations
    op.create_table('task_evaluations',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('task_id', sa.String(length=36), nullable=False),
    sa.Column('execution_id', sa.String(length=36), nullable=True),
    sa.Column('overall_status', sa.String(length=20), nullable=False),
    sa.Column('task_success', sa.Boolean(), nullable=False),
    sa.Column('tests_passed', sa.Integer(), nullable=True),
    sa.Column('tests_failed', sa.Integer(), nullable=True),
    sa.Column('tests_total', sa.Integer(), nullable=True),
    sa.Column('changes_made', sa.Boolean(), nullable=True),
    sa.Column('planned_files', sa.Integer(), nullable=True),
    sa.Column('changed_files', sa.Integer(), nullable=True),
    sa.Column('plan_adherence', sa.Float(), nullable=True),
    sa.Column('regression_detected', sa.Boolean(), nullable=True),
    sa.Column('security_violation', sa.Boolean(), nullable=True),
    sa.Column('human_intervention_required', sa.Boolean(), nullable=True),
    sa.Column('tool_failure_count', sa.Integer(), nullable=True),
    sa.Column('model_call_count', sa.Integer(), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('estimated_cost_usd', sa.Float(), nullable=True),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.task_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_task_evaluations_execution_id'), 'task_evaluations', ['execution_id'], unique=False)
    op.create_index(op.f('ix_task_evaluations_task_id'), 'task_evaluations', ['task_id'], unique=False)

def downgrade() -> None:
    op.drop_index(op.f('ix_task_evaluations_task_id'), table_name='task_evaluations')
    op.drop_index(op.f('ix_task_evaluations_execution_id'), table_name='task_evaluations')
    op.drop_table('task_evaluations')
    op.drop_index(op.f('ix_observability_events_task_id'), table_name='observability_events')
    op.drop_index(op.f('ix_observability_events_execution_id'), table_name='observability_events')
    op.drop_index(op.f('ix_observability_events_event_type'), table_name='observability_events')
    op.drop_index(op.f('ix_observability_events_created_at'), table_name='observability_events')
    op.drop_table('observability_events')
    op.drop_index(op.f('ix_audit_events_task_id'), table_name='audit_events')
    op.drop_index(op.f('ix_audit_events_execution_id'), table_name='audit_events')
    op.drop_index(op.f('ix_audit_events_event_type'), table_name='audit_events')
    op.drop_index(op.f('ix_audit_events_created_at'), table_name='audit_events')
    op.drop_table('audit_events')
    op.drop_constraint('jobs_execution_id_key', 'jobs', type_='unique')
    op.drop_column('jobs', 'execution_id')
