"""Tests for api/bot/config.py — config resolution and operating hours."""

import datetime
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings


def _clear():
    from api.bot.config import _clear_cache
    _clear_cache()


class ConfigResolutionTests(SimpleTestCase):
    def setUp(self):
        _clear()

    @patch("api.models.BotConfig")
    def test_get_config_returns_value_from_db(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("bot_key", "db_value")]
        from api.bot.config import get_config
        self.assertEqual(get_config("bot_key", "fallback"), "db_value")

    @patch("api.models.BotConfig")
    def test_get_config_falls_back_to_default(self, mock_model):
        mock_model.objects.all().values_list.return_value = []
        from api.bot.config import get_config
        self.assertEqual(get_config("missing_key", "default_val"), "default_val")

    @patch("api.models.BotConfig")
    def test_get_config_none_default_when_missing(self, mock_model):
        mock_model.objects.all().values_list.return_value = []
        from api.bot.config import get_config
        self.assertIsNone(get_config("nonexistent"))

    @patch("api.models.BotConfig")
    def test_get_config_handles_db_exception(self, mock_model):
        mock_model.objects.all().values_list.side_effect = Exception("DB down")
        from api.bot.config import get_config
        self.assertEqual(get_config("any", "fallback"), "fallback")


class FAQInfoSectionTests(SimpleTestCase):
    def setUp(self):
        _clear()

    @patch("api.models.BotConfig")
    def test_returns_db_value_when_set(self, mock_model):
        mock_model.objects.all().values_list.return_value = [
            ("faq_info_section", "DB FAQ text"),
        ]
        from api.bot.config import get_faq_info_section
        self.assertEqual(get_faq_info_section(), "DB FAQ text")

    @patch("api.models.BotConfig")
    def test_falls_back_to_settings_when_not_in_db(self, mock_model):
        mock_model.objects.all().values_list.return_value = []
        from api.bot.config import get_faq_info_section
        result = get_faq_info_section()
        self.assertIn(settings.BOT_OPERATING_HOURS, result)
        self.assertIn("SERVICIOS", result)
        self.assertIn("COBERTURA", result)
        self.assertIn("PAGO", result)


