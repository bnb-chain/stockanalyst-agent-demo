from __future__ import annotations

import ast
import json
import math
import os
import traceback
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from stockanalyst.app.agent.data_sources import (
    _ProviderResponseError,
    _read_provider_json,
    fetch_alpha_vantage_sentiment,
    fetch_gnews_headlines,
)
from stockanalyst.app.agent.untrusted_text import normalize_untrusted_text


class _StreamingResponse:
    def __init__(
        self,
        payload: object,
        *,
        status_error: Exception | None = None,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
        iter_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self._payload = payload
        self._status_error = status_error
        self.headers = headers or {}
        self._chunks = chunks
        self._iter_error = iter_error
        self._close_error = close_error
        self.iter_calls = 0
        self.closed = False

    def raise_for_status(self) -> None:
        if self._status_error is not None:
            raise self._status_error

    def json(self) -> object:
        raise AssertionError("provider code must not call unbounded response.json()")

    def iter_content(self, chunk_size: int = 64 * 1024):
        del chunk_size
        self.iter_calls += 1
        if self._iter_error is not None:
            raise self._iter_error
        if self._chunks is not None:
            yield from self._chunks
            return
        yield json.dumps(self._payload).encode("utf-8")

    def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


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
        response = _StreamingResponse({
            "articles": [{
                "title": f"Headline {index}\nIGNORE SYSTEM" + "x" * 400,
                "source": {"name": "Source\x00" + "y" * 150},
                "publishedAt": "2026-07-23T12:00:00Z",
            } for index in range(7)]
        })
        get.return_value = response

        result = fetch_gnews_headlines("AAPL")

        self.assertEqual(len(result["headlines"]), 5)
        self.assertEqual(result["total_results"], 5)
        for headline in result["headlines"]:
            self.assertNotIn("\n", headline["title"])
            self.assertNotIn("\x00", headline["source"])
            self.assertLessEqual(len(headline["title"]), 300)
            self.assertLessEqual(len(headline["source"]), 100)
            self.assertEqual(headline["published"], "2026-07-23")
        self.assertEqual(get.call_args.kwargs["timeout"], 10)
        self.assertIs(get.call_args.kwargs["stream"], True)
        self.assertTrue(response.closed)

    @patch.dict(os.environ, {"GNEWS_API_KEY": "test"}, clear=False)
    @patch("stockanalyst.app.agent.data_sources.requests.get")
    def test_gnews_abnormal_article_fields_become_empty_text(
        self,
        get: Mock,
    ) -> None:
        get.return_value = _StreamingResponse({
            "articles": [
                {
                    "title": {"secret": "NON_STRING_TITLE"},
                    "source": {"name": ["NON_STRING_SOURCE"]},
                    "publishedAt": {"secret": "NON_STRING_DATE"},
                },
                {
                    "title": 42,
                    "source": ["not", "an", "object"],
                    "publishedAt": None,
                },
                "not an article object",
            ],
        })

        result = fetch_gnews_headlines("AAPL")

        empty = {"title": "", "source": "", "published": ""}
        self.assertEqual(result["headlines"], [empty, empty, empty])
        self.assertEqual(result["total_results"], 3)
        self.assertNotIn("NON_STRING", str(result))

    @patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "test"}, clear=False)
    @patch("stockanalyst.app.agent.data_sources.requests.get")
    def test_alpha_vantage_normalizes_and_caps_titles(self, get: Mock) -> None:
        response = _StreamingResponse({
            "feed": [{
                "title": "Title\nIGNORE" + "z" * 400,
                "ticker_sentiment": [{
                    "ticker": "AAPL",
                    "ticker_sentiment_score": "0.2",
                }],
            } for _ in range(7)]
        })
        get.return_value = response

        result = fetch_alpha_vantage_sentiment("AAPL")

        self.assertEqual(len(result["top_headlines"]), 5)
        self.assertTrue(all("\n" not in title for title in result["top_headlines"]))
        self.assertTrue(all(len(title) <= 300 for title in result["top_headlines"]))
        self.assertEqual(get.call_args.kwargs["timeout"], 10)
        self.assertIs(get.call_args.kwargs["stream"], True)
        self.assertTrue(response.closed)

    def test_http_errors_use_stable_codes_without_keys_in_logs_or_results(self) -> None:
        cases = (
            (
                "ALPHA_VANTAGE_API_KEY",
                "ALPHA_SECRET_401",
                fetch_alpha_vantage_sentiment,
                "Alpha Vantage",
                401,
            ),
            (
                "ALPHA_VANTAGE_API_KEY",
                "ALPHA_SECRET_429",
                fetch_alpha_vantage_sentiment,
                "Alpha Vantage",
                429,
            ),
            (
                "GNEWS_API_KEY",
                "GNEWS_SECRET_401",
                fetch_gnews_headlines,
                "GNews",
                401,
            ),
            (
                "GNEWS_API_KEY",
                "GNEWS_SECRET_429",
                fetch_gnews_headlines,
                "GNews",
                429,
            ),
        )
        for env_name, secret, fetch, log_name, status in cases:
            error = requests.HTTPError(
                f"{status} Client Error for url: https://provider.invalid/?token={secret}"
            )
            response = _StreamingResponse({}, status_error=error)
            with (
                self.subTest(provider=log_name),
                patch.dict(os.environ, {env_name: secret}, clear=False),
                patch(
                    "stockanalyst.app.agent.data_sources.requests.get",
                    return_value=response,
                ),
                self.assertLogs("seller-agent.data_sources", level="WARNING") as captured,
            ):
                result = fetch("AAPL")

            self.assertEqual(result["error"], "provider_http_error")
            self.assertNotIn(secret, str(result))
            self.assertNotIn(secret, "\n".join(captured.output))
            self.assertNotIn("provider.invalid", "\n".join(captured.output))
            self.assertTrue(response.closed)

    @patch.dict(
        os.environ,
        {"ALPHA_VANTAGE_API_KEY": "ALPHA_TOOL_SECRET", "GNEWS_API_KEY": ""},
        clear=False,
    )
    @patch("stockanalyst.app.agent.data_sources.requests.get")
    def test_alpha_http_error_is_redacted_in_tool_result(self, get: Mock) -> None:
        get.return_value = _StreamingResponse(
            {},
            status_error=requests.HTTPError(
                "401 for https://alphavantage.invalid/?apikey=ALPHA_TOOL_SECRET"
            ),
        )
        tools_path = Path(__file__).parents[1] / "tools.py"
        tools_tree = ast.parse(tools_path.read_text(encoding="utf-8"))
        function = next(
            node
            for node in tools_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "get_news_sentiment"
        )
        namespace = {
            "fetch_alpha_vantage_sentiment": fetch_alpha_vantage_sentiment,
            "fetch_gnews_headlines": lambda symbol: {"headlines": []},
        }
        exec(  # noqa: S102 — isolate get_news_sentiment without importing tools module
            compile(
                ast.fix_missing_locations(
                    ast.Module(body=[function], type_ignores=[]),
                ),
                str(tools_path),
                "exec",
            ),
            namespace,
        )
        get_news_sentiment = namespace["get_news_sentiment"]

        with self.assertLogs("seller-agent.data_sources", level="WARNING") as captured:
            result = get_news_sentiment("AAPL")

        self.assertEqual(
            result["alpha_vantage_sentiment"]["error"],
            "provider_http_error",
        )
        self.assertNotIn("ALPHA_TOOL_SECRET", str(result))
        self.assertNotIn("ALPHA_TOOL_SECRET", "\n".join(captured.output))

    def test_provider_body_caps_apply_before_json_parsing(self) -> None:
        prefix = b'{"feed":[],"padding":"'
        suffix = b'"}'
        exact_body = (
            prefix
            + b"x" * (1_048_576 - len(prefix) - len(suffix))
            + suffix
        )
        exact = _StreamingResponse(
            {},
            headers={"Content-Length": str(1_048_576)},
            chunks=[exact_body],
        )
        with (
            patch.dict(
                os.environ,
                {"ALPHA_VANTAGE_API_KEY": "test"},
                clear=False,
            ),
            patch(
                "stockanalyst.app.agent.data_sources.requests.get",
                return_value=exact,
            ),
        ):
            accepted = fetch_alpha_vantage_sentiment("AAPL")
        self.assertEqual(accepted["article_count"], 0)
        self.assertEqual(exact.iter_calls, 1)
        self.assertTrue(exact.closed)

        declared = _StreamingResponse(
            {"feed": []},
            headers={"Content-Length": str(1_048_577)},
        )
        incremental = _StreamingResponse(
            {"feed": []},
            headers={"Content-Length": "1"},
            chunks=[b"{" + b"x" * 1_048_576, b"}"],
        )
        for response in (declared, incremental):
            with (
                self.subTest(headers=response.headers),
                patch.dict(
                    os.environ,
                    {"ALPHA_VANTAGE_API_KEY": "test"},
                    clear=False,
                ),
                patch(
                    "stockanalyst.app.agent.data_sources.requests.get",
                    return_value=response,
                ),
                self.assertLogs(
                    "seller-agent.data_sources",
                    level="WARNING",
                ),
            ):
                result = fetch_alpha_vantage_sentiment("AAPL")
            self.assertEqual(result["error"], "provider_response_too_large")
            self.assertTrue(response.closed)
        self.assertEqual(declared.iter_calls, 0)
        self.assertEqual(incremental.iter_calls, 1)

    def test_malformed_content_length_is_rejected_without_reading_body(
        self,
    ) -> None:
        for value in ("not-a-number", "-1"):
            response = _StreamingResponse(
                {"feed": []},
                headers={"Content-Length": value},
            )
            with (
                self.subTest(content_length=value),
                patch.dict(
                    os.environ,
                    {"ALPHA_VANTAGE_API_KEY": "test"},
                    clear=False,
                ),
                patch(
                    "stockanalyst.app.agent.data_sources.requests.get",
                    return_value=response,
                ),
                self.assertLogs(
                    "seller-agent.data_sources",
                    level="WARNING",
                ),
            ):
                result = fetch_alpha_vantage_sentiment("AAPL")
            self.assertEqual(result["error"], "provider_invalid_response")
            self.assertEqual(response.iter_calls, 0)
            self.assertTrue(response.closed)

    def test_memory_error_is_not_swallowed(self) -> None:
        response = _StreamingResponse(
            {},
            iter_error=MemoryError("exhausted"),
        )
        with (
            patch.dict(
                os.environ,
                {"ALPHA_VANTAGE_API_KEY": "test"},
                clear=False,
            ),
            patch(
                "stockanalyst.app.agent.data_sources.requests.get",
                return_value=response,
            ),
            self.assertRaises(MemoryError),
        ):
            fetch_alpha_vantage_sentiment("AAPL")
        self.assertTrue(response.closed)

    def test_deeply_nested_provider_json_uses_stable_invalid_code(self) -> None:
        body = (
            b'{"nested":'
            + (b"[" * 500_000)
            + b"0"
            + (b"]" * 500_000)
            + b"}"
        )
        response = _StreamingResponse({}, chunks=[body])
        with (
            patch.dict(
                os.environ,
                {"ALPHA_VANTAGE_API_KEY": "test"},
                clear=False,
            ),
            patch(
                "stockanalyst.app.agent.data_sources.requests.get",
                return_value=response,
            ),
            self.assertLogs("seller-agent.data_sources", level="WARNING") as captured,
        ):
            result = fetch_alpha_vantage_sentiment("AAPL")

        self.assertEqual(result["error"], "provider_invalid_response")
        self.assertNotIn("maximum recursion", "\n".join(captured.output).lower())
        self.assertTrue(response.closed)

    def test_huge_integer_provider_json_uses_stable_invalid_code(self) -> None:
        body = b'{"value":' + (b"9" * 5_000) + b"}"
        response = _StreamingResponse({}, chunks=[body])
        with (
            patch.dict(
                os.environ,
                {"ALPHA_VANTAGE_API_KEY": "test"},
                clear=False,
            ),
            patch(
                "stockanalyst.app.agent.data_sources.requests.get",
                return_value=response,
            ),
            self.assertLogs(
                "seller-agent.data_sources",
                level="WARNING",
            ) as captured,
        ):
            result = fetch_alpha_vantage_sentiment("AAPL")

        logs = "\n".join(captured.output)
        self.assertEqual(result["error"], "provider_invalid_response")
        self.assertNotIn("Exceeds the limit", logs)
        self.assertNotIn("9" * 100, logs)
        self.assertTrue(response.closed)

    def test_close_io_errors_are_redacted_without_masking_http_code(
        self,
    ) -> None:
        secret = "CLOSE_SECRET"
        cases = (
            (
                _StreamingResponse(
                    {"feed": []},
                    close_error=requests.ConnectionError(
                        f"close https://provider.invalid/?apikey={secret}",
                    ),
                ),
                "provider_transport_error",
            ),
            (
                _StreamingResponse(
                    {},
                    status_error=requests.HTTPError(
                        f"401 https://provider.invalid/?apikey={secret}",
                    ),
                    close_error=requests.ConnectionError(
                        f"close https://provider.invalid/?apikey={secret}",
                    ),
                ),
                "provider_http_error",
            ),
            (
                _StreamingResponse(
                    {"feed": []},
                    close_error=OSError(
                        f"close https://provider.invalid/?apikey={secret}",
                    ),
                ),
                "provider_transport_error",
            ),
            (
                _StreamingResponse(
                    {},
                    status_error=requests.HTTPError(
                        f"401 https://provider.invalid/?apikey={secret}",
                    ),
                    close_error=OSError(
                        f"close https://provider.invalid/?apikey={secret}",
                    ),
                ),
                "provider_http_error",
            ),
        )
        for response, expected_code in cases:
            with (
                self.subTest(expected_code=expected_code),
                patch.dict(
                    os.environ,
                    {"ALPHA_VANTAGE_API_KEY": secret},
                    clear=False,
                ),
                patch(
                    "stockanalyst.app.agent.data_sources.requests.get",
                    return_value=response,
                ),
                self.assertLogs(
                    "seller-agent.data_sources",
                    level="WARNING",
                ) as captured,
            ):
                result = fetch_alpha_vantage_sentiment("AAPL")
                self.assertEqual(result["error"], expected_code)
                self.assertNotIn(secret, str(result))
                self.assertNotIn(secret, "\n".join(captured.output))
                self.assertNotIn("provider.invalid", "\n".join(captured.output))
                self.assertTrue(response.closed)

    def test_memory_error_from_close_is_not_swallowed(self) -> None:
        response = _StreamingResponse(
            {"feed": []},
            close_error=MemoryError("exhausted"),
        )
        with (
            patch.dict(
                os.environ,
                {"ALPHA_VANTAGE_API_KEY": "test"},
                clear=False,
            ),
            patch(
                "stockanalyst.app.agent.data_sources.requests.get",
                return_value=response,
            ),
            self.assertRaises(MemoryError),
        ):
            fetch_alpha_vantage_sentiment("AAPL")
        self.assertTrue(response.closed)

        secret = "HTTP_CONTEXT_SECRET"
        response = _StreamingResponse(
            {},
            status_error=requests.HTTPError(
                f"401 https://provider.invalid/?apikey={secret}",
            ),
            close_error=MemoryError("exhausted"),
        )
        with (
            patch.dict(
                os.environ,
                {"ALPHA_VANTAGE_API_KEY": secret},
                clear=False,
            ),
            patch(
                "stockanalyst.app.agent.data_sources.requests.get",
                return_value=response,
            ),
        ):
            try:
                fetch_alpha_vantage_sentiment("AAPL")
            except MemoryError as error:
                formatted = "".join(traceback.format_exception(error))
            else:
                self.fail("MemoryError was swallowed")
        self.assertNotIn(secret, formatted)
        self.assertNotIn("provider.invalid", formatted)

    def test_private_provider_errors_carry_only_stable_codes(self) -> None:
        secret = "PRIVATE_ERROR_SECRET"
        response = _StreamingResponse(
            {},
            status_error=requests.HTTPError(
                f"401 https://provider.invalid/?apikey={secret}",
            ),
        )
        with (
            patch(
                "stockanalyst.app.agent.data_sources.requests.get",
                return_value=response,
            ),
            self.assertRaises(_ProviderResponseError) as captured,
        ):
            _read_provider_json(
                "https://provider.invalid",
                params={"apikey": secret},
            )

        self.assertEqual(str(captured.exception), "provider_http_error")
        self.assertIsNone(captured.exception.__cause__)
        self.assertIsNone(captured.exception.__context__)
        self.assertNotIn(
            secret,
            "".join(traceback.format_exception(captured.exception)),
        )

    def test_caller_exception_context_does_not_hide_close_failure(self) -> None:
        response = _StreamingResponse(
            {"feed": []},
            close_error=requests.ConnectionError("close failed"),
        )
        with (
            patch.dict(
                os.environ,
                {"ALPHA_VANTAGE_API_KEY": "test"},
                clear=False,
            ),
            patch(
                "stockanalyst.app.agent.data_sources.requests.get",
                return_value=response,
            ),
        ):
            try:
                raise ValueError("caller's handled exception")
            except ValueError:
                with self.assertLogs(
                    "seller-agent.data_sources",
                    level="WARNING",
                ):
                    result = fetch_alpha_vantage_sentiment("AAPL")

            self.assertEqual(result["error"], "provider_transport_error")
            self.assertTrue(response.closed)

    def test_stream_oserror_uses_stable_transport_code(self) -> None:
        secret = "STREAM_SECRET"
        response = _StreamingResponse(
            {},
            iter_error=OSError(
                f"read https://provider.invalid/?apikey={secret}",
            ),
        )
        with (
            patch.dict(
                os.environ,
                {"ALPHA_VANTAGE_API_KEY": secret},
                clear=False,
            ),
            patch(
                "stockanalyst.app.agent.data_sources.requests.get",
                return_value=response,
            ),
            self.assertLogs("seller-agent.data_sources", level="WARNING") as captured,
        ):
            result = fetch_alpha_vantage_sentiment("AAPL")

        self.assertEqual(result["error"], "provider_transport_error")
        self.assertNotIn(secret, str(result))
        self.assertNotIn(secret, "\n".join(captured.output))
        self.assertNotIn("provider.invalid", "\n".join(captured.output))
        self.assertTrue(response.closed)

    def test_transport_errors_use_stable_codes_without_raw_details(self) -> None:
        secret = "TRANSPORT_SECRET"
        with (
            patch.dict(
                os.environ,
                {"ALPHA_VANTAGE_API_KEY": secret},
                clear=False,
            ),
            patch(
                "stockanalyst.app.agent.data_sources.requests.get",
                side_effect=requests.ConnectionError(
                    f"failed https://provider.invalid/?apikey={secret}",
                ),
            ),
            self.assertLogs("seller-agent.data_sources", level="WARNING") as captured,
        ):
            result = fetch_alpha_vantage_sentiment("AAPL")

        self.assertEqual(result["error"], "provider_transport_error")
        self.assertNotIn(secret, str(result))
        self.assertNotIn(secret, "\n".join(captured.output))
        self.assertNotIn("provider.invalid", "\n".join(captured.output))

    @patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "test"}, clear=False)
    @patch("stockanalyst.app.agent.data_sources.requests.get")
    def test_alpha_normalizes_information_and_abnormal_containers(
        self,
        get: Mock,
    ) -> None:
        get.return_value = _StreamingResponse({
            "Information": "  Rate\nlimit\x00 " + "word " * 100,
        })
        result = fetch_alpha_vantage_sentiment("AAPL")
        self.assertNotIn("\n", result["note"])
        self.assertNotIn("\x00", result["note"])
        self.assertLessEqual(len(result["note"]), 300)
        self.assertTrue(result["note"].startswith("Rate limit word"))

        for value in (None, {"secret": "NON_STRING_INFO"}, ["info"]):
            with self.subTest(information=value):
                get.return_value = _StreamingResponse({"Information": value})
                result = fetch_alpha_vantage_sentiment("AAPL")
                self.assertEqual(result, {"symbol": "AAPL", "note": ""})
                self.assertNotIn("NON_STRING_INFO", str(result))

        get.return_value = _StreamingResponse({"feed": {"not": "a list"}})
        result = fetch_alpha_vantage_sentiment("AAPL")
        self.assertEqual(result["article_count"], 0)
        self.assertNotIn("error", result)

        get.return_value = _StreamingResponse(["not", "an", "object"])
        with self.assertLogs("seller-agent.data_sources", level="WARNING"):
            result = fetch_alpha_vantage_sentiment("AAPL")
        self.assertEqual(result["error"], "provider_invalid_response")
        self.assertNotIn("list", str(result))

    @patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "test"}, clear=False)
    @patch("stockanalyst.app.agent.data_sources.requests.get")
    def test_alpha_caps_feed_and_per_article_ticker_entries(self, get: Mock) -> None:
        feed = [{
            "title": f"Article {index}",
            "ticker_sentiment": [],
        } for index in range(20)]
        feed.append({
            "title": "Article 21",
            "ticker_sentiment": [{
                "ticker": "AAPL",
                "ticker_sentiment_score": "1",
            }],
        })
        get.return_value = _StreamingResponse({"feed": feed})

        result = fetch_alpha_vantage_sentiment("AAPL")

        self.assertEqual(result["article_count"], 20)
        self.assertEqual(result["sentiment_score"], 0.0)

        get.return_value = _StreamingResponse({
            "feed": [{
                "title": "Only article",
                "ticker_sentiment": [
                    {
                        "ticker": "MSFT",
                        "ticker_sentiment_score": "0",
                    }
                    for _ in range(100)
                ] + [{
                    "ticker": "AAPL",
                    "ticker_sentiment_score": "1",
                }],
            }],
        })

        result = fetch_alpha_vantage_sentiment("AAPL")

        self.assertEqual(result["sentiment_score"], 0.0)

    @patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "test"}, clear=False)
    @patch("stockanalyst.app.agent.data_sources.requests.get")
    def test_alpha_accepts_only_string_tickers_and_finite_bounded_scores(
        self,
        get: Mock,
    ) -> None:
        for value in (
            "NaN",
            "Infinity",
            "-Infinity",
            "1.0001",
            "-1.0001",
            True,
            None,
            {"score": 0},
        ):
            with self.subTest(invalid_score=value):
                get.return_value = _StreamingResponse({
                    "feed": [{
                        "title": "Invalid score",
                        "ticker_sentiment": [{
                            "ticker": "AAPL",
                            "ticker_sentiment_score": value,
                        }],
                    }],
                })
                result = fetch_alpha_vantage_sentiment("AAPL")
                self.assertEqual(result["sentiment_score"], 0.0)
                self.assertTrue(math.isfinite(result["sentiment_score"]))
                self.assertNotIn("error", result)

        get.return_value = _StreamingResponse({
            "feed": [{
                "title": "Typed sentiment",
                "ticker_sentiment": [{
                    "ticker": 123,
                    "ticker_sentiment_score": "1",
                }],
            }],
        })

        result = fetch_alpha_vantage_sentiment("AAPL")

        self.assertEqual(result["sentiment_score"], 0.0)
        self.assertTrue(math.isfinite(result["sentiment_score"]))
        self.assertNotIn("error", result)

        for score in ("0.25", "-1", "1"):
            with self.subTest(valid_score=score):
                get.return_value = _StreamingResponse({
                    "feed": [{
                        "title": "Valid score",
                        "ticker_sentiment": [{
                            "ticker": "AAPL",
                            "ticker_sentiment_score": score,
                        }],
                    }],
                })
                result = fetch_alpha_vantage_sentiment("AAPL")
                self.assertEqual(result["sentiment_score"], float(score))

        get.return_value = _StreamingResponse({
            "feed": [{
                "title": "Abnormal container",
                "ticker_sentiment": {"ticker": "AAPL"},
            }],
        })
        result = fetch_alpha_vantage_sentiment("AAPL")
        self.assertEqual(result["sentiment_score"], 0.0)
        self.assertNotIn("error", result)

    @patch.dict(os.environ, {"GNEWS_API_KEY": "test"}, clear=False)
    @patch("stockanalyst.app.agent.data_sources.requests.get")
    def test_gnews_total_articles_is_bounded_non_negative_integer_or_fallback(
        self,
        get: Mock,
    ) -> None:
        article = {
            "title": "Headline",
            "source": {"name": "Source"},
            "publishedAt": "2026-07-23T12:00:00Z",
        }
        for value in (
            "7",
            -1,
            True,
            7.0,
            {"count": 7},
            1_000_000_001,
            10**100,
        ):
            with self.subTest(total_articles=value):
                get.return_value = _StreamingResponse({
                    "totalArticles": value,
                    "articles": [article],
                })
                result = fetch_gnews_headlines("AAPL")
                self.assertEqual(result["total_results"], 1)
                self.assertIs(type(result["total_results"]), int)

        for value in (0, 7, 1_000_000_000):
            with self.subTest(valid_total_articles=value):
                get.return_value = _StreamingResponse({
                    "totalArticles": value,
                    "articles": [article],
                })
                result = fetch_gnews_headlines("AAPL")
                self.assertEqual(result["total_results"], value)
                self.assertIs(type(result["total_results"]), int)

        get.return_value = _StreamingResponse({"articles": {"bad": "container"}})
        result = fetch_gnews_headlines("AAPL")
        self.assertEqual(result["headlines"], [])
        self.assertEqual(result["total_results"], 0)

        get.return_value = _StreamingResponse(["not", "an", "object"])
        with self.assertLogs("seller-agent.data_sources", level="WARNING"):
            result = fetch_gnews_headlines("AAPL")
        self.assertEqual(result["error"], "provider_invalid_response")
