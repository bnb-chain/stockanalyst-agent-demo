import unittest

from stockanalyst.app.agent.x402_tokens import (
    TOKENS,
    U_TOKEN,
    USD1_TOKEN,
    USDC_TOKEN,
    USDT_TOKEN,
    supported_assets,
    token_by_asset,
)


class PaymentTokenRegistryTests(unittest.TestCase):
    def test_four_token_registry_is_stable_and_method_aware(self) -> None:
        self.assertEqual(
            [token.symbol for token in TOKENS],
            ["U", "USD1", "USDC", "USDT"],
        )
        self.assertEqual(
            [token.address for token in TOKENS],
            [
                "0xcE24439F2D9C6a2289F741120FE202248B666666",
                "0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d",
                "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
                "0x55d398326f99059fF775485246999027B3197955",
            ],
        )
        self.assertEqual(
            [token.transfer_method for token in TOKENS],
            ["eip3009", "eip3009", "permit2-exact", "permit2-exact"],
        )
        self.assertEqual([token.decimals for token in TOKENS], [18] * 4)
        self.assertEqual([token.amount for token in TOKENS], [10**18] * 4)
        self.assertEqual(U_TOKEN.domain, ("United Stables", "1"))
        self.assertEqual(
            USD1_TOKEN.domain,
            ("World Liberty Financial USD", "1"),
        )
        self.assertEqual(USDC_TOKEN.domain, ("USD Coin", "1"))
        self.assertEqual(USDT_TOKEN.domain, ("Tether USD", "1"))

        import stockanalyst.app.agent.x402_tokens as registry

        self.assertFalse(hasattr(registry, "PROMOTIONAL_TOKENS"))

    def test_asset_lookup_is_case_insensitive_and_closed(self) -> None:
        self.assertIs(token_by_asset(U_TOKEN.address.lower()), U_TOKEN)
        self.assertIs(
            token_by_asset(USD1_TOKEN.address.upper().replace("0X", "0x")),
            USD1_TOKEN,
        )
        self.assertIsNone(token_by_asset("0x" + "11" * 20))
        self.assertIsNone(token_by_asset(None))

    def test_supported_assets_has_fresh_method_metadata(self) -> None:
        first = supported_assets()
        second = supported_assets()
        expected = [
            {
                "symbol": "U",
                "asset": "0xcE24439F2D9C6a2289F741120FE202248B666666",
                "decimals": 18,
                "transferMethod": "eip3009",
            },
            {
                "symbol": "USD1",
                "asset": "0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d",
                "decimals": 18,
                "transferMethod": "eip3009",
            },
            {
                "symbol": "USDC",
                "asset": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
                "decimals": 18,
                "transferMethod": "permit2-exact",
            },
            {
                "symbol": "USDT",
                "asset": "0x55d398326f99059fF775485246999027B3197955",
                "decimals": 18,
                "transferMethod": "permit2-exact",
            },
        ]

        self.assertEqual(first, expected)
        self.assertEqual(
            [set(item) for item in first],
            [
                {"symbol", "asset", "decimals", "transferMethod"},
                {"symbol", "asset", "decimals", "transferMethod"},
                {"symbol", "asset", "decimals", "transferMethod"},
                {"symbol", "asset", "decimals", "transferMethod"},
            ],
        )
        self.assertIsNot(first, second)
        self.assertIsNot(first[0], second[0])
        first[0]["symbol"] = "changed"
        self.assertEqual(second, expected)
