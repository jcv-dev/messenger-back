"""Tests for Phase 6 — the LLM order draft (plan §4.3/§6.2).

The DeepSeek client and the calculator/ops calls are patched at the module
boundary; the suite never reaches the network. It covers the transcript
window, prompt context, tool execution, schema validation, the one retry,
caching by message window and the agent endpoint (auth, validation, throttle).
"""

import json
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from api.integrations import draft
from api.integrations.draft import (
    DRAFT_CACHE_PREFIX,
    DraftError,
    DraftNotConfigured,
    DraftResponseError,
    build_messages,
    collect_transcript,
    fetch_client_context,
    message_text,
    normalize_draft,
    resolve_draft_client,
    run_tool,
    summarize_addresses,
    summarize_orders,
)
from api.models import CityGroup, Conversation, Message
from api.tests import assign_user_group, get_or_create_tulua_group

LLM_SETTINGS = dict(
    ORDER_LLM_BASE_URL='https://llm.test/v1',
    ORDER_LLM_API_KEY='sk-test',
    ORDER_LLM_MODEL='deepseek-flash',
)
OPS_SETTINGS = dict(OPS_API_URL='https://ops.test', OPS_API_KEY='dmikey_x')

CATALOG = [
    {'key': 'domicilio', 'name': 'Domicilio', 'requires_address': True},
    {'key': 'compras', 'name': 'Compras', 'requires_address': False},
]
TOOL_CATALOG = [
    {'key': 'canasta', 'label': 'Canasta', 'surcharge': 0},
    {'key': 'maletin', 'label': 'Maletín', 'surcharge': 500},
]

MODEL_DRAFT = {
    'origin_address': 'Cra 5 #12-01, Tuluá',
    'origin_lat': 4.085,
    'origin_lng': -76.195,
    'payment_method': 'nequi',
    'profile': 'negocio',
    'acompanante': True,
    'tools': ['canasta', 'inventada'],
    'stops': [
        {
            'service_type': 'domicilio', 'dest_address': 'Calle 20 #3-10, Tuluá',
            'lat': 4.09, 'lng': -76.21, 'description': 'caja',
            'observation': 'timbre azul',
        },
        {
            'service_type': 'compras', 'dest_address': '',
            'description': 'mercado', 'observation': '',
        },
    ],
    'missing': ['precio del domicilio'],
    'confidence': 0.86,
}


def make_conversation(**kwargs):
    defaults = {
        'whatsapp_id': f'wsp-{uuid4().hex}',
        'contact_name': 'Ana Pérez',
        'contact_phone': '573001234567',
    }
    defaults.update(kwargs)
    return Conversation.objects.create(**defaults)


def make_message(conversation, content, direction='inbound', **kwargs):
    return Message.objects.create(
        conversation=conversation,
        direction=direction,
        message_type=kwargs.pop('message_type', 'text'),
        content=content,
        sender_name='Ana' if direction == 'inbound' else 'Agente',
        **kwargs,
    )


# ---------------------------------------------------------------------------
#  Transcript
# ---------------------------------------------------------------------------