class OperatingHoursTests(SimpleTestCase):
    def setUp(self):
        _clear()

    def _schedule_row(self, **kw):
        row = MagicMock()
        row.is_closed = False  # model default
        for k, v in kw.items():
            setattr(row, k, v)
        return row

    def _mock_filter(self, rows):
        def side_effect(**kwargs):
            mock_qs = MagicMock()

            def _filtered():
                result = []
                for r in rows:
                    match = True
                    for k, v in kwargs.items():
                        if getattr(r, k, None) != v:
                            match = False
                            break
                    if match:
                        result.append(r)
                return result

            filtered = _filtered()
            mock_qs.__iter__.return_value = iter(filtered)
            mock_qs.__len__.return_value = len(filtered)
            mock_qs.first.return_value = filtered[0] if filtered else None
            return mock_qs
        return patch("api.models.BotSchedule.objects.filter", side_effect=side_effect)

    @patch("api.models.BotConfig")
    @patch("api.bot.config.timezone.now")
    def test_inside_hours(self, mock_now, mock_cfg):
        mock_cfg.objects.all().values_list.return_value = []
        mock_now.return_value = datetime.datetime(2026, 6, 15, 15, 0, tzinfo=datetime.timezone.utc)
        t = datetime.time
        row = self._schedule_row(day_of_week=0, open_time=t(8, 0), close_time=t(20, 0), is_active=True)
        with self._mock_filter([row]):
            from api.bot.config import is_within_operating_hours
            self.assertTrue(is_within_operating_hours())

    @patch("api.models.BotConfig")
    @patch("api.bot.config.timezone.now")
    def test_outside_hours(self, mock_now, mock_cfg):
        mock_cfg.objects.all().values_list.return_value = []
        mock_now.return_value = datetime.datetime(2026, 6, 15, 12, 0, tzinfo=datetime.timezone.utc)
        t = datetime.time
        row = self._schedule_row(day_of_week=0, open_time=t(8, 0), close_time=t(20, 0), is_active=True)
        with self._mock_filter([row]):
            from api.bot.config import is_within_operating_hours
            self.assertFalse(is_within_operating_hours())

    @patch("api.models.BotConfig")
    @patch("api.bot.config.timezone.now")
    def test_date_override_wins(self, mock_now, mock_cfg):
        mock_cfg.objects.all().values_list.return_value = []
        mock_now.return_value = datetime.datetime(2026, 6, 15, 15, 0, tzinfo=datetime.timezone.utc)
        t = datetime.time
        weekly = self._schedule_row(day_of_week=0, open_time=t(8, 0), close_time=t(20, 0), is_active=True)
        override = self._schedule_row(date=datetime.date(2026, 6, 15), open_time=t(0, 0), close_time=None, is_active=True, is_closed=True)
        with self._mock_filter([weekly, override]):
            from api.bot.config import is_within_operating_hours
            self.assertFalse(is_within_operating_hours())

    @patch("api.models.BotConfig")
    @patch("api.bot.config.timezone.now")
    def test_closed_day_returns_false(self, mock_now, mock_cfg):
        mock_cfg.objects.all().values_list.return_value = []
        mock_now.return_value = datetime.datetime(2026, 6, 15, 15, 0, tzinfo=datetime.timezone.utc)
        t = datetime.time
        row = self._schedule_row(day_of_week=0, open_time=t(8, 0), close_time=None, is_active=True, is_closed=True)
        with self._mock_filter([row]):
            from api.bot.config import is_within_operating_hours
            self.assertFalse(is_within_operating_hours())

    @patch("api.models.BotConfig")
    @patch("api.bot.config.timezone.now")
    def test_no_schedule_returns_true(self, mock_now, mock_cfg):
        mock_cfg.objects.all().values_list.return_value = []
        mock_now.return_value = datetime.datetime(2026, 6, 15, 15, 0, tzinfo=datetime.timezone.utc)
        with patch("api.models.BotSchedule.objects.filter") as mock_filter:
            mock_filter.return_value.first.return_value = None
            from api.bot.config import is_within_operating_hours
            self.assertTrue(is_within_operating_hours())

    @patch("api.models.BotConfig")
    @patch("api.bot.config.timezone.now")
    def test_inactive_day_excluded(self, mock_now, mock_cfg):
        mock_cfg.objects.all().values_list.return_value = []
        mock_now.return_value = datetime.datetime(2026, 6, 15, 15, 0, tzinfo=datetime.timezone.utc)
        t = datetime.time
        row = self._schedule_row(day_of_week=0, open_time=t(8, 0), close_time=t(20, 0), is_active=False)
        with self._mock_filter([row]):
            from api.bot.config import is_within_operating_hours
            self.assertTrue(is_within_operating_hours())

    @patch("api.models.BotConfig")
    @patch("api.bot.config.timezone.now")
    def test_inactive_override_ignored(self, mock_now, mock_cfg):
        mock_cfg.objects.all().values_list.return_value = []
        mock_now.return_value = datetime.datetime(2026, 6, 15, 15, 0, tzinfo=datetime.timezone.utc)
        t = datetime.time
        weekly = self._schedule_row(day_of_week=0, open_time=t(8, 0), close_time=t(20, 0), is_active=True)
        override = self._schedule_row(date=datetime.date(2026, 6, 15), open_time=t(9, 0), close_time=t(18, 0), is_active=False)
        with self._mock_filter([weekly, override]):
            from api.bot.config import is_within_operating_hours
            self.assertTrue(is_within_operating_hours())

    @patch("api.models.BotConfig")
    @patch("api.bot.config.timezone.now")
    def test_exact_open_time_inside(self, mock_now, mock_cfg):
        mock_cfg.objects.all().values_list.return_value = []
        mock_now.return_value = datetime.datetime(2026, 6, 15, 13, 0, tzinfo=datetime.timezone.utc)
        t = datetime.time
        row = self._schedule_row(day_of_week=0, open_time=t(8, 0), close_time=t(20, 0), is_active=True)
        with self._mock_filter([row]):
            from api.bot.config import is_within_operating_hours
            self.assertTrue(is_within_operating_hours())

    @patch("api.models.BotConfig")
    @patch("api.bot.config.timezone.now")
    def test_exact_close_time_outside(self, mock_now, mock_cfg):
        mock_cfg.objects.all().values_list.return_value = []
        mock_now.return_value = datetime.datetime(2026, 6, 16, 1, 1, tzinfo=datetime.timezone.utc)
        t = datetime.time
        row = self._schedule_row(day_of_week=0, open_time=t(8, 0), close_time=t(20, 0), is_active=True)
        with self._mock_filter([row]):
            from api.bot.config import is_within_operating_hours
            self.assertFalse(is_within_operating_hours())


