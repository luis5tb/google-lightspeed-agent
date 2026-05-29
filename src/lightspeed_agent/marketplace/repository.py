"""Repository for storing and retrieving marketplace accounts and entitlements.

Uses PostgreSQL via SQLAlchemy for persistence.
"""

import logging

from sqlalchemy import select

from lightspeed_agent.db import (
    MarketplaceEntitlementModel,
    get_session,
)
from lightspeed_agent.marketplace.models import (
    Entitlement,
    EntitlementState,
)

logger = logging.getLogger(__name__)


class EntitlementRepository:
    """Repository for marketplace entitlements (orders).

    Uses PostgreSQL via SQLAlchemy for persistence.
    """

    async def get(self, entitlement_id: str) -> Entitlement | None:
        """Get an entitlement by ID.

        Args:
            entitlement_id: The Entitlement/Order ID.

        Returns:
            Entitlement if found, None otherwise.
        """
        async with get_session() as session:
            result = await session.execute(
                select(MarketplaceEntitlementModel).where(
                    MarketplaceEntitlementModel.id == entitlement_id
                )
            )
            model = result.scalar_one_or_none()
            if model:
                return self._model_to_entity(model)
            return None

    async def get_all_active(self) -> list[Entitlement]:
        """Get all active entitlements.

        Returns:
            List of active entitlements.
        """
        async with get_session() as session:
            result = await session.execute(
                select(MarketplaceEntitlementModel).where(
                    MarketplaceEntitlementModel.state == EntitlementState.ACTIVE.value
                )
            )
            models = result.scalars().all()
            return [self._model_to_entity(m) for m in models]

    async def create(self, entitlement: Entitlement) -> Entitlement:
        """Create a new entitlement.

        Args:
            entitlement: The entitlement to create.

        Returns:
            The created entitlement.
        """
        async with get_session() as session:
            model = MarketplaceEntitlementModel(
                id=entitlement.id,
                account_id=entitlement.account_id,
                provider_id=entitlement.provider_id,
                product_id=entitlement.metadata.get("product_id"),
                plan=entitlement.plan,
                state=entitlement.state.value,
                usage_reporting_id=entitlement.usage_reporting_id,
                offer_start_time=entitlement.offer_start_time,
                offer_end_time=entitlement.offer_end_time,
                cancellation_reason=entitlement.cancellation_reason,
                metadata_=entitlement.metadata,
            )
            session.add(model)
            await session.flush()
            await session.refresh(model)

            logger.info(
                "Created entitlement: %s (account=%s)",
                entitlement.id,
                entitlement.account_id,
            )
            return self._model_to_entity(model)

    async def update(self, entitlement: Entitlement) -> Entitlement:
        """Update an existing entitlement.

        Args:
            entitlement: The entitlement to update.

        Returns:
            The updated entitlement.
        """
        async with get_session() as session:
            result = await session.execute(
                select(MarketplaceEntitlementModel).where(
                    MarketplaceEntitlementModel.id == entitlement.id
                )
            )
            model = result.scalar_one_or_none()
            if not model:
                raise ValueError(f"Entitlement not found: {entitlement.id}")

            model.account_id = entitlement.account_id
            model.provider_id = entitlement.provider_id
            model.plan = entitlement.plan
            model.state = entitlement.state.value
            model.usage_reporting_id = entitlement.usage_reporting_id
            model.offer_start_time = entitlement.offer_start_time
            model.offer_end_time = entitlement.offer_end_time
            model.cancellation_reason = entitlement.cancellation_reason
            model.metadata_ = entitlement.metadata

            logger.info(
                "Updated entitlement: %s (state=%s)",
                entitlement.id,
                entitlement.state,
            )
            return self._model_to_entity(model)

    async def is_valid(self, entitlement_id: str) -> bool:
        """Check if an entitlement is valid (exists and active).

        Args:
            entitlement_id: The entitlement ID.

        Returns:
            True if valid, False otherwise.
        """
        entitlement = await self.get(entitlement_id)
        return entitlement is not None and entitlement.state == EntitlementState.ACTIVE

    def _model_to_entity(self, model: MarketplaceEntitlementModel) -> Entitlement:
        """Convert ORM model to Pydantic entity."""
        return Entitlement(
            id=model.id,
            account_id=model.account_id,
            provider_id=model.provider_id,
            plan=model.plan,
            state=EntitlementState(model.state),
            usage_reporting_id=model.usage_reporting_id,
            offer_start_time=model.offer_start_time,
            offer_end_time=model.offer_end_time,
            cancellation_reason=model.cancellation_reason,
            created_at=model.created_at,
            updated_at=model.updated_at,
            metadata=model.metadata_ or {},
        )


# Global repository instance
_entitlement_repo: EntitlementRepository | None = None


def get_entitlement_repository() -> EntitlementRepository:
    """Get the global entitlement repository instance.

    Returns:
        EntitlementRepository instance.
    """
    global _entitlement_repo
    if _entitlement_repo is None:
        _entitlement_repo = EntitlementRepository()
    return _entitlement_repo
