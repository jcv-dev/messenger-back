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
        for k, v in kw.items():
            setattr(row, k, v)
        return row

    def _mock_filter(self, rows):
        """Return schedule rows that match the filter call."""
        def side_effect(**kwargs):
            mock_qs = MagicMock()
            if "date" in kwargs:
                for r in rows:
                    if r.date == kwargs["date"] and getattr(r, 'is_active', True):
                        mock_qs.first.return_value = r
                        return mock_qs
                mock_qs.first.return_value = None
                return mock_qs
            if "day_of_week" in kwargs:
                for r in rows:
                    if r.day_of_week == kwargs["day_of_week"] and getattr(r, 'is_active', True):
                        mock_qs.first.return_value = r
                        return mock_qs
                mock_qs.first.return_value = None
                return mock_qs
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
        override = self._schedule_row(date=datetime.date(2026, 6, 15), close_time=None, is_active=True)
        with self._mock_filter([weekly, override]):
            from api.bot.config import is_within_operating_hours
            self.assertFalse(is_within_operating_hours())

    @patch("api.models.BotConfig")
    @patch("api.bot.config.timezone.now")
    def test_closed_day_returns_false(self, mock_now, mock_cfg):
        mock_cfg.objects.all().values_list.return_value = []
        mock_now.return_value = datetime.datetime(2026, 6, 15, 15, 0, tzinfo=datetime.timezone.utc)
        t = datetime.time
        row = self._schedule_row(day_of_week=0, open_time=t(8, 0), close_time=None, is_active=True)
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
        """Inactive schedule rows are excluded → no active schedule → assumes open."""
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