class TranscriptTests(TestCase):
    def setUp(self):
        self.conversation = make_conversation()

    def test_message_text_media_placeholders(self):
        image = Message(conversation=self.conversation, message_type='image', content='foto', direction='inbound')
        audio = Message(conversation=self.conversation, message_type='audio', content='', direction='inbound')
        sticker = Message(conversation=self.conversation, message_type='sticker', content='x', direction='inbound')
        location = Message(
            conversation=self.conversation, message_type='location', content='',
            direction='inbound', metadata={'location': {'name': 'Tienda', 'address': 'Calle 10'}},
        )
        self.assertEqual(message_text(image), '[imagen] foto')
        self.assertEqual(message_text(audio), '[audio]')
        self.assertEqual(message_text(sticker), '[sticker]')
        self.assertEqual(message_text(location), '[ubicación] Tienda · Calle 10')

    def test_message_text_flattens_json_content(self):
        message = Message(
            conversation=self.conversation, message_type='interactive', direction='inbound',
            content=json.dumps({'text': 'Quiero un domicilio', 'button': 'x'}),
        )
        self.assertEqual(message_text(message), 'Quiero un domicilio')

    def test_collect_transcript_starts_at_the_selected_message(self):
        before = make_message(self.conversation, 'mensaje viejo')
        start = make_message(self.conversation, 'recoge en mi casa')
        after = make_message(self.conversation, 'y déjalo en la calle 20', direction='outbound')

        transcript = collect_transcript(self.conversation, start)

        texts = [entry['text'] for entry in transcript]
        self.assertNotIn('mensaje viejo', texts)
        self.assertEqual(texts, ['recoge en mi casa', 'y déjalo en la calle 20'])
        self.assertEqual(transcript[0]['role'], 'user')
        self.assertEqual(transcript[1]['role'], 'assistant')
        self.assertLess(before.id, self.conversation.messages.order_by('id').first().id + 1)
        self.assertEqual(after.direction, 'outbound')

    def test_collect_transcript_skips_reactions_and_templates(self):
        start = make_message(self.conversation, 'hola')
        make_message(self.conversation, '❤️', message_type='reaction')
        make_message(self.conversation, '{"name": "aviso_asignado"}', message_type='template', direction='outbound')

        transcript = collect_transcript(self.conversation, start)

        self.assertEqual([entry['text'] for entry in transcript], ['hola'])

    def test_collect_transcript_caps_messages_and_characters(self):
        start = make_message(self.conversation, 'x' * 900)
        for index in range(70):
            make_message(self.conversation, f'mensaje {index}')

        transcript = collect_transcript(self.conversation, start)

        self.assertEqual(len(transcript), draft.MAX_MESSAGES)
        self.assertEqual(len(transcript[0]['text']), draft.MAX_MESSAGE_CHARS)


# ---------------------------------------------------------------------------
#  Prompt context
# ---------------------------------------------------------------------------


