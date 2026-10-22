import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, TimestampMixin


class TaskStatusEnum(str, enum.Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    # Generated changes exist but are unpublished until a human approves them.
    WAITING_PUBLICATION = "WAITING_PUBLICATION"
    CODING = "CODING"
    TESTING = "TESTING"
    PUBLISHING = "PUBLISHING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

class JobStatusEnum(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"

class ApprovalStatusEnum(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        Index("ix_users_email", "email"),
    )


class GitHubIdentity(Base, TimestampMixin):
    __tablename__ = "github_identities"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    github_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    github_login: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (
        Index("ix_github_identities_user_id", "user_id"),
        Index("ix_github_identities_github_user_id", "github_user_id"),
        Index("ix_github_identities_github_login", "github_login"),
    )


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=TaskStatusEnum.PENDING.value)
    execution_target: Mapped[str] = mapped_column(String(20), nullable=False)
    repository: Mapped[str | None] = mapped_column(String(255))
    workspace_path: Mapped[str | None] = mapped_column(String(1024))
    requested_task: Mapped[str] = mapped_column(Text, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    final_message: Mapped[str | None] = mapped_column(Text)

    implementation_plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    task_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # Phase 10A product additions
    parent_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.task_id", ondelete="SET NULL"), nullable=True)
    approval_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    pr_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Phase 10B.1 identity additions
    creator_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL", name="fk_tasks_creator_id"), nullable=True)

    __table_args__ = (
        Index("ix_tasks_status", "status"),
        Index("ix_tasks_created_at", "created_at"),
        Index("ix_tasks_parent_task_id", "parent_task_id"),
        Index("ix_tasks_repository", "repository"),
        Index("ix_tasks_creator_id", "creator_id"),
    )


class TaskGitHubLink(Base, TimestampMixin):
    __tablename__ = "task_github_links"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False, unique=True)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository_full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    issue_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pull_request_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pull_request_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trigger_comment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    triggering_github_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    triggering_github_login: Mapped[str] = mapped_column(String(255), nullable=False)

    # Phase 10B.4.2 additions for Check Run and Acknowledgement comment lifecycle
    check_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    check_run_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    check_run_conclusion: Mapped[str | None] = mapped_column(String(50), nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    acknowledgement_comment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        Index("ix_task_github_links_task_id", "task_id"),
        Index("ix_task_github_links_repo_issue", "repository_id", "issue_id"),
        Index("ix_task_github_links_repo_pr", "repository_id", "pull_request_id"),
        Index("ix_task_github_links_installation_id", "installation_id"),
        Index("ix_task_github_links_check_run_id", "check_run_id", unique=True),
        Index(
            "uq_task_github_links_repo_comment",
            "repository_id",
            "trigger_comment_id",
            unique=True,
            postgresql_where=text("trigger_comment_id IS NOT NULL"),
        ),
    )


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str | None] = mapped_column(String(20))
    message: Mapped[str | None] = mapped_column(Text)
    metadata_obj: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_task_events_task_id", "task_id"),
        Index("ix_task_events_created_at", "created_at"),
    )


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=JobStatusEnum.PENDING.value)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(255))
    lease_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_available_at", "available_at"),
    )


# TaskApproval.approval_type of publication approvals (plan approvals use "PLAN").
PUBLICATION_APPROVAL = "PUBLICATION"


class TaskApproval(Base, TimestampMixin):
    __tablename__ = "task_approvals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False, index=True)
    approval_type: Mapped[str] = mapped_column(String(50), nullable=False, default="PLAN")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ApprovalStatusEnum.PENDING.value)
    requested_by: Mapped[str | None] = mapped_column(String(255))
    approved_by: Mapped[str | None] = mapped_column(String(255))
    comment: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # PUBLICATION approvals only: the exact artifact approved, and who approved it.
    artifact_digest: Mapped[str | None] = mapped_column(String(64))
    approved_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL", name="fk_task_approvals_approved_by_user_id")
    )

    __table_args__ = (
        Index("ix_task_approvals_status", "status"),
        Index("ix_task_approvals_expires_at", "expires_at"),
    )


