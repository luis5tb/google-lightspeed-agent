"""Google Cloud Marketplace Procurement integration module.

This module handles:
- Pub/Sub events from Google Cloud Marketplace (account/entitlement lifecycle)
- DCR requests from Gemini Enterprise (OAuth client registration)
- Order tracking for usage metering

The marketplace handler service is run via: python -m lightspeed_agent.marketplace

Note: ``create_app`` is intentionally NOT re-exported here to avoid a
circular import (marketplace.app → marketplace.router → dcr → dcr.service
→ marketplace).  Import it directly::

    from lightspeed_agent.marketplace.app import create_app
"""

from lightspeed_agent.marketplace.models import (
    Entitlement,
    EntitlementState,
    ProcurementEvent,
    ProcurementEventType,
)
from lightspeed_agent.marketplace.purge import (
    DataPurgeService,
    PurgeResult,
    get_data_purge_service,
)
from lightspeed_agent.marketplace.repository import (
    EntitlementRepository,
    get_entitlement_repository,
)
from lightspeed_agent.marketplace.service import (
    ProcurementService,
    get_procurement_service,
)

__all__ = [
    # Models
    "Entitlement",
    "EntitlementState",
    "ProcurementEvent",
    "ProcurementEventType",
    # Purge
    "DataPurgeService",
    "PurgeResult",
    "get_data_purge_service",
    # Repository
    "EntitlementRepository",
    "get_entitlement_repository",
    # Service
    "ProcurementService",
    "get_procurement_service",
]
