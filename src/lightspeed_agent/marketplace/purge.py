"""Data purge service for cancelled/deleted marketplace entitlements."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
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
    dcr_client_deleted: bool = False
    entitlement_deleted: bool = False
    rate_limit_keys_deleted: int = 0
    error_count: int = 0


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

    def _ensure_rate_limiter(self) -> None:
        """Resolve rate limiter once (may not be available in all contexts)."""
        if self._rate_limiter is None:
            try:
                self._rate_limiter = get_redis_rate_limiter()
            except Exception:
                logger.warning("Redis rate limiter not available for purge cleanup")

    async def purge_order_data(self, order_id: str) -> PurgeResult:
        """Delete all data associated with a cancelled/deleted order.

        Deletion order (child records first):
        1. Usage records (by order_id)
        2. DCR client (local DB row)
        3. Entitlement record (by id = order_id)
        4. Rate limit keys (best-effort)
        """
        result = PurgeResult(order_id=order_id)
        self._ensure_rate_limiter()

        # 1. Usage records
        try:
            result.usage_records_deleted = await self._usage_repo.delete_by_order_id(
                order_id
            )
        except Exception:
            result.error_count += 1
            logger.exception("Failed to delete usage records for order %s", order_id)

        # 2. DCR client (local DB safety net)
        try:
            result.dcr_client_deleted = await self._dcr_repo.delete_by_order_id(order_id)
        except Exception:
            result.error_count += 1
            logger.exception("Failed to delete DCR client for order %s", order_id)

        # 3. Entitlement record — skip if child record deletion failed,
        #    so the entitlement remains discoverable for the next purge run.
        if result.error_count == 0:
            try:
                result.entitlement_deleted = await self._entitlement_repo.delete(order_id)
            except Exception:
                result.error_count += 1
                logger.exception("Failed to delete entitlement for order %s", order_id)
        else:
            logger.warning(
                "Skipping entitlement delete for order %s due to %d prior errors",
                order_id,
                result.error_count,
            )

        # 4. Rate limit keys (best-effort)
        if self._rate_limiter:
            try:
                result.rate_limit_keys_deleted = await self._rate_limiter.delete_keys_for_order(
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
            result.error_count,
        )
        return result

    async def purge_expired_data(
        self,
        retention_days: int,
        *,
        batch_size: int = 100,
        max_concurrency: int = 10,
    ) -> list[PurgeResult]:
        """Bulk purge data for entitlements cancelled/deleted longer than retention_days.

        Processes in batches until no more expired entitlements remain.
        """
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        self._ensure_rate_limiter()
        results: list[PurgeResult] = []
        sem = asyncio.Semaphore(max_concurrency)
        max_batches = 1000

        async def _limited_purge(order_id: str) -> PurgeResult:
            async with sem:
                return await self.purge_order_data(order_id)

        for _ in range(max_batches):
            expired = await self._entitlement_repo.get_expired_cancelled(
                cutoff, limit=batch_size
            )
            if not expired:
                break

            logger.info(
                "Purging batch of %d expired cancelled/deleted entitlements "
                "(retention=%d days)",
                len(expired),
                retention_days,
            )

            batch_results = await asyncio.gather(
                *(_limited_purge(e.id) for e in expired),
                return_exceptions=True,
            )
            for i, r in enumerate(batch_results):
                if isinstance(r, PurgeResult):
                    results.append(r)
                else:
                    order_id = expired[i].id
                    logger.error("Purge failed for order %s: %s", order_id, r)
                    results.append(PurgeResult(order_id=order_id, error_count=1))
        else:
            logger.warning(
                "Purge loop hit max batch limit (%d); remaining entries "
                "will be processed on the next run",
                max_batches,
            )

        return results


_purge_service: DataPurgeService | None = None


def get_data_purge_service() -> DataPurgeService:
    """Get the global data purge service instance."""
    global _purge_service
    if _purge_service is None:
        _purge_service = DataPurgeService()
    return _purge_service