class PromptContextTests(TestCase):
    def setUp(self):
        self.conversation = make_conversation()

    def test_build_messages_includes_client_context_and_rules(self):
        client_ref = {'ops_client_user_id': 88, 'name': 'Ana Pérez', 'phone': '3001234567'}
        context = {
            'addresses': [
                {'address': 'Cra 5 #12-01', 'is_default': 1, 'lat': 4.085, 'lng': -76.195},
                {'address': 'Calle 20 #3-10'},
            ],
            'orders': [
                {
                    'order_number': 1234, 'status_label': 'Entregado',
                    'created_at': '2026-09-01T10:00:00Z', 'origin': 'Cra 5 #12-01',
                    'total': 8000,
                    'stops': [{'stop': 1, 'service_type': 'domicilio', 'address': 'Calle 20 #3-10'}],
                },
            ],
        }
        transcript = [
            {'role': 'user', 'text': 'manda un domiciliario'},
            {'role': 'assistant', 'text': '¿a dónde?'},
        ]

        messages = build_messages(transcript, client_ref, context, CATALOG, TOOL_CATALOG)

        system = messages[0]['content']
        self.assertIn('Ana Pérez', system)
        self.assertIn('3001234567', system)
        self.assertIn('Cra 5 #12-01 (lat 4.085000, lng -76.195000) (predeterminada)', system)
        self.assertIn('Pedido #1234', system)
        self.assertIn('#1234', system)
        self.assertIn('domicilio (Domicilio, requiere dirección)', system)
        self.assertIn('canasta (Canasta)', system)
        self.assertIn('Tuluá', system)
        self.assertIn('json', messages[1]['content'].lower() + system.lower())
        self.assertIn('Cliente: manda un domiciliario', messages[1]['content'])
        self.assertIn('Agente: ¿a dónde?', messages[1]['content'])

    def test_summaries_are_compact(self):
        self.assertEqual(
            summarize_addresses([{'address': 'X', 'is_default': True}]),
            '- X (predeterminada)',
        )
        self.assertEqual(summarize_addresses([]), '')
        text = summarize_orders([{
            'order_number': 9, 'status_label': 'Cancelado', 'origin': 'A',
            'total': 31000, 'stops': [{'stop': 1, 'service_type': 'domicilio', 'address': 'B'}],
        }])
        self.assertIn('Pedido #9 (Cancelado)', text)
        self.assertIn('$31.000', text)
        self.assertIn('paradas: 1 domicilio B', text)
        self.assertEqual(summarize_orders([]), '')

    def test_resolve_draft_client_prefers_the_conversation_link(self):
        conversation = make_conversation(
            ops_client_user_id=88,
            ops_client_snapshot={'id': 88, 'name': 'Ana Pérez', 'phone': '3001234567', 'address': 'X'},
        )
        ref = resolve_draft_client(conversation)
        self.assertEqual(ref['ops_client_user_id'], 88)
        self.assertEqual(ref['name'], 'Ana Pérez')
        self.assertEqual(ref['phone'], '3001234567')
        self.assertTrue(ref['linked'])

    @override_settings(**OPS_SETTINGS)
    @patch('api.integrations.draft.fetch_client_by_phone')
    def test_resolve_draft_client_matches_the_phone(self, lookup):
        lookup.return_value = {'id': 5, 'name': 'Juan', 'phone': '3225365839', 'address': ''}
        ref = resolve_draft_client(make_conversation(contact_phone='573225365839'))
        self.assertEqual(ref['ops_client_user_id'], 5)
        self.assertEqual(ref['name'], 'Juan')
        self.assertFalse(ref['linked'])
        lookup.assert_called_once_with('573225365839')

    def test_resolve_draft_client_falls_back_to_the_contact(self):
        conversation = make_conversation(contact_name='', custom_name='Doña Marta')
        with patch('api.integrations.draft.fetch_client_by_phone', return_value=None):
            ref = resolve_draft_client(conversation)
        self.assertIsNone(ref['ops_client_user_id'])
        self.assertEqual(ref['name'], 'Doña Marta')
        self.assertEqual(ref['phone'], '3001234567')

    @override_settings(**OPS_SETTINGS)
    @patch('api.integrations.draft.ops.get_client_orders')
    @patch('api.integrations.draft.ops.get_client_addresses')
    def test_fetch_client_context_degrades_on_ops_errors(self, addresses, orders):
        from api.integrations import ops

        addresses.side_effect = ops.OpsAPIError('boom')
        orders.return_value = {'ok': True, 'orders': [{'order_number': 1}]}
        context = fetch_client_context(88)
        self.assertEqual(context['addresses'], [])
        self.assertEqual(context['orders'], [{'order_number': 1}])

    def test_fetch_client_context_skips_without_client(self):
        self.assertEqual(fetch_client_context(None), {'addresses': [], 'orders': []})


# ---------------------------------------------------------------------------
#  Tools
# ---------------------------------------------------------------------------


class ToolTests(TestCase):
    @patch('api.integrations.draft.calculator.geocode_search')
    def test_buscar_direccion_returns_compact_rows(self, search):
        search.return_value = [
            {'display_name': 'Calle 10 #20-28, Tuluá', 'place_id': 'p1', 'extra': 'x'},
            'basura',
            {'display_name': 'Carrera 10', 'place_id': 'p2'},
        ]
        result = run_tool('buscar_direccion', {'query': 'Calle 10'})
        self.assertEqual(result['resultados'], [
            {'display_name': 'Calle 10 #20-28, Tuluá', 'place_id': 'p1'},
            {'display_name': 'Carrera 10', 'place_id': 'p2'},
        ])
        search.assert_called_once_with('Calle 10')

    @patch('api.integrations.draft.calculator.geocode_details')
    def test_detalles_direccion_returns_coordinates(self, details):
        details.return_value = {'display_name': 'Calle 10 #20-28', 'lat': 4.1, 'lng': -76.2}
        result = run_tool('detalles_direccion', {'place_id': 'p1'})
        self.assertEqual(result, {'display_name': 'Calle 10 #20-28', 'lat': 4.1, 'lng': -76.2})

    def test_tool_argument_validation_and_unknown_tool(self):
        self.assertIn('error', run_tool('buscar_direccion', {'query': '  '}))
        self.assertIn('error', run_tool('detalles_direccion', {}))
        self.assertIn('error', run_tool('otra', {}))

    @patch('api.integrations.draft.calculator.geocode_search')
    def test_tool_errors_are_returned_to_the_model(self, search):
        from api.integrations import calculator

        search.side_effect = calculator.CalculatorAPIError('caída')
        self.assertEqual(run_tool('buscar_direccion', {'query': 'X'}), {'error': 'caída'})


