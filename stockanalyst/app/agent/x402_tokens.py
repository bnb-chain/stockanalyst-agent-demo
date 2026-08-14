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
USDC_TOKEN = PaymentToken(
    symbol="USDC",
    address="0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
    decimals=18,
    domain_name="USD Coin",
    domain_version="2",
    transfer_method="permit2-exact",
)
USDT_TOKEN = PaymentToken(
    symbol="USDT",
    address="0x55d398326f99059fF775485246999027B3197955",
    decimals=18,
    domain_name="Tether USD",
    domain_version="1",
    transfer_method="permit2-exact",
)
TOKENS: tuple[PaymentToken, ...] = (
    U_TOKEN,
    USD1_TOKEN,
    USDC_TOKEN,
    USDT_TOKEN,
)
_BY_ASSET = {token.address.lower(): token for token in TOKENS}


def supported_assets() -> list[dict[str, str | int]]:
    """Return stable, public token metadata without payment instructions."""
    return [
        {
            "symbol": token.symbol,
            "asset": token.address,
            "decimals": token.decimals,
            "transferMethod": token.transfer_method,
        }
        for token in TOKENS
    ]


def token_by_asset(asset: object) -> PaymentToken | None:
    return _BY_ASSET.get(asset.lower()) if isinstance(asset, str) else None
