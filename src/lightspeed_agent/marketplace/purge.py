"""Data purge service for cancelled/deleted marketplace entitlements."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from lightspeed_agent.dcr.repository import DCRClientRepository, get_dcr_client_repository
from lightspeed_agent.marketplace.repository import (
    EntitlementRepository,
    get_entitlement_repository,
)
from lightspeed_agent.metering.repository import UsageRepository, get_usage_repository
from lightspeed_agent.ratelimit.middleware import RedisRateLimiter, get_redis_rate_limiter

logger = logging.getLogger(__name__)


@dataclass
class PurgeResult:
    """Result of purging data for a single order."""

    order_id: str
    usage_records_deleted: int = 0
    entitlement_deleted: bool = False
    rate_limit_keys_deleted: int = 0
    errors: list[str] = field(default_factory=list)


class DataPurgeService:
    """Service for purging data associated with cancelled/deleted orders.

    Deletion order (child records first, no FK constraints):
    1. Usage records (by order_id)
    2. DCR client record (local DB safety net — external GMA deletion is
       handled separately by the cancel flow)
    3. Entitlement record (by id = order_id)
    4. Rate limit keys (best-effort, keys auto-expire within 1hr)
    """

    def __init__(
        self,
        usage_repo: UsageRepository | None = None,
        entitlement_repo: EntitlementRepository | None = None,
        dcr_repo: DCRClientRepository | None = None,
        rate_limiter: RedisRateLimiter | None = None,
    ) -> None:
        self._usage_repo = usage_repo or get_usage_repository()
        self._entitlement_repo = entitlement_repo or get_entitlement_repository()
        self._dcr_repo = dcr_repo or get_dcr_client_repository()
        self._rate_limiter = rate_limiter

    def _get_rate_limiter(self) -> RedisRateLimiter | None:
        """Lazy-init rate limiter (may not be available in all contexts)."""
        if self._rate_limiter is None:
            try:
                self._rate_limiter = get_redis_rate_limiter()
            except Exception:
                logger.debug("Redis rate limiter not available for purge cleanup")
        return self._rate_limiter

    async def purge_order_usage(self, order_id: str) -> int:
        """Delete usage records for an order. Returns count deleted."""
        return await self._usage_repo.delete_by_order_id(order_id)

    async def purge_order_data(self, order_id: str) -> PurgeResult:
        """Delete all data associated with a cancelled/deleted order.

        Deletion order (child records first):
        1. Usage records (by order_id)
        2. DCR client (local DB row)
        3. Entitlement record (by id = order_id)
        4. Rate limit keys (best-effort)
        """
        result = PurgeResult(order_id=order_id)

        # 1. Usage records
        try:
            result.usage_records_deleted = await self._usage_repo.delete_by_order_id(
                order_id
            )
        except Exception as e:
            result.errors.append(f"usage_records: {e}")
            logger.exception("Failed to delete usage records for order %s", order_id)

        # 2. DCR client (local DB safety net)
        try:
            await self._dcr_repo.delete_by_order_id(order_id)
        except Exception as e:
            result.errors.append(f"dcr_client: {e}")
            logger.exception("Failed to delete DCR client for order %s", order_id)

        # 3. Entitlement record
        try:
            result.entitlement_deleted = await self._entitlement_repo.delete(order_id)
        except Exception as e:
            result.errors.append(f"entitlement: {e}")
            logger.exception("Failed to delete entitlement for order %s", order_id)

        # 4. Rate limit keys (best-effort)
        limiter = self._get_rate_limiter()
        if limiter:
            try:
                result.rate_limit_keys_deleted = await limiter.delete_keys_for_order(
                    order_id
                )
            except Exception:
                logger.warning(
                    "Failed to delete rate limit keys for order %s", order_id
                )

        logger.info(
            "Purge complete for order %s: usage_records=%d, entitlement=%s, "
            "rate_limit_keys=%d, errors=%d",
            order_id,
            result.usage_records_deleted,
            result.entitlement_deleted,
            result.rate_limit_keys_deleted,
            len(result.errors),
        )
        return result

    async def purge_expired_data(self, retention_days: int) -> list[PurgeResult]:
        """Bulk purge data for entitlements cancelled/deleted longer than retention_days."""
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        expired = await self._entitlement_repo.get_expired_cancelled(cutoff)
        if not expired:
            return []

        logger.info(
            "Found %d expired cancelled/deleted entitlements (retention=%d days)",
            len(expired),
            retention_days,
        )

        results: list[PurgeResult] = []
        for entitlement in expired:
            result = await self.purge_order_data(entitlement.id)
            results.append(result)

        return results


_purge_service: DataPurgeService | None = None


def get_data_purge_service() -> DataPurgeService:
    """Get the global data purge service instance."""
    global _purge_service
    if _purge_service is None:
        _purge_service = DataPurgeService()
    return _purge_service
