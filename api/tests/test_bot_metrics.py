"""Tests for api.bot.metrics — in-process counters."""

from django.test import SimpleTestCase

from api.bot.metrics import incr, snapshot, reset, get_metrics


class MetricsTests(SimpleTestCase):
    def setUp(self):
        reset()

    def test_incr_and_snapshot(self):
        incr("messages.processed")
        incr("messages.processed")
        incr("llm.calls")
        stats = snapshot()
        self.assertEqual(stats["messages.processed"], 2)
        self.assertEqual(stats["llm.calls"], 1)

    def test_unknown_counter_zero(self):
        self.assertEqual(get_metrics().get("nonexistent"), 0)

    def test_reset_clears(self):
        incr("test", 5)
        reset()
        self.assertEqual(snapshot(), {})

    def test_incr_with_delta(self):
        incr("test", 10)
        self.assertEqual(snapshot()["test"], 10)
