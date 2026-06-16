"""Token lifecycle: load, refresh when stale, surface auth status."""

from __future__ import annotations

import logging

from schwab.auth.oauth2_flow_manager import OAuth2FlowManager
from schwab.auth.token_store import TokenBundle, TokenStore
from schwab.settings import SchwabSettings

logger = logging.getLogger(__name__)


class TokenLifecycle:
    def __init__(
        self,
        settings: SchwabSettings | None = None,
        token_store: TokenStore | None = None,
    ):
        self.settings = settings or SchwabSettings.from_env()
        self.token_store = token_store or TokenStore(self.settings.token_path)
        self.flow = OAuth2FlowManager(self.settings, self.token_store)

    def status(self) -> dict:
        bundle = self.token_store.load()
        if bundle is None:
            return {"authenticated": False, "reason": "no token file"}
        if bundle.is_expired and not bundle.refresh_token:
            return {"authenticated": False, "reason": "token expired, no refresh token"}
        return {
            "authenticated": True,
            "expired": bundle.is_expired,
            "expires_at": bundle.expires_at,
        }

    def get_access_token(self) -> str | None:
        """Return a valid access token, refreshing if needed. None if unauth."""
        bundle = self.token_store.load()
        if bundle is None:
            return None
        if not bundle.is_expired:
            return bundle.access_token
        if not bundle.refresh_token:
            return None
        try:
            refreshed: TokenBundle = self.flow.refresh(bundle.refresh_token)
            return refreshed.access_token
        except Exception:
            logger.exception("schwab token refresh failed")
            return None