class StateMachineToggleTests(SimpleTestCase):
    def setUp(self):
        _clear()

    @patch("api.models.BotConfig")
    def test_disabled_when_not_set(self, mock_model):
        mock_model.objects.all().values_list.return_value = []
        from api.bot.config import get_state_machine_enabled
        self.assertFalse(get_state_machine_enabled())

    @patch("api.models.BotConfig")
    def test_enabled_from_botconfig(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("state_machine_enabled", True)]
        from api.bot.config import get_state_machine_enabled
        self.assertTrue(get_state_machine_enabled())

    @patch("api.models.BotConfig")
    def test_env_var_takes_precedence(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("state_machine_enabled", False)]
        with patch.dict("os.environ", {"BOT_STATE_MACHINE": "1"}):
            from api.bot.config import get_state_machine_enabled
            self.assertTrue(get_state_machine_enabled())

    @patch("api.models.BotConfig")
    def test_env_var_0_does_not_override(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("state_machine_enabled", True)]
        with patch.dict("os.environ", {"BOT_STATE_MACHINE": "0"}):
            from api.bot.config import get_state_machine_enabled
            self.assertTrue(get_state_machine_enabled())


class BotEnabledTests(SimpleTestCase):
    def setUp(self):
        _clear()

    @patch("api.models.BotConfig")
    def test_enabled_by_default(self, mock_model):
        mock_model.objects.all().values_list.return_value = []
        from api.bot.config import is_bot_enabled
        self.assertTrue(is_bot_enabled())

    @patch("api.models.BotConfig")
    def test_disabled_from_botconfig(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("bot_enabled", False)]
        from api.bot.config import is_bot_enabled
        self.assertFalse(is_bot_enabled())

    @patch("api.models.BotConfig")
    def test_enabled_from_botconfig(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("bot_enabled", True)]
        from api.bot.config import is_bot_enabled
        self.assertTrue(is_bot_enabled())

    @patch("api.models.BotConfig")
    def test_env_var_false_overrides_db_true(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("bot_enabled", True)]
        with patch.dict("os.environ", {"BOT_ENABLED": "0"}):
            from api.bot.config import is_bot_enabled
            self.assertFalse(is_bot_enabled())

    @patch("api.models.BotConfig")
    def test_env_var_true_overrides_db_false(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("bot_enabled", False)]
        with patch.dict("os.environ", {"BOT_ENABLED": "1"}):
            from api.bot.config import is_bot_enabled
            self.assertTrue(is_bot_enabled())

    @patch("api.models.BotConfig")
    def test_env_var_no_disables(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("bot_enabled", True)]
        with patch.dict("os.environ", {"BOT_ENABLED": "no"}):
            from api.bot.config import is_bot_enabled
            self.assertFalse(is_bot_enabled())

    @patch("api.models.BotConfig")
    def test_env_var_false_disables(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("bot_enabled", True)]
        with patch.dict("os.environ", {"BOT_ENABLED": "false"}):
            from api.bot.config import is_bot_enabled
            self.assertFalse(is_bot_enabled())


