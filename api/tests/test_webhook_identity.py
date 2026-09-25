"""Inbound webhook identity resolution for WhatsApp usernames / BSUIDs.

Meta always includes a business-scoped user ID (``user_id`` / ``from_user_id``)
and only sometimes the sender's phone (``wa_id``). For a username contact the
phone that Meta attaches can belong to a different person (contact-book / 30-day
lookback), so username contacts must be keyed by their BSUID and must never be
merged into a phone-number conversation.
"""

import json

from django.test import TestCase

from api.models import Conversation, Message
from api.tests import get_or_create_tulua_group

BSUID = 'CO.1124445773246962'
PHONE = '573116133245'


class WebhookIdentityTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()  # wamid dedup keys live in the shared Redis cache
        self.group = get_or_create_tulua_group()

    def _post(self, value):
        payload = {'entry': [{'changes': [{'value': value}]}]}
        with self.settings(WHATSAPP_APP_SECRET=''):
            return self.client.post(
                '/webhook/', data=json.dumps(payload), content_type='application/json',
            )

    @staticmethod
    def _value(contacts, messages):
        return {
            'messaging_product': 'whatsapp',
            'metadata': {'phone_number_id': '123'},
            'contacts': contacts,
            'messages': messages,
        }

    def test_username_contact_with_foreign_phone_is_not_merged(self):
        """A username message carrying someone else's phone stays in its own
        BSUID conversation and does not touch the phone conversation."""
        paola = Conversation.objects.create(
            whatsapp_id=PHONE, contact_name='Paola Andrea Rivas',
            contact_phone=PHONE, custom_name='Paola Andrea Rivas central',
            group=self.group,
        )
        response = self._post(self._value(
            contacts=[{
                'profile': {'name': 'Shofi🫦🫦', 'username': 'shofiiii2227222'},
                'wa_id': PHONE,
                'user_id': BSUID,
            }],
            messages=[{
                'from': PHONE,
                'from_user_id': BSUID,
                'id': 'wamid.shofi1',
                'type': 'text',
                'text': {'body': 'necesito recoger un domicilio'},
            }],
        ))
        self.assertEqual(response.status_code, 200)

        paola.refresh_from_db()
        self.assertEqual(paola.contact_name, 'Paola Andrea Rivas')
        self.assertIsNone(paola.whatsapp_username)
        self.assertFalse(Message.objects.filter(conversation=paola).exists())

        shofi = Conversation.objects.get(whatsapp_id=BSUID)
        self.assertEqual(shofi.whatsapp_username, 'shofiiii2227222')
        self.assertIsNone(shofi.contact_phone)
        self.assertTrue(Message.objects.filter(
            conversation=shofi, content='necesito recoger un domicilio',
        ).exists())

    def test_username_contact_without_phone_keyed_by_bsuid(self):
        response = self._post(self._value(
            contacts=[{
                'profile': {'name': 'Mao', 'username': 'mao_user'},
                'user_id': BSUID,
            }],
            messages=[{
                'from_user_id': BSUID,
                'id': 'wamid.mao1',
                'type': 'text',
                'text': {'body': 'buenas'},
            }],
        ))
        self.assertEqual(response.status_code, 200)

        conv = Conversation.objects.get(whatsapp_id=BSUID)
        self.assertEqual(conv.whatsapp_username, 'mao_user')
        self.assertIsNone(conv.contact_phone)
        self.assertTrue(Message.objects.filter(conversation=conv).exists())

    def test_non_username_contact_still_keyed_by_phone(self):
        """Users without a username keep their phone-keyed conversation, even
        though Meta now also sends a BSUID."""
        response = self._post(self._value(
            contacts=[{
                'profile': {'name': 'Ana'},
                'wa_id': PHONE,
                'user_id': BSUID,
            }],
            messages=[{
                'from': PHONE,
                'from_user_id': BSUID,
                'id': 'wamid.ana1',
                'type': 'text',
                'text': {'body': 'hola'},
            }],
        ))
        self.assertEqual(response.status_code, 200)

        conv = Conversation.objects.get(whatsapp_id=PHONE)
        self.assertEqual(conv.contact_phone, PHONE)
        self.assertIsNone(conv.whatsapp_username)
        self.assertFalse(Conversation.objects.filter(whatsapp_id=BSUID).exists())

    def test_username_contact_reuses_existing_thread(self):
        """A username contact whose thread was previously keyed by phone is
        reused instead of forking a second conversation."""
        existing = Conversation.objects.create(
            whatsapp_id=PHONE, contact_name='Shofi', contact_phone=PHONE,
            whatsapp_username='shofiiii2227222', group=self.group,
        )
        response = self._post(self._value(
            contacts=[{
                'profile': {'name': 'Shofi🫦🫦', 'username': 'shofiiii2227222'},
                'wa_id': PHONE,
                'user_id': BSUID,
            }],
            messages=[{
                'from': PHONE,
                'from_user_id': BSUID,
                'id': 'wamid.shofi2',
                'type': 'text',
                'text': {'body': 'hola de nuevo'},
            }],
        ))
        self.assertEqual(response.status_code, 200)

        self.assertEqual(Conversation.objects.count(), 1)
        self.assertTrue(Message.objects.filter(
            conversation=existing, content='hola de nuevo',
        ).exists())