# ---------------------------------------------------------------------------
#  Schema validation
# ---------------------------------------------------------------------------


class NormalizeDraftTests(TestCase):
    def test_normalizes_a_full_draft(self):
        result = normalize_draft(MODEL_DRAFT, CATALOG, TOOL_CATALOG)
        self.assertEqual(result['origin_address'], 'Cra 5 #12-01, Tuluá')
        self.assertEqual(result['origin_lat'], 4.085)
        self.assertEqual(result['payment_method'], 'nequi')
        self.assertEqual(result['profile'], 'negocio')
        self.assertTrue(result['acompanante'])
        # Unknown tool keys are dropped against the live catalog.
        self.assertEqual(result['tools'], ['canasta'])
        self.assertEqual(len(result['stops']), 2)
        self.assertEqual(result['stops'][0]['stop_no'], 1)
        self.assertEqual(result['stops'][0]['price'], 0)
        self.assertEqual(result['stops'][1]['service_type'], 'compras')
        self.assertEqual(result['missing'], ['precio del domicilio'])
        self.assertEqual(result['confidence'], 0.86)

    def test_maps_calculator_service_keys_and_flags_missing_address(self):
        result = normalize_draft({
            'origin_address': 'A',
            'stops': [{'service_type': 'domicilios', 'dest_address': '', 'description': 'caja'}],
        }, CATALOG, TOOL_CATALOG)
        self.assertEqual(result['stops'][0]['service_type'], 'domicilio')
        self.assertIn('dirección de la parada 1', result['missing'])

    def test_defaults_are_safe(self):
        result = normalize_draft({
            'payment_method': 'tarjeta',
            'profile': 'empresa',
            'confidence': 7,
            'stops': [{'service_type': 'compras', 'description': 'mercado'}],
        }, CATALOG, TOOL_CATALOG)
        self.assertEqual(result['payment_method'], 'efectivo')
        self.assertEqual(result['profile'], 'usuario_final')
        self.assertEqual(result['confidence'], 1.0)
        self.assertEqual(result['missing'], ['dirección de origen'])

    def test_rejects_a_draft_without_usable_stops(self):
        with self.assertRaises(DraftResponseError):
            normalize_draft({'origin_address': 'A', 'stops': []}, CATALOG, TOOL_CATALOG)
        with self.assertRaises(DraftResponseError):
            normalize_draft('no soy un dict', CATALOG, TOOL_CATALOG)

    def test_ignores_empty_stops_and_caps_lists(self):
        result = normalize_draft({
            'origin_address': 'A',
            'missing': ['a'] * 30,
            'tools': ['canasta'] * 5,
            'stops': [
                {'service_type': 'domicilio', 'dest_address': 'B', 'description': 'x'},
                {'service_type': 'domicilio'},
            ],
        }, CATALOG, TOOL_CATALOG)
        self.assertEqual(len(result['stops']), 1)
        self.assertEqual(result['tools'], ['canasta'])
        self.assertEqual(len(result['missing']), 20)


# ---------------------------------------------------------------------------
#  Generation loop (retry + caching)
# ---------------------------------------------------------------------------


