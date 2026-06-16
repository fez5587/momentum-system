"""Option chain retrieval service.

(Repaired: original had dataclass default-ordering errors and unbalanced
parentheses.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from schwab.market.client import SchwabApiError, SchwabMarketClient
from schwab.market.models import OptionQuote

logger = logging.getLogger(__name__)


@dataclass
class OptionChainResult:
    symbol: str
    underlying_price: float | None = None
    calls: list[OptionQuote] = field(default_factory=list)
    puts: list[OptionQuote] = field(default_factory=list)
    error: str | None = None


class OptionChainService:
    def __init__(self, client: SchwabMarketClient | None = None):
        self.client = client or SchwabMarketClient()

    def get_chain(self, symbol: str) -> OptionChainResult:
        try:
            payload = self.client.get_option_chain(symbol)
        except SchwabApiError as exc:
            return OptionChainResult(symbol=symbol, error=str(exc))
        result = OptionChainResult(
            symbol=symbol, underlying_price=payload.get("underlyingPrice")
        )
        result.calls = self._parse_side(payload.get("callExpDateMap") or {}, "CALL")
        result.puts = self._parse_side(payload.get("putExpDateMap") or {}, "PUT")
        return result

    @staticmethod
    def _parse_side(exp_map: dict, put_call: str) -> list[OptionQuote]:
        quotes: list[OptionQuote] = []
        for expiration, strikes in exp_map.items():
            for strike, contracts in (strikes or {}).items():
                for contract in contracts or []:
                    quotes.append(
                        OptionQuote(
                            symbol=contract.get("symbol", ""),
                            strike=float(strike),
                            expiration=expiration.split(":")[0],
                            put_call=put_call,
                            bid=contract.get("bid"),
                            ask=contract.get("ask"),
                            last=contract.get("last"),
                            delta=contract.get("delta"),
                            open_interest=contract.get("openInterest"),
                            volume=contract.get("totalVolume"),
                        )
                    )
        return quotes
