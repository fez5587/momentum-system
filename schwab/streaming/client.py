"""Schwab streaming client scaffold.

(Repaired: original had dataclass default-ordering errors and unbalanced
parentheses.) Real-time streaming requires the streamer-info handshake from
user preferences; this scaffold builds the login payload and exposes a
callback interface, while the live system polls REST minute bars instead.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class StreamerInfo:
    streamer_socket_url: str = ""
    schwab_client_customer_id: str = ""
    schwab_client_correl_id: str = ""
    schwab_client_channel: str = "N9"
    schwab_client_function_id: str = "APIAPP"

    @classmethod
    def from_user_preferences(cls, payload: dict) -> "StreamerInfo":
        info = (payload.get("streamerInfo") or [{}])[0]
        return cls(
            streamer_socket_url=info.get("streamerSocketUrl", ""),
            schwab_client_customer_id=info.get("schwabClientCustomerId", ""),
            schwab_client_correl_id=info.get("schwabClientCorrelId", ""),
            schwab_client_channel=info.get("schwabClientChannel", "N9"),
            schwab_client_function_id=info.get("schwabClientFunctionId", "APIAPP"),
        )


@dataclass
class StreamingClient:
    streamer_info: StreamerInfo
    access_token: str
    request_id: int = 0
    on_message: Callable[[dict], None] | None = None
    subscriptions: list[dict] = field(default_factory=list)

    def _next_request_id(self) -> int:
        self.request_id += 1
        return self.request_id

    def build_login_request(self) -> dict:
        return {
            "service": "ADMIN",
            "command": "LOGIN",
            "requestid": self._next_request_id(),
            "SchwabClientCustomerId": self.streamer_info.schwab_client_customer_id,
            "SchwabClientCorrelId": self.streamer_info.schwab_client_correl_id,
            "parameters": {
                "Authorization": self.access_token,
                "SchwabClientChannel": self.streamer_info.schwab_client_channel,
                "SchwabClientFunctionId": self.streamer_info.schwab_client_function_id,
            },
        }

    def build_quote_subscription(self, symbols: list[str]) -> dict:
        request = {
            "service": "LEVELONE_EQUITIES",
            "command": "SUBS",
            "requestid": self._next_request_id(),
            "SchwabClientCustomerId": self.streamer_info.schwab_client_customer_id,
            "SchwabClientCorrelId": self.streamer_info.schwab_client_correl_id,
            "parameters": {
                "keys": ",".join(symbols),
                "fields": "0,1,2,3,4,5,8",
            },
        }
        self.subscriptions.append(request)
        return request

    def handle_raw_message(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("unparseable streaming message: %.120s", raw)
            return
        if self.on_message:
            self.on_message(payload)
