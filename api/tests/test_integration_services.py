"""Tests for the ops ↔ calculator service type mapping."""

from django.test import SimpleTestCase

from api.integrations.services import (
    SERVICE_TYPE_MAP,
    calculator_service,
    ops_service,
)


class ServiceMappingTests(SimpleTestCase):
    def test_all_ops_keys_map_to_calculator(self):
        self.assertEqual(calculator_service('domicilio'), 'domicilios')
        self.assertEqual(calculator_service('mensajeria'), 'mensajeria')
        self.assertEqual(calculator_service('compras'), 'purchases')
        self.assertEqual(calculator_service('diligencias'), 'tramites')
        self.assertEqual(calculator_service('bancarios'), 'bancarios')
        self.assertEqual(calculator_service('domii_fijo'), 'domii_fijo')

    def test_normalizes_case_and_spaces(self):
        self.assertEqual(calculator_service('  Domicilio '), 'domicilios')

    def test_unknown_returns_empty(self):
        self.assertEqual(calculator_service('otro'), '')
        self.assertEqual(calculator_service(None), '')

    def test_reverse_mapping(self):
        for ops_key, calc_key in SERVICE_TYPE_MAP.items():
            self.assertEqual(ops_service(calc_key), ops_key)
