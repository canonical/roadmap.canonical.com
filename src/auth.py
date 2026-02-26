"""OIDC authentication helpers for the roadmap app.

Uses Authlib to integrate with an OpenID Connect provider (Hydra-based IAM).
Session state is stored in a signed cookie (via Starlette's SessionMiddleware).

Only needs four settings: client_id, client_secret, issuer (for OIDC discovery),
and redirect_uri.  Works identically in local dev (.env) and charm deployment
(juju config → APP_OIDC_* env vars).
"""

from __future__ import annotations

import logging

from authlib.integrations.starlette_client import OAuth
from fastapi import Request
from starlette.responses import RedirectResponse

from .settings import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Authlib OAuth registry
# ---------------------------------------------------------------------------
oauth = OAuth()


def configure_oauth() -> None:
    """Register the OIDC provider.  Must be called once at startup."""
    oauth.register(
        name="oidc",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        server_metadata_url=f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_authenticated(request: Request) -> bool:
    """Return ``True`` when the session contains a valid user dict."""
    return "user" in request.session


def get_user(request: Request) -> dict | None:
    """Return the stored user-info dict, or ``None``."""
    return request.session.get("user")


async def login_redirect(request: Request) -> RedirectResponse:
    """Redirect the browser to the OIDC authorization endpoint."""
    return await oauth.oidc.authorize_redirect(request, settings.oidc_redirect_uri)


async def handle_callback(request: Request) -> RedirectResponse:
    """Exchange the authorization code for tokens, store user info in session."""
    token = await oauth.oidc.authorize_access_token(request)

    # Prefer the id_token claims; fall back to userinfo endpoint.
    userinfo = token.get("userinfo")
    if not userinfo:
        userinfo = await oauth.oidc.userinfo(token=token)

    request.session["user"] = dict(userinfo)
    logger.info("OIDC login: %s", userinfo.get("email") or userinfo.get("sub"))
    return RedirectResponse(url="/")
