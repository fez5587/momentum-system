from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, cast


TradingMode = Literal["schwab_live", "alpaca_paper"]


@dataclass(frozen=True)
class TradingModeSettings:
    execution_mode: TradingMode = "alpaca_paper"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "TradingModeSettings":
        values = dict(os.environ)
        if env is not None:
            values.update(env)
        mode = values.get("TRADING_EXECUTION_MODE", "alpaca_paper")
        if mode not in {"schwab_live", "alpaca_paper"}:
            mode = "alpaca_paper"
        return cls(execution_mode=cast(TradingMode, mode))
