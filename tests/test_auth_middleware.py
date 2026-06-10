"""Tests for auth middleware order/client authorization checks."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lightspeed_agent.auth.middleware import AuthenticationMiddleware
from lightspeed_agent.auth.models import AuthenticatedUser
from lightspeed_agent.marketplace.models import Entitlement, EntitlementState


class TestAuthenticationMiddleware:
    """Tests for order/client authorization helper in middleware."""

    @pytest.fixture
    def middleware(self):
        """Create middleware with a no-op ASGI app."""
        return AuthenticationMiddleware(app=lambda scope, receive, send: None)

    @pytest.mark.asyncio
    async def test_resolve_and_validate_order_allows_active_mapped_client(self, middleware):
        """Allow when entitlement is active and mapped client matches."""
        entitlement = Entitlement(
            id="order-1",
            account_id="account-1",
            provider_id="provider-1",
            state=EntitlementState.ACTIVE,
        )
        entitlement_repo = MagicMock()
        entitlement_repo.get = AsyncMock(return_value=entitlement)
        dcr_repo = MagicMock()
        dcr_repo.get_by_client_id = AsyncMock(
            return_value=MagicMock(client_id="client-1", order_id="order-1")
        )

        with patch(
            "lightspeed_agent.marketplace.repository.get_entitlement_repository",
            return_value=entitlement_repo,
        ), patch(
            "lightspeed_agent.dcr.repository.get_dcr_client_repository",
            return_value=dcr_repo,
        ):
            result = await middleware._resolve_and_validate_order(
                client_id="client-1",
            )

        assert result == "order-1"

    @pytest.mark.asyncio
    async def test_resolve_and_validate_order_denies_inactive_entitlement(self, middleware):
        """Deny when entitlement is not active."""
        entitlement = Entitlement(
            id="order-2",
            account_id="account-1",
            provider_id="provider-1",
            state=EntitlementState.CANCELLED,
        )
        entitlement_repo = MagicMock()
        entitlement_repo.get = AsyncMock(return_value=entitlement)
        dcr_repo = MagicMock()
        dcr_repo.get_by_client_id = AsyncMock(
            return_value=MagicMock(client_id="client-2", order_id="order-2")
        )

        with patch(
            "lightspeed_agent.marketplace.repository.get_entitlement_repository",
            return_value=entitlement_repo,
        ), patch(
            "lightspeed_agent.dcr.repository.get_dcr_client_repository",
            return_value=dcr_repo,
        ):
            result = await middleware._resolve_and_validate_order(
                client_id="client-2",
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_and_validate_order_denies_no_dcr_client(self, middleware):
        """Deny when no DCR client is found for the client_id."""
        dcr_repo = MagicMock()
        dcr_repo.get_by_client_id = AsyncMock(return_value=None)

        with patch(
            "lightspeed_agent.marketplace.repository.get_entitlement_repository",
            return_value=MagicMock(),
        ), patch(
            "lightspeed_agent.dcr.repository.get_dcr_client_repository",
            return_value=dcr_repo,
        ):
            result = await middleware._resolve_and_validate_order(
                client_id="client-unknown",
            )

        assert result is None


class TestSkipOrderValidation:
    """Tests for skip_order_validation dispatch() behaviour."""

    @pytest.fixture
    def mock_user(self):
        """Create a mock authenticated user."""
        return AuthenticatedUser(
            user_id="test-user",
            client_id="test-client",
            username="tester",
            org_id="test-org",
            scopes=["openid", "api.console", "api.ocm"],
            token_exp=datetime(2099, 1, 1, tzinfo=UTC),
        )

    @staticmethod
    def _make_request(state=None):
        """Build a mock request with a proper headers mock."""
        request = MagicMock()
        request.url.path = "/"
        request.method = "POST"
        mock_headers = MagicMock()
        mock_headers.get = lambda key, default=None: (
            "Bearer fake-token" if key == "Authorization" else default
        )
        mock_headers.startswith = MagicMock(return_value=True)
        request.headers = mock_headers
        if state is not None:
            request.state = state
        return request

    @pytest.mark.asyncio
    async def test_skip_order_validation_does_not_call_resolve(self, mock_user):
        """When skip_order_validation=True, _resolve_and_validate_order is not called."""
        mock_settings = MagicMock()
        mock_settings.skip_jwt_validation = False
        mock_settings.skip_order_validation = True

        mock_introspector = MagicMock()
        mock_introspector.validate_token = AsyncMock(return_value=mock_user)

        async def fake_call_next(request):
            return MagicMock(status_code=200)

        with (
            patch(
                "lightspeed_agent.auth.middleware.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "lightspeed_agent.auth.middleware.get_token_introspector",
                return_value=mock_introspector,
            ),
        ):
            middleware = AuthenticationMiddleware(
                app=lambda scope, receive, send: None,
            )
            request = self._make_request()

            response = await middleware.dispatch(request, fake_call_next)

        # Token introspection still happens
        mock_introspector.validate_token.assert_called_once_with("fake-token")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_skip_order_validation_sets_order_id_none(self, mock_user):
        """When skip_order_validation=True, request.state.order_id is set to None."""
        mock_settings = MagicMock()
        mock_settings.skip_jwt_validation = False
        mock_settings.skip_order_validation = True

        mock_introspector = MagicMock()
        mock_introspector.validate_token = AsyncMock(return_value=mock_user)

        captured_state = MagicMock()

        async def fake_call_next(request):
            return MagicMock(status_code=200)

        with (
            patch(
                "lightspeed_agent.auth.middleware.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "lightspeed_agent.auth.middleware.get_token_introspector",
                return_value=mock_introspector,
            ),
        ):
            middleware = AuthenticationMiddleware(
                app=lambda scope, receive, send: None,
            )
            request = self._make_request(state=captured_state)

            await middleware.dispatch(request, fake_call_next)

        # order_id must be None when order validation is skipped
        assert captured_state.order_id is None
