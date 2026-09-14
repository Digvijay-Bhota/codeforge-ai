from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import GitHubIdentity, TaskGitHubLink, User

logger = logging.getLogger(__name__)


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_user(self, user_id: str) -> User | None:
        result = await self.session.execute(select(User).where(User.id == user_id))
        return result.scalars().first()

    async def get_user_by_email(self, email: str) -> User | None:
        result = await self.session.execute(select(User).where(User.email == email))
        return result.scalars().first()

    async def get_user_by_github_id(self, github_user_id: int) -> User | None:
        result = await self.session.execute(
            select(User)
            .join(GitHubIdentity, GitHubIdentity.user_id == User.id)
            .where(GitHubIdentity.github_user_id == github_user_id)
        )
        return result.scalars().first()

    async def get_user_by_github_login(self, github_login: str) -> User | None:
        result = await self.session.execute(
            select(User)
            .join(GitHubIdentity, GitHubIdentity.user_id == User.id)
            .where(GitHubIdentity.github_login == github_login)
        )
        return result.scalars().first()

    async def get_github_identity(self, user_id: str) -> GitHubIdentity | None:
        result = await self.session.execute(
            select(GitHubIdentity).where(GitHubIdentity.user_id == user_id)
        )
        return result.scalars().first()

    async def upsert_github_user(
        self,
        github_user_id: int,
        github_login: str,
        display_name: str,
        email: str | None = None,
        avatar_url: str | None = None,
    ) -> tuple[User, GitHubIdentity]:
        """Upsert User and linked GitHubIdentity.

        Ensures a single GitHub user ID links to exactly one CodeForge user.
        Updates profile fields if changed.
        """
        identity_result = await self.session.execute(
            select(GitHubIdentity).where(GitHubIdentity.github_user_id == github_user_id)
        )
        identity = identity_result.scalars().first()

        if identity:
            user = await self.get_user(identity.user_id)
            if not user:
                user = User(
                    id=identity.user_id,
                    display_name=display_name,
                    email=email,
                    avatar_url=avatar_url,
                    is_active=True,
                )
                self.session.add(user)
            else:
                user.display_name = display_name
                if email:
                    user.email = email
                if avatar_url:
                    user.avatar_url = avatar_url

            identity.github_login = github_login
            await self.session.flush()
            return user, identity

        new_user_id = str(uuid.uuid4())
        user = User(
            id=new_user_id,
            display_name=display_name,
            email=email,
            avatar_url=avatar_url,
            is_active=True,
        )
        self.session.add(user)
        await self.session.flush()

        identity = GitHubIdentity(
            user_id=user.id,
            github_user_id=github_user_id,
            github_login=github_login,
        )
        self.session.add(identity)
        await self.session.flush()
        return user, identity

    async def create_task_github_link(self, link: TaskGitHubLink) -> TaskGitHubLink:
        self.session.add(link)
        await self.session.flush()
        return link

    async def get_task_github_link(
        self, task_id: str, for_update: bool = False
    ) -> TaskGitHubLink | None:
        query = select(TaskGitHubLink).where(TaskGitHubLink.task_id == task_id)
        if for_update:
            query = query.with_for_update().execution_options(populate_existing=True)
        result = await self.session.execute(query)
        return result.scalars().first()

    async def get_task_github_link_by_issue(
        self, repository_id: int, issue_id: int
    ) -> TaskGitHubLink | None:
        result = await self.session.execute(
            select(TaskGitHubLink).where(
                TaskGitHubLink.repository_id == repository_id,
                TaskGitHubLink.issue_id == issue_id,
            )
        )
        return result.scalars().first()

    async def get_task_github_link_by_comment(
        self, repository_id: int, comment_id: int
    ) -> TaskGitHubLink | None:
        result = await self.session.execute(
            select(TaskGitHubLink).where(
                TaskGitHubLink.repository_id == repository_id,
                TaskGitHubLink.trigger_comment_id == comment_id,
            )
        )
        return result.scalars().first()

    async def update_task_github_link(
        self, link: TaskGitHubLink
    ) -> TaskGitHubLink:
        self.session.add(link)
        await self.session.flush()
        return link

