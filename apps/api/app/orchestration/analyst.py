"""Phase 5: Repository Analyst.

READ-ONLY wrapper around the existing Phase 2 repository intelligence layer.

Responsibilities:
  - Accept a task description and a workspace.
  - Invoke RepositoryScanner and GitScanner via the existing
    build_repository_context() helper.
  - Return a bounded RepositoryContext.

The analyst does NOT:
  - Modify any files.
  - Hold WRITE or DANGEROUS MCP permissions.
  - Duplicate Phase 2 scanning logic.
  - Introduce embeddings or RAG.
"""

from __future__ import annotations

import logging

from app.repository.context import build_repository_context
from app.repository.models import RepositoryContext
from app.workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

MAX_ANALYST_FAILURE_MSG = 400  # characters — keep error messages bounded


class RepositoryAnalystError(Exception):
    """Raised when the Repository Analyst cannot produce a RepositoryContext."""


class RepositoryAnalyst:
    """Deterministic read-only repository analysis agent.

    Wraps the existing Phase 2 scanning infrastructure.  This class exists as
    a named orchestration stage so the Orchestrator can treat it as a discrete
    step with explicit failure handling.
    """

    def analyse(
        self, workspace: WorkspaceManager, task_description: str
    ) -> RepositoryContext:
        """Scan *workspace* and return a :class:`RepositoryContext`.

        Args:
            workspace: Sandboxed workspace to analyse.  Read-only access only.
            task_description: Used to rank relevant files.

        Returns:
            A bounded :class:`RepositoryContext`.

        Raises:
            :class:`RepositoryAnalystError`: if scanning fails.
        """
        logger.info(
            "RepositoryAnalyst: scanning workspace=%s task=%.60s",
            workspace.root,
            task_description,
        )
        try:
            context = build_repository_context(workspace, task_description)
            logger.info(
                "RepositoryAnalyst: finished — languages=%s frameworks=%s relevant_files=%d",
                list(context.repository_map.languages.keys()),
                context.repository_map.frameworks,
                len(context.relevant_files),
            )
            return context
        except Exception as exc:
            # Bound the error message — never expose a full stack trace upward.
            safe_msg = str(exc)[: MAX_ANALYST_FAILURE_MSG]
            logger.error("RepositoryAnalyst: scan failed — %s", safe_msg)
            raise RepositoryAnalystError(
                f"Repository analysis failed: {safe_msg}"
            ) from exc
