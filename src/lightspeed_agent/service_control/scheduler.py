"""Scheduler for periodic usage reporting."""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from lightspeed_agent.service_control.reporter import UsageReporter, get_usage_reporter

logger = logging.getLogger(__name__)


class ReportingScheduler:
    """Scheduler for periodic usage reporting to Google Cloud.

    This scheduler:
    - Runs hourly usage reports (Google's minimum requirement)
    - Retries failed reports periodically
    - Provides status and health information
    """

    def __init__(
        self,
        reporter: UsageReporter | None = None,
        hourly_interval_seconds: int = 3600,
        retry_interval_seconds: int = 300,
        purge_interval_seconds: int | None = None,
    ) -> None:
        """Initialize the scheduler.

        Args:
            reporter: Usage reporter instance.
            hourly_interval_seconds: Interval for hourly reports (default: 3600).
            retry_interval_seconds: Interval for retry attempts (default: 300).
            purge_interval_seconds: Interval for data purge runs (default: from settings).
        """
        self._reporter = reporter or get_usage_reporter()
        self._hourly_interval = hourly_interval_seconds
        self._retry_interval = retry_interval_seconds
        self._purge_interval = purge_interval_seconds

        # Task handles
        self._hourly_task: asyncio.Task[None] | None = None
        self._retry_task: asyncio.Task[None] | None = None
        self._purge_task: asyncio.Task[None] | None = None

        # State
        self._running = False
        self._last_hourly_run: datetime | None = None
        self._last_retry_run: datetime | None = None
        self._last_purge_run: datetime | None = None
        self._hourly_run_count = 0
        self._retry_run_count = 0
        self._purge_run_count = 0

        # Callbacks for alerting
        self._on_report_failure: Callable[[str, str], None] | None = None

    def set_failure_callback(self, callback: Callable[[str, str], None]) -> None:
        """Set callback for report failures.

        Args:
            callback: Function taking (order_id, error_message).
        """
        self._on_report_failure = callback

    async def _run_hourly_reports(self) -> None:
        """Run hourly reports in a loop."""
        # Wait until the next hour boundary
        now = datetime.now(UTC)
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        initial_delay = (next_hour - now).total_seconds()

        logger.info(
            "Scheduler: First hourly report in %.1f seconds at %s",
            initial_delay,
            next_hour.isoformat(),
        )
        await asyncio.sleep(initial_delay)

        while self._running:
            try:
                logger.info("Scheduler: Starting hourly usage report")
                self._last_hourly_run = datetime.now(UTC)
                self._hourly_run_count += 1

                results = await self._reporter.run_hourly_cycle()

                # Check for failures and alert
                for result in results:
                    if not result.success and self._on_report_failure:
                        self._on_report_failure(
                            result.order_id,
                            result.error_message or "Unknown error",
                        )

                logger.info(
                    "Scheduler: Hourly report complete. Success: %d, Failed: %d",
                    sum(1 for r in results if r.success),
                    sum(1 for r in results if not r.success),
                )

            except Exception as e:
                logger.exception("Scheduler: Hourly report failed: %s", e)

            # Wait for next run
            await asyncio.sleep(self._hourly_interval)

    async def _run_retry_loop(self) -> None:
        """Run retry loop for failed reports."""
        while self._running:
            try:
                failed_count = self._reporter.get_failed_reports_count()
                if failed_count > 0:
                    logger.info(
                        "Scheduler: Retrying %d failed reports",
                        failed_count,
                    )
                    self._last_retry_run = datetime.now(UTC)
                    self._retry_run_count += 1

                    results = await self._reporter.retry_failed_reports()

                    # Alert on continued failures
                    for result in results:
                        if not result.success and self._on_report_failure:
                            self._on_report_failure(
                                result.order_id,
                                f"Retry failed: {result.error_message}",
                            )

            except Exception as e:
                logger.exception("Scheduler: Retry loop failed: %s", e)

            await asyncio.sleep(self._retry_interval)

    async def _run_data_purge(self) -> None:
        """Periodically purge expired cancelled/deleted entitlement data."""
        from lightspeed_agent.config import get_settings
        from lightspeed_agent.marketplace.purge import get_data_purge_service

        settings = get_settings()
        interval = self._purge_interval or (settings.data_purge_interval_hours * 3600)

        # Initial delay: stagger 5 minutes after startup to avoid thundering herd
        await asyncio.sleep(300)

        while self._running:
            try:
                logger.info("Scheduler: Starting data purge run")
                self._last_purge_run = datetime.now(UTC)
                self._purge_run_count += 1

                purge_service = get_data_purge_service()
                results = await purge_service.purge_expired_data(settings.data_retention_days)

                total_usage = sum(r.usage_records_deleted for r in results)
                total_entitlements = sum(1 for r in results if r.entitlement_deleted)
                total_errors = sum(r.error_count for r in results)

                logger.info(
                    "Scheduler: Data purge complete. "
                    "Orders=%d, usage_records=%d, entitlements=%d, errors=%d",
                    len(results),
                    total_usage,
                    total_entitlements,
                    total_errors,
                )
            except Exception as e:
                logger.exception("Scheduler: Data purge failed: %s", e)

            await asyncio.sleep(interval)

    async def start(self) -> None:
        """Start the scheduler.

        This starts the background tasks for:
        - Hourly usage reporting
        - Retry of failed reports
        - Data purge (if enabled)
        """
        if self._running:
            logger.warning("Scheduler already running")
            return

        logger.info("Starting usage reporting scheduler")
        self._running = True

        # Start background tasks
        self._hourly_task = asyncio.create_task(
            self._run_hourly_reports(),
            name="hourly_usage_reports",
        )
        self._retry_task = asyncio.create_task(
            self._run_retry_loop(),
            name="retry_failed_reports",
        )

        # Start data purge task if enabled
        from lightspeed_agent.config import get_settings

        settings = get_settings()
        if settings.data_purge_enabled:
            self._purge_task = asyncio.create_task(
                self._run_data_purge(),
                name="data_purge",
            )

        logger.info(
            "Scheduler started with hourly_interval=%ds, retry_interval=%ds, purge_enabled=%s",
            self._hourly_interval,
            self._retry_interval,
            settings.data_purge_enabled,
        )

    async def stop(self) -> None:
        """Stop the scheduler gracefully."""
        if not self._running:
            return

        logger.info("Stopping usage reporting scheduler")
        self._running = False

        # Cancel tasks
        if self._hourly_task:
            self._hourly_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._hourly_task

        if self._retry_task:
            self._retry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._retry_task

        if self._purge_task:
            self._purge_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._purge_task

        logger.info("Scheduler stopped")

    async def run_immediate_report(self) -> None:
        """Run an immediate hourly report (for testing/debugging)."""
        logger.info("Running immediate usage report")
        await self._reporter.run_hourly_cycle()

    def get_status(self) -> dict[str, Any]:
        """Get scheduler status.

        Returns:
            Status dictionary.
        """
        reporter_stats = self._reporter.get_reporting_stats()

        return {
            "running": self._running,
            "hourly_interval_seconds": self._hourly_interval,
            "retry_interval_seconds": self._retry_interval,
            "last_hourly_run": (
                self._last_hourly_run.isoformat() if self._last_hourly_run else None
            ),
            "last_retry_run": (self._last_retry_run.isoformat() if self._last_retry_run else None),
            "last_purge_run": (self._last_purge_run.isoformat() if self._last_purge_run else None),
            "hourly_run_count": self._hourly_run_count,
            "retry_run_count": self._retry_run_count,
            "purge_run_count": self._purge_run_count,
            **reporter_stats,
        }

    @property
    def is_running(self) -> bool:
        """Check if scheduler is running."""
        return self._running


# Global scheduler instance
_reporting_scheduler: ReportingScheduler | None = None


def get_reporting_scheduler() -> ReportingScheduler:
    """Get the global reporting scheduler instance.

    Returns:
        ReportingScheduler instance.
    """
    global _reporting_scheduler
    if _reporting_scheduler is None:
        _reporting_scheduler = ReportingScheduler()
    return _reporting_scheduler


async def start_reporting_scheduler() -> ReportingScheduler:
    """Start the global reporting scheduler.

    Returns:
        The started scheduler.
    """
    scheduler = get_reporting_scheduler()
    await scheduler.start()
    return scheduler


async def stop_reporting_scheduler() -> None:
    """Stop the global reporting scheduler."""
    global _reporting_scheduler
    if _reporting_scheduler:
        await _reporting_scheduler.stop()
