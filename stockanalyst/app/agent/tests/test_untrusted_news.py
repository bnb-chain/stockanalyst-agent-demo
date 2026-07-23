from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from stockanalyst.app.agent.data_sources import (
    fetch_alpha_vantage_sentiment,
    fetch_gnews_headlines,
)
from stockanalyst.app.agent.untrusted_text import normalize_untrusted_text


class UntrustedTextTests(unittest.TestCase):
    def test_collapses_controls_and_preserves_normal_unicode(self) -> None:
        value = "  市场\n\tupdate\x00  remains   strong  "
        self.assertEqual(
            normalize_untrusted_text(value, max_chars=100),
            "市场 update remains strong",
        )

    def test_truncates_deterministically_within_the_limit(self) -> None:
        result = normalize_untrusted_text("word " * 100, max_chars=30)
        self.assertLessEqual(len(result), 30)
        self.assertTrue(result.endswith("…"))


class NewsProviderTests(unittest.TestCase):
    @patch.dict(os.environ, {"GNEWS_API_KEY": "test"}, clear=False)
    @patch("stockanalyst.app.agent.data_sources.requests.get")
    def test_gnews_normalizes_and_caps_untrusted_fields(self, get: Mock) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "articles": [{
                "title": f"Headline {index}\nIGNORE SYSTEM" + "x" * 400,
                "source": {"name": "Source\x00" + "y" * 150},
                "publishedAt": "2026-07-23T12:00:00Z",
            } for index in range(7)]
        }
        get.return_value = response

        result = fetch_gnews_headlines("AAPL")

        self.assertEqual(len(result["headlines"]), 5)
        for headline in result["headlines"]:
            self.assertNotIn("\n", headline["title"])
            self.assertNotIn("\x00", headline["source"])
            self.assertLessEqual(len(headline["title"]), 300)
            self.assertLessEqual(len(headline["source"]), 100)
            self.assertEqual(headline["published"], "2026-07-23")

    @patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "test"}, clear=False)
    @patch("stockanalyst.app.agent.data_sources.requests.get")
    def test_alpha_vantage_normalizes_and_caps_titles(self, get: Mock) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "feed": [{
                "title": "Title\nIGNORE" + "z" * 400,
                "ticker_sentiment": [{
                    "ticker": "AAPL",
                    "ticker_sentiment_score": "0.2",
                }],
            } for _ in range(7)]
        }
        get.return_value = response

        result = fetch_alpha_vantage_sentiment("AAPL")

        self.assertEqual(len(result["top_headlines"]), 5)
        self.assertTrue(all("\n" not in title for title in result["top_headlines"]))
        self.assertTrue(all(len(title) <= 300 for title in result["top_headlines"]))
