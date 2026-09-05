
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import GitHubInstallation, GitHubInstallationRepository, WebhookDelivery


class WebhookRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def record_delivery(self, delivery: WebhookDelivery) -> bool:
        """Record delivery, returning True if inserted, False if duplicate."""
        stmt = insert(WebhookDelivery).values(
            delivery_id=delivery.delivery_id,
            event_type=delivery.event_type,
            installation_id=delivery.installation_id,
            repository=delivery.repository,
            status=delivery.status
        ).on_conflict_do_nothing(index_elements=["delivery_id"])

        result = await self.session.execute(stmt)
        return result.rowcount > 0

    async def get_installation(self, installation_id: int) -> GitHubInstallation | None:
        result = await self.session.execute(
            select(GitHubInstallation).where(GitHubInstallation.installation_id == installation_id)
        )
        return result.scalars().first()

    async def is_repository_authorized(self, installation_id: int, repository: str) -> bool:
        result = await self.session.execute(
            select(GitHubInstallationRepository).where(
                GitHubInstallationRepository.installation_id == installation_id,
                GitHubInstallationRepository.repository == repository,
                GitHubInstallationRepository.authorized .is_(True)
            )
        )
        return result.scalars().first() is not None