class TypedGetterTests(SimpleTestCase):
    def setUp(self):
        _clear()

    @patch("api.models.BotConfig")
    def test_llm_temperature_from_db(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("llm_temperature", 0.5)]
        from api.bot.config import get_llm_temperature
        self.assertEqual(get_llm_temperature(), 0.5)

    @patch("api.models.BotConfig")
    def test_llm_temperature_fallback(self, mock_model):
        mock_model.objects.all().values_list.return_value = []
        from api.bot.config import get_llm_temperature
        self.assertEqual(get_llm_temperature(), settings.BOT_LLM_TEMPERATURE)

    @patch("api.models.BotConfig")
    def test_llm_max_tokens_fallback(self, mock_model):
        mock_model.objects.all().values_list.return_value = []
        from api.bot.config import get_llm_max_tokens
        self.assertEqual(get_llm_max_tokens(), settings.BOT_LLM_MAX_OUTPUT_TOKENS)

    @patch("api.models.BotConfig")
    def test_llm_retry_count_db(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("llm_retry_count", 5)]
        from api.bot.config import get_llm_retry_count
        self.assertEqual(get_llm_retry_count(), 5)

    @patch("api.models.BotConfig")
    def test_tools_cache_ttl_fallback(self, mock_model):
        mock_model.objects.all().values_list.return_value = []
        from api.bot.config import get_tools_cache_ttl
        self.assertEqual(get_tools_cache_ttl(), settings.BOT_TOOLS_CACHE_TTL)

    @patch("api.models.BotConfig")
    def test_max_user_message_length_db(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("max_user_message_length", 500)]
        from api.bot.config import get_max_user_message_length
        self.assertEqual(get_max_user_message_length(), 500)


class AllowedURLDomainsTests(SimpleTestCase):
    def setUp(self):
        _clear()

    @patch("api.models.BotConfig")
    @override_settings(BOT_ALLOWED_OUTPUT_URL_DOMAINS=["example.com"])
    def test_combines_config_and_settings(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("allowed_url_domains", ["custom.org"])]
        from api.bot.config import get_allowed_url_domains
        result = get_allowed_url_domains()
        self.assertIn("custom.org", result)
        self.assertIn("example.com", result)

    @patch("api.models.BotConfig")
    @override_settings(BOT_ALLOWED_OUTPUT_URL_DOMAINS=[])
    def test_handles_non_list_config_gracefully(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("allowed_url_domains", "not_a_list")]
        from api.bot.config import get_allowed_url_domains
        result = get_allowed_url_domains()
        self.assertEqual(result, [])

    @patch("api.models.BotConfig")
    @override_settings(BOT_ALLOWED_OUTPUT_URL_DOMAINS=["builtin.com"])
    def test_only_settings_when_no_config(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("allowed_url_domains", ["custom.org"])]
        from api.bot.config import get_allowed_url_domains
        result = get_allowed_url_domains()
        self.assertIn("custom.org", result)
        self.assertIn("builtin.com", result)


class ConfigCacheTests(SimpleTestCase):
    def setUp(self):
        _clear()

    @patch("api.models.BotConfig")
    def test_cache_returns_stale_within_ttl(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("key1", "v1")]
        from api.bot.config import get_config
        self.assertEqual(get_config("key1"), "v1")

        mock_model.objects.all().values_list.return_value = [("key1", "v2")]
        self.assertEqual(get_config("key1"), "v1")

    @patch("api.models.BotConfig")
    def test_clear_cache_invalidates(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("key1", "v1")]
        from api.bot.config import get_config, _clear_cache
        self.assertEqual(get_config("key1"), "v1")

        mock_model.objects.all().values_list.return_value = [("key1", "v2")]
        _clear_cache()
        self.assertEqual(get_config("key1"), "v2")

    @patch("api.bot.config.time.time")
    @patch("api.models.BotConfig")
    def test_cache_refreshes_after_ttl(self, mock_model, mock_time):
        mock_time.return_value = 1000.0
        mock_model.objects.all().values_list.return_value = [("key1", "v1")]
        from api.bot.config import get_config
        self.assertEqual(get_config("key1"), "v1")

        mock_time.return_value = 1030.0
        mock_model.objects.all().values_list.return_value = [("key1", "v2")]
        self.assertEqual(get_config("key1"), "v2")


