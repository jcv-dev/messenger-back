"""Tests for phone normalization (plan §5)."""

from django.test import SimpleTestCase

from api.integrations.phones import digits_only, is_ops_10, is_wa, to_ops, to_wa


class PhoneNormalizationTests(SimpleTestCase):
    def test_digits_only(self):
        self.assertEqual(digits_only('+57 (300) 123-4567'), '573001234567')
        self.assertEqual(digits_only(None), '')
        self.assertEqual(digits_only(3001234567), '3001234567')

    def test_to_wa_from_ops_10_digits(self):
        self.assertEqual(to_wa('3001234567'), '573001234567')

    def test_to_wa_keeps_wa_format(self):
        self.assertEqual(to_wa('573001234567'), '573001234567')

    def test_to_wa_accepts_punctuation(self):
        self.assertEqual(to_wa('+57 300 123 4567'), '573001234567')

    def test_to_wa_invalid_returns_empty(self):
        self.assertEqual(to_wa('123'), '')
        self.assertEqual(to_wa(''), '')
        self.assertEqual(to_wa(None), '')

    def test_to_ops_from_wa(self):
        self.assertEqual(to_ops('573001234567'), '3001234567')

    def test_to_ops_keeps_10_digits(self):
        self.assertEqual(to_ops('3001234567'), '3001234567')

    def test_to_ops_empty(self):
        self.assertEqual(to_ops(None), '')

    def test_is_wa(self):
        self.assertTrue(is_wa('573001234567'))
        self.assertFalse(is_wa('3001234567'))
        self.assertFalse(is_wa('57300123456'))

    def test_is_ops_10(self):
        self.assertTrue(is_ops_10('3001234567'))
        self.assertFalse(is_ops_10('573001234567'))
        self.assertFalse(is_ops_10('300123456'))
