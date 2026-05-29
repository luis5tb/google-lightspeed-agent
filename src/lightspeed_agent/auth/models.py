"""Pydantic models for authentication and authorization."""

from datetime import datetime

from pydantic import BaseModel, Field


class JWTClaims(BaseModel):
    """JWT token claims."""

    iss: str = Field(..., description="Issuer")
    sub: str = Field(..., description="Subject (user ID)")
    aud: str | list[str] = Field(..., description="Audience")
    exp: int = Field(..., description="Expiration time (Unix timestamp)")
    iat: int = Field(..., description="Issued at time (Unix timestamp)")
    jti: str | None = Field(default=None, description="JWT ID")
    azp: str | None = Field(default=None, description="Authorized party (client_id)")
    scope: str | None = Field(default=None, description="Token scope")
    preferred_username: str | None = Field(default=None, description="Preferred username")
    email: str | None = Field(default=None, description="Email address")
    name: str | None = Field(default=None, description="Full name")
    org_id: str | None = Field(default=None, description="Organization ID")


class AuthenticatedUser(BaseModel):
    """Authenticated user information extracted from JWT."""

    user_id: str = Field(..., description="User ID (sub claim)")
    client_id: str = Field(..., description="Client ID (azp or aud claim)")
    username: str | None = Field(default=None, description="Username")
    email: str | None = Field(default=None, description="Email")
    name: str | None = Field(default=None, description="Full name")
    org_id: str | None = Field(default=None, description="Organization ID")
    scopes: list[str] = Field(default_factory=list, description="Granted scopes")
    token_exp: datetime = Field(..., description="Token expiration time")
    # Metadata for additional claims (order_id, etc.)
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description="Additional metadata from token claims",
    )
    # Raw access token for forwarding to downstream services (e.g., MCP)
    access_token: str | None = Field(
        default=None,
        description="Raw access token for forwarding to downstream services",
        exclude=True,  # Exclude from serialization for security
    )
