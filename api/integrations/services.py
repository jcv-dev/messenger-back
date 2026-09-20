"""Service type mapping between the ops catalog and the calculator API."""

# ops catalog key -> calculator service type
SERVICE_TYPE_MAP = {
    'domicilio': 'domicilios',
    'mensajeria': 'mensajeria',
    'compras': 'purchases',
    'diligencias': 'tramites',
    'bancarios': 'bancarios',
    'domii_fijo': 'domii_fijo',
}

# calculator service type -> ops catalog key
CALCULATOR_TO_OPS = {v: k for k, v in SERVICE_TYPE_MAP.items()}

OPS_SERVICE_KEYS = frozenset(SERVICE_TYPE_MAP)
CALCULATOR_SERVICE_KEYS = frozenset(CALCULATOR_TO_OPS)


def normalize_ops_key(value) -> str:
    return (value or '').strip().lower()


def calculator_service(ops_key) -> str:
    """ops catalog key → calculator service type ('' when unknown)."""
    return SERVICE_TYPE_MAP.get(normalize_ops_key(ops_key), '')


def ops_service(calculator_key) -> str:
    """calculator service type → ops catalog key ('' when unknown)."""
    return CALCULATOR_TO_OPS.get(normalize_ops_key(calculator_key), '')
