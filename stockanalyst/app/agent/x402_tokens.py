from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PaymentToken:
    symbol: str
    address: str
    decimals: int
    domain_name: str
    domain_version: str
    transfer_method: str = "eip3009"

    @property
    def amount(self) -> int:
        return 10**self.decimals

    @property
    def domain(self) -> tuple[str, str]:
        return self.domain_name, self.domain_version


U_TOKEN = PaymentToken(
    symbol="U",
    address="0xcE24439F2D9C6a2289F741120FE202248B666666",
    decimals=18,
    domain_name="United Stables",
    domain_version="1",
)
USD1_TOKEN = PaymentToken(
    symbol="USD1",
    address="0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d",
    decimals=18,
    domain_name="World Liberty Financial USD",
    domain_version="1",
)
TOKENS: tuple[PaymentToken, ...] = (U_TOKEN, USD1_TOKEN)
_BY_ASSET = {token.address.lower(): token for token in TOKENS}


def supported_assets() -> list[dict[str, str | int]]:
    """Return stable, public token metadata without payment instructions."""
    return [
        {
            "symbol": token.symbol,
            "asset": token.address,
            "decimals": token.decimals,
        }
        for token in TOKENS
    ]


def token_by_asset(asset: object) -> PaymentToken | None:
    return _BY_ASSET.get(asset.lower()) if isinstance(asset, str) else None
