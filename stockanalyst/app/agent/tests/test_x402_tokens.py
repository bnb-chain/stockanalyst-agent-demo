import unittest

from stockanalyst.app.agent.x402_tokens import (
    TOKENS,
    U_TOKEN,
    USD1_TOKEN,
    supported_assets,
    token_by_asset,
)


class PaymentTokenRegistryTests(unittest.TestCase):
    def test_registry_is_ordered_immutable_mainnet_metadata(self) -> None:
        self.assertEqual([token.symbol for token in TOKENS], ["U", "USD1"])
        self.assertEqual(
            U_TOKEN.address,
            "0xcE24439F2D9C6a2289F741120FE202248B666666",
        )
        self.assertEqual(
            USD1_TOKEN.address,
            "0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d",
        )
        self.assertEqual([token.decimals for token in TOKENS], [18, 18])
        self.assertEqual([token.amount for token in TOKENS], [10**18, 10**18])
        self.assertEqual(U_TOKEN.domain, ("United Stables", "1"))
        self.assertEqual(
            USD1_TOKEN.domain,
            ("World Liberty Financial USD", "1"),
        )

    def test_asset_lookup_is_case_insensitive_and_closed(self) -> None:
        self.assertIs(token_by_asset(U_TOKEN.address.lower()), U_TOKEN)
        self.assertIs(
            token_by_asset(USD1_TOKEN.address.upper().replace("0X", "0x")),
            USD1_TOKEN,
        )
        self.assertIsNone(token_by_asset("0x" + "11" * 20))
        self.assertIsNone(token_by_asset(None))

    def test_supported_assets_are_fresh_ordered_public_metadata(self) -> None:
        first = supported_assets()
        second = supported_assets()
        expected = [
            {
                "symbol": "U",
                "asset": "0xcE24439F2D9C6a2289F741120FE202248B666666",
                "decimals": 18,
            },
            {
                "symbol": "USD1",
                "asset": "0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d",
                "decimals": 18,
            },
        ]

        self.assertEqual(first, expected)
        self.assertEqual(
            [set(item) for item in first],
            [
                {"symbol", "asset", "decimals"},
                {"symbol", "asset", "decimals"},
            ],
        )
        self.assertIsNot(first, second)
        self.assertIsNot(first[0], second[0])
        first[0]["symbol"] = "changed"
        self.assertEqual(second, expected)
