"""Token persistence for Schwab OAuth2 tokens.

SECURITY: token files contain live credentials. They are written 0600 and
data/schwab_tokens.json is git-ignored. Never commit token files.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class TokenBundle:
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0  # unix epoch seconds
    token_type: str = "Bearer"
    scope: str = ""

    @property
    def is_expired(self) -> bool:
        return not self.access_token or time.time() >= self.expires_at - 60

    @classmethod
    def from_oauth_response(cls, payload: dict) -> "TokenBundle":
        return cls(
            access_token=payload.get("access_token", ""),
            refresh_token=payload.get("refresh_token", ""),
            expires_at=time.time() + float(payload.get("expires_in", 1800)),
            token_type=payload.get("token_type", "Bearer"),
            scope=payload.get("scope", ""),
        )


class TokenStore:
    def __init__(self, path: str | Path = "data/schwab_tokens.json"):
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> TokenBundle | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        return TokenBundle(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            expires_at=float(data.get("expires_at", 0)),
            token_type=data.get("token_type", "Bearer"),
            scope=data.get("scope", ""),
        )

    def save(self, bundle: TokenBundle) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(bundle), indent=2))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()
