"""Outbox delivery state: scheduled retries and a visible failed state

Revision ID: e1a2b3c4d5f6
Revises: d7e8f9a0b1c2
Create Date: 2026-10-22 20:00:00.000000

Retry state used to live in the event payload, and an event that ran out of
attempts was marked published.  It now has columns: attempts, when the next
attempt is due, the last (redacted) error and its category, and failed_at for
an event that was given up.  Events already given up under the old scheme
(payload "terminal_failure") become failed events, so they can be inspected
and requeued; their payload is left as it was.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1a2b3c4d5f6"
down_revision: str | None = "d7e8f9a0b1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("outbox_events", sa.Column("attempts", sa.Integer(), server_default="0", nullable=False))
    op.add_column("outbox_events", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox_events", sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox_events", sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox_events", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column("outbox_events", sa.Column("last_error_category", sa.String(length=32), nullable=True))
    op.add_column("outbox_events", sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_outbox_events_pending",
        "outbox_events",
        ["next_attempt_at", "created_at"],
        unique=False,
        postgresql_where=sa.text("published_at IS NULL AND failed_at IS NULL"),
    )
    op.create_index(
        "ix_outbox_events_failed_at",
        "outbox_events",
        ["failed_at"],
        unique=False,
        postgresql_where=sa.text("failed_at IS NOT NULL"),
    )
    # Events given up under the old scheme: published_at was their failure time.
    op.execute(
        """
        UPDATE outbox_events
           SET failed_at = published_at,
               published_at = NULL,
               attempts = COALESCE(NULLIF(payload->>'attempts', '')::integer, 1),
               last_failed_at = published_at,
               first_failed_at = published_at,
               last_error = left(COALESCE(payload->>'last_error', payload->>'error'), 1000),
               last_error_category = CASE
                   WHEN payload->>'max_attempts_exceeded' = 'true' THEN 'transient'
                   ELSE 'permanent'
               END
         WHERE payload->>'terminal_failure' = 'true'
           AND published_at IS NOT NULL
        """
    )


def downgrade() -> None:
    # Failed events go back to the old scheme's "published" terminal state.
    op.execute(
        """
        UPDATE outbox_events
           SET published_at = failed_at
         WHERE failed_at IS NOT NULL
           AND published_at IS NULL
        """
    )
    op.drop_index("ix_outbox_events_failed_at", table_name="outbox_events")
    op.drop_index("ix_outbox_events_pending", table_name="outbox_events")
    op.drop_column("outbox_events", "failed_at")
    op.drop_column("outbox_events", "last_error_category")
    op.drop_column("outbox_events", "last_error")
    op.drop_column("outbox_events", "last_failed_at")
    op.drop_column("outbox_events", "first_failed_at")
    op.drop_column("outbox_events", "next_attempt_at")
    op.drop_column("outbox_events", "attempts")
