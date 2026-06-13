"""Tests for api/bot/router.py _build_hours_response() — FAQ hours builder."""

import datetime
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase


class HoursResponseTests(SimpleTestCase):
    """_build_hours_response() formats BotSchedule rows into FAQ text."""

    def _make_row(self, day_of_week=None, date=None, open_time=None,
                  close_time=None, is_active=True, label=""):
        row = MagicMock(spec=[])
        row.day_of_week = day_of_week
        row.date = date
        row.open_time = open_time
        row.close_time = close_time
        row.is_active = is_active
        row.label = label
        return row

    def _mock_queryset(self, rows):
        """Build a QuerySet-like mock that supports .exists() and iteration."""
        qs = MagicMock()
        qs.exists.return_value = len(rows) > 0
        qs.__iter__.return_value = iter(rows)
        return qs

    @patch("api.models.BotSchedule")
    def test_full_week_schedule(self, mock_model):
        t = datetime.time
        rows = [
            self._make_row(0, open_time=t(8, 0), close_time=t(20, 0)),
            self._make_row(1, open_time=t(8, 0), close_time=t(20, 0)),
            self._make_row(2, open_time=t(8, 0), close_time=t(20, 0)),
            self._make_row(3, open_time=t(8, 0), close_time=t(20, 0)),
            self._make_row(4, open_time=t(8, 0), close_time=t(20, 0)),
            self._make_row(5, open_time=t(8, 0), close_time=t(20, 0)),
            self._make_row(6, open_time=t(9, 0), close_time=t(18, 0)),
        ]
        mock_model.objects.filter.return_value.order_by.return_value = self._mock_queryset(rows)

        from api.bot.router import _build_hours_response
        result = _build_hours_response()
        self.assertIn("Nuestro horario de atención:", result)
        self.assertIn("Lunes: 8:00 AM a 8:00 PM", result)
        self.assertIn("Domingo: 9:00 AM a 6:00 PM", result)

    @patch("api.models.BotSchedule")
    def test_empty_schedule(self, mock_model):
        mock_model.objects.filter.return_value.order_by.return_value = self._mock_queryset([])

        from api.bot.router import _build_hours_response
        result = _build_hours_response()
        self.assertEqual(result, "Nuestro horario de atención:\n- No configurado.")

    @patch("api.models.BotSchedule")
    def test_closed_day(self, mock_model):
        t = datetime.time
        rows = [
            self._make_row(0, open_time=t(8, 0), close_time=None),
        ]
        mock_model.objects.filter.return_value.order_by.return_value = self._mock_queryset(rows)

        from api.bot.router import _build_hours_response
        result = _build_hours_response()
        self.assertIn("Lunes: Cerrado", result)

    @patch("api.models.BotSchedule")
    def test_date_override(self, mock_model):
        t = datetime.time
        rows = [
            self._make_row(0, open_time=t(8, 0), close_time=t(20, 0)),
            self._make_row(date=datetime.date(2026, 12, 25), open_time=t(9, 0),
                           close_time=t(14, 0), label="Navidad"),
        ]
        mock_model.objects.filter.return_value.order_by.return_value = self._mock_queryset(rows)

        from api.bot.router import _build_hours_response
        result = _build_hours_response()
        self.assertIn("Lunes: 8:00 AM a 8:00 PM", result)
        self.assertIn("Navidad", result)
        self.assertIn("9:00 AM a 2:00 PM", result)

    @patch("api.models.BotSchedule")
    def test_inactive_excluded(self, mock_model):
        t = datetime.time
        rows = [
            self._make_row(0, open_time=t(8, 0), close_time=t(20, 0), is_active=True),
        ]
        mock_model.objects.filter.return_value.order_by.return_value = self._mock_queryset(rows)

        from api.bot.router import _build_hours_response
        result = _build_hours_response()
        self.assertIn("Lunes", result)
        mock_model.objects.filter.assert_called_once_with(is_active=True)

    @patch("api.models.BotSchedule")
    def test_date_override_closed(self, mock_model):
        rows = [
            self._make_row(0, open_time=datetime.time(8, 0), close_time=datetime.time(20, 0)),
            self._make_row(date=datetime.date(2026, 1, 1), close_time=None, label="Año Nuevo"),
        ]
        mock_model.objects.filter.return_value.order_by.return_value = self._mock_queryset(rows)

        from api.bot.router import _build_hours_response
        result = _build_hours_response()
        self.assertIn("Año Nuevo", result)
        self.assertIn("Cerrado", result)
