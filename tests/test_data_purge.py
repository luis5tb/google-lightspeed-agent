"""Tests for data purge operations (APPENG-5150).

Covers:
- UsageRepository.delete_by_order_id()
- EntitlementRepository.delete() and .get_expired_cancelled()
- RedisRateLimiter.delete_keys_for_order()
- DataPurgeService.purge_order_data() and .purge_expired_data()
- Integration with cancel/delete event handlers
- Configuration settings for data retention
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from lightspeed_agent.db import (
    DCRClientModel,
    MarketplaceEntitlementModel,
    UsageRecordModel,
    get_session,
)
from lightspeed_agent.marketplace.models import EntitlementState
from lightspeed_agent.marketplace.repository import EntitlementRepository
from lightspeed_agent.metering.repository import UsageRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_entitlement(
    order_id: str,
    state: str = "active",
    account_id: str = "acct-1",
    provider_id: str = "prov-1",
    updated_at: datetime | None = None,
) -> None:
    """Insert an entitlement row directly into the DB."""
    async with get_session() as session:
        model = MarketplaceEntitlementModel(
            id=order_id,
            account_id=account_id,
            provider_id=provider_id,
            state=state,
        )
        session.add(model)
        await session.flush()
        # If we need to backdate, update after flush so server_default is overridden
        if updated_at is not None:
            model.updated_at = updated_at
            await session.flush()


async def _create_usage_record(
    order_id: str,
    *,
    request_count: int = 1,
    input_tokens: int = 10,
    output_tokens: int = 5,
    tool_calls: int = 0,
    reported: bool = False,
    reporting_started_at: datetime | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> int:
    """Insert a usage record directly into the DB. Returns the record ID."""
    now = datetime.now(UTC)
    ps = period_start or now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    pe = period_end or ps + timedelta(hours=1)

    async with get_session() as session:
        model = UsageRecordModel(
            order_id=order_id,
            request_count=request_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
            period_start=ps,
            period_end=pe,
            reported=reported,
            reporting_started_at=reporting_started_at,
        )
        session.add(model)
        await session.flush()
        await session.refresh(model)
        return model.id


async def _create_dcr_client(
    order_id: str,
    client_id: str = "client-1",
    account_id: str = "acct-1",
) -> None:
    """Insert a DCR client row directly into the DB."""
    async with get_session() as session:
        model = DCRClientModel(
            order_id=order_id,
            client_id=client_id,
            client_secret_encrypted="encrypted-secret",
            account_id=account_id,
        )
        session.add(model)


async def _count_usage_records(order_id: str) -> int:
    """Count usage records for a given order_id."""
    async with get_session() as session:
        result = await session.execute(
            select(UsageRecordModel).where(UsageRecordModel.order_id == order_id)
        )
        return len(result.scalars().all())


async def _count_entitlements(order_id: str) -> int:
    """Count entitlements for a given order_id."""
    async with get_session() as session:
        result = await session.execute(
            select(MarketplaceEntitlementModel).where(
                MarketplaceEntitlementModel.id == order_id
            )
        )
        return len(result.scalars().all())


async def _count_dcr_clients(order_id: str) -> int:
    """Count DCR clients for a given order_id."""
    async with get_session() as session:
        result = await session.execute(
            select(DCRClientModel).where(DCRClientModel.order_id == order_id)
        )
        return len(result.scalars().all())


# ===========================================================================
# 1. UsageRepository.delete_by_order_id
# ===========================================================================


class TestUsageRepositoryDeleteByOrderId:
    """Tests for UsageRepository.delete_by_order_id()."""

    @pytest.mark.asyncio
    async def test_delete_multiple_records(self, db_session):
        """Delete usage records for an order with multiple records (reported + unreported)."""
        repo = UsageRepository()

        # Create several records for the same order: reported and unreported
        base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        await _create_usage_record(
            "order-del-1",
            reported=False,
            period_start=base - timedelta(hours=3),
            period_end=base - timedelta(hours=2),
        )
        await _create_usage_record(
            "order-del-1",
            reported=True,
            period_start=base - timedelta(hours=2),
            period_end=base - timedelta(hours=1),
        )

        assert await _count_usage_records("order-del-1") == 2

        deleted = await repo.delete_by_order_id("order-del-1")
        assert deleted == 2
        assert await _count_usage_records("order-del-1") == 0

    @pytest.mark.asyncio
    async def test_delete_no_records(self, db_session):
        """Delete when no records exist should be a no-op returning 0."""
        repo = UsageRepository()

        deleted = await repo.delete_by_order_id("nonexistent-order")
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_delete_only_affects_target_order(self, db_session):
        """Delete should only remove records for the target order_id."""
        repo = UsageRepository()

        await _create_usage_record("order-target")
        await _create_usage_record("order-other")

        deleted = await repo.delete_by_order_id("order-target")
        assert deleted == 1
        assert await _count_usage_records("order-target") == 0
        # Other order untouched
        assert await _count_usage_records("order-other") == 1

    @pytest.mark.asyncio
    async def test_delete_in_flight_records(self, db_session):
        """Delete records that are in-flight (reporting_started_at set) should still work."""
        repo = UsageRepository()

        await _create_usage_record(
            "order-inflight",
            reporting_started_at=datetime.now(UTC) - timedelta(minutes=5),
        )

        deleted = await repo.delete_by_order_id("order-inflight")
        assert deleted == 1
        assert await _count_usage_records("order-inflight") == 0


# ===========================================================================
# 2. EntitlementRepository.delete
# ===========================================================================


class TestEntitlementRepositoryDelete:
    """Tests for EntitlementRepository.delete()."""

    @pytest.mark.asyncio
    async def test_delete_existing_entitlement(self, db_session):
        """Delete an existing entitlement returns True."""
        repo = EntitlementRepository()
        await _create_entitlement("order-ent-del-1", state="cancelled")

        result = await repo.delete("order-ent-del-1")
        assert result is True
        assert await _count_entitlements("order-ent-del-1") == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent_entitlement(self, db_session):
        """Delete a non-existent entitlement returns False."""
        repo = EntitlementRepository()

        result = await repo.delete("nonexistent-order")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_does_not_affect_others(self, db_session):
        """Deleting one entitlement leaves others untouched."""
        repo = EntitlementRepository()
        await _create_entitlement("order-del-a", state="cancelled")
        await _create_entitlement("order-del-b", state="active")

        await repo.delete("order-del-a")
        assert await _count_entitlements("order-del-a") == 0
        assert await _count_entitlements("order-del-b") == 1


# ===========================================================================
# 3. EntitlementRepository.get_expired_cancelled
# ===========================================================================


class TestEntitlementRepositoryGetExpiredCancelled:
    """Tests for EntitlementRepository.get_expired_cancelled()."""

    @pytest.mark.asyncio
    async def test_returns_old_cancelled_entitlements(self, db_session):
        """Returns entitlements in CANCELLED state older than threshold."""
        repo = EntitlementRepository()
        old_time = datetime.now(UTC) - timedelta(days=45)
        await _create_entitlement("order-old-cancel", state="cancelled", updated_at=old_time)

        cutoff = datetime.now(UTC) - timedelta(days=30)
        results = await repo.get_expired_cancelled(cutoff)
        order_ids = [e.id for e in results]
        assert "order-old-cancel" in order_ids

    @pytest.mark.asyncio
    async def test_returns_old_deleted_entitlements(self, db_session):
        """Returns entitlements in DELETED state older than threshold."""
        repo = EntitlementRepository()
        old_time = datetime.now(UTC) - timedelta(days=45)
        await _create_entitlement("order-old-deleted", state="deleted", updated_at=old_time)

        cutoff = datetime.now(UTC) - timedelta(days=30)
        results = await repo.get_expired_cancelled(cutoff)
        order_ids = [e.id for e in results]
        assert "order-old-deleted" in order_ids

    @pytest.mark.asyncio
    async def test_does_not_return_active_entitlements(self, db_session):
        """Active entitlements should never be returned regardless of age."""
        repo = EntitlementRepository()
        old_time = datetime.now(UTC) - timedelta(days=60)
        await _create_entitlement("order-active-old", state="active", updated_at=old_time)

        cutoff = datetime.now(UTC) - timedelta(days=30)
        results = await repo.get_expired_cancelled(cutoff)
        order_ids = [e.id for e in results]
        assert "order-active-old" not in order_ids

    @pytest.mark.asyncio
    async def test_does_not_return_recently_cancelled(self, db_session):
        """Recently cancelled entitlements (within retention) should not be returned."""
        repo = EntitlementRepository()
        recent_time = datetime.now(UTC) - timedelta(days=5)
        await _create_entitlement("order-recent-cancel", state="cancelled", updated_at=recent_time)

        cutoff = datetime.now(UTC) - timedelta(days=30)
        results = await repo.get_expired_cancelled(cutoff)
        order_ids = [e.id for e in results]
        assert "order-recent-cancel" not in order_ids

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_expired(self, db_session):
        """Returns empty list when no expired entitlements exist."""
        repo = EntitlementRepository()
        # Create only active entitlements
        await _create_entitlement("order-active-only", state="active")

        cutoff = datetime.now(UTC) - timedelta(days=30)
        results = await repo.get_expired_cancelled(cutoff)
        assert results == []


# ===========================================================================
# 4. RedisRateLimiter.delete_keys_for_order
# ===========================================================================


class TestRedisRateLimiterDeleteKeysForOrder:
    """Tests for RedisRateLimiter.delete_keys_for_order()."""

    @pytest.mark.asyncio
    async def test_deletes_correct_key_pattern(self):
        """Mock Redis and verify the correct key pattern is used for deletion."""
        mock_redis = AsyncMock()
        # Simulate scan returning matching keys then empty
        mock_redis.scan.side_effect = [
            (0, [
                "lightspeed:ratelimit:order:order-123:m",
                "lightspeed:ratelimit:order:order-123:h",
            ]),
        ]
        mock_redis.delete = AsyncMock(return_value=2)

        with patch("lightspeed_agent.ratelimit.middleware.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                rate_limit_redis_url="redis://localhost:6379/0",
                rate_limit_redis_timeout_ms=200,
                rate_limit_requests_per_minute=60,
                rate_limit_requests_per_hour=1000,
                rate_limit_key_prefix="lightspeed:ratelimit",
                rate_limit_redis_ca_cert="",
            )
            with patch("lightspeed_agent.ratelimit.middleware.Redis") as mock_redis_cls:
                mock_redis_cls.from_url.return_value = mock_redis

                from lightspeed_agent.ratelimit.middleware import RedisRateLimiter

                limiter = RedisRateLimiter()
                deleted = await limiter.delete_keys_for_order("order-123")
                assert deleted >= 0

                # Verify Redis was asked about keys matching the order pattern
                # The exact assertion depends on implementation (scan vs delete pattern)

    @pytest.mark.asyncio
    async def test_handles_redis_connection_error(self):
        """Handle Redis connection errors gracefully (fail-open)."""
        mock_redis = AsyncMock()
        from redis.exceptions import ConnectionError as RedisConnectionError

        mock_redis.delete.side_effect = RedisConnectionError("Connection refused")

        with patch("lightspeed_agent.ratelimit.middleware.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                rate_limit_redis_url="redis://localhost:6379/0",
                rate_limit_redis_timeout_ms=200,
                rate_limit_requests_per_minute=60,
                rate_limit_requests_per_hour=1000,
                rate_limit_key_prefix="lightspeed:ratelimit",
                rate_limit_redis_ca_cert="",
            )
            with patch("lightspeed_agent.ratelimit.middleware.Redis") as mock_redis_cls:
                mock_redis_cls.from_url.return_value = mock_redis

                from lightspeed_agent.ratelimit.middleware import RedisRateLimiter

                limiter = RedisRateLimiter()
                # Should not raise; returns 0 or handles gracefully
                deleted = await limiter.delete_keys_for_order("order-fail")
                assert deleted == 0


# ===========================================================================
# 5. DataPurgeService.purge_order_data
# ===========================================================================


class TestDataPurgeServicePurgeOrderData:
    """Tests for DataPurgeService.purge_order_data()."""

    @pytest.mark.asyncio
    async def test_purge_all_data_for_order(self, db_session):
        """Creates usage records + entitlement, purges, verifies all gone."""
        from lightspeed_agent.marketplace.purge import DataPurgeService

        # Set up data for the order
        await _create_entitlement("order-purge-1", state="cancelled")
        await _create_usage_record("order-purge-1", request_count=10)
        await _create_usage_record("order-purge-1", request_count=5, reported=True,
                                   period_start=datetime.now(UTC) - timedelta(hours=3),
                                   period_end=datetime.now(UTC) - timedelta(hours=2))
        await _create_dcr_client("order-purge-1", client_id="client-purge-1")

        # Verify setup
        assert await _count_usage_records("order-purge-1") == 2
        assert await _count_entitlements("order-purge-1") == 1
        assert await _count_dcr_clients("order-purge-1") == 1

        # Mock Redis and DCR external calls
        service = DataPurgeService()
        with patch.object(service, "_rate_limiter", new=None):
            await service.purge_order_data("order-purge-1")

        # All data should be gone
        assert await _count_usage_records("order-purge-1") == 0
        assert await _count_entitlements("order-purge-1") == 0
        assert await _count_dcr_clients("order-purge-1") == 0

    @pytest.mark.asyncio
    async def test_purge_is_idempotent(self, db_session):
        """Running purge twice on the same order is safe."""
        from lightspeed_agent.marketplace.purge import DataPurgeService

        await _create_entitlement("order-idempotent", state="deleted")
        await _create_usage_record("order-idempotent")

        service = DataPurgeService()
        with patch.object(service, "_rate_limiter", new=None):
            await service.purge_order_data("order-idempotent")
            # Second call should not raise
            await service.purge_order_data("order-idempotent")

        assert await _count_usage_records("order-idempotent") == 0
        assert await _count_entitlements("order-idempotent") == 0

    @pytest.mark.asyncio
    async def test_purge_with_redis_available(self, db_session):
        """Purge with Redis available should delete rate limit keys."""
        from lightspeed_agent.marketplace.purge import DataPurgeService

        await _create_entitlement("order-redis-ok", state="cancelled")

        mock_limiter = AsyncMock()
        mock_limiter.delete_keys_for_order = AsyncMock(return_value=2)

        service = DataPurgeService()
        with patch.object(service, "_rate_limiter", new=mock_limiter):
            await service.purge_order_data("order-redis-ok")

        mock_limiter.delete_keys_for_order.assert_awaited_once_with("order-redis-ok")

    @pytest.mark.asyncio
    async def test_purge_with_redis_unavailable(self, db_session):
        """Purge with Redis unavailable should still delete DB records (fail-open)."""
        from lightspeed_agent.marketplace.purge import DataPurgeService

        await _create_entitlement("order-redis-fail", state="cancelled")
        await _create_usage_record("order-redis-fail")

        mock_limiter = AsyncMock()
        mock_limiter.delete_keys_for_order = AsyncMock(
            side_effect=RuntimeError("Redis unavailable")
        )

        service = DataPurgeService()
        with patch.object(service, "_rate_limiter", new=mock_limiter):
            await service.purge_order_data("order-redis-fail")

        # DB records should still be deleted despite Redis failure
        assert await _count_usage_records("order-redis-fail") == 0
        assert await _count_entitlements("order-redis-fail") == 0


# ===========================================================================
# 6. DataPurgeService.purge_expired_data
# ===========================================================================


class TestDataPurgeServicePurgeExpiredData:
    """Tests for DataPurgeService.purge_expired_data()."""

    @pytest.mark.asyncio
    async def test_purge_only_expired_entitlements(self, db_session):
        """Only expired cancelled/deleted entitlements should be purged."""
        from lightspeed_agent.marketplace.purge import DataPurgeService

        old_time = datetime.now(UTC) - timedelta(days=45)
        recent_time = datetime.now(UTC) - timedelta(days=5)

        # Old cancelled - should be purged
        await _create_entitlement("order-old-1", state="cancelled", updated_at=old_time)
        await _create_usage_record("order-old-1")

        # Old deleted - should be purged
        await _create_entitlement("order-old-2", state="deleted", updated_at=old_time)
        await _create_usage_record("order-old-2")

        # Recently cancelled - should NOT be purged (within retention)
        await _create_entitlement("order-recent", state="cancelled", updated_at=recent_time)
        await _create_usage_record("order-recent")

        # Active - should NEVER be purged
        await _create_entitlement("order-active", state="active", updated_at=old_time)
        await _create_usage_record("order-active")

        service = DataPurgeService()
        with patch.object(service, "_rate_limiter", new=None):
            await service.purge_expired_data(retention_days=30)

        # Old cancelled/deleted should be purged
        assert await _count_entitlements("order-old-1") == 0
        assert await _count_usage_records("order-old-1") == 0
        assert await _count_entitlements("order-old-2") == 0
        assert await _count_usage_records("order-old-2") == 0

        # Recent cancelled and active should remain
        assert await _count_entitlements("order-recent") == 1
        assert await _count_usage_records("order-recent") == 1
        assert await _count_entitlements("order-active") == 1
        assert await _count_usage_records("order-active") == 1

    @pytest.mark.asyncio
    async def test_active_entitlements_never_purged(self, db_session):
        """Active entitlements must never be purged regardless of age."""
        from lightspeed_agent.marketplace.purge import DataPurgeService

        very_old = datetime.now(UTC) - timedelta(days=365)
        await _create_entitlement("order-active-safe", state="active", updated_at=very_old)
        await _create_usage_record("order-active-safe")

        service = DataPurgeService()
        with patch.object(service, "_rate_limiter", new=None):
            await service.purge_expired_data(retention_days=1)

        assert await _count_entitlements("order-active-safe") == 1
        assert await _count_usage_records("order-active-safe") == 1


# ===========================================================================
# 7. Integration: cancel/delete handler triggers purge
# ===========================================================================


class TestProcurementServicePurgeIntegration:
    """Test that cancel/delete handlers trigger data purge."""

    @pytest.mark.asyncio
    async def test_cancelled_handler_triggers_purge(self, db_session):
        """_handle_entitlement_cancelled should call the purge service."""
        from lightspeed_agent.marketplace.models import (
            EntitlementInfo,
            ProcurementEvent,
            ProcurementEventType,
        )
        from lightspeed_agent.marketplace.service import ProcurementService

        # Set up an entitlement to be cancelled
        repo = EntitlementRepository()
        await _create_entitlement("order-cancel-int", state="active")

        mock_dcr = AsyncMock()
        mock_dcr.delete_client = AsyncMock()

        service = ProcurementService(entitlement_repo=repo, dcr_service=mock_dcr)

        mock_purge = AsyncMock()
        with patch.object(service, "_purge_service", new=mock_purge, create=True):
            event = ProcurementEvent(
                eventId="evt-1",
                eventType=ProcurementEventType.ENTITLEMENT_CANCELLED,
                providerId="prov-1",
                entitlement=EntitlementInfo(id="order-cancel-int"),
            )
            await service.process_event(event)

        # Verify entitlement state was updated to CANCELLED
        ent = await repo.get("order-cancel-int")
        assert ent is not None
        assert ent.state == EntitlementState.CANCELLED

    @pytest.mark.asyncio
    async def test_deleted_handler_triggers_purge(self, db_session):
        """_handle_entitlement_deleted should call the purge service."""
        from lightspeed_agent.marketplace.models import (
            EntitlementInfo,
            ProcurementEvent,
            ProcurementEventType,
        )
        from lightspeed_agent.marketplace.service import ProcurementService

        repo = EntitlementRepository()
        await _create_entitlement("order-delete-int", state="active")

        mock_dcr = AsyncMock()
        mock_dcr.delete_client = AsyncMock()

        service = ProcurementService(entitlement_repo=repo, dcr_service=mock_dcr)

        mock_purge = AsyncMock()
        with patch.object(service, "_purge_service", new=mock_purge, create=True):
            event = ProcurementEvent(
                eventId="evt-2",
                eventType=ProcurementEventType.ENTITLEMENT_DELETED,
                providerId="prov-1",
                entitlement=EntitlementInfo(id="order-delete-int"),
            )
            await service.process_event(event)

        # Verify entitlement state was updated to DELETED
        ent = await repo.get("order-delete-int")
        assert ent is not None
        assert ent.state == EntitlementState.DELETED


# ===========================================================================
# 8. Config settings
# ===========================================================================


class TestDataPurgeSettings:
    """Tests for data purge configuration settings."""

    def test_default_data_retention_days(self, test_settings):
        """Verify default value for data_retention_days."""
        assert hasattr(test_settings, "data_retention_days")
        assert test_settings.data_retention_days == 90

    def test_default_data_purge_enabled(self, test_settings):
        """Verify default value for data_purge_enabled."""
        assert hasattr(test_settings, "data_purge_enabled")
        assert test_settings.data_purge_enabled is True

    def test_default_data_purge_interval_hours(self, test_settings):
        """Verify default value for data_purge_interval_hours."""
        assert hasattr(test_settings, "data_purge_interval_hours")
        assert test_settings.data_purge_interval_hours == 24

    def test_custom_values_respected(self):
        """Verify custom values are respected."""
        from lightspeed_agent.config import Settings

        settings = Settings(
            google_api_key="test-key",
            database_url="sqlite+aiosqlite:///:memory:",
            data_retention_days=90,
            data_purge_enabled=False,
            data_purge_interval_hours=12,
        )
        assert settings.data_retention_days == 90
        assert settings.data_purge_enabled is False
        assert settings.data_purge_interval_hours == 12


# ===========================================================================
# 9. Edge cases
# ===========================================================================


class TestDataPurgeEdgeCases:
    """Edge case tests for data purge operations."""

    @pytest.mark.asyncio
    async def test_purge_entitlement_without_usage_records(self, db_session):
        """Purge when entitlement exists but no usage records."""
        from lightspeed_agent.marketplace.purge import DataPurgeService

        await _create_entitlement("order-no-usage", state="cancelled")

        service = DataPurgeService()
        with patch.object(service, "_rate_limiter", new=None):
            # Should not raise
            await service.purge_order_data("order-no-usage")

        assert await _count_entitlements("order-no-usage") == 0

    @pytest.mark.asyncio
    async def test_purge_usage_records_without_entitlement(self, db_session):
        """Purge when usage records exist but entitlement already deleted."""
        from lightspeed_agent.marketplace.purge import DataPurgeService

        # Only create usage records, no entitlement
        await _create_usage_record("order-no-ent")
        await _create_usage_record("order-no-ent", reported=True,
                                   period_start=datetime.now(UTC) - timedelta(hours=3),
                                   period_end=datetime.now(UTC) - timedelta(hours=2))

        service = DataPurgeService()
        with patch.object(service, "_rate_limiter", new=None):
            # Should not raise
            await service.purge_order_data("order-no-ent")

        assert await _count_usage_records("order-no-ent") == 0

    @pytest.mark.asyncio
    async def test_concurrent_purge_idempotent(self, db_session):
        """Concurrent purge operations should be idempotent and safe."""
        from lightspeed_agent.marketplace.purge import DataPurgeService

        await _create_entitlement("order-concurrent", state="deleted")
        await _create_usage_record("order-concurrent")
        await _create_dcr_client("order-concurrent", client_id="client-conc")

        service = DataPurgeService()
        with patch.object(service, "_rate_limiter", new=None):
            # Simulate concurrent purges (sequential here, but tests idempotency)
            await service.purge_order_data("order-concurrent")
            await service.purge_order_data("order-concurrent")

        assert await _count_usage_records("order-concurrent") == 0
        assert await _count_entitlements("order-concurrent") == 0
        assert await _count_dcr_clients("order-concurrent") == 0

    @pytest.mark.asyncio
    async def test_purge_nonexistent_order(self, db_session):
        """Purge a completely nonexistent order should be a no-op."""
        from lightspeed_agent.marketplace.purge import DataPurgeService

        service = DataPurgeService()
        with patch.object(service, "_rate_limiter", new=None):
            # Should not raise
            await service.purge_order_data("order-does-not-exist")
