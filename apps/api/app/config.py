"""Application configuration via Pydantic Settings.

All settings are loaded from environment variables (or a .env file).
Never hard-code secrets — add them to .env.example as placeholders only.
"""

import re
from enum import Enum
from pathlib import Path

from pydantic import Field, StrictInt, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.execution.gvisor_runtime import (
    NPROC_MAX,
    NPROC_MIN,
    GVisorRuntimeRequirement,
    task_limit_violations,
)

# APP_ENV values in which repository tests may run directly on the worker host,
# and in which the public development JWT secret is accepted.
DEVELOPMENT_ENVIRONMENTS = frozenset({"development", "test"})

# The session-signing secret shipped as the default and in .env.example.  It is
# public, so anyone could forge session tokens with it: it is only accepted when
# APP_ENV is explicitly development or test.
DEFAULT_JWT_SECRET = "insecure-codeforge-secret-key-change-in-production-32chars"  # nosec B105
MIN_JWT_SECRET_LENGTH = 32

# Smallest accepted TASK_LEASE_SECONDS (see Settings.task_lease_seconds).
MIN_TASK_LEASE_SECONDS = 300

# A content-addressed image reference: ``name[:tag]@sha256:<digest>`` or a local
# image ID ``sha256:<id>``.  A bare tag can be repointed, so it is never accepted
# for the Docker test executor.  The name must not start with "-" (it is passed
# to the docker CLI as an argument).
PINNED_IMAGE_PATTERN = re.compile(
    r"^(?:[a-z0-9][a-z0-9._/:-]*@)?sha256:[0-9a-f]{64}$"
)


class ExecutionMode(str, Enum):
    """How repository-controlled code (the test suite) is executed."""

    # Nothing runs; every task fails closed at the testing stage.
    DISABLED = "disabled"
    # Runs on the worker host under the worker's UID.  NOT a security
    # boundary; only allowed when APP_ENV is explicitly development or test.
    HOST_UNSAFE = "host_unsafe"
    # A hardened, throwaway Docker container (see docs/security/
    # docker-test-executor.md).  A container boundary on a shared kernel, not
    # complete isolation; it does not permit automatic repairs.
    DOCKER = "docker"
    # The docker controls under a verified gVisor runtime plus a guest NPROC
    # limit (see docs/security/gvisor-test-executor.md).  Stronger than docker,
    # but it still does not permit automatic repairs.
    GVISOR = "gvisor"
    # An approved isolated runtime that permits automatic repairs.  None ships
    # yet (gvisor does not qualify), so this mode also fails closed.
    SANDBOX = "sandbox"