@override_settings(**LLM_SETTINGS)
class GenerateTests(TestCase):
    def setUp(self):
        self.conversation = make_conversation()
        try:
            cache.delete_pattern(f'{DRAFT_CACHE_PREFIX}{self.conversation.id}:*')
        except Exception:
            cache.clear()

    def test_retries_once_when_the_model_breaks_the_schema(self):
        messages = [{'role': 'user', 'content': 'x'}]
        with patch('api.integrations.draft._run_tool_loop') as loop:
            loop.side_effect = ['no soy json', json.dumps(MODEL_DRAFT)]
            result = draft._generate(messages, CATALOG, TOOL_CATALOG)

        self.assertEqual(result['stops'][0]['service_type'], 'domicilio')
        self.assertEqual(loop.call_count, 2)
        # The repair message quotes the first failure and stays in the thread.
        self.assertIn('no cumple el esquema', messages[-1]['content'])

    def test_fails_after_the_single_retry(self):
        with patch('api.integrations.draft._run_tool_loop', return_value='no soy json'):
            with self.assertRaises(DraftError):
                draft._generate([{'role': 'user', 'content': 'x'}], CATALOG, TOOL_CATALOG)

    def test_tool_loop_executes_calls_and_stops_without_them(self):
        messages = [{'role': 'user', 'content': 'x'}]
        responses = [
            {
                'role': 'assistant', 'content': '',
                'tool_calls': [{'id': 'c1', 'name': 'buscar_direccion', 'arguments': {'query': 'Calle 10'}}],
            },
            {'role': 'assistant', 'content': json.dumps(MODEL_DRAFT), 'tool_calls': []},
        ]
        with patch('api.integrations.draft.llm.chat', side_effect=responses) as chat:
            with patch('api.integrations.draft.run_tool', return_value={'resultados': []}) as tool:
                content = draft._run_tool_loop(messages)

        self.assertIn('origin_address', content)
        self.assertEqual(chat.call_count, 2)
        tool.assert_called_once_with('buscar_direccion', {'query': 'Calle 10'})
        tool_messages = [m for m in messages if m.get('role') == 'tool']
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0]['tool_call_id'], 'c1')

    def test_draft_from_message_caches_by_message_window(self):
        start = make_message(self.conversation, 'manda un domiciliario')
        with patch('api.integrations.draft._generate', return_value=dict(MODEL_DRAFT)) as generate:
            with patch('api.integrations.draft.llm.is_configured', return_value=True):
                with patch('api.integrations.draft.resolve_draft_client', return_value={'ops_client_user_id': None, 'name': '', 'phone': ''}):
                    with patch('api.integrations.draft.fetch_client_context', return_value={'addresses': [], 'orders': []}):
                        first = draft.draft_from_message(self.conversation, start)
                        second = draft.draft_from_message(self.conversation, start)

        self.assertFalse(first['cached'])
        self.assertTrue(second['cached'])
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(first['from_message_id'], start.id)

        # A new inbound message opens a new window and regenerates.
        make_message(self.conversation, '¿ya va?')
        with patch('api.integrations.draft._generate', return_value=dict(MODEL_DRAFT)) as generate:
            third = draft.draft_from_message(self.conversation, start)
        self.assertFalse(third['cached'])
        self.assertEqual(generate.call_count, 1)

    def test_draft_without_llm_configuration_raises(self):
        message = make_message(self.conversation, 'manda un domiciliario')
        with override_settings(ORDER_LLM_API_KEY=''):
            with self.assertRaises(DraftNotConfigured):
                draft.draft_from_message(self.conversation, message)

    def test_draft_without_messages_raises(self):
        message = make_message(self.conversation, '')
        message.message_type = 'reaction'
        message.save(update_fields=['message_type'])
        with self.assertRaises(DraftError):
            draft.draft_from_message(self.conversation, message)


# ---------------------------------------------------------------------------
#  Endpoint
# ---------------------------------------------------------------------------

DRAFT_RESPONSE = {
    'from_message_id': 1,
    'last_message_id': 2,
    'model': 'deepseek-flash',
    'cached': False,
    'client': {'ops_client_user_id': 88, 'name': 'Ana Pérez', 'phone': '3001234567'},
    'origin_address': 'Cra 5 #12-01',
    'origin_lat': 4.085,
    'origin_lng': -76.195,
    'payment_method': 'efectivo',
    'profile': 'usuario_final',
    'acompanante': False,
    'tools': ['canasta'],
    'stops': [{'stop_no': 1, 'service_type': 'domicilio', 'dest_address': 'X', 'lat': None, 'lng': None}],
    'missing': [],
    'confidence': 0.9,
}


class OrderDraftEndpointTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='draftuser', password='pass123')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.group = get_or_create_tulua_group()
        assign_user_group(self.user, self.group)

        self.conversation = make_conversation(
            whatsapp_id='573001234598', contact_name='Ana', group=self.group,
        )
        self.message = make_message(self.conversation, 'manda un domiciliario')
        make_message(self.conversation, 'listo', direction='outbound')

        cache.delete(f'throttle_order_draft_{self.user.pk}')

    def url(self):
        return f'/api/conversations/{self.conversation.id}/orders/draft/'

    def test_requires_auth(self):
        self.client.credentials()
        response = self.client.post(self.url(), {'from_message_id': self.message.id})
        self.assertEqual(response.status_code, 401)

    @patch('api.order_views.draft_from_message', return_value=dict(DRAFT_RESPONSE))
    def test_returns_the_draft_payload(self, generate):
        response = self.client.post(self.url(), {'from_message_id': self.message.id})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['ok'])
        self.assertEqual(response.data['origin_address'], 'Cra 5 #12-01')
        self.assertEqual(response.data['client']['ops_client_user_id'], 88)
        self.assertEqual(response.data['confidence'], 0.9)
        generate.assert_called_once()
        self.assertEqual(generate.call_args[0][1].id, self.message.id)

    @patch('api.order_views.draft_from_message', return_value=dict(DRAFT_RESPONSE))
    def test_rejects_a_message_from_another_conversation(self, generate):
        other = make_conversation(whatsapp_id='573001234597', group=self.group)
        other_message = make_message(other, 'hola')
        response = self.client.post(self.url(), {'from_message_id': other_message.id})
        self.assertEqual(response.status_code, 400)
        self.assertIn('no pertenece', response.data['error'])
        generate.assert_not_called()

    def test_rejects_an_invalid_body(self):
        response = self.client.post(self.url(), {})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(self.url(), {'from_message_id': 0})
        self.assertEqual(response.status_code, 400)

    @patch('api.order_views.draft_from_message')
    def test_maps_errors(self, generate):
        generate.side_effect = DraftNotConfigured('sin configurar')
        response = self.client.post(self.url(), {'from_message_id': self.message.id})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data['error'], 'sin configurar')

        generate.side_effect = DraftError('el modelo falló')
        response = self.client.post(self.url(), {'from_message_id': self.message.id})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.data['error'], 'el modelo falló')

    @patch('api.order_views.draft_from_message', return_value=dict(DRAFT_RESPONSE))
    def test_rate_limit_is_ten_per_minute_per_user(self, generate):
        for _ in range(10):
            response = self.client.post(self.url(), {'from_message_id': self.message.id})
            self.assertEqual(response.status_code, 200)
        response = self.client.post(self.url(), {'from_message_id': self.message.id})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(generate.call_count, 10)

    @patch('api.order_views.draft_from_message', return_value=dict(DRAFT_RESPONSE))
    def test_other_group_conversations_are_hidden(self, generate):
        other_group = CityGroup.objects.create(name='Otro', slug='otro-draft')
        hidden = make_conversation(whatsapp_id='573001234596', group=other_group)
        hidden_message = make_message(hidden, 'hola')
        response = self.client.post(
            f'/api/conversations/{hidden.id}/orders/draft/',
            {'from_message_id': hidden_message.id},
        )
        self.assertEqual(response.status_code, 404)


class DraftNotConfiguredFallthroughTests(TestCase):
    """``draft_from_message`` maps a missing key to ``DraftNotConfigured``."""

    def test_llm_not_configured_is_wrapped(self):
        conversation = make_conversation(whatsapp_id='573001234595')
        message = make_message(conversation, 'manda un domiciliario')
        with override_settings(ORDER_LLM_API_KEY=''):
            with self.assertRaises(DraftNotConfigured):
                draft.draft_from_message(conversation, message)
