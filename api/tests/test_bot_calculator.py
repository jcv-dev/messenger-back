"""Tests for the calculator module — validation and sanitization."""

from unittest.mock import patch, AsyncMock, MagicMock, call

from django.test import SimpleTestCase

from api.bot.calculator import (
    geocode_search, geocode_details, calculate_price,
    _sanitize, _validate_place_id,
)


class CalculatorValidationTests(SimpleTestCase):
    def test_sanitize_strips_control_chars(self):
        result = _sanitize("hello\x00world\x01test", 200)
        self.assertEqual(result, "helloworldtest")

    def test_sanitize_truncates(self):
        result = _sanitize("a" * 500, 50)
        self.assertEqual(len(result), 50)

    def test_sanitize_non_string_returns_empty(self):
        self.assertEqual(_sanitize(None, 100), "")
        self.assertEqual(_sanitize(123, 100), "")

    def test_validate_place_id_valid(self):
        self.assertTrue(_validate_place_id("ChIJvX8..."))
        self.assertTrue(_validate_place_id("abc123"))

    def test_validate_place_id_invalid(self):
        self.assertFalse(_validate_place_id(""))
        self.assertFalse(_validate_place_id(None))
        self.assertFalse(_validate_place_id("<script>"))
        self.assertFalse(_validate_place_id("a" * 600))

    @patch("api.bot.calculator.httpx.AsyncClient")
    async def test_geocode_search_sanitizes_query(self, mock_client_class):
        captured_params = {}

        async def mock_request(*args, **kwargs):
            captured_params.update(kwargs.get("params", {}))
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"results": []}
            mock_resp.raise_for_status.return_value = None
            return mock_resp

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.request = mock_request
        mock_client_class.return_value = mock_client

        result = await geocode_search("  normal query  ")
        self.assertEqual(result, {"results": []})
        self.assertEqual(captured_params.get("q"), "normal query")

    @patch("api.bot.calculator.httpx.AsyncClient")
    async def test_geocode_details_rejects_invalid_place_id(self, mock_client_class):
        result = await geocode_details("<script>alert(1)</script>")
        self.assertIn("error", result)
        self.assertIn("inválido", result["error"])
        mock_client_class.assert_not_called()

    @patch("api.bot.calculator.httpx.AsyncClient")
    async def test_geocode_details_valid_passes(self, mock_client_class):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"lat": 4.0, "lng": -76.0}
        mock_resp.raise_for_status.return_value = None

        async def mock_request(*args, **kwargs):
            return mock_resp

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.request = mock_request
        mock_client_class.return_value = mock_client

        result = await geocode_details("ChIJabc123")
        self.assertEqual(result, {"lat": 4.0, "lng": -76.0})

    @patch("api.bot.calculator.httpx.AsyncClient")
    async def test_empty_search_returns_early(self, mock_client_class):
        result = await geocode_search("")
        self.assertIn("error", result)
        mock_client_class.assert_not_called()

    @patch("api.bot.calculator.httpx.AsyncClient")
    async def test_calculate_price_validates_profile(self, mock_client_class):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"total": 5000}
        mock_resp.raise_for_status.return_value = None

        async def mock_request(*args, **kwargs):
            return mock_resp

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.request = mock_request
        mock_client_class.return_value = mock_client

        result = await calculate_price(
            "usuario_final",
            [{
                "service_type": "domicilios",
                "origin": {"address": "Calle 10", "lat": 4.0, "lng": -76.0},
                "destination": {"address": "Calle 20", "lat": 4.1, "lng": -76.1},
            }],
            payment_method="efectivo",
            acompanante=False,
        )
        self.assertEqual(result, {"total": 5000})

    @patch("api.bot.calculator.httpx.AsyncClient")
    async def test_calculate_price_rejects_invalid_segments(self, mock_client_class):
        with self.assertRaises(ValueError):
            await calculate_price(
                "usuario_final",
                [{"invalid": "structure"}],
            )
        mock_client_class.assert_not_called()