class TaskPublicationArtifact(Base, TimestampMixin):
    """The exact generated change set a human must approve before it is published.

    Bound by its digest (sha256 over repository, base branch and commit, the
    commit message, the pull request title and body, and the files); the
    publication job republishes exactly these and verifies the digest.
    """

    __tablename__ = "task_publication_artifacts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False, unique=True
    )
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    commit_message: Mapped[str] = mapped_column(Text, nullable=False)
    pull_request_title: Mapped[str] = mapped_column(Text, nullable=False)
    pull_request_body: Mapped[str] = mapped_column(Text, nullable=False)
    files: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    total_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    commit_sha: Mapped[str | None] = mapped_column(String(40))
    pull_request_number: Mapped[int | None] = mapped_column(Integer)
    pull_request_url: Mapped[str | None] = mapped_column(String(1024))


class OutboxEvent(Base):
    """A side effect to perform after the transaction that recorded it (see app/outbox_policy.py).

    Pending: published_at and failed_at unset; due once next_attempt_at
    (unset: at once) has passed.  Delivered: published_at.  Given up:
    failed_at, kept for diagnosis and operator requeue (app/outbox_admin.py).
    """

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Delivery state.  Failed attempts since the event was created or requeued.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Redacted and bounded; never payload contents.
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_category: Mapped[str | None] = mapped_column(String(32))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_outbox_events_published_at", "published_at"),
        Index(
            "ix_outbox_events_pending",
            "next_attempt_at",
            "created_at",
            postgresql_where=text("published_at IS NULL AND failed_at IS NULL"),
        ),
        Index("ix_outbox_events_failed_at", "failed_at", postgresql_where=text("failed_at IS NOT NULL")),
    )


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    delivery_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    installation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(50), nullable=False)


class GitHubInstallation(Base, TimestampMixin):
    __tablename__ = "github_installations"

    installation_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_login: Mapped[str] = mapped_column(String(255), nullable=False)
    account_type: Mapped[str] = mapped_column(String(50), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class GitHubInstallationRepository(Base, TimestampMixin):
    __tablename__ = "github_installation_repositories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    installation_id: Mapped[int] = mapped_column(ForeignKey("github_installations.installation_id", ondelete="CASCADE"), nullable=False)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    authorized: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint("installation_id", "repository", name="uq_github_install_repo"),
        Index("ix_github_install_repo_repository", "repository"),
    )

class ObservabilityEvent(Base):
    __tablename__ = "observability_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    job_id: Mapped[int | None] = mapped_column(Integer)
    execution_id: Mapped[str | None] = mapped_column(String(36), index=True)
    trace_id: Mapped[str | None] = mapped_column(String(36))
    parent_event_id: Mapped[int | None] = mapped_column(ForeignKey("observability_events.id"))
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    component: Mapped[str | None] = mapped_column(String(50))
    stage: Mapped[str | None] = mapped_column(String(50))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(20))
    metadata_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

class TaskEvaluation(Base):
    __tablename__ = "task_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id", ondelete="CASCADE"), index=True, nullable=False)
    execution_id: Mapped[str | None] = mapped_column(String(36), index=True)
    overall_status: Mapped[str] = mapped_column(String(20))
    task_success: Mapped[bool] = mapped_column(Boolean)
    tests_passed: Mapped[int | None] = mapped_column(Integer)
    tests_failed: Mapped[int | None] = mapped_column(Integer)
    tests_total: Mapped[int | None] = mapped_column(Integer)
    changes_made: Mapped[bool | None] = mapped_column(Boolean)
    planned_files: Mapped[int | None] = mapped_column(Integer)
    changed_files: Mapped[int | None] = mapped_column(Integer)
    plan_adherence: Mapped[float | None] = mapped_column(Float)
    regression_detected: Mapped[bool | None] = mapped_column(Boolean)
    security_violation: Mapped[bool | None] = mapped_column(Boolean)
    human_intervention_required: Mapped[bool | None] = mapped_column(Boolean)
    tool_failure_count: Mapped[int | None] = mapped_column(Integer)
    model_call_count: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)
    score: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(String(36), index=True)
    execution_id: Mapped[str | None] = mapped_column(String(36), index=True)
    actor_type: Mapped[str] = mapped_column(String(50))
    actor_id: Mapped[str | None] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    resource_type: Mapped[str | None] = mapped_column(String(50))
    resource_id: Mapped[str | None] = mapped_column(String(255))
    result: Mapped[str | None] = mapped_column(String(50))
    metadata_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
