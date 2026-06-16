"""Schwab OAuth2 authorization-code flow."""

from __future__ import annotations

import base64
import json
import logging
import urllib.parse
import urllib.request

from schwab.auth.token_store import TokenBundle, TokenStore
from schwab.settings import SchwabSettings

logger = logging.getLogger(__name__)

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"


class OAuth2FlowManager:
    def __init__(
        self,
        settings: SchwabSettings | None = None,
        token_store: TokenStore | None = None,
        use_broker_app: bool = True,
    ):
        self.settings = settings or SchwabSettings.from_env()
        self.token_store = token_store or TokenStore(self.settings.token_path)
        if use_broker_app and self.settings.has_broker_credentials:
            self.app_key = self.settings.broker_app_key
            self.app_secret = self.settings.broker_app_secret
        else:
            self.app_key = self.settings.market_data_app_key
            self.app_secret = self.settings.market_data_app_secret

    def build_authorization_url(self, state: str = "momentum") -> str:
        params = urllib.parse.urlencode(
            {
                "client_id": self.app_key,
                "redirect_uri": self.settings.redirect_uri,
                "response_type": "code",
                "state": state,
            }
        )
        return f"{AUTH_URL}?{params}"

    def _basic_auth_header(self) -> str:
        raw = f"{self.app_key}:{self.app_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _token_request(self, form: dict) -> TokenBundle:
        data = urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(
            TOKEN_URL,
            data=data,
            headers={
                "Authorization": self._basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        bundle = TokenBundle.from_oauth_response(payload)
        self.token_store.save(bundle)
        return bundle

    def exchange_code(self, authorization_code: str) -> TokenBundle:
        """Exchange the redirect ?code= for access + refresh tokens."""
        return self._token_request(
            {
                "grant_type": "authorization_code",
                "code": urllib.parse.unquote(authorization_code),
                "redirect_uri": self.settings.redirect_uri,
            }
        )

    def refresh(self, refresh_token: str) -> TokenBundle:
        return self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