class Settings(BaseSettings):
    """Top-level application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # Validation errors must not echo inputs: they include secrets and
        # credential-bearing URLs.
        hide_input_in_errors=True,
    )

    # ── Application ────────────────────────────────────────────────────────────
    app_name: str = "CodeForge AI"
    app_env: str = Field(default="development", description="development, test, or production")
    log_level: str = "INFO"

    # ── Database ───────────────────────────────────────────────────────────────
    database_url: str = Field(default="postgresql+asyncpg://codeforge:codeforge@postgres:5432/codeforge")
    db_pool_size: int = Field(default=20, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0)
    db_pool_recycle: int = Field(default=3600, ge=-1)

    # ── Redis ──────────────────────────────────────────────────────────────────
    redis_url: str = Field(default="redis://redis:6379/0")

    # ── Phase 7: Task Persistence & Async ──────────────────────────────────────
    task_queue_name: str = "codeforge:tasks"
    task_worker_id: str = "worker-1"
    # How long a claimed job is leased to its worker (renewed by the worker's
    # heartbeat).  An expired lease means the worker died: recovery returns the
    # job to PENDING.  A PENDING job left unclaimed this long is presumed to have
    # lost its queue message and is re-enqueued.
    # At least MIN_TASK_LEASE_SECONDS: synchronous git calls can block the
    # worker's event loop, and so its heartbeat, for up to 180 s at a stretch
    # (clone 120 + config check 30 + checkout 30; commit is 3 x (30 + 30)).
    # With renewals every 60 s, a lease of 240 s or less can expire under a
    # healthy worker, whose finished run is then discarded and its task failed.
    task_lease_seconds: int = Field(default=300, ge=MIN_TASK_LEASE_SECONDS)
    outbox_batch_size: int = Field(default=50, le=1000)
    outbox_poll_interval: float = Field(default=1.0, ge=0.1)
    task_output_max_bytes: int = 10000
    outbox_max_retries: int = Field(default=3, ge=1, le=10)

    # Worker limits
    # Not enforced yet: each worker process runs one job at a time.  Running
    # jobs concurrently first needs the blocking git calls moved off the event
    # loop, or one job's clone could starve another job's lease heartbeat.
    worker_concurrency: int = Field(default=5, ge=1, le=100)

    # ── Phase 1: LLM / Agent ───────────────────────────────────────────────────
    openai_api_key: str = ""
    codeforge_model: str = "gpt-4o"
    # Maximum model turns for each coding-agent run.  A turn is one model call
    # plus the tool calls it requests.  It cannot be disabled.
    codeforge_coding_max_turns: int = Field(default=25, ge=1, le=100)
    # How repository tests are executed; see ExecutionMode.  Off by default.
    codeforge_test_execution_mode: ExecutionMode = ExecutionMode.DISABLED
    # Repair attempts after a failed test run (hard limit 5).  Defaults to 0,
    # and a nonzero value requires CODEFORGE_TEST_EXECUTION_MODE=sandbox:
    # every repair attempt runs the repository's tests again.
    codeforge_max_repair_attempts: int = Field(default=0, ge=0, le=5)
    # Docker test executor (CODEFORGE_TEST_EXECUTION_MODE=docker).  The image
    # must be digest-pinned (see PINNED_IMAGE_PATTERN) and is never pulled.
    codeforge_docker_test_image: str = ""
    codeforge_docker_cli: str = "docker"
    codeforge_docker_timeout_seconds: int = Field(default=300, ge=10, le=3600)
    codeforge_docker_cpus: float = Field(default=1.0, gt=0, le=16)
    codeforge_docker_memory_mb: int = Field(default=2048, ge=1024, le=65536)
    codeforge_docker_pids_limit: int = Field(default=256, ge=32, le=4096)
    # gVisor test executor (CODEFORGE_TEST_EXECUTION_MODE=gvisor).  Reuses the
    # Docker image and limits above.  The runtime name and the absolute runsc
    # path must match the daemon's registration exactly; there are no defaults.
    codeforge_gvisor_runtime: str = ""
    codeforge_gvisor_runtime_path: str = ""
    # Guest task limit (RLIMIT_NPROC).  64 is the measured value; raising it
    # needs new measurements (see the task-limit policy in gvisor_runtime.py).
    codeforge_gvisor_nproc_limit: int = Field(default=64, ge=NPROC_MIN, le=NPROC_MAX)
    # execution_target=local runs the agent and the repository's tests directly
    # on the worker host against an operator-supplied path.  Off by default;
    # when enabled, WORKSPACE_ROOT must be set and every local workspace must
    # resolve (symlinks included) to a directory strictly inside the owner's
    # WORKSPACE_ROOT/<github user id>/ directory.
    codeforge_allow_local_execution: bool = False
    workspace_root: str = ""
    # Immutable GitHub user IDs (not logins) trusted to run code on the worker
    # host: only they may create, retry, approve and execute local tasks, and
    # under host_unsafe only their tasks' tests run.  A JSON list, e.g.
    # [12345678].  Empty by default, which trusts nobody; logging in through
    # GitHub OAuth never grants this.
    # StrictInt: [true], ["123"] or [1.0] are rejected, never coerced to an ID.
    codeforge_local_execution_github_ids: list[StrictInt] = Field(default_factory=list)
    # Where GitHub tasks clone repositories (default: the system temporary
    # directory).  Must not overlap WORKSPACE_ROOT, so local tasks can never
    # reach another task's clone.
    codeforge_github_clone_root: str = ""

    # ── Phase 6: GitHub Integration ────────────────────────────────────────────
    github_token: str = ""
    github_api_url: str = "https://api.github.com"
    github_app_id: str = ""
    github_app_private_key: str = ""
    github_webhook_secret: str = ""
    github_app_name: str = "CodeForge"
    github_token_refresh_buffer_seconds: int = 300
    github_client_timeout_connect: float = 5.0
    github_client_timeout_read: float = 10.0
    github_client_timeout_write: float = 10.0
    github_client_timeout_pool: float = 5.0
    github_client_max_connections: int = 100
    github_client_max_keepalive: int = 20
    github_client_keepalive_expiry: float = 30.0
    github_client_max_retries: int = 3
    github_client_retry_backoff_factor: float = 0.5
    github_client_max_retry_after: int = 60

    # ── Phase 10B: Identity, OAuth & JWT ───────────────────────────────────────
    github_client_id: str = ""
    github_client_secret: str = ""
    github_oauth_redirect_uri: str = "http://localhost:8000/api/v1/auth/github/callback"
    allowed_oauth_redirect_uris: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:8000/api/v1/auth/github/callback",
            "http://localhost:3000/api/v1/auth/github/callback",
            "http://127.0.0.1:8000/api/v1/auth/github/callback",
        ]
    )
    # Must be a random value of at least MIN_JWT_SECRET_LENGTH characters unless
    # APP_ENV is explicitly development or test (see validate_session_secret).
    jwt_secret_key: str = DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_expiration_seconds: int = 86400  # 24 hours
    oauth_state_ttl_seconds: int = 600  # 10 minutes

    def is_explicit_development_environment(self) -> bool:
        """Whether APP_ENV is *explicitly* development or test.

        APP_ENV defaults to "development", so a deployment that forgets to set
        it (or sets "staging", "prod", "" ...) must not get development-only
        behaviour.
        """
        return "app_env" in self.model_fields_set and self.app_env in DEVELOPMENT_ENVIRONMENTS

    def allows_host_test_execution(self) -> bool:
        """Whether repository tests may run on the worker host (host_unsafe)."""
        return self.is_explicit_development_environment()

    def jwt_secret_violation(self) -> str | None:
        """Why jwt_secret_key must not sign session tokens here (None: it may).

        Outside an explicit development/test environment the secret must not
        be the public default or a placeholder, and must be at least
        MIN_JWT_SECRET_LENGTH characters.  The secret itself is never quoted.
        """
        if self.is_explicit_development_environment():
            return None
        secret = self.jwt_secret_key
        if secret == DEFAULT_JWT_SECRET or "insecure" in secret.lower():
            problem = "is the public default or a placeholder"
        elif len(secret) < MIN_JWT_SECRET_LENGTH:
            problem = f"is shorter than {MIN_JWT_SECRET_LENGTH} characters"
        else:
            return None
        got = repr(self.app_env) if "app_env" in self.model_fields_set else "unset"
        return (
            f"JWT_SECRET_KEY must be securely configured: it {problem}.  Only an explicit "
            f"APP_ENV=development or APP_ENV=test accepts it (APP_ENV is {got})"
        )

    @model_validator(mode="after")
    def validate_test_execution(self) -> "Settings":
        if (
            self.codeforge_test_execution_mode == ExecutionMode.HOST_UNSAFE
            and not self.allows_host_test_execution()
        ):
            got = repr(self.app_env) if "app_env" in self.model_fields_set else "unset"
            raise ValueError(
                "CODEFORGE_TEST_EXECUTION_MODE=host_unsafe requires APP_ENV to be explicitly "
                f"set to development or test (got {got})"
            )
        if (
            self.codeforge_max_repair_attempts > 0
            and self.codeforge_test_execution_mode != ExecutionMode.SANDBOX
        ):
            raise ValueError(
                "CODEFORGE_MAX_REPAIR_ATTEMPTS > 0 requires CODEFORGE_TEST_EXECUTION_MODE=sandbox"
            )
        mode = self.codeforge_test_execution_mode
        if mode in (ExecutionMode.DOCKER, ExecutionMode.GVISOR) and not (
            PINNED_IMAGE_PATTERN.fullmatch(self.codeforge_docker_test_image)
        ):
            raise ValueError(
                f"CODEFORGE_TEST_EXECUTION_MODE={mode.value} requires CODEFORGE_DOCKER_TEST_IMAGE "
                "pinned by digest (name@sha256:<digest> or an image ID sha256:<id>)"
            )
        if mode == ExecutionMode.GVISOR:
            try:
                GVisorRuntimeRequirement(
                    name=self.codeforge_gvisor_runtime, path=self.codeforge_gvisor_runtime_path
                )
            except ValueError as exc:
                raise ValueError(
                    "CODEFORGE_TEST_EXECUTION_MODE=gvisor requires CODEFORGE_GVISOR_RUNTIME and "
                    f"CODEFORGE_GVISOR_RUNTIME_PATH: {exc}"
                ) from None
            problems = task_limit_violations(
                self.codeforge_gvisor_nproc_limit, self.codeforge_docker_pids_limit
            )
            if problems:
                raise ValueError(
                    "CODEFORGE_TEST_EXECUTION_MODE=gvisor: CODEFORGE_GVISOR_NPROC_LIMIT / "
                    "CODEFORGE_DOCKER_PIDS_LIMIT: " + "; ".join(problems)
                )
        return self

    @model_validator(mode="after")
    def validate_local_execution(self) -> "Settings":
        if self.codeforge_allow_local_execution and not self.workspace_root:
            raise ValueError(
                "WORKSPACE_ROOT is required when CODEFORGE_ALLOW_LOCAL_EXECUTION is enabled"
            )
        if any(github_id <= 0 for github_id in self.codeforge_local_execution_github_ids):
            raise ValueError("CODEFORGE_LOCAL_EXECUTION_GITHUB_IDS must contain positive GitHub user IDs")
        if self.workspace_root and self.codeforge_github_clone_root:
            # Checked again, on existing paths, before every clone.
            workspace = Path(self.workspace_root).resolve()
            clones = Path(self.codeforge_github_clone_root).resolve()
            if workspace.is_relative_to(clones) or clones.is_relative_to(workspace):
                raise ValueError("CODEFORGE_GITHUB_CLONE_ROOT and WORKSPACE_ROOT must not overlap")
        return self

    @model_validator(mode="after")
    def validate_production(self) -> "Settings":
        if self.app_env == "production":
            if not self.openai_api_key:
                raise ValueError("OPENAI_API_KEY is required in production")
            # In production, we might require more strict checks
            if self.database_url == "postgresql+asyncpg://codeforge:codeforge@postgres:5432/codeforge":
                raise ValueError("DATABASE_URL must be explicitly configured in production")
        return self

    @model_validator(mode="after")
    def validate_session_secret(self) -> "Settings":
        # Every environment, not only APP_ENV=production: a missing, misspelled
        # or staging APP_ENV must not leave session tokens forgeable.
        problem = self.jwt_secret_violation()
        if problem:
            raise ValueError(problem)
        return self

# Module-level singleton
settings = Settings()