class GroupedHoursTextTests(SimpleTestCase):
    """get_grouped_hours_text() — smart day grouping, future overrides."""

    def _row(self, day_of_week=None, date=None, open_time=None,
             close_time=None, is_active=True, label="", is_closed=False):
        r = MagicMock()
        r.day_of_week = day_of_week
        r.date = date
        r.open_time = open_time
        r.close_time = close_time
        r.is_active = is_active
        r.label = label
        r.is_closed = is_closed
        return r

    def _mock_qs(self, rows):
        qs = MagicMock()
        qs.__iter__.return_value = iter(rows)
        qs.order_by.return_value = qs
        return qs

    def _make_side_effect(self, rows):
        """Return side_effect list for two filter() calls: recurring, then overrides."""
        recurring_qs = self._mock_qs(rows)
        override_qs = self._mock_qs([])
        return [recurring_qs, override_qs]

    @patch("api.models.BotSchedule")
    @patch("api.bot.config.timezone.now")
    def test_groups_consecutive_same_schedule(self, mock_now, mock_bs):
        """Mon-Fri same schedule → 'Lunes a Viernes' group."""
        mock_now.return_value = datetime.datetime(2026, 6, 13, 15, 0, tzinfo=datetime.timezone.utc)
        t = datetime.time
        rows = [
            self._row(day_of_week=0, open_time=t(8, 0), close_time=t(20, 0)),
            self._row(day_of_week=1, open_time=t(8, 0), close_time=t(20, 0)),
            self._row(day_of_week=2, open_time=t(8, 0), close_time=t(20, 0)),
            self._row(day_of_week=3, open_time=t(8, 0), close_time=t(20, 0)),
            self._row(day_of_week=4, open_time=t(8, 0), close_time=t(20, 0)),
        ]
        mock_bs.objects.filter.side_effect = self._make_side_effect(rows)
        from api.bot.config import get_grouped_hours_text
        result = get_grouped_hours_text()
        self.assertIn("Lunes a Viernes", result)
        self.assertNotIn("Martes:", result)

    @patch("api.models.BotSchedule")
    @patch("api.bot.config.timezone.now")
    def test_split_at_different_hours(self, mock_now, mock_bs):
        """Sat/Sun different hours → separate lines."""
        mock_now.return_value = datetime.datetime(2026, 6, 13, 15, 0, tzinfo=datetime.timezone.utc)
        t = datetime.time
        rows = [
            self._row(day_of_week=0, open_time=t(8, 0), close_time=t(20, 0)),
            self._row(day_of_week=1, open_time=t(8, 0), close_time=t(20, 0)),
            self._row(day_of_week=2, open_time=t(8, 0), close_time=t(20, 0)),
            self._row(day_of_week=3, open_time=t(8, 0), close_time=t(20, 0)),
            self._row(day_of_week=4, open_time=t(8, 0), close_time=t(20, 0)),
            self._row(day_of_week=5, open_time=t(8, 0), close_time=t(20, 0)),
            self._row(day_of_week=6, open_time=t(9, 0), close_time=t(18, 0)),
        ]
        mock_bs.objects.filter.side_effect = self._make_side_effect(rows)
        from api.bot.config import get_grouped_hours_text
        result = get_grouped_hours_text()
        self.assertIn("Lunes a Sábado", result)
        self.assertIn("Domingo y Festivos: 9:00 AM", result)

    @patch("api.models.BotSchedule")
    @patch("api.bot.config.timezone.now")
    def test_all_different(self, mock_now, mock_bs):
        """All different → no grouping."""
        mock_now.return_value = datetime.datetime(2026, 6, 13, 15, 0, tzinfo=datetime.timezone.utc)
        t = datetime.time
        rows = [
            self._row(day_of_week=0, open_time=t(8, 0), close_time=t(18, 0)),
            self._row(day_of_week=1, open_time=t(9, 0), close_time=t(18, 0)),
            self._row(day_of_week=2, open_time=t(8, 0), close_time=t(17, 0)),
        ]
        mock_bs.objects.filter.side_effect = self._make_side_effect(rows)
        from api.bot.config import get_grouped_hours_text
        result = get_grouped_hours_text()
        self.assertIn("Lunes:", result)
        self.assertIn("Martes:", result)
        self.assertIn("Miércoles:", result)

    @patch("api.models.BotSchedule")
    @patch("api.bot.config.timezone.now")
    def test_non_consecutive_days_interleaved(self, mock_now, mock_bs):
        """Mon/Wed/Fri same hours, Tue/Thu same hours → grouped by schedule."""
        mock_now.return_value = datetime.datetime(2026, 6, 13, 15, 0, tzinfo=datetime.timezone.utc)
        t = datetime.time
        rows = [
            self._row(day_of_week=0, open_time=t(8, 0), close_time=t(20, 0)),
            self._row(day_of_week=1, open_time=t(9, 0), close_time=t(18, 0)),
            self._row(day_of_week=2, open_time=t(8, 0), close_time=t(20, 0)),
            self._row(day_of_week=3, open_time=t(9, 0), close_time=t(18, 0)),
            self._row(day_of_week=4, open_time=t(8, 0), close_time=t(20, 0)),
            self._row(day_of_week=5, open_time=t(9, 0), close_time=t(18, 0)),
            self._row(day_of_week=6, open_time=t(9, 0), close_time=t(18, 0)),
        ]
        mock_bs.objects.filter.side_effect = self._make_side_effect(rows)
        from api.bot.config import get_grouped_hours_text
        result = get_grouped_hours_text()
        self.assertIn("Lunes, Miércoles y Viernes", result)
        self.assertIn("Martes, Jueves, Sábado y Domingo y Festivos", result)

    @patch("api.models.BotSchedule")
    @patch("api.bot.config.timezone.now")
    def test_closed_day_grouped(self, mock_now, mock_bs):
        """Closed days grouped; open days with gaps → comma+y list."""
        mock_now.return_value = datetime.datetime(2026, 6, 13, 15, 0, tzinfo=datetime.timezone.utc)
        t = datetime.time
        rows = [
            self._row(day_of_week=0, open_time=t(8, 0), close_time=t(20, 0)),
            self._row(day_of_week=1, open_time=t(8, 0), close_time=t(20, 0)),
            self._row(day_of_week=2, open_time=None, close_time=None, is_closed=True),
            self._row(day_of_week=3, open_time=None, close_time=None, is_closed=True),
            self._row(day_of_week=4, open_time=t(8, 0), close_time=t(20, 0)),
            self._row(day_of_week=5, open_time=t(8, 0), close_time=t(20, 0)),
            self._row(day_of_week=6, open_time=t(9, 0), close_time=t(18, 0)),
        ]
        mock_bs.objects.filter.side_effect = self._make_side_effect(rows)
        from api.bot.config import get_grouped_hours_text
        result = get_grouped_hours_text()
        self.assertIn("Lunes, Martes, Viernes y Sábado", result)
        self.assertIn("Miércoles a Jueves: Cerrado", result)

    @patch("api.models.BotSchedule")
    @patch("api.bot.config.timezone.now")
    def test_everyday_single_group(self, mock_now, mock_bs):
        """All 7 days same schedule → 'Todos los días'."""
        mock_now.return_value = datetime.datetime(2026, 6, 13, 15, 0, tzinfo=datetime.timezone.utc)
        t = datetime.time
        rows = [
            self._row(day_of_week=i, open_time=t(8, 0), close_time=t(20, 0))
            for i in range(7)
        ]
        mock_bs.objects.filter.side_effect = self._make_side_effect(rows)
        from api.bot.config import get_grouped_hours_text
        result = get_grouped_hours_text()
        self.assertIn("Todos los días", result)
        self.assertNotIn("Lunes", result)

    @patch("api.models.BotSchedule")
    @patch("api.bot.config.timezone.now")
    def test_empty_returns_empty_string(self, mock_now, mock_bs):
        """No schedule at all → empty string."""
        mock_now.return_value = datetime.datetime(2026, 6, 13, 15, 0, tzinfo=datetime.timezone.utc)
        mock_bs.objects.filter.side_effect = self._make_side_effect([])
        from api.bot.config import get_grouped_hours_text
        result = get_grouped_hours_text()
        self.assertEqual(result, "")


class SendDelaySecondsTests(SimpleTestCase):
    def setUp(self):
        _clear()

    @patch("api.models.BotConfig")
    def test_default_delay_10_seconds(self, mock_model):
        mock_model.objects.all().values_list.return_value = []
        from api.bot.config import get_send_delay_seconds
        self.assertEqual(get_send_delay_seconds(), 10)

    @patch("api.models.BotConfig")
    def test_returns_db_value(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("send_delay_seconds", 30)]
        from api.bot.config import get_send_delay_seconds
        self.assertEqual(get_send_delay_seconds(), 30)

    @patch("api.models.BotConfig")
    def test_zero_disables_delay(self, mock_model):
        mock_model.objects.all().values_list.return_value = [("send_delay_seconds", 0)]
        from api.bot.config import get_send_delay_seconds
        self.assertEqual(get_send_delay_seconds(), 0)
